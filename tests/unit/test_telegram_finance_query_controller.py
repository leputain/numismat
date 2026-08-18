import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.finance_queries import (
    ACTIVE_TRANSACTION_PREFIX,
    DELETED_TRANSACTION_PREFIX,
    FinanceQueryController,
    FinanceReportPeriod,
    InvalidFinanceQueryCallback,
    PeriodReportQuery,
    StaleFinanceQueryCallback,
    TelegramQueryContext,
    TelegramQueryReceipt,
    TransactionCallbackTarget,
    parse_history_page_callback,
    parse_transaction_callback,
    parse_trash_page_callback,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    CategoryTotalSnapshot,
    CurrencyTotals,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.interactions import MAX_OBJECT_VERSION, MAX_PAGE
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
ACTIVE_TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000401")
DELETED_TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000402")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000501")


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


class _RecordingReader:
    def __init__(
        self,
        events: list[str],
        *,
        transactions: tuple[TransactionSnapshot, ...] = (),
    ) -> None:
        self.events = events
        self.owner = _owner_snapshot()
        self.transactions = tuple(transactions)
        self.owner_ids: list[UUID] = []
        self.list_calls: list[tuple[int, int, bool]] = []
        self.period_calls: list[tuple[datetime, datetime]] = []

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.events.append("query.owner")
        self.owner_ids.append(owner_id)
        return self.owner if owner_id == OWNER_ID else None

    async def get_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot | None:
        self.events.append("query.transaction")
        self.owner_ids.append(owner_id)
        if owner_id != OWNER_ID:
            return None
        return next(
            (
                transaction
                for transaction in self.transactions
                if transaction.transaction_id == transaction_id
            ),
            None,
        )

    async def list_transactions(
        self,
        owner_id: UUID,
        *,
        page: int,
        page_size: int,
        deleted: bool = False,
    ) -> tuple[tuple[TransactionSnapshot, ...], int]:
        self.events.append(f"query.list:{page}:{page_size}:{deleted}")
        self.owner_ids.append(owner_id)
        self.list_calls.append((page, page_size, deleted))
        matching = tuple(
            transaction
            for transaction in self.transactions
            if owner_id == OWNER_ID and (transaction.deleted_at is not None) is deleted
        )
        start = page * page_size
        return matching[start : start + page_size], len(matching)

    async def totals_by_currency(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
    ) -> tuple[CurrencyTotals, ...]:
        self.events.append("query.totals")
        self.owner_ids.append(owner_id)
        self.period_calls.append((start, end))
        matching = self._in_period(owner_id, start, end)
        currencies = sorted({item.currency for item in matching})
        return tuple(
            CurrencyTotals(
                currency=currency,
                income_minor=sum(
                    item.amount_minor
                    for item in matching
                    if item.currency == currency and item.kind is TransactionType.INCOME
                ),
                expense_minor=sum(
                    item.amount_minor
                    for item in matching
                    if item.currency == currency and item.kind is TransactionType.EXPENSE
                ),
            )
            for currency in currencies
        )

    async def category_totals(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[CategoryTotalSnapshot, ...]:
        self.events.append("query.categories")
        self.owner_ids.append(owner_id)
        grouped: dict[tuple[UUID, str, str, str], int] = {}
        for item in self._in_period(owner_id, start, end):
            if item.kind is not TransactionType.EXPENSE:
                continue
            key = (
                item.category_id,
                item.category_name,
                item.category_emoji,
                item.currency,
            )
            grouped[key] = grouped.get(key, 0) + item.amount_minor
        return tuple(
            CategoryTotalSnapshot(
                category_id=category_id,
                name=name,
                emoji=emoji,
                currency=currency,
                amount_minor=amount_minor,
            )
            for (category_id, name, emoji, currency), amount_minor in sorted(
                grouped.items(), key=lambda value: -value[1]
            )[:limit]
        )

    async def period_transactions(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]:
        self.events.append("query.period_transactions")
        self.owner_ids.append(owner_id)
        return tuple(
            sorted(
                self._in_period(owner_id, start, end),
                key=lambda item: item.occurred_at,
                reverse=True,
            )[:limit]
        )

    def _in_period(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
    ) -> tuple[TransactionSnapshot, ...]:
        if owner_id != OWNER_ID:
            return ()
        return tuple(
            transaction
            for transaction in self.transactions
            if transaction.deleted_at is None and start <= transaction.occurred_at < end
        )


class _RecordingDrafts:
    def __init__(
        self,
        events: list[str],
        active: DraftSnapshot | None,
    ) -> None:
        self.events = events
        self.active = active

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        assert self.active is not None
        assert self.active.ref == expected
        self.events.append("draft.suspend")
        if not self.active.suspended:
            self.active = replace(
                self.active,
                revision=self.active.revision + 1,
                suspended=True,
            )
        return self.active


@dataclass(slots=True)
class _FixedClock:
    events: list[str]
    moment: datetime

    def now(self, timezone: str) -> datetime:
        assert timezone == "Europe/Moscow"
        self.events.append("clock.now")
        return self.moment


def _owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(
        owner_id=OWNER_ID,
        locale="ru",
        timezone="Europe/Moscow",
        base_currency="RUB",
        default_account_id=ACCOUNT_ID,
    )


def _transaction(
    transaction_id: UUID,
    *,
    kind: TransactionType = TransactionType.EXPENSE,
    amount_minor: int = 12_345,
    occurred_at: datetime | None = None,
    deleted_at: datetime | None = None,
    version: int = 4,
) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=transaction_id,
        kind=kind,
        amount_minor=amount_minor,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Основной <счёт>",
        category_id=CATEGORY_ID,
        category_name="Кафе & рестораны",
        category_emoji="🍽",
        occurred_at=occurred_at or datetime(2026, 8, 13, 9, tzinfo=UTC),
        description="секретное описание <не логировать>",
        source="manual",
        deleted_at=deleted_at,
        version=version,
    )


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _executor(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    claimed: bool = True,
) -> tuple[TelegramMutationExecutor, _FakeSession]:
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
        assert (locale, timezone, currency) == ("ru", "UTC", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    factory = _FakeSessionFactory(session)
    return (
        TelegramMutationExecutor(cast(async_sessionmaker[AsyncSession], factory)),
        session,
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    reader: _RecordingReader,
    receipts: list[TelegramQueryReceipt],
    *,
    claimed: bool = True,
    drafts: _RecordingDrafts | None = None,
    clock: _FixedClock | None = None,
) -> FinanceQueryController:
    executor, session = _executor(monkeypatch, events, claimed=claimed)

    def reader_factory(actual_session: AsyncSession) -> _RecordingReader:
        assert actual_session is cast(Any, session)
        events.append("reader_factory")
        return reader

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TelegramQueryReceipt,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    def draft_factory(actual_session: AsyncSession) -> _RecordingDrafts:
        assert actual_session is cast(Any, session)
        assert drafts is not None
        events.append("draft_factory")
        return drafts

    return FinanceQueryController(
        executor,
        reader_factory,
        enqueue_receipt,
        draft_factory=draft_factory if drafts is not None else None,
        clock=clock,
    )


def test_callback_parsers_accept_canonical_and_legacy_targets() -> None:
    target = parse_transaction_callback(
        f"{ACTIVE_TRANSACTION_PREFIX}{ACTIVE_TRANSACTION_ID}:4:{MAX_PAGE}"
    )
    legacy = parse_transaction_callback(
        f"{DELETED_TRANSACTION_PREFIX}{DELETED_TRANSACTION_ID}:{MAX_OBJECT_VERSION}",
        prefix=DELETED_TRANSACTION_PREFIX,
    )

    assert target == TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, 4, MAX_PAGE)
    assert legacy == TransactionCallbackTarget(
        DELETED_TRANSACTION_ID,
        MAX_OBJECT_VERSION,
        0,
    )
    assert parse_history_page_callback(f"h:{MAX_PAGE}") == MAX_PAGE
    assert parse_trash_page_callback("z:list:0") == 0


@pytest.mark.parametrize(
    "data",
    [
        "h:-1",
        f"h:{MAX_PAGE + 1}",
        "h:noop",
        "x:1",
        "h:１",
    ],
)
def test_page_callback_parser_rejects_malformed_or_unbounded_values(data: str) -> None:
    with pytest.raises(InvalidFinanceQueryCallback, match="Кнопка повреждена"):
        parse_history_page_callback(data)


@pytest.mark.parametrize(
    "data",
    [
        f"{ACTIVE_TRANSACTION_PREFIX}{ACTIVE_TRANSACTION_ID}:0:0",
        f"{ACTIVE_TRANSACTION_PREFIX}{ACTIVE_TRANSACTION_ID}:4:-1",
        f"{ACTIVE_TRANSACTION_PREFIX}{ACTIVE_TRANSACTION_ID}:4:{MAX_PAGE + 1}",
        f"{ACTIVE_TRANSACTION_PREFIX}not-a-uuid:4:0",
        f"wrong:{ACTIVE_TRANSACTION_ID}:4:0",
    ],
)
def test_transaction_callback_parser_rejects_stale_wire_shapes(data: str) -> None:
    with pytest.raises(InvalidFinanceQueryCallback, match="Кнопка повреждена"):
        parse_transaction_callback(data)


@pytest.mark.parametrize(
    ("version", "page", "error"),
    [
        (0, 0, ValueError),
        (MAX_OBJECT_VERSION + 1, 0, ValueError),
        (True, 0, TypeError),
        (1, -1, InvalidFinanceQueryCallback),
        (1, MAX_PAGE + 1, InvalidFinanceQueryCallback),
    ],
)
def test_transaction_callback_target_cannot_bypass_parser_bounds(
    version: int,
    page: int,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, version, page)


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_receipt_rejects_non_positive_and_boolean_message_ids(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramQueryReceipt("safe", message_id=message_id)


@pytest.mark.asyncio
async def test_active_page_runs_claim_query_receipt_outbox_and_commit_in_one_executor(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    active = _transaction(ACTIVE_TRANSACTION_ID)
    deleted = _transaction(
        DELETED_TRANSACTION_ID,
        deleted_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
        version=5,
    )
    reader = _RecordingReader(events, transactions=(active, deleted))
    controller = _controller(monkeypatch, events, reader, receipts)

    result = await controller.list_transactions(
        TelegramQueryContext(_request(), message_id=94_000_004),
        page=0,
        deleted=False,
    )

    assert result is receipts[0]
    assert "Кафе &amp; рестораны" in result.text
    assert result.reply_markup is not None
    assert "z:list:0" in str(result.reply_markup.model_dump())
    assert reader.owner_ids and set(reader.owner_ids) == {OWNER_ID}
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "reader_factory",
        "query.list:0:5:False",
        "query.list:0:1:True",
        "query.owner",
        "outbox",
        "commit",
        "session.exit",
    ]
    assert "секретное описание" not in caplog.text
    assert "12_345" not in caplog.text
    assert str(ACTIVE_TRANSACTION_ID) not in caplog.text


@pytest.mark.asyncio
async def test_page_is_clamped_and_reloaded_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    transactions = tuple(
        _transaction(
            UUID(int=1_000 + index),
            occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC) - timedelta(minutes=index),
        )
        for index in range(6)
    )
    reader = _RecordingReader(events, transactions=transactions)
    controller = _controller(monkeypatch, events, reader, receipts)

    result = await controller.list_transactions(
        TelegramQueryContext(_request(update_id=None)),
        page=MAX_PAGE,
        deleted=False,
    )

    assert result is not None
    assert result.message_id is None
    assert "Страница 2 из 2" in result.text
    assert reader.list_calls == [
        (MAX_PAGE, 5, False),
        (1, 5, False),
        (0, 1, True),
    ]
    assert receipts == []
    assert events[-2:] == ["commit", "session.exit"]


@pytest.mark.asyncio
async def test_open_history_suspends_active_draft_and_queues_bound_resume_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    reader = _RecordingReader(
        events,
        transactions=(_transaction(ACTIVE_TRANSACTION_ID),),
    )
    drafts = _RecordingDrafts(
        events,
        DraftSnapshot(
            draft_id=DRAFT_ID,
            state="quick_confirm",
            payload={"description": "секретный черновик"},
            revision=7,
        ),
    )
    controller = _controller(
        monkeypatch,
        events,
        reader,
        receipts,
        drafts=drafts,
    )

    result = await controller.open_history(TelegramQueryContext(_request()))

    assert result is receipts[0]
    assert result.suspended_draft == DraftRef(DRAFT_ID, 8)
    assert drafts.active is not None and drafts.active.suspended
    assert result.reply_markup is not None
    button_texts = [button.text for row in result.reply_markup.inline_keyboard for button in row]
    assert "▶️ Продолжить черновик" in button_texts
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "reader_factory",
        "draft_factory",
        "draft.get",
        "draft.suspend",
        "query.list:0:5:False",
        "query.list:0:1:True",
        "query.owner",
        "outbox",
        "commit",
        "session.exit",
    ]
    assert str(DRAFT_ID) not in repr(result)
    assert "секретный черновик" not in repr(result)


@pytest.mark.asyncio
async def test_untracked_open_history_returns_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    reader = _RecordingReader(events)
    drafts = _RecordingDrafts(events, None)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        receipts,
        drafts=drafts,
    )

    result = await controller.open_history(TelegramQueryContext(_request(update_id=None)))

    assert result is not None
    assert result.suspended_draft is None
    assert receipts == []
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


@pytest.mark.asyncio
async def test_open_today_report_uses_owner_timezone_without_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    transaction = _transaction(
        ACTIVE_TRANSACTION_ID,
        occurred_at=datetime(2026, 8, 13, 8, tzinfo=UTC),
    )
    reader = _RecordingReader(events, transactions=(transaction,))
    drafts = _RecordingDrafts(events, None)
    clock = _FixedClock(events, datetime(2026, 8, 13, 12, 30, tzinfo=UTC))
    controller = _controller(
        monkeypatch,
        events,
        reader,
        receipts,
        drafts=drafts,
        clock=clock,
    )

    result = await controller.open_report(
        TelegramQueryContext(_request()),
        FinanceReportPeriod.TODAY,
    )

    assert result is receipts[0]
    assert "📅 Сегодня · 13.08.2026" in result.text
    assert len(reader.period_calls) == 1
    assert reader.period_calls[0] == (
        datetime(2026, 8, 12, 21, tzinfo=UTC),
        datetime(2026, 8, 13, 21, tzinfo=UTC),
    )
    assert result.reply_markup is None
    assert events.index("clock.now") < events.index("query.totals")
    assert events.index("query.period_transactions") < events.index("outbox")
    assert events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_open_month_report_compares_previous_period_and_suspends_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    current = _transaction(
        ACTIVE_TRANSACTION_ID,
        amount_minor=12_345,
        occurred_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
    )
    previous = _transaction(
        UUID("00000000-0000-7000-8000-000000000404"),
        amount_minor=10_000,
        occurred_at=datetime(2026, 7, 10, 8, tzinfo=UTC),
    )
    reader = _RecordingReader(events, transactions=(current, previous))
    drafts = _RecordingDrafts(
        events,
        DraftSnapshot(
            draft_id=DRAFT_ID,
            state="wizard_amount",
            payload={},
            revision=11,
        ),
    )
    clock = _FixedClock(events, datetime(2026, 8, 13, 9, 30, tzinfo=UTC))
    controller = _controller(
        monkeypatch,
        events,
        reader,
        receipts,
        drafts=drafts,
        clock=clock,
    )

    result = await controller.open_report(
        TelegramQueryContext(_request()),
        FinanceReportPeriod.MONTH,
    )

    assert result is receipts[0]
    assert "📊 Август 2026" in result.text
    assert "К тому же периоду прошлого месяца: <b>+23%</b>" in result.text
    assert result.suspended_draft == DraftRef(DRAFT_ID, 12)
    assert result.reply_markup is not None
    assert len(reader.period_calls) == 3
    assert reader.period_calls[0] == reader.period_calls[2]
    assert reader.period_calls[1] == (
        datetime(2026, 6, 30, 21, tzinfo=UTC),
        datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
    )
    assert events.index("draft.suspend") < events.index("query.totals")
    assert events.index("query.period_transactions") < events.index("outbox")
    assert events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_deleted_transaction_view_checks_owner_state_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    deleted = _transaction(
        DELETED_TRANSACTION_ID,
        deleted_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
        version=5,
    )
    reader = _RecordingReader(events, transactions=(deleted,))
    controller = _controller(monkeypatch, events, reader, receipts)

    result = await controller.get_transaction(
        TelegramQueryContext(_request(), message_id=94_000_004),
        TransactionCallbackTarget(DELETED_TRANSACTION_ID, 5, 2),
        deleted=True,
    )

    assert result is not None
    assert "Операция в Корзине" in result.text
    assert result.reply_markup is not None
    assert f"z:restore:{DELETED_TRANSACTION_ID}:5:2" in str(result.reply_markup.model_dump())
    assert set(reader.owner_ids) == {OWNER_ID}


@pytest.mark.asyncio
async def test_delete_confirmation_uses_exact_active_snapshot_and_atomic_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    active = _transaction(ACTIVE_TRANSACTION_ID, version=7)
    reader = _RecordingReader(events, transactions=(active,))
    controller = _controller(monkeypatch, events, reader, receipts)

    result = await controller.confirm_transaction_delete(
        TelegramQueryContext(_request(), message_id=94_000_004),
        TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, 7, 3),
    )

    assert result is receipts[0]
    assert "Удалить операцию?" in result.text
    assert "останется в Корзине" in result.text
    assert result.reply_markup is not None
    assert f"tx:del:do:{ACTIVE_TRANSACTION_ID}:7:3" in str(result.reply_markup.model_dump())
    assert events.index("query.transaction") < events.index("query.owner")
    assert events.index("query.owner") < events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_delete_confirmation_rejects_deleted_or_stale_snapshot_without_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for transaction, expected_version in (
        (_transaction(ACTIVE_TRANSACTION_ID, version=4), 3),
        (
            _transaction(
                ACTIVE_TRANSACTION_ID,
                version=4,
                deleted_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
            ),
            4,
        ),
    ):
        events: list[str] = []
        receipts: list[TelegramQueryReceipt] = []
        controller = _controller(
            monkeypatch,
            events,
            _RecordingReader(events, transactions=(transaction,)),
            receipts,
        )

        with pytest.raises(StaleFinanceQueryCallback, match="Операция изменилась"):
            await controller.confirm_transaction_delete(
                TelegramQueryContext(_request(), message_id=94_000_004),
                TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, expected_version, 0),
            )

        assert receipts == []
        assert "outbox" not in events
        assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_transaction_view_rolls_back_when_callback_version_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    active = _transaction(ACTIVE_TRANSACTION_ID, version=4)
    reader = _RecordingReader(events, transactions=(active,))
    controller = _controller(monkeypatch, events, reader, receipts)

    with pytest.raises(StaleFinanceQueryCallback, match="Операция изменилась"):
        await controller.get_transaction(
            TelegramQueryContext(_request(), message_id=94_000_004),
            TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, 3, 0),
            deleted=False,
        )

    assert receipts == []
    assert events[-2:] == ["rollback", "session.exit"]
    assert "query.owner" not in events


@pytest.mark.asyncio
async def test_period_report_uses_shared_owner_scoped_query_and_escaped_presenter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    expense = _transaction(
        ACTIVE_TRANSACTION_ID,
        amount_minor=12_345,
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    income = _transaction(
        UUID("00000000-0000-7000-8000-000000000403"),
        kind=TransactionType.INCOME,
        amount_minor=50_000,
        occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    reader = _RecordingReader(events, transactions=(expense, income))
    controller = _controller(monkeypatch, events, reader, receipts)

    result = await controller.get_period_report(
        TelegramQueryContext(_request(), message_id=None),
        PeriodReportQuery(start, end, "📊 <Август & отчёт>"),
    )

    assert result is receipts[0]
    assert "📊 &lt;Август &amp; отчёт&gt;" in result.text
    assert "Доходы" in result.text
    assert "Расходы по категориям" in result.text
    assert set(reader.owner_ids) == {OWNER_ID}
    assert events.index("query.totals") < events.index("query.categories")
    assert events.index("query.categories") < events.index("query.period_transactions")
    assert events.index("query.owner") < events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_duplicate_update_returns_none_without_query_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    reader = _RecordingReader(events, transactions=(_transaction(ACTIVE_TRANSACTION_ID),))
    controller = _controller(monkeypatch, events, reader, receipts, claimed=False)

    result = await controller.list_transactions(
        TelegramQueryContext(_request(), message_id=94_000_004),
        page=0,
        deleted=False,
    )

    assert result is None
    assert receipts == []
    assert reader.owner_ids == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_duplicate_initial_navigation_does_not_suspend_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramQueryReceipt] = []
    reader = _RecordingReader(events)
    drafts = _RecordingDrafts(
        events,
        DraftSnapshot(DRAFT_ID, "quick_confirm", {}, revision=3),
    )
    controller = _controller(
        monkeypatch,
        events,
        reader,
        receipts,
        claimed=False,
        drafts=drafts,
    )

    result = await controller.open_history(TelegramQueryContext(_request()))

    assert result is None
    assert drafts.active is not None and not drafts.active.suspended
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


def test_controller_contracts_hide_financial_and_telegram_values_from_repr() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    values = (
        TelegramQueryContext(_request(), message_id=94_000_004),
        TransactionCallbackTarget(ACTIVE_TRANSACTION_ID, 4, 3),
        PeriodReportQuery(start, start + timedelta(days=1), "секретный отчёт"),
        TelegramQueryReceipt(
            text="секретное описание 123,45 ₽",
            message_id=94_000_004,
            suspended_draft=DraftRef(DRAFT_ID, 7),
        ),
    )

    for value in values:
        rendered = repr(value)
        assert "секрет" not in rendered
        assert "91_000_001" not in rendered
        assert "94000004" not in rendered
        assert "123,45" not in rendered
        assert str(ACTIVE_TRANSACTION_ID) not in rendered
        assert str(DRAFT_ID) not in rendered
