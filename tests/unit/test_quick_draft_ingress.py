from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_conflicts import (
    PendingQuickIntent,
    PendingWizardIntent,
    decode_pending_draft_intent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    QUICK_DRAFT_INGRESS_CONFLICT_STATES,
    QUICK_DRAFT_INGRESS_DELEGATED_STATES,
    SETTINGS_DRAFT_TEXT_INPUT_STATES,
    TRANSACTION_DRAFT_TEXT_INPUT_STATES,
    BeginQuickDraftCommand,
    DraftIngressOperation,
    DraftIngressStatus,
    QuickDraftIngressNotApplicableError,
)
from finbot.application.draft_preparation import (
    DraftPreparationState,
    PreparedDraftResult,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import (
    DraftSnapshot,
    OwnerSnapshot,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.finance_draft_text_input import FINANCE_DRAFT_TEXT_INPUT_STATES
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.application.use_cases.drafts import DraftUseCases

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
OTHER_OWNER_ID = UUID("00000000-0000-7000-8000-000000000102")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
PRIVATE_TEXT = "1450 закрытое описание"


def _owner(owner_id: UUID = OWNER_ID) -> OwnerSnapshot:
    return OwnerSnapshot(owner_id, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _prepared(
    state: DraftPreparationState = DraftPreparationState.REVIEW,
    *,
    flow: str = "quick",
) -> PreparedDraftResult:
    if state is DraftPreparationState.TYPE_REQUIRED:
        return PreparedDraftResult(
            state,
            {
                "flow": flow,
                "input_mode": "amount_only",
                "amount_minor": 145_000,
            },
        )
    return PreparedDraftResult(
        state,
        {
            "flow": flow,
            "type": "expense",
            "amount_minor": 145_000,
            "occurred_at": "2026-08-13T12:30:00+03:00",
            "description": "закрытое описание",
            "category_explicit": False,
            "needs_confirmation": False,
        },
    )


class _Owners:
    def __init__(self, owner: OwnerSnapshot | None = None) -> None:
        self.owner = owner or _owner()
        self.calls: list[UUID] = []

    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        self.calls.append(owner_id)
        return self.owner


class _Repeats:
    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        raise AssertionError("repeat preparation must not run during quick ingress")


class _QuickDrafts:
    def __init__(self, result: PreparedDraftResult | None = None) -> None:
        self.result = result or _prepared()
        self.calls: list[PrepareQuickDraftCommand] = []
        self.error: Exception | None = None

    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return self.result


def _use_cases(
    repository: InMemoryDraftRepository,
    *,
    quick_drafts: _QuickDrafts | None = None,
    owners: _Owners | None = None,
) -> DraftIngressUseCases:
    return DraftIngressUseCases(
        DraftUseCases(repository),
        _Repeats(),
        owners or _Owners(),
        quick_drafts=quick_drafts or _QuickDrafts(),
    )


def test_state_policy_is_closed_disjoint_and_covers_every_known_text_owner() -> None:
    assert SETTINGS_DRAFT_TEXT_INPUT_STATES == {
        "settings_account_create",
        "settings_account_rename",
        "settings_category_create",
        "settings_category_rename",
    }
    assert TRANSACTION_DRAFT_TEXT_INPUT_STATES == {
        "edit_amount",
        "edit_date",
        "edit_description",
    }
    assert QUICK_DRAFT_INGRESS_DELEGATED_STATES == (
        FINANCE_DRAFT_TEXT_INPUT_STATES
        | SETTINGS_DRAFT_TEXT_INPUT_STATES
        | TRANSACTION_DRAFT_TEXT_INPUT_STATES
    )
    assert QUICK_DRAFT_INGRESS_CONFLICT_STATES == {
        "account_required",
        "category_required",
        "edit_account",
        "edit_category",
        "edit_date_menu",
        "edit_menu",
        "edit_type",
        "quick_account",
        "quick_category",
        "quick_confirm",
        "review",
        "review_account",
        "review_category",
        "review_date",
        "review_type",
        "wizard_account",
        "wizard_category",
        "wizard_confirm",
        "wizard_date",
        "wizard_type",
    }
    assert QUICK_DRAFT_INGRESS_CONFLICT_STATES.isdisjoint(QUICK_DRAFT_INGRESS_DELEGATED_STATES)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(QUICK_DRAFT_INGRESS_CONFLICT_STATES))
async def test_every_navigation_state_stages_one_exact_typed_conflict(state: str) -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        state,
        {"flow": "existing", "history_page": 9},
    )
    quick_drafts = _QuickDrafts()

    result = await _use_cases(repository, quick_drafts=quick_drafts).begin_quick(
        BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
    )

    assert result.operation is DraftIngressOperation.QUICK
    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.draft_id == initial.draft_id
    assert result.draft.revision == initial.revision + 1
    assert result.draft.state == state
    assert result.draft.payload["flow"] == "existing"
    assert "history_page" not in result.draft.payload
    assert decode_pending_draft_intent(result.draft.payload["pending_intent"]) == (
        PendingQuickIntent(PRIVATE_TEXT)
    )
    assert quick_drafts.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(QUICK_DRAFT_INGRESS_DELEGATED_STATES))
async def test_every_owned_text_input_state_is_not_applicable_without_mutation(
    state: str,
) -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(OWNER_ID, state, {"flow": "existing"})
    quick_drafts = _QuickDrafts()

    with pytest.raises(QuickDraftIngressNotApplicableError):
        await _use_cases(repository, quick_drafts=quick_drafts).begin_quick(
            BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
        )

    assert await repository.get_active(OWNER_ID) == initial
    assert quick_drafts.calls == []


@pytest.mark.asyncio
async def test_unknown_active_state_fails_closed_to_the_telegram_boundary() -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "future_text_input",
        {"flow": "future"},
    )

    with pytest.raises(QuickDraftIngressNotApplicableError):
        await _use_cases(repository).begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))

    assert await repository.get_active(OWNER_ID) == initial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["settings_account_create", "edit_amount", "future_suspended_state"],
)
async def test_suspension_takes_precedence_and_stages_a_conflict_for_any_state(
    state: str,
) -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(OWNER_ID, state, {"flow": "existing"})
    suspended = await repository.set_suspended(OWNER_ID, initial.ref, True)

    result = await _use_cases(repository).begin_quick(
        BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
    )

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.draft_id == suspended.draft_id
    assert result.draft.revision == suspended.revision + 1
    assert result.draft.suspended
    assert decode_pending_draft_intent(result.draft.payload["pending_intent"]) == (
        PendingQuickIntent(PRIVATE_TEXT)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["quick_confirm", "settings_account_create", "future_text_input"],
)
async def test_existing_pending_intent_is_never_overwritten(state: str) -> None:
    repository = InMemoryDraftRepository()
    encoded = encode_pending_draft_intent(PendingWizardIntent())
    initial = await repository.create_if_absent(
        OWNER_ID,
        state,
        {"flow": "existing", "pending_intent": encoded},
    )

    with pytest.raises(InvalidStateError, match="Сначала выберите действие"):
        await _use_cases(repository).begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))

    assert await repository.get_active(OWNER_ID) == initial


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(DraftPreparationState))
async def test_absent_draft_is_prepared_authoritatively_and_created_canonically(
    state: DraftPreparationState,
) -> None:
    repository = InMemoryDraftRepository()
    prepared = _prepared(state)
    quick_drafts = _QuickDrafts(prepared)

    result = await _use_cases(repository, quick_drafts=quick_drafts).begin_quick(
        BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
    )

    assert result.operation is DraftIngressOperation.QUICK
    assert result.status is DraftIngressStatus.STARTED
    assert result.draft.state == state.value
    assert result.draft.payload == prepared.payload
    assert "pending_intent" not in result.draft.payload
    assert quick_drafts.calls == [PrepareQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)]


@pytest.mark.asyncio
async def test_preparation_failure_or_non_quick_result_never_creates_a_draft() -> None:
    repository = InMemoryDraftRepository()
    quick_drafts = _QuickDrafts()
    quick_drafts.error = ApplicationValidationError("Быстрый ввод не распознан")

    with pytest.raises(ApplicationValidationError, match="не распознан"):
        await _use_cases(repository, quick_drafts=quick_drafts).begin_quick(
            BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
        )
    assert await repository.get_active(OWNER_ID) is None

    quick_drafts.error = None
    quick_drafts.result = _prepared(flow="wizard")
    with pytest.raises(ApplicationValidationError, match="повреждён"):
        await _use_cases(repository, quick_drafts=quick_drafts).begin_quick(
            BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
        )
    assert await repository.get_active(OWNER_ID) is None


class _AbsentDraftRaceRepository(InMemoryDraftRepository):
    def __init__(self, winner_state: str) -> None:
        super().__init__()
        self._winner_state = winner_state
        self.injected = False

    async def create_if_absent(
        self,
        owner_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        if not self.injected:
            self.injected = True
            await super().create_if_absent(
                owner_id,
                self._winner_state,
                {"flow": "winner"},
            )
        return await super().create_if_absent(owner_id, state, payload)


@pytest.mark.asyncio
async def test_absent_row_race_stages_against_an_exact_navigation_winner() -> None:
    repository = _AbsentDraftRaceRepository("wizard_type")

    result = await _use_cases(repository).begin_quick(
        BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)
    )

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.state == "wizard_type"
    assert result.draft.payload["flow"] == "winner"
    assert isinstance(
        decode_pending_draft_intent(result.draft.payload["pending_intent"]),
        PendingQuickIntent,
    )


@pytest.mark.asyncio
async def test_absent_row_race_never_mutates_a_delegated_winner() -> None:
    repository = _AbsentDraftRaceRepository("settings_account_create")

    with pytest.raises(QuickDraftIngressNotApplicableError):
        await _use_cases(repository).begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.state == "settings_account_create"
    assert current.revision == 1
    assert current.payload == {"flow": "winner"}


class _RevisionRaceRepository(InMemoryDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.injected = False

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        current = await super().get_active(owner_id)
        if current is not None and not self.injected:
            self.injected = True
            await super().update(owner_id, current.ref, current.state, {"flow": "winner"})
        return current


@pytest.mark.asyncio
async def test_revision_race_never_overwrites_the_concurrent_winner() -> None:
    repository = _RevisionRaceRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "quick_confirm",
        {"flow": "existing"},
    )

    with pytest.raises(DraftRevisionConflictError):
        await _use_cases(repository).begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.draft_id == initial.draft_id
    assert current.revision == initial.revision + 1
    assert current.payload == {"flow": "winner"}


@pytest.mark.asyncio
async def test_owner_mismatch_and_missing_quick_preparer_fail_before_creation() -> None:
    repository = InMemoryDraftRepository()
    use_cases = DraftIngressUseCases(
        DraftUseCases(repository),
        _Repeats(),
        _Owners(_owner(OTHER_OWNER_ID)),
    )

    with pytest.raises(ApplicationValidationError, match="владельца"):
        await use_cases.begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))
    assert await repository.get_active(OWNER_ID) is None

    use_cases = DraftIngressUseCases(DraftUseCases(repository), _Repeats(), _Owners())
    with pytest.raises(RuntimeError, match="not configured"):
        await use_cases.begin_quick(BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT))
    assert await repository.get_active(OWNER_ID) is None


def test_quick_command_is_bounded_typed_and_repr_safe() -> None:
    command = BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)

    with pytest.raises(TypeError):
        BeginQuickDraftCommand(str(OWNER_ID), PRIVATE_TEXT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BeginQuickDraftCommand(OWNER_ID, 1450)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BeginQuickDraftCommand(OWNER_ID, "   ")
    with pytest.raises(ValueError):
        BeginQuickDraftCommand(OWNER_ID, "x" * 4097)

    result = replace(_prepared(), payload=dict(_prepared().payload))
    rendered = repr((command, result))
    assert str(OWNER_ID) not in rendered
    assert PRIVATE_TEXT not in rendered
    assert "145000" not in rendered
    assert "RUB" not in rendered
    assert "закрытое" not in rendered
