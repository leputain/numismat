from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.transaction_lifecycle import (
    TelegramTransactionLifecycleContext,
    TransactionLifecycleController,
    TransactionLifecycleOperation,
    TransactionLifecycleReceiptSnapshot,
    TransactionLifecycleSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
    VersionedTransactionCommand,
)
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000401")


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


class _Transactions:
    def __init__(self, events: list[str], result: TransactionMutationResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[VersionedTransactionCommand] = []

    async def delete(self, command: VersionedTransactionCommand) -> TransactionMutationResult:
        self.events.append("delete")
        self.commands.append(command)
        return self.result

    async def restore(self, command: VersionedTransactionCommand) -> TransactionMutationResult:
        self.events.append("restore")
        self.commands.append(command)
        return self.result


class _OwnerReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("owner_snapshot")
        return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", None)


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_500,
        currency="RUB",
        account_id=UUID("00000000-0000-7000-8000-000000000201"),
        account_name="Private account",
        category_id=UUID("00000000-0000-7000-8000-000000000301"),
        category_name="Private category",
        category_emoji="▫️",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="private description",
        version=4,
    )


def _request(update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> tuple[TransactionLifecycleController, _Transactions]:
    session = _FakeSession(events)
    mutation = TransactionMutationResult(
        TRANSACTION_ID,
        4,
        "active",
        _transaction(),
    )
    transactions = _Transactions(events, mutation)

    async def claim(actual: AsyncSession, update_id: int | None) -> bool:
        assert actual is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual: AsyncSession,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        locale: str,
        timezone: str,
        currency: str,
    ) -> _Owner:
        assert actual is cast(Any, session)
        assert (telegram_user_id, telegram_chat_id) == (92_000_002, 93_000_003)
        assert (locale, timezone, currency) == ("ru", "Europe/Moscow", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    executor = TelegramMutationExecutor(
        cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))
    )

    def use_cases(actual: AsyncSession) -> TransactionLifecycleSessionUseCases:
        assert actual is cast(Any, session)
        events.append("use_cases")
        return TransactionLifecycleSessionUseCases(
            transactions=cast(TransactionUseCases, transactions),
            get_owner_settings=GetOwnerSettings(_OwnerReader(events)),
        )

    async def enqueue(
        actual: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TransactionLifecycleReceiptSnapshot,
    ) -> None:
        assert actual is cast(Any, session)
        assert request.update_id == 91_000_001
        assert receipt.mutation is mutation
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error

    return TransactionLifecycleController(executor, use_cases, enqueue), transactions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_event"),
    [
        (TransactionLifecycleOperation.DELETE, "delete"),
        (TransactionLifecycleOperation.RESTORE, "restore"),
    ],
)
async def test_tracked_lifecycle_uses_exact_command_and_atomic_receipt_order(
    monkeypatch: pytest.MonkeyPatch,
    operation: TransactionLifecycleOperation,
    expected_event: str,
) -> None:
    events: list[str] = []
    controller, transactions = _controller(monkeypatch, events)
    context = TelegramTransactionLifecycleContext(_request(), 700, 3)

    receipt = (
        await controller.delete(context, TRANSACTION_ID, 3)
        if operation is TransactionLifecycleOperation.DELETE
        else await controller.restore(context, TRANSACTION_ID, 3)
    )

    assert receipt is not None
    assert receipt.operation is operation
    assert (receipt.message_id, receipt.history_page) == (700, 3)
    assert transactions.commands == [VersionedTransactionCommand(OWNER_ID, TRANSACTION_ID, 3)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_cases",
        expected_event,
        "owner_snapshot",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_lifecycle_never_reaches_transaction_use_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller, transactions = _controller(monkeypatch, events, claimed=False)

    result = await controller.delete(
        TelegramTransactionLifecycleContext(_request(), 700, 0),
        TRANSACTION_ID,
        3,
    )

    assert result is None
    assert transactions.commands == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_lifecycle_returns_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller, _transactions = _controller(monkeypatch, events)

    receipt = await controller.restore(
        TelegramTransactionLifecycleContext(_request(None), 700, None),
        TRANSACTION_ID,
        3,
    )

    assert receipt is not None
    assert receipt.history_page is None
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


@pytest.mark.asyncio
async def test_lifecycle_outbox_failure_rolls_back_whole_executor_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller, _transactions = _controller(
        monkeypatch,
        events,
        enqueue_error=RuntimeError("outbox failed"),
    )

    with pytest.raises(RuntimeError, match="outbox failed"):
        await controller.delete(
            TelegramTransactionLifecycleContext(_request(), 700, 0),
            TRANSACTION_ID,
            3,
        )

    assert events[-2:] == ["rollback", "session.exit"]
    assert "commit" not in events


@pytest.mark.asyncio
async def test_lifecycle_rejects_invalid_context_before_opening_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller, transactions = _controller(monkeypatch, events)

    with pytest.raises(TypeError, match="context"):
        await controller.delete(
            cast(TelegramTransactionLifecycleContext, object()),
            TRANSACTION_ID,
            3,
        )

    assert events == []
    assert transactions.commands == []


def test_lifecycle_snapshots_hide_private_values_from_repr() -> None:
    mutation = TransactionMutationResult(TRANSACTION_ID, 4, "active", _transaction())
    receipt = TransactionLifecycleReceiptSnapshot(
        TransactionLifecycleOperation.RESTORE,
        mutation,
        OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", None),
        700,
        2,
    )

    assert repr(receipt) == (
        "TransactionLifecycleReceiptSnapshot("
        "operation=<TransactionLifecycleOperation.RESTORE: 'restore'>)"
    )
