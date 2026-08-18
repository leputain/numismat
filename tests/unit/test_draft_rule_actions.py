from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_rules import (
    DraftRuleAction,
    DraftRuleStagingResult,
    StageDraftRuleCommand,
)
from finbot.application.dto import CreateDraftCommand, DraftRef, DraftSnapshot
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.use_cases.draft_rules import DraftRuleUseCases
from finbot.application.use_cases.drafts import DraftUseCases

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")


class _RecordingDraftRepository(InMemoryDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.mutations: list[str] = []

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        self.mutations.append("update")
        return await super().update(owner_id, expected, state, payload)


def _payload() -> dict[str, object]:
    return {
        "flow": "quick",
        "type": "expense",
        "amount_minor": 12_345,
        "account_id": str(ACCOUNT_ID),
        "category_id": str(CATEGORY_ID),
        "description": "Кофе у дома",
        "category_explicit": False,
        "rule_offer_pattern": "кофе у дома",
    }


async def _subject(
    *,
    state: str = "quick_confirm",
    payload: Mapping[str, Any] | None = None,
    suspended: bool = False,
) -> tuple[DraftRuleUseCases, _RecordingDraftRepository, DraftSnapshot]:
    repository = _RecordingDraftRepository()
    drafts = DraftUseCases(repository)
    created = await drafts.create(CreateDraftCommand(OWNER_ID, state, payload or _payload()))
    if suspended:
        created = await repository.set_suspended(OWNER_ID, created.ref, True)
    repository.mutations.clear()
    return DraftRuleUseCases(drafts), repository, created


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["wizard_confirm", "quick_confirm", "review"])
@pytest.mark.parametrize("action", [DraftRuleAction.GLOBAL, DraftRuleAction.ACCOUNT])
async def test_valid_scope_stages_only_revalidated_pending_rule(
    state: str,
    action: DraftRuleAction,
) -> None:
    use_cases, repository, draft = await _subject(state=state)

    result = await use_cases.execute(StageDraftRuleCommand(OWNER_ID, draft.ref, action))

    assert result.action is action
    assert result.draft.state == state
    assert result.draft.ref == DraftRef(draft.draft_id, draft.revision + 1)
    assert result.draft.payload["pending_rule"] == {
        "pattern": "кофе у дома",
        "scope": action.value,
        "account_id": str(ACCOUNT_ID),
        "category_id": str(CATEGORY_ID),
    }
    original = dict(draft.payload)
    changed = dict(result.draft.payload)
    changed.pop("pending_rule")
    assert changed == original
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_remove_drops_only_pending_rule_and_still_advances_revision() -> None:
    payload = _payload()
    payload["pending_rule"] = {
        "pattern": "кофе у дома",
        "scope": "account",
        "account_id": str(ACCOUNT_ID),
        "category_id": str(CATEGORY_ID),
    }
    # Removal intentionally does not depend on the offer still being valid.
    payload["flow"] = "wizard"
    use_cases, repository, draft = await _subject(payload=payload)

    result = await use_cases.execute(
        StageDraftRuleCommand(OWNER_ID, draft.ref, DraftRuleAction.REMOVE)
    )

    assert "pending_rule" not in result.draft.payload
    expected_payload = dict(draft.payload)
    expected_payload.pop("pending_rule")
    assert dict(result.draft.payload) == expected_payload
    assert result.draft.revision == draft.revision + 1
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_stale_reference_fails_without_mutation() -> None:
    use_cases, repository, draft = await _subject()

    with pytest.raises(DraftRevisionConflictError) as error:
        await use_cases.execute(
            StageDraftRuleCommand(
                OWNER_ID,
                DraftRef(draft.draft_id, draft.revision + 1),
                DraftRuleAction.GLOBAL,
            )
        )

    assert error.value.current_revision == draft.revision
    assert repository.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "suspended"),
    [("wizard_category", False), ("quick_confirm", True)],
)
async def test_invalid_review_state_fails_without_mutation(
    state: str,
    suspended: bool,
) -> None:
    use_cases, repository, draft = await _subject(state=state, suspended=suspended)

    with pytest.raises(InvalidStateError):
        await use_cases.execute(StageDraftRuleCommand(OWNER_ID, draft.ref, DraftRuleAction.ACCOUNT))

    assert repository.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule_offer_pattern", ""),
        ("account_id", ""),
        ("account_id", "not-a-uuid"),
        ("category_id", ""),
        ("category_id", "not-a-uuid"),
        ("description", "обед в столовой"),
        ("flow", "wizard"),
        ("category_explicit", True),
    ],
)
async def test_invalid_or_changed_offer_fails_without_mutation(
    field: str,
    value: object,
) -> None:
    payload = _payload()
    payload[field] = value
    use_cases, repository, draft = await _subject(payload=payload)

    with pytest.raises(ApplicationValidationError):
        await use_cases.execute(StageDraftRuleCommand(OWNER_ID, draft.ref, DraftRuleAction.GLOBAL))

    current = await repository.get_active(OWNER_ID)
    assert current == draft
    assert repository.mutations == []


def test_rule_contracts_are_closed_and_hide_private_payloads_from_repr() -> None:
    snapshot = DraftSnapshot(
        draft_id=UUID("00000000-0000-7000-8000-000000000401"),
        state="quick_confirm",
        payload=_payload(),
        revision=8,
    )
    command = StageDraftRuleCommand(OWNER_ID, snapshot.ref, DraftRuleAction.ACCOUNT)
    result = DraftRuleStagingResult(DraftRuleAction.ACCOUNT, snapshot)

    rendered = repr((command, result))
    for private in (
        str(OWNER_ID),
        str(snapshot.draft_id),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "Кофе",
        "кофе",
    ):
        assert private not in rendered

    with pytest.raises(TypeError, match="action"):
        StageDraftRuleCommand(OWNER_ID, snapshot.ref, "account")  # type: ignore[arg-type]
