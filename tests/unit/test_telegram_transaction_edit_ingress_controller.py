from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.transaction_edit_ingress import (
    TelegramTransactionEditContext,
    TransactionEditIngressController,
    TransactionEditReceiptSnapshot,
    TransactionEditSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot, TransactionSnapshot
from finbot.application.errors import ObjectVersionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    BeginTransactionEditResult,
    TransactionEditIngressStatus,
)
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


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


class _Begin:
    def __init__(self, events: list[str], result: BeginTransactionEditResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[BeginTransactionEditCommand] = []
        self.error: Exception | None = None

    async def execute(self, command: BeginTransactionEditCommand) -> BeginTransactionEditResult:
        self.events.append("begin.execute")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
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


def _owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Закрытый счёт",
        category_id=CATEGORY_ID,
        category_name="Закрытая категория",
        category_emoji="▫️",
        occurred_at=NOW,
        description="закрытое описание",
        version=4,
    )


def _result(
    status: TransactionEditIngressStatus = TransactionEditIngressStatus.DRAFT_CREATED,
) -> BeginTransactionEditResult:
    if status is TransactionEditIngressStatus.DRAFT_CREATED:
        draft = DraftSnapshot(
            DRAFT_ID,
            "edit_menu",
            {"transaction_id": str(TRANSACTION_ID), "version": 4},
            revision=1,
        )
    else:
        draft = DraftSnapshot(
            DRAFT_ID,
            "wizard_amount",
            {
                "flow": "wizard",
                "pending_intent": {
                    "kind": "edit",
                    "transaction_id": str(TRANSACTION_ID),
                    "version": 4,
                },
            },
            revision=8,
            suspended=True,
        )
    return BeginTransactionEditResult(status, _owner_snapshot(), _transaction(), draft)


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    begin: _Begin,
    receipts: list[TransactionEditReceiptSnapshot],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> TransactionEditIngressController:
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

    def use_case_factory(actual_session: AsyncSession) -> TransactionEditSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return TransactionEditSessionUseCases(cast(BeginTransactionEdit, begin))

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TransactionEditReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return TransactionEditIngressController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        enqueue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TransactionEditIngressStatus.DRAFT_CREATED,
        TransactionEditIngressStatus.CONFLICT_STAGED,
    ],
)
async def test_controller_claims_mutates_and_enqueues_exact_draft_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    status: TransactionEditIngressStatus,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditReceiptSnapshot] = []
    begin = _Begin(events, _result(status))
    controller = _controller(monkeypatch, events, begin, receipts)

    receipt = await controller.begin(
        TelegramTransactionEditContext(_request(), 94_000_004, 3),
        TRANSACTION_ID,
        4,
    )

    assert receipt is receipts[0]
    assert receipt.result is begin.result
    assert receipt.draft_ref == begin.result.draft.ref
    if status is TransactionEditIngressStatus.CONFLICT_STAGED:
        assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert receipt.message_id == 94_000_004
    assert receipt.history_page == 3
    assert begin.commands == [BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "begin.execute",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_use_case_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditReceiptSnapshot] = []
    begin = _Begin(events, _result())
    controller = _controller(monkeypatch, events, begin, receipts, claimed=False)

    receipt = await controller.begin(
        TelegramTransactionEditContext(_request(), 94_000_004, 3),
        TRANSACTION_ID,
        4,
    )

    assert receipt is None
    assert begin.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_stale_transaction_rolls_back_without_outbox_or_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditReceiptSnapshot] = []
    begin = _Begin(events, _result())
    begin.error = ObjectVersionConflictError(current_version=5)
    controller = _controller(monkeypatch, events, begin, receipts)

    with pytest.raises(ObjectVersionConflictError) as conflict:
        await controller.begin(
            TelegramTransactionEditContext(_request(), 94_000_004, 3),
            TRANSACTION_ID,
            4,
        )

    assert conflict.value.current_version == 5
    assert receipts == []
    assert "outbox" not in events
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_business_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditReceiptSnapshot] = []
    begin = _Begin(events, _result())
    controller = _controller(
        monkeypatch,
        events,
        begin,
        receipts,
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.begin(
            TelegramTransactionEditContext(_request(), 94_000_004, 3),
            TRANSACTION_ID,
            4,
        )

    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_update_returns_receipt_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditReceiptSnapshot] = []
    begin = _Begin(events, _result(TransactionEditIngressStatus.CONFLICT_STAGED))
    controller = _controller(monkeypatch, events, begin, receipts)

    receipt = await controller.begin(
        TelegramTransactionEditContext(_request(update_id=None), 94_000_004, 3),
        TRANSACTION_ID,
        4,
    )

    assert receipt is not None
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "begin.execute",
        "commit",
        "session.exit",
    ]


def test_controller_contracts_reject_invalid_values_and_hide_private_repr() -> None:
    request = _request()
    context = TelegramTransactionEditContext(request, 94_000_004, 3)
    receipt = TransactionEditReceiptSnapshot(_result(), 94_000_004, 3)
    values = (
        context,
        receipt,
        TransactionEditSessionUseCases(cast(BeginTransactionEdit, object())),
    )

    with pytest.raises(TypeError):
        TelegramTransactionEditContext(request, True, 3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TelegramTransactionEditContext(request, 94_000_004, -1)
    with pytest.raises(TypeError):
        TelegramTransactionEditContext(request, 94_000_004, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between"):
        TelegramTransactionEditContext(request, 94_000_004, MAX_PAGE + 1)

    rendered = repr(values)
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
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered
