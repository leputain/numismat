from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_conflicts import (
    PendingRepeatIntent,
    PendingWizardIntent,
    decode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    BeginRepeatDraftCommand,
    BeginWizardDraftCommand,
    DraftIngressOperation,
    DraftIngressResult,
    DraftIngressStatus,
)
from finbot.application.dto import (
    DraftSnapshot,
    OwnerSnapshot,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    ReviewedTransactionInput,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
OTHER_OWNER_ID = UUID("00000000-0000-7000-8000-000000000102")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
OTHER_TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000502")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)


def _owner(owner_id: UUID = OWNER_ID) -> OwnerSnapshot:
    return OwnerSnapshot(owner_id, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _prepared(
    *,
    source_transaction_id: UUID = TRANSACTION_ID,
    source_version: int = 7,
    occurred_at: datetime = NOW,
) -> PreparedTransactionDraft:
    return PreparedTransactionDraft(
        source_transaction_id=source_transaction_id,
        source_version=source_version,
        transaction=ReviewedTransactionInput(
            kind=TransactionType.EXPENSE,
            amount_minor=12_345,
            account_id=ACCOUNT_ID,
            category_id=CATEGORY_ID,
            occurred_at=occurred_at,
            description="закрытое описание",
        ),
        currency="RUB",
        account_name="Закрытый счёт",
        category_name="Закрытая категория",
        category_emoji="▫️",
    )


class _OwnerQuery:
    def __init__(self, owner: OwnerSnapshot | None = None) -> None:
        self.owner = owner or _owner()
        self.calls: list[UUID] = []

    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        self.calls.append(owner_id)
        return self.owner


class _RepeatTransactions:
    def __init__(self, prepared: PreparedTransactionDraft | None = None) -> None:
        self.prepared = prepared or _prepared()
        self.calls: list[PrepareRepeatDraftCommand] = []

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        self.calls.append(command)
        return self.prepared


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value
        self.calls: list[str] = []

    def now(self, timezone: str) -> datetime:
        self.calls.append(timezone)
        return self.value


def _use_cases(
    repository: InMemoryDraftRepository,
    *,
    owners: _OwnerQuery | None = None,
    transactions: _RepeatTransactions | None = None,
    clock: _Clock | None = None,
) -> DraftIngressUseCases:
    owner_query = owners or _OwnerQuery()
    repeat_transactions = transactions or _RepeatTransactions()
    return DraftIngressUseCases(
        DraftUseCases(repository),
        repeat_transactions,
        owner_query,
        clock or _Clock(),
    )


@pytest.mark.asyncio
async def test_begin_wizard_creates_canonical_channel_neutral_draft() -> None:
    repository = InMemoryDraftRepository()
    owners = _OwnerQuery()

    result = await _use_cases(repository, owners=owners).begin_wizard(
        BeginWizardDraftCommand(OWNER_ID)
    )

    assert result.operation is DraftIngressOperation.WIZARD
    assert result.status is DraftIngressStatus.STARTED
    assert result.owner == _owner()
    assert result.draft.state == "wizard_type"
    assert result.draft.payload == {"flow": "wizard"}
    assert result.draft.revision == 1
    assert result.draft.suspended is False
    assert owners.calls == [OWNER_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("suspended", [False, True])
async def test_begin_wizard_stages_typed_conflict_on_exact_existing_draft(
    suspended: bool,
) -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "quick_confirm",
        {"flow": "quick", "history_page": 9},
    )
    if suspended:
        initial = await repository.set_suspended(OWNER_ID, initial.ref, True)

    result = await _use_cases(repository).begin_wizard(BeginWizardDraftCommand(OWNER_ID))

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.draft_id == initial.draft_id
    assert result.draft.revision == initial.revision + 1
    assert result.draft.state == initial.state
    assert result.draft.suspended is suspended
    assert result.draft.payload["flow"] == "quick"
    assert "history_page" not in result.draft.payload
    assert isinstance(
        decode_pending_draft_intent(result.draft.payload["pending_intent"]),
        PendingWizardIntent,
    )


@pytest.mark.asyncio
async def test_wizard_existing_pending_intent_fails_without_mutation() -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "wizard_amount",
        {
            "flow": "wizard",
            "pending_intent": {"kind": "quick", "text": "bounded input"},
        },
    )

    with pytest.raises(InvalidStateError):
        await _use_cases(repository).begin_wizard(BeginWizardDraftCommand(OWNER_ID))

    assert await repository.get_active(OWNER_ID) == initial


@pytest.mark.asyncio
async def test_existing_pending_intent_fails_without_mutation_or_repeat_lookup() -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "wizard_amount",
        {
            "flow": "wizard",
            "pending_intent": {"kind": "quick", "text": "bounded input"},
        },
    )
    transactions = _RepeatTransactions()
    clock = _Clock()
    use_cases = _use_cases(repository, transactions=transactions, clock=clock)

    with pytest.raises(InvalidStateError):
        await use_cases.begin_repeat(BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7))

    assert await repository.get_active(OWNER_ID) == initial
    assert transactions.calls == []
    assert clock.calls == []


@pytest.mark.asyncio
async def test_begin_repeat_uses_owner_timezone_and_authoritative_source_version() -> None:
    repository = InMemoryDraftRepository()
    transactions = _RepeatTransactions()
    clock = _Clock()

    result = await _use_cases(
        repository,
        transactions=transactions,
        clock=clock,
    ).begin_repeat(BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7))

    assert result.operation is DraftIngressOperation.REPEAT
    assert result.status is DraftIngressStatus.STARTED
    assert result.draft.state == "review"
    assert result.draft.payload == _prepared().to_payload()
    assert transactions.calls == [PrepareRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7, NOW)]
    assert clock.calls == ["Europe/Moscow"]


@pytest.mark.asyncio
async def test_begin_repeat_stages_only_authoritative_bounded_intent() -> None:
    repository = InMemoryDraftRepository()
    initial = await repository.create_if_absent(
        OWNER_ID,
        "wizard_date",
        {"flow": "wizard", "history_page": 4},
    )

    result = await _use_cases(repository).begin_repeat(
        BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7)
    )

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.draft_id == initial.draft_id
    assert result.draft.revision == initial.revision + 1
    assert result.draft.state == initial.state
    assert result.draft.payload["flow"] == "wizard"
    assert "history_page" not in result.draft.payload
    assert decode_pending_draft_intent(result.draft.payload["pending_intent"]) == (
        PendingRepeatIntent.from_prepared(_prepared())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prepared",
    [
        _prepared(source_transaction_id=OTHER_TRANSACTION_ID),
        _prepared(source_version=8),
        _prepared(occurred_at=NOW + timedelta(seconds=1)),
    ],
    ids=["source-id", "source-version", "occurred-at"],
)
async def test_non_authoritative_repeat_result_fails_before_draft_mutation(
    prepared: PreparedTransactionDraft,
) -> None:
    repository = InMemoryDraftRepository()
    transactions = _RepeatTransactions(prepared)

    with pytest.raises(ApplicationValidationError):
        await _use_cases(repository, transactions=transactions).begin_repeat(
            BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7)
        )

    assert await repository.get_active(OWNER_ID) is None
    assert transactions.calls == [PrepareRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7, NOW)]


@pytest.mark.asyncio
async def test_non_authoritative_owner_snapshot_fails_before_draft_mutation() -> None:
    repository = InMemoryDraftRepository()

    with pytest.raises(ApplicationValidationError):
        await _use_cases(repository, owners=_OwnerQuery(_owner(OTHER_OWNER_ID))).begin_wizard(
            BeginWizardDraftCommand(OWNER_ID)
        )

    assert await repository.get_active(OWNER_ID) is None


class _AbsentDraftRaceRepository(InMemoryDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.injected = False

    async def create_if_absent(
        self,
        owner_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        if not self.injected:
            self.injected = True
            await super().create_if_absent(owner_id, "quick_confirm", {"flow": "winner"})
        return await super().create_if_absent(owner_id, state, payload)


@pytest.mark.asyncio
async def test_absent_row_create_race_stages_against_concurrent_winner() -> None:
    repository = _AbsentDraftRaceRepository()

    result = await _use_cases(repository).begin_wizard(BeginWizardDraftCommand(OWNER_ID))

    assert result.status is DraftIngressStatus.CONFLICT
    assert result.draft.state == "quick_confirm"
    assert result.draft.payload["flow"] == "winner"
    assert isinstance(
        decode_pending_draft_intent(result.draft.payload["pending_intent"]),
        PendingWizardIntent,
    )


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
async def test_existing_draft_revision_race_never_overwrites_winner() -> None:
    repository = _RevisionRaceRepository()
    initial = await repository.create_if_absent(OWNER_ID, "wizard_type", {"flow": "wizard"})

    with pytest.raises(DraftRevisionConflictError):
        await _use_cases(repository).begin_wizard(BeginWizardDraftCommand(OWNER_ID))

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.draft_id == initial.draft_id
    assert current.revision == initial.revision + 1
    assert current.payload == {"flow": "winner"}


def test_contracts_reject_invalid_values_and_hide_financial_repr() -> None:
    command = BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7)
    result = DraftIngressResult(
        DraftIngressOperation.REPEAT,
        DraftIngressStatus.STARTED,
        _owner(),
        DraftSnapshot(DRAFT_ID, "review", _prepared().to_payload()),
    )

    with pytest.raises(TypeError):
        BeginWizardDraftCommand(str(OWNER_ID))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BeginRepeatDraftCommand(OWNER_ID, str(TRANSACTION_ID), 7)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 0)
    with pytest.raises(TypeError):
        DraftIngressResult(  # type: ignore[arg-type]
            "repeat",
            DraftIngressStatus.STARTED,
            _owner(),
            result.draft,
        )

    rendered = repr((command, result, replace(_prepared(), source_version=9)))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "RUB",
        "Закрытый",
        "закрытое",
    ):
        assert private not in rendered
