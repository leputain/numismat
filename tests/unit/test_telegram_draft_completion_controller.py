import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.draft_completion import (
    DraftCompletionController,
    DraftCompletionKind,
    DraftCompletionReceiptSnapshot,
    DraftCompletionSessionUseCases,
    TelegramDraftCompletionContext,
)
from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueOperation,
    OcrQueueSessionUseCases,
)
from finbot.adapters.telegram.controllers.plain_drafts import (
    PlainDraftOperation,
    PlainDraftReceiptSnapshot,
    PlainDraftSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    ConfirmTransactionDraftCommand,
    DraftRef,
    DraftSnapshot,
    OcrQueueActionCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.use_cases.ocr_queue import (
    CancelOcrQueue,
    ConfirmOcrQueueItem,
    SkipOcrQueueItem,
)
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts, ListCategories
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
EXPECTED = DraftRef(DRAFT_ID, 7)
OCCURRED_AT = datetime(2026, 8, 13, 10, tzinfo=UTC)


@dataclass(slots=True)
class _Owner:
    id: UUID


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


class _RecordingDrafts:
    def __init__(self, events: list[str], active: DraftSnapshot | None) -> None:
        self.events = events
        self.active = active
        self.cancelled: list[tuple[UUID, DraftRef]] = []

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def cancel(self, owner_id: UUID, expected: DraftRef) -> None:
        assert owner_id == OWNER_ID
        self.events.append("draft.cancel")
        if self.active is None or self.active.ref != expected:
            raise DraftRevisionConflictError(
                current_revision=self.active.revision if self.active is not None else None
            )
        self.cancelled.append((owner_id, expected))
        self.active = None


class _RecordingTransactions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands: list[ConfirmTransactionDraftCommand] = []
        self.transaction = _transaction()

    async def confirm(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> TransactionMutationResult:
        self.events.append("plain.confirm")
        self.commands.append(command)
        return TransactionMutationResult(
            entity_id=self.transaction.transaction_id,
            version=self.transaction.version,
            resulting_state="confirmed",
            transaction=self.transaction,
        )


class _RecordingOcrAction:
    def __init__(
        self,
        events: list[str],
        name: str,
        result: OcrQueueMutationResult,
    ) -> None:
        self.events = events
        self.name = name
        self.result = result
        self.commands: list[OcrQueueActionCommand] = []

    async def __call__(self, command: OcrQueueActionCommand) -> OcrQueueMutationResult:
        self.events.append(f"ocr.{self.name}")
        self.commands.append(command)
        return self.result


class _OwnerReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner = OwnerSnapshot(
            owner_id=OWNER_ID,
            locale="ru",
            timezone="Europe/Moscow",
            base_currency="RUB",
            default_account_id=ACCOUNT_ID,
        )

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("owner.get")
        return self.owner


class _CatalogReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.account = AccountSnapshot(
            account_id=ACCOUNT_ID,
            name="Тайный счёт",
            account_type="cash",
            currency="RUB",
            archived_at=None,
            version=2,
        )
        self.category = CategorySnapshot(
            category_id=CATEGORY_ID,
            kind=TransactionType.EXPENSE,
            name="Тайная категория",
            emoji="▫️",
            archived_at=None,
            version=3,
        )

    async def list_accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID
        assert not archived
        self.events.append("accounts.list")
        return (self.account,)

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert owner_id == OWNER_ID
        assert kind == TransactionType.EXPENSE.value
        assert not archived
        self.events.append("categories.list")
        return (self.category,)


class _PresentationGuard:
    def __init__(self, events: list[str], *, current: bool = True) -> None:
        self.events = events
        self.current = current
        self.calls: list[tuple[UUID, DraftRef, int, int]] = []

    async def __call__(
        self,
        session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        del session
        self.events.append("guard")
        self.calls.append((owner_id, expected, chat_id, message_id))
        return self.current


@dataclass(slots=True)
class _Harness:
    controller: DraftCompletionController
    events: list[str]
    drafts: _RecordingDrafts
    transactions: _RecordingTransactions
    ocr_actions: dict[str, _RecordingOcrAction]
    receipts: list[DraftCompletionReceiptSnapshot]
    guard: _PresentationGuard
    owner_reader: _OwnerReader
    catalogs: _CatalogReader


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Тайный счёт",
        category_id=CATEGORY_ID,
        category_name="Тайная категория",
        category_emoji="▫️",
        occurred_at=OCCURRED_AT,
        description="секретное описание",
    )


def _plain_draft(*, revision: int = EXPECTED.revision) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state="review",
        payload={
            "flow": "quick",
            "amount_minor": 12_345,
            "description": "секретное описание",
            "pending_rule": {
                "pattern": "секретное описание",
                "scope": "global",
                "account_id": str(ACCOUNT_ID),
                "category_id": str(CATEGORY_ID),
            },
        },
        revision=revision,
    )


def _ocr_draft(*, revision: int = EXPECTED.revision) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state="review",
        payload={
            "flow": "ocr",
            "amount_minor": 12_345,
            "description": "секрет OCR",
            "ocr_batch": {"version": 1, "remaining": []},
        },
        revision=revision,
    )


def _advanced_ocr_draft(state: str) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state=state,
        payload={
            "flow": "ocr",
            "type": TransactionType.EXPENSE.value,
            "ocr_batch": {"version": 1, "remaining": []},
        },
        revision=EXPECTED.revision + 1,
    )


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _context(*, update_id: int | None = 91_000_001) -> TelegramDraftCompletionContext:
    return TelegramDraftCompletionContext(_request(update_id=update_id), 94_000_004)


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: DraftSnapshot | None,
    claims: list[bool] | None = None,
    presentation_current: bool = True,
    fail_enqueue: bool = False,
    confirm_result: OcrQueueMutationResult | None = None,
) -> _Harness:
    events: list[str] = []
    session = _FakeSession(events)
    drafts = _RecordingDrafts(events, active)
    transactions = _RecordingTransactions(events)
    owner_reader = _OwnerReader(events)
    catalogs = _CatalogReader(events)
    receipts: list[DraftCompletionReceiptSnapshot] = []
    remaining_claims = list(claims or [True])

    completed = OcrQueueMutationResult(
        status=OcrQueueStatus.COMPLETED,
        saved=1,
        skipped=0,
        transaction=_transaction(),
    )
    ocr_actions = {
        "confirm": _RecordingOcrAction(
            events,
            "confirm",
            confirm_result or completed,
        ),
        "skip": _RecordingOcrAction(
            events,
            "skip",
            OcrQueueMutationResult(
                status=OcrQueueStatus.COMPLETED,
                saved=0,
                skipped=1,
            ),
        ),
        "cancel": _RecordingOcrAction(
            events,
            "cancel",
            OcrQueueMutationResult(
                status=OcrQueueStatus.CANCELLED,
                saved=0,
                skipped=0,
            ),
        ),
    }

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return remaining_claims.pop(0)

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
    executor = TelegramMutationExecutor(
        cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))
    )

    def use_case_factory(actual_session: AsyncSession) -> DraftCompletionSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftCompletionSessionUseCases(
            plain=PlainDraftSessionUseCases(
                transactions=cast(TransactionUseCases, transactions),
                drafts=drafts,
                get_owner_settings=GetOwnerSettings(owner_reader),
            ),
            ocr_queue=OcrQueueSessionUseCases(
                confirm_current=cast(ConfirmOcrQueueItem, ocr_actions["confirm"]),
                skip_current=cast(SkipOcrQueueItem, ocr_actions["skip"]),
                cancel_queue=cast(CancelOcrQueue, ocr_actions["cancel"]),
                get_owner_settings=GetOwnerSettings(owner_reader),
                list_accounts=ListAccounts(catalogs),
                list_categories=ListCategories(catalogs),
            ),
        )

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftCompletionReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if fail_enqueue:
            raise RuntimeError("synthetic outbox failure")
        receipts.append(receipt)

    guard = _PresentationGuard(events, current=presentation_current)
    controller = DraftCompletionController(
        executor,
        use_case_factory,
        guard,
        enqueue_receipt,
    )
    return _Harness(
        controller,
        events,
        drafts,
        transactions,
        ocr_actions,
        receipts,
        guard,
        owner_reader,
        catalogs,
    )


@pytest.mark.asyncio
async def test_plain_confirm_uses_transaction_confirmation_and_preserves_staged_rule_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_plain_draft())

    result = await harness.controller.confirm(_context(), EXPECTED)

    assert result is harness.receipts[0]
    assert result is not None
    assert result.kind is DraftCompletionKind.PLAIN
    assert result.ocr_queue is None
    assert result.plain is not None
    assert result.plain.operation is PlainDraftOperation.CONFIRMED
    assert result.plain.expected is EXPECTED
    assert result.plain.message_id == 94_000_004
    assert result.plain.transaction is harness.transactions.transaction
    assert harness.transactions.commands == [ConfirmTransactionDraftCommand(OWNER_ID, EXPECTED)]
    assert "pending_rule" in cast(DraftSnapshot, harness.drafts.active).payload
    assert harness.guard.calls == [(OWNER_ID, EXPECTED, 93_000_003, 94_000_004)]
    assert harness.events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "guard",
        "use_case_factory",
        "draft.get",
        "plain.confirm",
        "owner.get",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_plain_cancel_deletes_the_exact_draft_without_transaction_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_plain_draft())

    result = await harness.controller.cancel(_context(), EXPECTED)

    assert result is not None
    assert result.kind is DraftCompletionKind.PLAIN
    assert result.plain is not None
    assert result.plain.operation is PlainDraftOperation.CANCELLED
    assert result.plain.transaction is None
    assert harness.drafts.cancelled == [(OWNER_ID, EXPECTED)]
    assert harness.transactions.commands == []
    assert harness.events.count("session.enter") == 1
    assert harness.events.count("use_case_factory") == 1
    assert harness.events.index("outbox") < harness.events.index("commit")


@pytest.mark.asyncio
async def test_ocr_confirm_routes_to_queue_use_case_and_keeps_transaction_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_ocr_draft())

    result = await harness.controller.confirm(_context(), EXPECTED)

    assert result is not None
    assert result.kind is DraftCompletionKind.OCR_QUEUE
    assert result.plain is None
    assert result.ocr_queue is not None
    assert result.ocr_queue.operation is OcrQueueOperation.CONFIRMED
    assert result.ocr_queue.expected is EXPECTED
    assert result.ocr_queue.message_id == 94_000_004
    assert result.ocr_queue.result.transaction == _transaction()
    assert harness.ocr_actions["confirm"].commands == [OcrQueueActionCommand(OWNER_ID, EXPECTED)]
    assert harness.transactions.commands == []


@pytest.mark.asyncio
async def test_ocr_skip_routes_to_queue_use_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_ocr_draft())

    result = await harness.controller.skip_ocr_item(_context(), EXPECTED)

    assert result is not None
    assert result.kind is DraftCompletionKind.OCR_QUEUE
    assert result.ocr_queue is not None
    assert result.ocr_queue.operation is OcrQueueOperation.SKIPPED
    assert result.ocr_queue.result.status is OcrQueueStatus.COMPLETED
    assert harness.ocr_actions["skip"].commands == [OcrQueueActionCommand(OWNER_ID, EXPECTED)]


@pytest.mark.asyncio
async def test_ocr_cancel_routes_to_queue_use_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_ocr_draft())

    result = await harness.controller.cancel(_context(), EXPECTED)

    assert result is not None
    assert result.kind is DraftCompletionKind.OCR_QUEUE
    assert result.ocr_queue is not None
    assert result.ocr_queue.operation is OcrQueueOperation.CANCELLED
    assert result.ocr_queue.result.status is OcrQueueStatus.CANCELLED
    assert harness.ocr_actions["cancel"].commands == [OcrQueueActionCommand(OWNER_ID, EXPECTED)]
    assert harness.drafts.cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "catalog_event"),
    (("account_required", "accounts.list"), ("category_required", "categories.list")),
)
async def test_advanced_ocr_receipt_reads_required_catalog_inside_same_uow(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    catalog_event: str,
) -> None:
    advanced = OcrQueueMutationResult(
        status=OcrQueueStatus.ADVANCED,
        saved=1,
        skipped=0,
        draft=_advanced_ocr_draft(state),
        transaction=_transaction(),
    )
    harness = _harness(monkeypatch, active=_ocr_draft(), confirm_result=advanced)

    result = await harness.controller.confirm(_context(), EXPECTED)

    assert result is not None and result.ocr_queue is not None
    assert catalog_event in harness.events
    assert harness.events.index(catalog_event) < harness.events.index("outbox")
    if state == "account_required":
        assert result.ocr_queue.active_accounts == (harness.catalogs.account,)
        assert result.ocr_queue.active_categories == ()
    else:
        assert result.ocr_queue.active_accounts == ()
        assert result.ocr_queue.active_categories == (harness.catalogs.category,)


@pytest.mark.asyncio
async def test_skip_rejects_plain_draft_as_wrong_kind_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_plain_draft())

    with pytest.raises(InvalidStateError, match="очереди OCR"):
        await harness.controller.skip_ocr_item(_context(), EXPECTED)

    assert harness.transactions.commands == []
    assert all(action.commands == [] for action in harness.ocr_actions.values())
    assert harness.drafts.cancelled == []
    assert harness.receipts == []
    assert harness.events[-3:] == ["draft.get", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_stale_presentation_rolls_back_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        monkeypatch,
        active=_plain_draft(),
        presentation_current=False,
    )

    with pytest.raises(DraftRevisionConflictError):
        await harness.controller.confirm(_context(), EXPECTED)

    assert "use_case_factory" not in harness.events
    assert "draft.get" not in harness.events
    assert harness.receipts == []
    assert harness.events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "guard",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [None, _plain_draft(revision=8)])
async def test_stale_or_missing_active_draft_rolls_back_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    active: DraftSnapshot | None,
) -> None:
    harness = _harness(monkeypatch, active=active)

    with pytest.raises(DraftRevisionConflictError):
        await harness.controller.confirm(_context(), EXPECTED)

    assert harness.transactions.commands == []
    assert all(action.commands == [] for action in harness.ocr_actions.values())
    assert harness.receipts == []
    assert harness.events[-3:] == ["draft.get", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_duplicate_update_never_guards_classifies_or_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_plain_draft(), claims=[False])

    result = await harness.controller.confirm(_context(), EXPECTED)

    assert result is None
    assert harness.guard.calls == []
    assert harness.transactions.commands == []
    assert harness.receipts == []
    assert harness.events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_business_uow_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        monkeypatch,
        active=_plain_draft(),
        fail_enqueue=True,
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await harness.controller.confirm(_context(), EXPECTED)

    assert "commit" not in harness.events
    assert harness.receipts == []
    assert harness.events[-3:] == ["outbox", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_none_update_commits_and_returns_receipt_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_ocr_draft())

    result = await harness.controller.confirm(_context(update_id=None), EXPECTED)

    assert result is not None
    assert result.kind is DraftCompletionKind.OCR_QUEUE
    assert harness.receipts == []
    assert "outbox" not in harness.events
    assert harness.events[-2:] == ["commit", "session.exit"]


def test_completion_contracts_hide_private_values_from_repr() -> None:
    owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
    plain = PlainDraftReceiptSnapshot(
        PlainDraftOperation.CONFIRMED,
        EXPECTED,
        owner,
        94_000_004,
        _transaction(),
    )
    receipt = DraftCompletionReceiptSnapshot(DraftCompletionKind.PLAIN, plain=plain)
    context = _context()

    for value in (context, receipt):
        rendered = repr(value)
        for marker in (
            OWNER_ID,
            ACCOUNT_ID,
            CATEGORY_ID,
            DRAFT_ID,
            TRANSACTION_ID,
            "12345",
            "RUB",
            "Тайн",
            "секрет",
            "91000001",
            "92000002",
            "93000003",
            "94000004",
        ):
            assert str(marker) not in rendered


def test_discriminated_receipt_rejects_mismatched_payload() -> None:
    with pytest.raises(ValueError, match="plain payload"):
        DraftCompletionReceiptSnapshot(DraftCompletionKind.PLAIN)
    with pytest.raises(ValueError, match="OCR payload"):
        DraftCompletionReceiptSnapshot(DraftCompletionKind.OCR_QUEUE)


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramDraftCompletionContext(_request(), message_id)


@pytest.mark.asyncio
async def test_invalid_expected_revision_is_rejected_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch, active=_plain_draft())

    with pytest.raises(TypeError, match="revision"):
        await harness.controller.confirm(_context(), DraftRef(DRAFT_ID, True))

    assert harness.events == []


def test_controller_has_no_framework_database_or_bootstrap_dependency() -> None:
    path = (
        Path(__file__).parents[2] / "src/finbot/adapters/telegram/controllers/draft_completion.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("aiogram", "sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imports
        for prefix in forbidden
    )
