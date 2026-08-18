from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.draft_conflicts import (
    DraftConflictController,
    DraftConflictPresentationContextReader,
    DraftConflictReceiptChoices,
    DraftConflictReceiptSnapshot,
    DraftConflictSessionUseCases,
    TelegramDraftConflictContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.draft_conflicts import (
    DraftConflictResult,
    ResolveDraftConflictCommand,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftConflictResolution,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.use_cases.draft_conflicts import ResolveDraftConflict
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    GetTransaction,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
REPLACEMENT_DRAFT_ID = UUID("00000000-0000-7000-8000-000000000402")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000501")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000601")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000701")
EXPECTED = DraftRef(DRAFT_ID, 7)
MESSAGE_ID = 94_000_004

OWNER = OwnerSnapshot(
    owner_id=OWNER_ID,
    locale="ru",
    timezone="Europe/Moscow",
    base_currency="RUB",
    default_account_id=ACCOUNT_ID,
    fast_mode=False,
)
ACCOUNT = AccountSnapshot(
    account_id=ACCOUNT_ID,
    name="закрытый основной счёт",
    account_type="card",
    currency="RUB",
    archived_at=None,
    version=3,
)
CATEGORY = CategorySnapshot(
    category_id=CATEGORY_ID,
    kind=TransactionType.EXPENSE,
    name="закрытая категория",
    emoji="🔐",
    archived_at=None,
    version=4,
)
TRANSACTION = TransactionSnapshot(
    transaction_id=TRANSACTION_ID,
    kind=TransactionType.EXPENSE,
    amount_minor=12_345,
    currency="RUB",
    account_id=ACCOUNT_ID,
    account_name=ACCOUNT.name,
    category_id=CATEGORY_ID,
    category_name=CATEGORY.name,
    category_emoji=CATEGORY.emoji,
    occurred_at=datetime(2026, 8, 13, 9, 30, tzinfo=UTC),
    description="закрытое описание",
    version=6,
)

_CATEGORY_STATES = frozenset(
    {"wizard_category", "quick_category", "review_category", "category_required"}
)
_ACCOUNT_STATES = frozenset(
    {"wizard_account", "quick_account", "review_account", "account_required"}
)
_EDIT_STATES = frozenset(
    {
        "edit_menu",
        "edit_category",
        "edit_account",
        "edit_date_menu",
        "edit_date",
        "edit_amount",
        "edit_description",
    }
)
_OWNER_ONLY_STATES = frozenset(
    {
        "wizard_type",
        "wizard_amount",
        "review_amount",
        "custom_category",
        "custom_account",
        "wizard_date",
        "custom_date",
        "review_date",
        "review_date_input",
        "wizard_description",
        "wizard_confirm",
        "quick_confirm",
        "review",
        "review_type",
        "settings_account_create",
        "settings_account_rename",
        "settings_category_create",
        "settings_category_rename",
        "rolling_legacy_state",
    }
)


@dataclass(slots=True)
class _Owner:
    id: UUID


@dataclass(frozen=True, slots=True)
class _PresentationContext:
    history_page: int | None
    pending_history_page: int | None


_DEFAULT_PRESENTATION_CONTEXT = _PresentationContext(5, 11)


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _FakeSession:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


class _Resolver:
    def __init__(self, events: list[str], result: DraftConflictResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[ResolveDraftConflictCommand] = []

    async def execute(self, command: ResolveDraftConflictCommand) -> DraftConflictResult:
        self.events.append("resolve.execute")
        self.commands.append(command)
        return self.result


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _payload_for(state: str) -> dict[str, object]:
    if state in _CATEGORY_STATES:
        return {"flow": "wizard", "type": TransactionType.EXPENSE.value}
    if state in _EDIT_STATES:
        return {"transaction_id": str(TRANSACTION_ID), "version": TRANSACTION.version}
    if state in {"wizard_confirm", "quick_confirm", "review"}:
        return {
            "flow": "quick",
            "type": TransactionType.EXPENSE.value,
            "amount_minor": TRANSACTION.amount_minor,
            "currency": TRANSACTION.currency,
            "account_id": str(ACCOUNT_ID),
            "account_name": ACCOUNT.name,
            "category_id": str(CATEGORY_ID),
            "category_name": CATEGORY.name,
            "category_emoji": CATEGORY.emoji,
            "occurred_at": TRANSACTION.occurred_at.isoformat(),
            "description": TRANSACTION.description,
        }
    return {"flow": "wizard"}


def _result(
    resolution: DraftConflictResolution = DraftConflictResolution.RESUME,
    *,
    state: str = "review",
    payload: dict[str, object] | None = None,
) -> DraftConflictResult:
    return DraftConflictResult(
        resolution,
        DraftSnapshot(
            REPLACEMENT_DRAFT_ID if resolution is DraftConflictResolution.REPLACE else DRAFT_ID,
            state,
            _payload_for(state) if payload is None else payload,
            revision=1 if resolution is DraftConflictResolution.REPLACE else 8,
        ),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    resolver: _Resolver,
    receipts: list[DraftConflictReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_context: _PresentationContext | None = _DEFAULT_PRESENTATION_CONTEXT,
    owner: OwnerSnapshot = OWNER,
    accounts: tuple[AccountSnapshot, ...] = (ACCOUNT,),
    categories: tuple[CategorySnapshot, ...] = (CATEGORY,),
    transaction: TransactionSnapshot = TRANSACTION,
    fail_query: str | None = None,
    fail_outbox: bool = False,
) -> DraftConflictController:
    session = _FakeSession(events)

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual_session: AsyncSession,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        locale: str,
        timezone: str,
        currency: str,
    ) -> _Owner:
        assert actual_session is cast(Any, session)
        assert (telegram_user_id, telegram_chat_id) == (92_000_002, 93_000_003)
        assert (locale, timezone, currency) == ("ru", "Europe/Moscow", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    sessions = cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))

    async def read_presentation_context(
        actual_session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> _PresentationContext | None:
        assert actual_session is cast(Any, session)
        assert owner_id == OWNER_ID
        assert expected == EXPECTED
        assert (chat_id, message_id) == (93_000_003, MESSAGE_ID)
        assert allow_suspended is True
        events.append("presentation.context")
        return presentation_context

    async def get_owner(owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        events.append("owner.get")
        if fail_query == "owner":
            raise RuntimeError("owner query failed")
        return owner

    async def list_accounts(
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID
        assert archived is False
        events.append("accounts.list")
        if fail_query == "accounts":
            raise RuntimeError("account query failed")
        return accounts

    async def list_categories(
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert owner_id == OWNER_ID
        assert kind is TransactionType.EXPENSE
        assert archived is False
        events.append("categories.list")
        if fail_query == "categories":
            raise RuntimeError("category query failed")
        return categories

    async def get_transaction(
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot:
        assert owner_id == OWNER_ID
        assert transaction_id == TRANSACTION_ID
        events.append("transaction.get")
        if fail_query == "transaction":
            raise RuntimeError("transaction query failed")
        return transaction

    def use_case_factory(actual_session: AsyncSession) -> DraftConflictSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftConflictSessionUseCases(
            resolve=cast(ResolveDraftConflict, resolver),
            get_owner_settings=cast(GetOwnerSettings, get_owner),
            list_accounts=cast(ListAccounts, list_accounts),
            list_categories=cast(ListCategories, list_categories),
            get_transaction=cast(GetTransaction, get_transaction),
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftConflictReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if fail_outbox:
            raise RuntimeError("outbox failed")
        receipts.append(receipt)

    return DraftConflictController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        cast(DraftConflictPresentationContextReader, read_presentation_context),
        enqueue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected_history_page"),
    [
        (DraftConflictResolution.RESUME, 5),
        (DraftConflictResolution.KEEP, 5),
        (DraftConflictResolution.REPLACE, 11),
    ],
)
async def test_all_resolutions_capture_effective_context_and_complete_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    resolution: DraftConflictResolution,
    expected_history_page: int,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    resolver = _Resolver(events, _result(resolution))
    controller = _controller(monkeypatch, events, resolver, receipts)

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(), MESSAGE_ID),
        EXPECTED,
        resolution,
    )

    assert receipt is receipts[0]
    assert receipt.expected is EXPECTED
    assert receipt.result is resolver.result
    assert receipt.draft_ref == resolver.result.draft.ref
    assert receipt.owner is OWNER
    assert receipt.choices == DraftConflictReceiptChoices()
    assert receipt.transaction is None
    assert receipt.history_page == expected_history_page
    assert resolver.commands == [ResolveDraftConflictCommand(OWNER_ID, EXPECTED, resolution)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.context",
        "use_case_factory",
        "resolve.execute",
        "owner.get",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", list(DraftConflictResolution))
async def test_legacy_exact_binding_defaults_to_page_zero(
    monkeypatch: pytest.MonkeyPatch,
    resolution: DraftConflictResolution,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Resolver(events, _result(resolution)),
        receipts,
        presentation_context=_PresentationContext(None, None),
    )

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(), MESSAGE_ID), EXPECTED, resolution
    )

    assert receipt is not None
    assert receipt.history_page == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_reads"),
    [
        *((state, ()) for state in sorted(_OWNER_ONLY_STATES)),
        *((state, ("categories.list",)) for state in sorted(_CATEGORY_STATES)),
        *((state, ("accounts.list",)) for state in sorted(_ACCOUNT_STATES)),
        *(
            (state, ("transaction.get",))
            for state in sorted(_EDIT_STATES - {"edit_category", "edit_account"})
        ),
        ("edit_category", ("transaction.get", "categories.list")),
        ("edit_account", ("transaction.get", "accounts.list")),
    ],
)
async def test_every_supported_resulting_state_captures_exact_renderer_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_reads: tuple[str, ...],
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Resolver(events, _result(state=state)),
        receipts,
    )

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(), MESSAGE_ID),
        EXPECTED,
        DraftConflictResolution.RESUME,
    )

    assert receipt is not None
    observed_reads = tuple(
        event
        for event in events
        if event in {"accounts.list", "categories.list", "transaction.get"}
    )
    assert observed_reads == expected_reads
    if "accounts.list" in expected_reads:
        assert receipt.choices.accounts == (ACCOUNT,)
    else:
        assert receipt.choices.accounts == ()
    if "categories.list" in expected_reads:
        assert receipt.choices.categories == (CATEGORY,)
    else:
        assert receipt.choices.categories == ()
    assert (receipt.transaction is TRANSACTION) is state.startswith("edit_")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "state", "expected_reads"),
    [
        ("wizard", "wizard_type", ()),
        ("quick", "quick_category", ("categories.list",)),
        ("repeat", "review", ()),
        ("edit", "edit_menu", ("transaction.get",)),
    ],
)
async def test_replace_for_each_typed_conflict_producer_uses_pending_context(
    monkeypatch: pytest.MonkeyPatch,
    producer: str,
    state: str,
    expected_reads: tuple[str, ...],
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Resolver(events, _result(DraftConflictResolution.REPLACE, state=state)),
        receipts,
        presentation_context=_PresentationContext(3, 17),
    )

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(), MESSAGE_ID),
        EXPECTED,
        DraftConflictResolution.REPLACE,
    )

    assert producer in {"wizard", "quick", "repeat", "edit"}
    assert receipt is not None
    assert receipt.history_page == 17
    actual_reads = tuple(
        event for event in events if event.endswith(".list") or event == "transaction.get"
    )
    assert actual_reads == expected_reads


@pytest.mark.asyncio
async def test_stale_presentation_rolls_back_before_use_case_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    resolver = _Resolver(events, _result(DraftConflictResolution.KEEP))
    controller = _controller(
        monkeypatch,
        events,
        resolver,
        receipts,
        presentation_context=None,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.resolve(
            TelegramDraftConflictContext(_request(), MESSAGE_ID),
            EXPECTED,
            DraftConflictResolution.KEEP,
        )

    assert resolver.commands == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.context",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "failing_query"),
    [
        ("review", "owner"),
        ("wizard_account", "accounts"),
        ("wizard_category", "categories"),
        ("edit_menu", "transaction"),
    ],
)
async def test_any_renderer_query_failure_rolls_back_resolution_and_skips_outbox(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    failing_query: str,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    resolver = _Resolver(events, _result(state=state))
    controller = _controller(
        monkeypatch,
        events,
        resolver,
        receipts,
        fail_query=failing_query,
    )

    with pytest.raises(RuntimeError, match="query failed"):
        await controller.resolve(
            TelegramDraftConflictContext(_request(), MESSAGE_ID),
            EXPECTED,
            DraftConflictResolution.RESUME,
        )

    assert len(resolver.commands) == 1
    assert receipts == []
    assert "outbox" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transaction",
    [
        TransactionSnapshot(
            transaction_id=TRANSACTION_ID,
            kind=TRANSACTION.kind,
            amount_minor=TRANSACTION.amount_minor,
            currency=TRANSACTION.currency,
            account_id=TRANSACTION.account_id,
            account_name=TRANSACTION.account_name,
            category_id=TRANSACTION.category_id,
            category_name=TRANSACTION.category_name,
            category_emoji=TRANSACTION.category_emoji,
            occurred_at=TRANSACTION.occurred_at,
            description=TRANSACTION.description,
            version=TRANSACTION.version + 1,
        ),
        TransactionSnapshot(
            transaction_id=TRANSACTION_ID,
            kind=TRANSACTION.kind,
            amount_minor=TRANSACTION.amount_minor,
            currency=TRANSACTION.currency,
            account_id=TRANSACTION.account_id,
            account_name=TRANSACTION.account_name,
            category_id=TRANSACTION.category_id,
            category_name=TRANSACTION.category_name,
            category_emoji=TRANSACTION.category_emoji,
            occurred_at=TRANSACTION.occurred_at,
            description=TRANSACTION.description,
            deleted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
            version=TRANSACTION.version,
        ),
    ],
    ids=("version-changed", "deleted"),
)
async def test_non_authoritative_edit_snapshot_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    transaction: TransactionSnapshot,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Resolver(events, _result(state="edit_menu")),
        receipts,
        transaction=transaction,
    )

    with pytest.raises(ValueError, match="no longer authoritative"):
        await controller.resolve(
            TelegramDraftConflictContext(_request(), MESSAGE_ID),
            EXPECTED,
            DraftConflictResolution.RESUME,
        )

    assert receipts == []
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_the_complete_unit_of_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Resolver(events, _result()),
        receipts,
        fail_outbox=True,
    )

    with pytest.raises(RuntimeError, match="outbox failed"):
        await controller.resolve(
            TelegramDraftConflictContext(_request(), MESSAGE_ID),
            EXPECTED,
            DraftConflictResolution.RESUME,
        )

    assert receipts == []
    assert events[-3:] == ["outbox", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_context_queries_mutation_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    resolver = _Resolver(events, _result())
    controller = _controller(monkeypatch, events, resolver, receipts, claimed=False)

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(), MESSAGE_ID),
        EXPECTED,
        DraftConflictResolution.RESUME,
    )

    assert receipt is None
    assert resolver.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_invocation_commits_complete_receipt_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftConflictReceiptSnapshot] = []
    controller = _controller(monkeypatch, events, _Resolver(events, _result()), receipts)

    receipt = await controller.resolve(
        TelegramDraftConflictContext(_request(update_id=None), MESSAGE_ID),
        EXPECTED,
        DraftConflictResolution.RESUME,
    )

    assert isinstance(receipt, DraftConflictReceiptSnapshot)
    assert receipts == []
    assert events[-2:] == ["commit", "session.exit"]
    assert "outbox" not in events


def _edit_receipt(
    *,
    state: str = "edit_category",
    history_page: int = 5,
    choices: DraftConflictReceiptChoices | None = None,
    transaction: TransactionSnapshot | None = TRANSACTION,
    payload: dict[str, object] | None = None,
) -> DraftConflictReceiptSnapshot:
    return DraftConflictReceiptSnapshot(
        expected=EXPECTED,
        result=_result(state=state, payload=payload),
        owner=OWNER,
        message_id=MESSAGE_ID,
        history_page=history_page,
        choices=(
            DraftConflictReceiptChoices(categories=(CATEGORY,)) if choices is None else choices
        ),
        transaction=transaction,
    )


def test_controller_contracts_hide_all_private_values_from_repr() -> None:
    result = _result(state="edit_category")
    receipt = _edit_receipt()
    hidden = cast(Any, object())
    values = (
        TelegramDraftConflictContext(_request(), MESSAGE_ID),
        DraftConflictReceiptChoices(categories=(CATEGORY,)),
        receipt,
        DraftConflictSessionUseCases(hidden, hidden, hidden, hidden, hidden),
        result,
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(REPLACEMENT_DRAFT_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        str(TRANSACTION_ID),
        "RUB",
        "12345",
        "закрыт",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramDraftConflictContext(_request(), message_id)


@pytest.mark.parametrize("history_page", [True, -1, MAX_PAGE + 1])
def test_receipt_rejects_invalid_history_page(history_page: int) -> None:
    with pytest.raises(TypeError if isinstance(history_page, bool) else ValueError):
        DraftConflictReceiptSnapshot(
            expected=EXPECTED,
            result=_result(),
            owner=OWNER,
            message_id=MESSAGE_ID,
            history_page=history_page,
        )


def test_edit_receipt_rejects_history_page_that_cannot_fit_edit_callback() -> None:
    with pytest.raises(ValueError, match="cannot be encoded"):
        _edit_receipt(history_page=MAX_PAGE // 3 + 1)


@pytest.mark.parametrize(
    ("state", "choices", "transaction"),
    [
        ("edit_menu", DraftConflictReceiptChoices(), None),
        ("review", DraftConflictReceiptChoices(), TRANSACTION),
        ("wizard_account", DraftConflictReceiptChoices(categories=(CATEGORY,)), None),
        ("wizard_category", DraftConflictReceiptChoices(accounts=(ACCOUNT,)), None),
    ],
)
def test_receipt_rejects_incomplete_or_cross_screen_dependencies(
    state: str,
    choices: DraftConflictReceiptChoices,
    transaction: TransactionSnapshot | None,
) -> None:
    with pytest.raises(ValueError):
        DraftConflictReceiptSnapshot(
            expected=EXPECTED,
            result=_result(state=state),
            owner=OWNER,
            message_id=MESSAGE_ID,
            history_page=5,
            choices=choices,
            transaction=transaction,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"flow": "quick", "pending_intent": {"kind": "wizard"}},
        {"flow": "quick", "history_page": 7},
    ],
)
def test_receipt_rejects_remaining_conflict_or_adapter_state(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        DraftConflictReceiptSnapshot(
            expected=EXPECTED,
            result=_result(payload=payload),
            owner=OWNER,
            message_id=MESSAGE_ID,
            history_page=0,
        )


def test_choices_reject_archived_or_mixed_catalog_rows() -> None:
    archived = AccountSnapshot(
        account_id=ACCOUNT_ID,
        name=ACCOUNT.name,
        account_type=ACCOUNT.account_type,
        currency=ACCOUNT.currency,
        archived_at=datetime(2026, 8, 13, tzinfo=UTC),
        version=ACCOUNT.version,
    )
    with pytest.raises(ValueError, match="active"):
        DraftConflictReceiptChoices(accounts=(archived,))
    with pytest.raises(ValueError, match="two catalog"):
        DraftConflictReceiptChoices(accounts=(ACCOUNT,), categories=(CATEGORY,))
