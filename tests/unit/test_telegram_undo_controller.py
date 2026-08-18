from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.undo import (
    TelegramUndoContext,
    UndoController,
    UndoReceiptSnapshot,
    UndoSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import OwnerSnapshot, TransactionSnapshot
from finbot.application.undo import UndoAction, UndoActionResult
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.undo import UndoLastAction
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")


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


class _UndoRepository:
    def __init__(self, events: list[str], result: UndoActionResult | None) -> None:
        self.events = events
        self.result = result

    async def undo_last(self, owner_id: UUID) -> UndoActionResult | None:
        assert owner_id == OWNER_ID
        self.events.append("undo")
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
        transaction_id=UUID("00000000-0000-7000-8000-000000000401"),
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
    result: UndoActionResult | None,
    receipts: list[UndoReceiptSnapshot],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> UndoController:
    session = _FakeSession(events)

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

    def use_cases(actual: AsyncSession) -> UndoSessionUseCases:
        assert actual is cast(Any, session)
        events.append("use_cases")
        return UndoSessionUseCases(
            undo_last=UndoLastAction(_UndoRepository(events, result)),
            get_owner_settings=GetOwnerSettings(_OwnerReader(events)),
        )

    async def enqueue(
        actual: AsyncSession,
        request: TelegramMutationRequest,
        receipt: UndoReceiptSnapshot,
    ) -> None:
        assert actual is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return UndoController(executor, use_cases, enqueue)


@pytest.mark.asyncio
async def test_tracked_undo_mutation_and_receipt_commit_in_one_ordered_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[UndoReceiptSnapshot] = []
    result = UndoActionResult(UndoAction.UPDATE, _transaction())
    controller = _controller(monkeypatch, events, result, receipts)

    receipt = await controller.execute(TelegramUndoContext(_request()))

    assert receipt is receipts[0]
    assert receipt.result is result
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_cases",
        "undo",
        "owner_snapshot",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_update_never_reaches_undo_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(monkeypatch, events, None, [], claimed=False)

    assert await controller.execute(TelegramUndoContext(_request())) is None
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_undo_returns_only_after_commit_and_skips_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(monkeypatch, events, None, [])

    receipt = await controller.execute(TelegramUndoContext(_request(None)))

    assert receipt is not None and receipt.result is None
    assert events[-2:] == ["commit", "session.exit"]
    assert "outbox" not in events


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_the_whole_executor_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        UndoActionResult(UndoAction.DELETE, _transaction()),
        [],
        enqueue_error=RuntimeError("outbox failed"),
    )

    with pytest.raises(RuntimeError, match="outbox failed"):
        await controller.execute(TelegramUndoContext(_request()))

    assert events[-2:] == ["rollback", "session.exit"]
    assert "commit" not in events


def test_undo_receipt_repr_hides_owner_and_transaction_values() -> None:
    receipt = UndoReceiptSnapshot(
        OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", None),
        UndoActionResult(UndoAction.UPDATE, _transaction()),
    )

    assert repr(receipt) == "UndoReceiptSnapshot()"
