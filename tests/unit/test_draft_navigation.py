from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_navigation import (
    DRAFT_NAVIGATION_ALLOWED_STATES,
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftDateChoice,
    DraftNavigationAction,
    DraftNavigationChoice,
    DraftNavigationChoices,
    DraftNavigationCommand,
    DraftNavigationStatus,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    CreateDraftCommand,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import (
    CatalogUnavailableError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
SECOND_ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000202")
SECOND_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000302")
NOW = datetime(2026, 8, 13, 21, 30, tzinfo=UTC)


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

    async def delete(self, owner_id: UUID, expected: DraftRef) -> None:
        self.mutations.append("delete")
        await super().delete(owner_id, expected)


class _Queries:
    def __init__(self) -> None:
        self.owner_calls = 0
        self.account_calls = 0
        self.category_calls: list[TransactionType | str | None] = []
        self.catalog_calls: list[tuple[object, ...]] = []
        self.clock_calls: list[str] = []
        self.owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
        self.accounts = (AccountSnapshot(ACCOUNT_ID, "Закрытый счёт", "card", "RUB", None, 4),)
        self.categories = (
            CategorySnapshot(
                CATEGORY_ID,
                TransactionType.EXPENSE,
                "Закрытая категория",
                "🔒",
                None,
                5,
            ),
        )
        self.selected_account = self.accounts[0]
        self.selected_category = self.categories[0]
        self.resolved_account: AccountSnapshot | None = self.accounts[0]
        self.fallback_categories = {
            TransactionType.EXPENSE: self.categories[0],
            TransactionType.INCOME: CategorySnapshot(
                SECOND_CATEGORY_ID,
                TransactionType.INCOME,
                "Прочие доходы",
                "▫️",
                None,
                6,
            ),
        }

    async def owner_query(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.owner_calls += 1
        return self.owner

    async def account_query(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID
        assert archived is False
        self.account_calls += 1
        return self.accounts

    async def category_query(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert owner_id == OWNER_ID
        assert archived is False
        self.category_calls.append(kind)
        return self.categories

    async def select_account(
        self,
        owner_id: UUID,
        reference: DraftCatalogRef,
    ) -> AccountSnapshot:
        self.catalog_calls.append(("select_account", owner_id, reference))
        return self.selected_account

    async def select_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        reference: DraftCatalogRef,
    ) -> CategorySnapshot:
        self.catalog_calls.append(("select_category", owner_id, kind, reference))
        return self.selected_category

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        self.catalog_calls.append(("resolve_account", owner_id, hint, default_account_id))
        return self.resolved_account

    async def resolve_fallback_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
    ) -> CategorySnapshot:
        self.catalog_calls.append(("resolve_fallback_category", owner_id, kind))
        return self.fallback_categories[kind]

    def now(self, timezone: str) -> datetime:
        self.clock_calls.append(timezone)
        return NOW


def _payload(state: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "flow": "wizard",
        "type": "expense",
        "amount_minor": 12_345,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Закрытый счёт",
        "account_slug": "old-account",
        "currency": "RUB",
        "category_id": str(CATEGORY_ID),
        "category_name": "Закрытая категория",
        "category_emoji": "🔒",
        "category_slug": "old-category",
        "occurred_at": datetime(2026, 8, 13, 12, tzinfo=UTC).isoformat(),
        "description": "закрытое описание",
        "rule_offer_pattern": "секрет",
        "pending_rule": {"scope": "global"},
    }
    if state.startswith("review_"):
        payload["review_return_state"] = "quick_confirm"
    if state == "custom_category":
        payload["custom_back_state"] = "quick_category"
    if state == "custom_account":
        payload["custom_back_state"] = "quick_account"
    if state == "wizard_description":
        payload["return_state"] = "wizard_confirm"
        payload["description_back_state"] = "wizard_date"
    return payload


async def _subject(
    state: str,
    *,
    payload: Mapping[str, Any] | None = None,
    suspended: bool = False,
) -> tuple[DraftNavigationUseCases, _RecordingDraftRepository, _Queries, DraftSnapshot]:
    repository = _RecordingDraftRepository()
    drafts = DraftUseCases(repository)
    created = await drafts.create(CreateDraftCommand(OWNER_ID, state, payload or _payload(state)))
    if suspended:
        created = await repository.set_suspended(OWNER_ID, created.ref, True)
    repository.mutations.clear()
    queries = _Queries()
    use_cases = DraftNavigationUseCases(
        drafts,
        queries.owner_query,
        queries.account_query,
        queries.category_query,
        queries,
        queries,
    )
    return use_cases, repository, queries, created


_ALL_STATES = sorted(
    {state for states in DRAFT_NAVIGATION_ALLOWED_STATES.values() for state in states}
    | {"not_a_navigation_state"}
)
_INVALID_STATE_CASES = [
    (action, state)
    for action in DraftNavigationAction
    for state in _ALL_STATES
    if state not in DRAFT_NAVIGATION_ALLOWED_STATES[action]
]


def _choice_for(action: DraftNavigationAction) -> DraftNavigationChoice | None:
    if action is DraftNavigationAction.SELECT_TYPE:
        return TransactionType.EXPENSE
    if action is DraftNavigationAction.SELECT_CATEGORY:
        return DraftCatalogRef(CATEGORY_ID, 5)
    if action is DraftNavigationAction.SELECT_ACCOUNT:
        return DraftCatalogRef(ACCOUNT_ID, 4)
    if action is DraftNavigationAction.SELECT_DATE:
        return DraftDateChoice.TODAY
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state"),
    _INVALID_STATE_CASES,
    ids=lambda value: value.value if isinstance(value, DraftNavigationAction) else value,
)
async def test_every_action_rejects_every_state_outside_its_declared_matrix_without_mutation(
    action: DraftNavigationAction,
    state: str,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)

    with pytest.raises(InvalidStateError, match="неактуален"):
        await use_cases.execute(
            DraftNavigationCommand(OWNER_ID, draft.ref, action, _choice_for(action))
        )

    assert repository.mutations == []
    assert queries.owner_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", tuple(DraftNavigationAction))
async def test_every_action_rejects_a_suspended_valid_state_without_mutation(
    action: DraftNavigationAction,
) -> None:
    state = next(iter(DRAFT_NAVIGATION_ALLOWED_STATES[action]))
    use_cases, repository, queries, draft = await _subject(state, suspended=True)

    with pytest.raises(InvalidStateError, match="приостановлен"):
        await use_cases.execute(
            DraftNavigationCommand(OWNER_ID, draft.ref, action, _choice_for(action))
        )

    assert repository.mutations == []
    assert queries.owner_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "target", "choice_kind"),
    [
        (DraftNavigationAction.EDIT_TYPE, "review_type", None),
        (DraftNavigationAction.EDIT_AMOUNT, "review_amount", None),
        (DraftNavigationAction.EDIT_CATEGORY, "review_category", "categories"),
        (DraftNavigationAction.EDIT_ACCOUNT, "review_account", "accounts"),
        (DraftNavigationAction.EDIT_DATE, "review_date", None),
        (DraftNavigationAction.EDIT_DESCRIPTION, "wizard_description", None),
    ],
)
async def test_edit_actions_create_an_exact_revision_transition_and_typed_choices(
    action: DraftNavigationAction,
    target: str,
    choice_kind: str | None,
) -> None:
    use_cases, repository, queries, draft = await _subject("quick_confirm")

    result = await use_cases.execute(DraftNavigationCommand(OWNER_ID, draft.ref, action))

    assert result.status is DraftNavigationStatus.UPDATED
    assert result.draft is not None
    assert result.draft.state == target
    assert result.draft.revision == draft.revision + 1
    assert repository.mutations == ["update"]
    if action is DraftNavigationAction.EDIT_DESCRIPTION:
        assert result.draft.payload["return_state"] == "quick_confirm"
        assert result.draft.payload["description_back_state"] == "quick_confirm"
        assert "review_return_state" not in result.draft.payload
    else:
        assert result.draft.payload["review_return_state"] == "quick_confirm"
    assert result.choices.accounts == (queries.accounts if choice_kind == "accounts" else ())
    assert result.choices.categories == (queries.categories if choice_kind == "categories" else ())
    assert queries.category_calls == (
        [TransactionType.EXPENSE] if choice_kind == "categories" else []
    )


@pytest.mark.asyncio
async def test_skip_description_returns_to_review_and_clears_learning_metadata() -> None:
    use_cases, repository, _, draft = await _subject("wizard_description")

    result = await use_cases.execute(
        DraftNavigationCommand(OWNER_ID, draft.ref, DraftNavigationAction.SKIP_DESCRIPTION)
    )

    assert result.draft is not None
    assert result.draft.state == "wizard_confirm"
    assert result.draft.payload["description"] == ""
    assert "return_state" not in result.draft.payload
    assert "description_back_state" not in result.draft.payload
    assert "rule_offer_pattern" not in result.draft.payload
    assert "pending_rule" not in result.draft.payload
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "target", "choice_kind"),
    [
        ("review_type", "quick_confirm", None),
        ("review_amount", "quick_confirm", None),
        ("review_category", "quick_confirm", None),
        ("review_account", "quick_confirm", None),
        ("review_date", "quick_confirm", None),
        ("review_date_input", "quick_confirm", None),
        ("wizard_amount", "wizard_type", None),
        ("wizard_category", "wizard_amount", None),
        ("custom_category", "quick_category", "categories"),
        ("wizard_account", "wizard_category", "categories"),
        ("quick_account", "quick_category", "categories"),
        ("account_required", "category_required", "categories"),
        ("custom_account", "quick_account", "accounts"),
        ("custom_date", "wizard_date", None),
        ("wizard_date", "wizard_account", "accounts"),
        ("wizard_description", "wizard_date", None),
        ("wizard_confirm", "wizard_description", None),
        ("quick_confirm", "quick_account", "accounts"),
        ("review", "account_required", "accounts"),
        ("quick_category", None, None),
        ("category_required", None, None),
    ],
)
async def test_back_matrix_has_one_explicit_transition_for_every_allowed_state(
    state: str,
    target: str | None,
    choice_kind: str | None,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)

    result = await use_cases.execute(
        DraftNavigationCommand(OWNER_ID, draft.ref, DraftNavigationAction.BACK)
    )

    if target is None:
        assert result.status is DraftNavigationStatus.CLOSED
        assert result.draft is None
        assert repository.mutations == ["delete"]
    else:
        assert result.status is DraftNavigationStatus.UPDATED
        assert result.draft is not None
        assert result.draft.state == target
        assert result.draft.revision == draft.revision + 1
        assert repository.mutations == ["update"]
    assert result.choices.accounts == (queries.accounts if choice_kind == "accounts" else ())
    assert result.choices.categories == (queries.categories if choice_kind == "categories" else ())


@pytest.mark.asyncio
async def test_stale_ref_fails_before_queries_and_mutation() -> None:
    use_cases, repository, queries, draft = await _subject("quick_confirm")
    stale = DraftRef(draft.draft_id, draft.revision + 1)

    with pytest.raises(DraftRevisionConflictError):
        await use_cases.execute(
            DraftNavigationCommand(OWNER_ID, stale, DraftNavigationAction.EDIT_AMOUNT)
        )

    assert repository.mutations == []
    assert queries.owner_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "action", "missing_key"),
    [
        ("review_type", DraftNavigationAction.BACK, "review_return_state"),
        ("wizard_description", DraftNavigationAction.SKIP_DESCRIPTION, "return_state"),
        ("custom_account", DraftNavigationAction.BACK, "custom_back_state"),
    ],
)
async def test_missing_return_metadata_fails_closed_without_mutation(
    state: str,
    action: DraftNavigationAction,
    missing_key: str,
) -> None:
    payload = _payload(state)
    payload.pop(missing_key, None)
    use_cases, repository, _, draft = await _subject(state, payload=payload)

    with pytest.raises(InvalidStateError, match="безопасного экрана возврата"):
        await use_cases.execute(DraftNavigationCommand(OWNER_ID, draft.ref, action))

    assert repository.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "kind", "target"),
    [
        ("wizard_type", TransactionType.INCOME, "wizard_amount"),
        ("review_type", TransactionType.INCOME, "quick_confirm"),
    ],
)
async def test_select_type_uses_closed_kind_and_resolves_review_fallback_category(
    state: str,
    kind: TransactionType,
    target: str,
) -> None:
    payload = _payload(state)
    payload["amount"] = payload.pop("amount_minor")
    use_cases, repository, queries, draft = await _subject(state, payload=payload)

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_TYPE,
            kind,
        )
    )

    assert result.draft is not None
    assert result.draft.state == target
    assert result.draft.payload["type"] == kind.value
    assert result.draft.payload["amount_minor"] == 12_345
    assert "amount" not in result.draft.payload
    assert "rule_offer_pattern" not in result.draft.payload
    assert "pending_rule" not in result.draft.payload
    if state == "review_type":
        fallback = queries.fallback_categories[kind]
        assert result.draft.payload["category_id"] == str(fallback.category_id)
        assert queries.catalog_calls == [("resolve_fallback_category", OWNER_ID, kind)]
        assert "review_return_state" not in result.draft.payload
    else:
        assert "category_id" not in result.draft.payload
        assert queries.catalog_calls == []
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_amount_only_type_selection_skips_amount_and_loads_category_choices() -> None:
    payload = {
        "flow": "quick",
        "input_mode": "amount_only",
        "amount_minor": 50_050,
    }
    use_cases, repository, queries, draft = await _subject("wizard_type", payload=payload)

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_TYPE,
            TransactionType.EXPENSE,
        )
    )

    assert result.draft is not None
    assert result.draft.state == "wizard_category"
    assert result.draft.payload == {
        "flow": "quick",
        "input_mode": "amount_only",
        "amount_minor": 50_050,
        "type": "expense",
    }
    assert result.choices.categories == queries.categories
    assert queries.category_calls == [TransactionType.EXPENSE]
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_amount_only_advances_through_the_full_guided_review_path() -> None:
    use_cases, repository, _queries, draft = await _subject(
        "wizard_type",
        payload={
            "flow": "quick",
            "input_mode": "amount_only",
            "amount_minor": 50_050,
        },
    )

    transitions = (
        (DraftNavigationAction.SELECT_TYPE, TransactionType.EXPENSE, "wizard_category"),
        (
            DraftNavigationAction.SELECT_CATEGORY,
            DraftCatalogRef(CATEGORY_ID, 5),
            "wizard_account",
        ),
        (
            DraftNavigationAction.SELECT_ACCOUNT,
            DraftCatalogRef(ACCOUNT_ID, 4),
            "wizard_date",
        ),
        (DraftNavigationAction.SELECT_DATE, DraftDateChoice.TODAY, "wizard_description"),
        (DraftNavigationAction.SKIP_DESCRIPTION, None, "wizard_confirm"),
    )
    current = draft
    for action, choice, expected_state in transitions:
        result = await use_cases.execute(
            DraftNavigationCommand(OWNER_ID, current.ref, action, choice)
        )
        assert result.draft is not None
        assert result.draft.state == expected_state
        assert result.draft.payload["amount_minor"] == 50_050
        current = result.draft

    assert current.payload["description"] == ""
    assert current.payload["currency"] == "RUB"
    assert repository.mutations == ["update"] * 5


@pytest.mark.asyncio
async def test_amount_only_category_back_returns_to_type_and_preserves_amount() -> None:
    payload = {
        "flow": "quick",
        "input_mode": "amount_only",
        "amount_minor": 50_050,
        "type": "expense",
    }
    use_cases, repository, _queries, draft = await _subject(
        "wizard_category",
        payload=payload,
    )

    result = await use_cases.execute(
        DraftNavigationCommand(OWNER_ID, draft.ref, DraftNavigationAction.BACK)
    )

    assert result.draft is not None
    assert result.draft.state == "wizard_type"
    assert result.draft.payload == {
        "flow": "quick",
        "input_mode": "amount_only",
        "amount_minor": 50_050,
    }
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_amount_only_marker_without_amount_fails_closed_without_mutation() -> None:
    payload = {"flow": "quick", "input_mode": "amount_only"}
    use_cases, repository, _queries, draft = await _subject("wizard_type", payload=payload)

    with pytest.raises(InvalidStateError, match="не содержит сумму"):
        await use_cases.execute(
            DraftNavigationCommand(
                OWNER_ID,
                draft.ref,
                DraftNavigationAction.SELECT_TYPE,
                TransactionType.EXPENSE,
            )
        )

    assert repository.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "target"),
    [
        ("wizard_category", "wizard_account"),
        ("quick_category", "quick_confirm"),
        ("category_required", "review"),
        ("review_category", "quick_confirm"),
    ],
)
async def test_select_category_validates_version_and_preserves_legacy_state_semantics(
    state: str,
    target: str,
) -> None:
    payload = _payload(state)
    if state == "review_category":
        payload["flow"] = "quick"
        payload["category_explicit"] = False
        payload["description"] = "любимая закрытая кофейня"
    use_cases, repository, queries, draft = await _subject(state, payload=payload)
    queries.selected_category = CategorySnapshot(
        SECOND_CATEGORY_ID,
        TransactionType.EXPENSE,
        "Новая категория",
        "▫️",
        None,
        7,
    )
    reference = DraftCatalogRef(SECOND_CATEGORY_ID, 7)

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_CATEGORY,
            reference,
        )
    )

    assert result.draft is not None
    assert result.draft.state == target
    assert result.draft.payload["category_id"] == str(SECOND_CATEGORY_ID)
    assert "category_slug" not in result.draft.payload
    assert queries.catalog_calls[0] == (
        "select_category",
        OWNER_ID,
        TransactionType.EXPENSE,
        reference,
    )
    if state == "wizard_category":
        assert result.choices.accounts == queries.accounts
    elif state == "review_category":
        assert result.draft.payload["rule_offer_pattern"]
        assert "pending_rule" not in result.draft.payload
    else:
        assert queries.catalog_calls[1] == (
            "resolve_account",
            OWNER_ID,
            None,
            ACCOUNT_ID,
        )
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "target"),
    [
        ("quick_category", "quick_account"),
        ("category_required", "account_required"),
    ],
)
async def test_category_selection_falls_back_to_account_choice_when_resolution_fails(
    state: str,
    target: str,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)
    queries.resolved_account = None

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_CATEGORY,
            DraftCatalogRef(CATEGORY_ID, 5),
        )
    )

    assert result.draft is not None
    assert result.draft.state == target
    assert "account_id" not in result.draft.payload
    assert result.choices.accounts == queries.accounts
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state", "target"),
    [
        (DraftNavigationAction.SELECT_CATEGORY, "wizard_category", "custom_category"),
        (DraftNavigationAction.SELECT_ACCOUNT, "wizard_account", "custom_account"),
    ],
)
async def test_custom_catalog_sentinel_is_explicit_and_never_parsed_as_an_entity(
    action: DraftNavigationAction,
    state: str,
    target: str,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)

    result = await use_cases.execute(
        DraftNavigationCommand(OWNER_ID, draft.ref, action, DraftCatalogChoice.CUSTOM)
    )

    assert result.draft is not None
    assert result.draft.state == target
    assert result.draft.payload["custom_back_state"] == state
    assert queries.catalog_calls == []
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "target"),
    [
        ("wizard_account", "wizard_date"),
        ("quick_account", "quick_confirm"),
        ("account_required", "review"),
        ("review_account", "quick_confirm"),
    ],
)
async def test_select_account_validates_version_and_uses_closed_state_matrix(
    state: str,
    target: str,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)
    reference = DraftCatalogRef(ACCOUNT_ID, 4)

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_ACCOUNT,
            reference,
        )
    )

    assert result.draft is not None
    assert result.draft.state == target
    assert result.draft.payload["account_id"] == str(ACCOUNT_ID)
    assert "account_slug" not in result.draft.payload
    assert queries.catalog_calls == [("select_account", OWNER_ID, reference)]
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_changing_account_invalidates_rule_metadata_but_same_account_preserves_it() -> None:
    same_use_cases, _, _, same_draft = await _subject("review_account")
    same = await same_use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            same_draft.ref,
            DraftNavigationAction.SELECT_ACCOUNT,
            DraftCatalogRef(ACCOUNT_ID, 4),
        )
    )
    assert same.draft is not None
    assert "pending_rule" in same.draft.payload

    changed_use_cases, _, changed_queries, changed_draft = await _subject("review_account")
    changed_queries.selected_account = AccountSnapshot(
        SECOND_ACCOUNT_ID,
        "Другой счёт",
        "card",
        "RUB",
        None,
        6,
    )
    changed = await changed_use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            changed_draft.ref,
            DraftNavigationAction.SELECT_ACCOUNT,
            DraftCatalogRef(SECOND_ACCOUNT_ID, 6),
        )
    )
    assert changed.draft is not None
    assert "rule_offer_pattern" not in changed.draft.payload
    assert "pending_rule" not in changed.draft.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "choice", "target", "expected_day"),
    [
        ("wizard_date", DraftDateChoice.TODAY, "wizard_description", 14),
        ("wizard_date", DraftDateChoice.YESTERDAY, "wizard_description", 13),
        ("wizard_date", DraftDateChoice.CUSTOM, "custom_date", None),
        ("review_date", DraftDateChoice.TODAY, "quick_confirm", 14),
        ("review_date", DraftDateChoice.YESTERDAY, "quick_confirm", 13),
        ("review_date", DraftDateChoice.CUSTOM, "review_date_input", None),
    ],
)
async def test_select_date_uses_injected_clock_and_owner_timezone(
    state: str,
    choice: DraftDateChoice,
    target: str,
    expected_day: int | None,
) -> None:
    use_cases, repository, queries, draft = await _subject(state)

    result = await use_cases.execute(
        DraftNavigationCommand(
            OWNER_ID,
            draft.ref,
            DraftNavigationAction.SELECT_DATE,
            choice,
        )
    )

    assert result.draft is not None
    assert result.draft.state == target
    if expected_day is None:
        assert queries.clock_calls == []
    else:
        occurred = datetime.fromisoformat(str(result.draft.payload["occurred_at"]))
        assert occurred.day == expected_day
        assert occurred.utcoffset() is not None
        assert queries.clock_calls == ["Europe/Moscow"]
    if state == "wizard_date" and choice is not DraftDateChoice.CUSTOM:
        assert result.draft.payload["return_state"] == "wizard_confirm"
        assert result.draft.payload["description_back_state"] == "wizard_date"
    assert "pending_rule" in result.draft.payload
    assert repository.mutations == ["update"]


@pytest.mark.asyncio
async def test_archived_or_version_mismatched_catalog_result_fails_without_draft_mutation() -> None:
    use_cases, repository, queries, draft = await _subject("wizard_account")
    queries.selected_account = AccountSnapshot(
        SECOND_ACCOUNT_ID,
        "Устаревший",
        "card",
        "RUB",
        datetime(2026, 8, 13, tzinfo=UTC),
        5,
    )

    with pytest.raises(CatalogUnavailableError, match="недоступен"):
        await use_cases.execute(
            DraftNavigationCommand(
                OWNER_ID,
                draft.ref,
                DraftNavigationAction.SELECT_ACCOUNT,
                DraftCatalogRef(ACCOUNT_ID, 4),
            )
        )

    assert repository.mutations == []


@pytest.mark.asyncio
async def test_double_click_with_same_exact_ref_mutates_once() -> None:
    use_cases, repository, _queries, draft = await _subject("wizard_type")
    command = DraftNavigationCommand(
        OWNER_ID,
        draft.ref,
        DraftNavigationAction.SELECT_TYPE,
        TransactionType.EXPENSE,
    )

    await use_cases.execute(command)
    with pytest.raises(DraftRevisionConflictError):
        await use_cases.execute(command)

    assert repository.mutations == ["update"]


@pytest.mark.parametrize(
    ("action", "choice"),
    [
        (DraftNavigationAction.SELECT_TYPE, None),
        (DraftNavigationAction.SELECT_TYPE, DraftDateChoice.TODAY),
        (DraftNavigationAction.SELECT_CATEGORY, TransactionType.EXPENSE),
        (DraftNavigationAction.SELECT_ACCOUNT, DraftDateChoice.TODAY),
        (DraftNavigationAction.SELECT_DATE, DraftCatalogChoice.CUSTOM),
        (DraftNavigationAction.BACK, DraftDateChoice.TODAY),
    ],
)
def test_navigation_command_rejects_open_or_mismatched_choice_types(
    action: DraftNavigationAction,
    choice: DraftNavigationChoice | None,
) -> None:
    with pytest.raises(TypeError):
        DraftNavigationCommand(OWNER_ID, DraftRef(CATEGORY_ID, 1), action, choice)


def test_navigation_contracts_hide_financial_and_telegram_values_from_repr() -> None:
    queries = _Queries()
    draft = DraftSnapshot(
        UUID("00000000-0000-7000-8000-000000000401"),
        "quick_confirm",
        _payload("quick_confirm"),
        revision=7,
    )
    command = DraftNavigationCommand(
        OWNER_ID,
        draft.ref,
        DraftNavigationAction.EDIT_CATEGORY,
    )
    selection = DraftNavigationCommand(
        OWNER_ID,
        draft.ref,
        DraftNavigationAction.SELECT_ACCOUNT,
        DraftCatalogRef(ACCOUNT_ID, 4),
    )
    choices = DraftNavigationChoices(accounts=queries.accounts)

    rendered = repr((command, selection, choices, queries.owner, draft))
    for private in (
        str(OWNER_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "Закрыт",
        "RUB",
        "12345",
        "секрет",
    ):
        assert private not in rendered


def test_allowed_state_matrix_is_closed_and_exhaustive_for_the_action_enum() -> None:
    assert set(DRAFT_NAVIGATION_ALLOWED_STATES) == set(DraftNavigationAction)
    assert all(DRAFT_NAVIGATION_ALLOWED_STATES[action] for action in DraftNavigationAction)
