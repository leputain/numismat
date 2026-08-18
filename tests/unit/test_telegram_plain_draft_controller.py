import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.plain_drafts import (
    PlainDraftController,
    PlainDraftOperation,
    PlainDraftReceiptSnapshot,
    PlainDraftSessionUseCases,
    TelegramPlainDraftContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")


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


class _RecordingTransactions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.confirm_commands: list[ConfirmTransactionDraftCommand] = []
        transaction = TransactionSnapshot(
            transaction_id=TRANSACTION_ID,
            kind=TransactionType.EXPENSE,
            amount_minor=12_345,
            currency="RUB",
            account_id=ACCOUNT_ID,
            account_name="Тайный счёт",
            category_id=CATEGORY_ID,
            category_name="Тайная категория",
            category_emoji="▫️",
            occurred_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
            description="секретное описание",
        )
        self.result = TransactionMutationResult(
            transaction.transaction_id,
            transaction.version,
            "confirmed",
            transaction,
        )

    async def confirm(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> TransactionMutationResult:
        self.events.append("command.confirm")
        self.confirm_commands.append(command)
        return self.result


class _RecordingDrafts:
    def __init__(self, events: list[str], active: DraftSnapshot | None = None) -> None:
        self.events = events
        self.active = active
        self.get_calls: list[UUID] = []
        self.cancel_calls: list[tuple[UUID, DraftRef]] = []

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        self.events.append("draft.get")
        self.get_calls.append(owner_id)
        return self.active

    async def cancel(self, owner_id: UUID, expected: DraftRef) -> None:
        self.events.append("draft.cancel")
        self.cancel_calls.append((owner_id, expected))
        if self.active is None or self.active.ref != expected:
            raise DraftRevisionConflictError(
                current_revision=self.active.revision if self.active is not None else None
            )
        self.active = None


def _draft(
    *,
    state: str = "wizard_amount",
    payload: dict[str, object] | None = None,
    revision: int = 7,
) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state=state,
        payload=payload or {"description": "секретное описание"},
        revision=revision,
        updated_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


class _OwnerReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner = OwnerSnapshot(
            OWNER_ID,
            "ru",
            "Europe/Moscow",
            "RUB",
            ACCOUNT_ID,
            fast_mode=True,
        )

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("query.owner")
        return self.owner


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


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _context(*, update_id: int | None = 91_000_001) -> TelegramPlainDraftContext:
    return TelegramPlainDraftContext(_request(update_id=update_id), message_id=94_000_004)


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    transactions: _RecordingTransactions,
    owner_reader: _OwnerReader,
    guard: _PresentationGuard,
    receipts: list[PlainDraftReceiptSnapshot],
    *,
    drafts: _RecordingDrafts | None = None,
    claims: list[bool] | None = None,
    fail_enqueue: bool = False,
) -> PlainDraftController:
    session = _FakeSession(events)
    draft_lifecycle = drafts or _RecordingDrafts(events)
    remaining_claims = list(claims or [True])

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

    def use_case_factory(actual_session: AsyncSession) -> PlainDraftSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return PlainDraftSessionUseCases(
            transactions=cast(TransactionUseCases, transactions),
            drafts=draft_lifecycle,
            get_owner_settings=GetOwnerSettings(owner_reader),
        )

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: PlainDraftReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if fail_enqueue:
            raise RuntimeError("synthetic outbox failure")
        receipts.append(receipt)

    return PlainDraftController(executor, use_case_factory, guard, enqueue_receipt)


@pytest.mark.asyncio
async def test_confirm_guards_exact_message_and_enqueues_transaction_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    transactions = _RecordingTransactions(events)
    owner_reader = _OwnerReader(events)
    guard = _PresentationGuard(events)
    controller = _controller(
        monkeypatch,
        events,
        transactions,
        owner_reader,
        guard,
        receipts,
    )
    expected = DraftRef(DRAFT_ID, 7)

    result = await controller.confirm(_context(), expected)

    assert result is receipts[0]
    assert result is not None
    assert result.operation is PlainDraftOperation.CONFIRMED
    assert result.transaction is transactions.result.transaction
    assert result.owner is owner_reader.owner
    assert result.message_id == 94_000_004
    assert transactions.confirm_commands == [ConfirmTransactionDraftCommand(OWNER_ID, expected)]
    assert guard.calls == [(OWNER_ID, expected, 93_000_003, 94_000_004)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "guard",
        "use_case_factory",
        "command.confirm",
        "query.owner",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_cancel_deletes_any_exact_non_ocr_wizard_state_without_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    transactions = _RecordingTransactions(events)
    drafts = _RecordingDrafts(events, _draft(state="wizard_amount"))
    controller = _controller(
        monkeypatch,
        events,
        transactions,
        _OwnerReader(events),
        _PresentationGuard(events),
        receipts,
        drafts=drafts,
    )
    expected = DraftRef(DRAFT_ID, 7)

    result = await controller.cancel(_context(), expected)

    assert result is receipts[0]
    assert result is not None
    assert result.operation is PlainDraftOperation.CANCELLED
    assert result.transaction is None
    assert drafts.active is None
    assert drafts.get_calls == [OWNER_ID]
    assert drafts.cancel_calls == [(OWNER_ID, expected)]
    assert transactions.confirm_commands == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "guard",
        "use_case_factory",
        "draft.get",
        "draft.cancel",
        "query.owner",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_cancel_refuses_ocr_queue_without_deleting_or_enqueuing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    drafts = _RecordingDrafts(
        events,
        _draft(state="quick_confirm", payload={"ocr_batch": {"version": 1}}),
    )
    controller = _controller(
        monkeypatch,
        events,
        _RecordingTransactions(events),
        _OwnerReader(events),
        _PresentationGuard(events),
        receipts,
        drafts=drafts,
    )

    with pytest.raises(InvalidStateError, match="специализированной"):
        await controller.cancel(_context(), DraftRef(DRAFT_ID, 7))

    assert drafts.active is not None
    assert drafts.cancel_calls == []
    assert receipts == []
    assert events[-3:] == ["draft.get", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_cancel_rejects_stale_active_draft_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    drafts = _RecordingDrafts(events, _draft(revision=8))
    controller = _controller(
        monkeypatch,
        events,
        _RecordingTransactions(events),
        _OwnerReader(events),
        _PresentationGuard(events),
        [],
        drafts=drafts,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.cancel(_context(), DraftRef(DRAFT_ID, 7))

    assert drafts.cancel_calls == []
    assert events[-3:] == ["draft.get", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_stale_presentation_rolls_back_before_constructing_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    transactions = _RecordingTransactions(events)
    controller = _controller(
        monkeypatch,
        events,
        transactions,
        _OwnerReader(events),
        _PresentationGuard(events, current=False),
        receipts,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.confirm(_context(), DraftRef(DRAFT_ID, 7))

    assert transactions.confirm_commands == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "guard",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_double_click_second_update_is_duplicate_and_never_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    transactions = _RecordingTransactions(events)
    controller = _controller(
        monkeypatch,
        events,
        transactions,
        _OwnerReader(events),
        _PresentationGuard(events),
        receipts,
        claims=[True, False],
    )
    expected = DraftRef(DRAFT_ID, 7)

    first = await controller.confirm(_context(), expected)
    second = await controller.confirm(_context(), expected)

    assert first is receipts[0]
    assert second is None
    assert len(transactions.confirm_commands) == 1
    assert events[-4:] == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _RecordingTransactions(events),
        _OwnerReader(events),
        _PresentationGuard(events),
        [],
        fail_enqueue=True,
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.confirm(_context(), DraftRef(DRAFT_ID, 7))

    assert "commit" not in events
    assert events[-3:] == ["outbox", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_none_update_commits_and_returns_post_commit_receipt_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[PlainDraftReceiptSnapshot] = []
    transactions = _RecordingTransactions(events)
    controller = _controller(
        monkeypatch,
        events,
        transactions,
        _OwnerReader(events),
        _PresentationGuard(events),
        receipts,
    )

    result = await controller.confirm(_context(update_id=None), DraftRef(DRAFT_ID, 7))

    assert result is not None
    assert result.transaction is transactions.result.transaction
    assert receipts == []
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


def test_plain_draft_contracts_hide_private_values_from_repr() -> None:
    events: list[str] = []
    transactions = _RecordingTransactions(events)
    owner = _OwnerReader(events).owner
    expected = DraftRef(DRAFT_ID, 7)
    receipt = PlainDraftReceiptSnapshot(
        PlainDraftOperation.CONFIRMED,
        expected,
        owner,
        94_000_004,
        transactions.result.transaction,
    )
    values = (
        TelegramPlainDraftContext(_request(), 94_000_004),
        receipt,
        PlainDraftSessionUseCases(
            cast(TransactionUseCases, transactions),
            _RecordingDrafts(events, _draft()),
            GetOwnerSettings(_OwnerReader(events)),
        ),
        ConfirmTransactionDraftCommand(OWNER_ID, expected),
    )

    for value in values:
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


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramPlainDraftContext(_request(), message_id)


@pytest.mark.asyncio
async def test_invalid_expected_revision_is_rejected_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _RecordingTransactions(events),
        _OwnerReader(events),
        _PresentationGuard(events),
        [],
    )

    with pytest.raises(TypeError, match="revision"):
        await controller.confirm(_context(), DraftRef(DRAFT_ID, True))

    assert events == []


def test_plain_draft_controller_has_no_framework_database_or_bootstrap_dependency() -> None:
    path = Path(__file__).parents[2] / "src/finbot/adapters/telegram/controllers/plain_drafts.py"
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
