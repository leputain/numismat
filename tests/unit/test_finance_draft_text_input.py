from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fakes.repositories import InMemoryDraftRepository

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
from finbot.application.finance_draft_text_input import (
    FINANCE_DRAFT_TEXT_INPUT_STATES,
    FinanceDraftTextInputCommand,
    FinanceDraftTextInputError,
    FinanceDraftTextInputNotApplicableError,
    FinanceDraftTextInputResult,
    FinanceDraftTextInputStatus,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.finance_draft_text_input import (
    FinanceDraftTextInputUseCase,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
SECOND_ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000202")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
SECOND_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000302")
LOCAL_NOW = datetime(2026, 8, 13, 18, 45, tzinfo=ZoneInfo("Europe/Moscow"))


class _Dependencies:
    def __init__(self) -> None:
        self.owner = OwnerSnapshot(
            OWNER_ID,
            "ru",
            "Europe/Moscow",
            "RUB",
            ACCOUNT_ID,
        )
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
        self.created_account = AccountSnapshot(
            SECOND_ACCOUNT_ID,
            "Новый счёт",
            "other",
            "RUB",
            None,
            1,
        )
        self.created_category = CategorySnapshot(
            SECOND_CATEGORY_ID,
            TransactionType.EXPENSE,
            "Новая категория",
            "▫️",
            None,
            1,
        )
        self.resolved_account: AccountSnapshot | None = self.accounts[0]
        self.category_error: Exception | None = None
        self.account_error: Exception | None = None
        self.events: list[str] = []

    async def owner_query(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("owner")
        return self.owner

    async def account_query(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert (owner_id, archived) == (OWNER_ID, False)
        self.events.append("list_accounts")
        return self.accounts

    async def category_query(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert (owner_id, kind, archived) == (OWNER_ID, TransactionType.EXPENSE, False)
        self.events.append("list_categories")
        return self.categories

    async def create_or_get_category(
        self,
        owner_id: UUID,
        name: str,
        kind: TransactionType,
    ) -> CategorySnapshot:
        assert (owner_id, name, kind) == (
            OWNER_ID,
            "Новая категория",
            TransactionType.EXPENSE,
        )
        self.events.append("create_or_get_category")
        if self.category_error is not None:
            raise self.category_error
        return self.created_category

    async def create_or_get_account(
        self,
        owner_id: UUID,
        name: str,
        currency: str,
    ) -> AccountSnapshot:
        assert (owner_id, name, currency) == (OWNER_ID, "Новый счёт", "RUB")
        self.events.append("create_or_get_account")
        if self.account_error is not None:
            raise self.account_error
        return self.created_account

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        assert (owner_id, hint, default_account_id) == (OWNER_ID, None, ACCOUNT_ID)
        self.events.append("resolve_account")
        return self.resolved_account

    def now(self, timezone: str) -> datetime:
        assert timezone == "Europe/Moscow"
        self.events.append("clock")
        return LOCAL_NOW


class _RecordingDraftRepository(InMemoryDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.update_calls = 0

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        self.update_calls += 1
        return await super().update(owner_id, expected, state, payload)


def _payload(state: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "flow": "wizard",
        "type": "expense",
        "amount_minor": 12_345,
        "account_id": str(ACCOUNT_ID),
        "account_name": "Закрытый счёт",
        "currency": "RUB",
        "category_id": str(CATEGORY_ID),
        "category_name": "Закрытая категория",
        "category_emoji": "🔒",
        "occurred_at": LOCAL_NOW.isoformat(),
        "description": "закрытое описание",
    }
    if state.startswith("review_"):
        payload["review_return_state"] = "review"
    if state == "custom_category":
        payload["custom_back_state"] = "wizard_category"
    if state == "custom_account":
        payload["custom_back_state"] = "wizard_account"
    if state == "wizard_description":
        payload["return_state"] = "quick_confirm"
        payload["description_back_state"] = "quick_confirm"
    return payload


async def _subject(
    state: str,
    *,
    payload: Mapping[str, Any] | None = None,
    suspended: bool = False,
    repository: _RecordingDraftRepository | None = None,
) -> tuple[
    FinanceDraftTextInputUseCase,
    _RecordingDraftRepository,
    _Dependencies,
    DraftSnapshot,
]:
    repo = repository or _RecordingDraftRepository()
    drafts = DraftUseCases(repo)
    created = await drafts.create(CreateDraftCommand(OWNER_ID, state, payload or _payload(state)))
    if suspended:
        created = await repo.set_suspended(OWNER_ID, created.ref, True)
    repo.update_calls = 0
    dependencies = _Dependencies()
    use_case = FinanceDraftTextInputUseCase(
        drafts,
        dependencies.owner_query,
        dependencies.account_query,
        dependencies.category_query,
        dependencies,
        dependencies,
    )
    return use_case, repo, dependencies, created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text", "expected_state"),
    [
        ("wizard_amount", "1450", "wizard_category"),
        ("custom_date", "12.08.2026", "wizard_description"),
        ("wizard_description", "новый комментарий", "quick_confirm"),
        ("review_amount", "1,25", "review"),
        ("review_date_input", "вчера", "review"),
    ],
)
async def test_scalar_text_states_advance_exact_draft_with_canonical_payload(
    state: str,
    text: str,
    expected_state: str,
) -> None:
    use_case, repository, dependencies, draft = await _subject(state)

    result = await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, text))

    assert result.status is FinanceDraftTextInputStatus.UPDATED
    assert result.retry_error is None
    assert result.draft.draft_id == draft.draft_id
    assert result.draft.revision == draft.revision + 1
    assert result.draft.state == expected_state
    assert repository.update_calls == 1
    assert "amount" not in result.draft.payload
    if state == "wizard_amount":
        assert result.draft.payload["amount_minor"] == 145_000
        assert result.choices.categories == dependencies.categories
    elif state == "review_amount":
        assert result.draft.payload["amount_minor"] == 125
        assert "review_return_state" not in result.draft.payload
    elif state == "custom_date":
        occurred = datetime.fromisoformat(str(result.draft.payload["occurred_at"]))
        assert (occurred.year, occurred.month, occurred.day) == (2026, 8, 12)
        assert result.draft.payload["return_state"] == "wizard_confirm"
    elif state == "review_date_input":
        occurred = datetime.fromisoformat(str(result.draft.payload["occurred_at"]))
        assert (occurred.year, occurred.month, occurred.day) == (2026, 8, 12)
    else:
        assert result.draft.payload["description"] == "новый комментарий"
        assert "return_state" not in result.draft.payload
        assert "description_back_state" not in result.draft.payload


@pytest.mark.asyncio
async def test_description_dash_clears_comment_and_rule_learning() -> None:
    payload = _payload("wizard_description")
    payload.update({"rule_offer_pattern": "скрытый", "pending_rule": {"scope": "global"}})
    use_case, _, _, draft = await _subject("wizard_description", payload=payload)

    result = await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, "-"))

    assert result.draft.payload["description"] == ""
    assert "rule_offer_pattern" not in result.draft.payload
    assert "pending_rule" not in result.draft.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("back_state", "resolved", "expected_state", "amount_only"),
    [
        ("wizard_category", True, "wizard_account", False),
        ("wizard_category", True, "wizard_account", True),
        ("review_category", True, "quick_confirm", False),
        ("category_required", True, "review", False),
        ("category_required", False, "account_required", False),
    ],
)
async def test_custom_category_preserves_navigation_and_rule_semantics(
    back_state: str,
    resolved: bool,
    expected_state: str,
    amount_only: bool,
) -> None:
    payload = _payload("custom_category")
    payload.update(
        {
            "flow": "quick" if amount_only or back_state != "wizard_category" else "wizard",
            "custom_back_state": back_state,
            "review_return_state": "quick_confirm",
            "description": "кофе зерно",
            "category_explicit": False,
            "pending_rule": {"scope": "global"},
        }
    )
    if amount_only:
        payload["input_mode"] = "amount_only"
    use_case, _, dependencies, draft = await _subject("custom_category", payload=payload)
    dependencies.resolved_account = dependencies.accounts[0] if resolved else None

    result = await use_case.execute(
        FinanceDraftTextInputCommand(OWNER_ID, draft.ref, "  Новая   категория  ")
    )

    assert result.draft.state == expected_state
    assert result.draft.payload["category_id"] == str(SECOND_CATEGORY_ID)
    assert result.draft.payload["category_name"] == "Новая категория"
    assert "custom_back_state" not in result.draft.payload
    if back_state == "review_category":
        assert str(result.draft.payload.get("rule_offer_pattern", ""))
        assert "pending_rule" not in result.draft.payload
    if expected_state in {"wizard_account", "account_required"}:
        assert result.choices.accounts == dependencies.accounts
    if expected_state == "review":
        assert result.draft.payload["account_id"] == str(ACCOUNT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "back_state", "expected_state", "amount_only"),
    [
        ("wizard", "wizard_account", "wizard_date", False),
        ("quick", "wizard_account", "wizard_date", True),
        ("quick", "quick_account", "quick_confirm", False),
        ("quick", "account_required", "review", False),
        ("quick", "review_account", "review", False),
    ],
)
async def test_custom_account_preserves_flow_and_safe_return_state(
    flow: str,
    back_state: str,
    expected_state: str,
    amount_only: bool,
) -> None:
    payload = _payload("custom_account")
    payload.update(
        {
            "flow": flow,
            "custom_back_state": back_state,
            "review_return_state": "review",
            "rule_offer_pattern": "скрытый",
            "pending_rule": {"scope": "account"},
        }
    )
    if amount_only:
        payload["input_mode"] = "amount_only"
    use_case, _, _, draft = await _subject("custom_account", payload=payload)

    result = await use_case.execute(
        FinanceDraftTextInputCommand(OWNER_ID, draft.ref, "  Новый   счёт  ")
    )

    assert result.draft.state == expected_state
    assert result.draft.payload["account_id"] == str(SECOND_ACCOUNT_ID)
    assert result.draft.payload["account_name"] == "Новый счёт"
    assert result.draft.payload["currency"] == "RUB"
    assert "custom_back_state" not in result.draft.payload
    assert "rule_offer_pattern" not in result.draft.payload
    assert "pending_rule" not in result.draft.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text", "error"),
    [
        ("wizard_amount", "не сумма", FinanceDraftTextInputError.INVALID_AMOUNT),
        ("review_amount", "-1", FinanceDraftTextInputError.INVALID_AMOUNT),
        ("custom_category", "   ", FinanceDraftTextInputError.INVALID_CATEGORY_NAME),
        ("custom_account", "x" * 61, FinanceDraftTextInputError.INVALID_ACCOUNT_NAME),
        ("custom_date", "32.13", FinanceDraftTextInputError.INVALID_DATE),
        ("review_date_input", "завтра", FinanceDraftTextInputError.INVALID_DATE),
        (
            "wizard_description",
            "x" * 501,
            FinanceDraftTextInputError.INVALID_DESCRIPTION,
        ),
    ],
)
async def test_invalid_input_returns_revised_safe_retry_without_catalog_side_effect(
    state: str,
    text: str,
    error: FinanceDraftTextInputError,
) -> None:
    use_case, repository, dependencies, draft = await _subject(state)

    result = await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, text))

    assert result.status is FinanceDraftTextInputStatus.RETRY
    assert result.retry_error is error
    assert result.draft.state == state
    assert result.draft.revision == draft.revision + 1
    assert repository.update_calls == 1
    assert "create_or_get_category" not in dependencies.events
    assert "create_or_get_account" not in dependencies.events
    assert text not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "error", "expected"),
    [
        (
            "custom_category",
            CatalogUnavailableError("safe"),
            FinanceDraftTextInputError.CATEGORY_UNAVAILABLE,
        ),
        (
            "custom_account",
            CatalogUnavailableError("safe"),
            FinanceDraftTextInputError.ACCOUNT_UNAVAILABLE,
        ),
    ],
)
async def test_unavailable_catalog_returns_revised_retry(
    state: str,
    error: Exception,
    expected: FinanceDraftTextInputError,
) -> None:
    use_case, _, dependencies, draft = await _subject(state)
    if state == "custom_category":
        dependencies.category_error = error
        text = "Новая категория"
    else:
        dependencies.account_error = error
        text = "Новый счёт"

    result = await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, text))

    assert result.status is FinanceDraftTextInputStatus.RETRY
    assert result.retry_error is expected
    assert result.draft.state == state


@pytest.mark.asyncio
async def test_missing_stale_suspended_and_unsupported_drafts_fail_without_mutation() -> None:
    empty_repository = _RecordingDraftRepository()
    empty_dependencies = _Dependencies()
    empty_use_case = FinanceDraftTextInputUseCase(
        DraftUseCases(empty_repository),
        empty_dependencies.owner_query,
        empty_dependencies.account_query,
        empty_dependencies.category_query,
        empty_dependencies,
        empty_dependencies,
    )
    with pytest.raises(DraftRevisionConflictError):
        await empty_use_case.execute(
            FinanceDraftTextInputCommand(OWNER_ID, DraftRef(DRAFT_ID, 1), "1")
        )
    assert empty_dependencies.events == []

    use_case, repository, _, draft = await _subject("wizard_amount")
    with pytest.raises(DraftRevisionConflictError):
        await use_case.execute(
            FinanceDraftTextInputCommand(
                OWNER_ID,
                DraftRef(UUID("00000000-0000-7000-8000-000000000999"), draft.revision),
                "1",
            )
        )
    assert repository.update_calls == 0

    suspended_case, suspended_repo, _, suspended = await _subject(
        "wizard_amount",
        suspended=True,
    )
    with pytest.raises(FinanceDraftTextInputNotApplicableError, match="приостановлен"):
        await suspended_case.execute(FinanceDraftTextInputCommand(OWNER_ID, suspended.ref, "1"))
    assert suspended_repo.update_calls == 0

    unsupported, unsupported_repo, _, other = await _subject("wizard_type")
    with pytest.raises(FinanceDraftTextInputNotApplicableError, match="не принимает"):
        await unsupported.execute(FinanceDraftTextInputCommand(OWNER_ID, other.ref, "1"))
    assert unsupported_repo.update_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["custom_category", "custom_account"])
async def test_corrupt_custom_return_state_fails_before_catalog_mutation(state: str) -> None:
    payload = _payload(state)
    payload["custom_back_state"] = "untrusted_state"
    use_case, repository, dependencies, draft = await _subject(state, payload=payload)
    text = "Новая категория" if state == "custom_category" else "Новый счёт"

    with pytest.raises(InvalidStateError, match="безопасного экрана"):
        await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, text))

    assert repository.update_calls == 0
    assert "create_or_get_category" not in dependencies.events
    assert "create_or_get_account" not in dependencies.events


@pytest.mark.asyncio
async def test_pending_conflict_rejects_text_without_overwriting_draft_or_receipt_state() -> None:
    payload = _payload("wizard_amount")
    payload["pending_intent"] = {"kind": "wizard"}
    use_case, repository, dependencies, draft = await _subject(
        "wizard_amount",
        payload=payload,
    )

    with pytest.raises(InvalidStateError, match="Сначала выберите действие"):
        await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, "1450"))

    assert repository.update_calls == 0
    assert dependencies.events == []
    assert await repository.get_active(OWNER_ID) == draft


class _RacingDraftRepository(_RecordingDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.raced = False

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        current = await super().get_active(owner_id)
        if current is not None and not self.raced:
            self.raced = True
            await super().update(owner_id, current.ref, current.state, {"flow": "winner"})
        return current


@pytest.mark.asyncio
async def test_exact_cas_race_never_overwrites_winning_draft() -> None:
    repository = _RacingDraftRepository()
    use_case, _, _, draft = await _subject("wizard_amount", repository=repository)

    with pytest.raises(DraftRevisionConflictError):
        await use_case.execute(FinanceDraftTextInputCommand(OWNER_ID, draft.ref, "1450"))

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.revision == draft.revision + 1
    assert current.payload == {"flow": "winner"}


def test_contracts_are_bounded_exhaustive_and_repr_safe() -> None:
    command = FinanceDraftTextInputCommand(
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        "секретный ввод",
    )
    owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
    draft = DraftSnapshot(
        DRAFT_ID,
        "review",
        {"amount_minor": 12_345, "description": "закрытое описание"},
        revision=8,
    )
    result = FinanceDraftTextInputResult(
        FinanceDraftTextInputStatus.UPDATED,
        owner,
        draft,
    )

    assert FINANCE_DRAFT_TEXT_INPUT_STATES == {
        "wizard_amount",
        "custom_category",
        "custom_account",
        "custom_date",
        "wizard_description",
        "review_amount",
        "review_date_input",
    }
    with pytest.raises(ValueError, match="too long"):
        FinanceDraftTextInputCommand(OWNER_ID, DraftRef(DRAFT_ID, 7), "x" * 4097)

    rendered = repr((command, result))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(ACCOUNT_ID),
        "секретный ввод",
        "12345",
        "закрытое описание",
        "Europe/Moscow",
        "RUB",
    ):
        assert private not in rendered
