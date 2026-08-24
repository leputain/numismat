from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from fakes.queries import InMemoryQueryRepository

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    OwnerSnapshot,
    TimeSeriesGrain,
    TransactionCursor,
    TransactionListFilters,
    TransactionSnapshot,
)
from finbot.application.errors import (
    ApplicationErrorCode,
    ApplicationValidationError,
    EntityNotFoundError,
)
from finbot.application.use_cases.queries import (
    ComparePeriods,
    GetDashboard,
    GetOwnerSettings,
    GetPeriodReport,
    GetTimeSeries,
    GetTransaction,
    ListAccounts,
    ListCategories,
    ListTransactions,
    ListTransactionsByCursor,
)
from finbot.domain.transactions import TransactionType


def _transaction(
    *,
    occurred_at: datetime,
    currency: str = "RUB",
    kind: TransactionType = TransactionType.EXPENSE,
    amount_minor: int = 100,
    deleted_at: datetime | None = None,
    account_id: UUID | None = None,
    category_id: UUID | None = None,
) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=uuid7(),
        kind=kind,
        amount_minor=amount_minor,
        currency=currency,
        account_id=account_id or uuid7(),
        account_name="Синтетический счёт",
        category_id=category_id or uuid7(),
        category_name="Синтетическая категория",
        category_emoji="▫️",
        occurred_at=occurred_at,
        description="",
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
async def test_get_transaction_is_owner_scoped_and_maps_missing_to_typed_error() -> None:
    owner_id = uuid7()
    other_owner_id = uuid7()
    transaction = _transaction(occurred_at=datetime.now(UTC))
    repository = InMemoryQueryRepository(transactions={owner_id: (transaction,)})
    query = GetTransaction(repository)

    assert await query(owner_id, transaction.transaction_id) == transaction
    with pytest.raises(EntityNotFoundError) as missing:
        await query(other_owner_id, transaction.transaction_id)
    assert missing.value.code is ApplicationErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_list_transactions_returns_an_immutable_bounded_page() -> None:
    owner_id = uuid7()
    now = datetime.now(UTC)
    transactions = (
        _transaction(occurred_at=now),
        _transaction(occurred_at=now - timedelta(minutes=1)),
    )
    repository = InMemoryQueryRepository(transactions={owner_id: transactions})

    page = await ListTransactions(repository)(owner_id, page=0, page_size=1)

    assert page.items == transactions[:1]
    assert page.total == 2
    assert page.total_pages == 2
    with pytest.raises(FrozenInstanceError):
        page.total = 3  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "page_size"),
    [(-1, 20), (False, 20), (0, 0), (0, 101)],
)
async def test_list_transactions_rejects_invalid_pagination(page: int, page_size: int) -> None:
    with pytest.raises(ApplicationValidationError) as invalid:
        await ListTransactions(InMemoryQueryRepository())(uuid7(), page=page, page_size=page_size)
    assert invalid.value.code is ApplicationErrorCode.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_cursor_transactions_use_occurred_at_and_uuid_without_offset() -> None:
    owner_id = uuid7()
    occurred_at = datetime(2026, 8, 13, 10, tzinfo=UTC)
    first = _transaction(occurred_at=occurred_at)
    second = _transaction(occurred_at=occurred_at)
    third = _transaction(occurred_at=occurred_at - timedelta(seconds=1))
    ordered = tuple(
        sorted(
            (first, second),
            key=lambda item: item.transaction_id.int,
            reverse=True,
        )
    ) + (third,)
    repository = InMemoryQueryRepository(transactions={owner_id: ordered})
    query = ListTransactionsByCursor(repository)

    first_page = await query(owner_id, limit=2)
    assert tuple(item.transaction for item in first_page.items) == ordered[:2]
    assert first_page.has_more is True

    anchor = first_page.items[-1].cursor
    second_page = await query(owner_id, cursor=anchor, limit=2)
    assert tuple(item.transaction for item in second_page.items) == ordered[2:]
    assert second_page.has_more is False
    assert "2026" not in repr(anchor)
    assert str(anchor.transaction_id) not in repr(anchor)


@pytest.mark.asyncio
async def test_cursor_transactions_apply_all_owner_scoped_filters_before_limit() -> None:
    owner_id = uuid7()
    other_owner_id = uuid7()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    account_id = uuid7()
    category_id = uuid7()
    target = _transaction(
        occurred_at=start + timedelta(days=1),
        account_id=account_id,
        category_id=category_id,
    )
    excluded = (
        _transaction(
            occurred_at=start - timedelta(seconds=1),
            account_id=account_id,
            category_id=category_id,
        ),
        _transaction(
            occurred_at=start + timedelta(days=1),
            kind=TransactionType.INCOME,
            account_id=account_id,
            category_id=category_id,
        ),
        _transaction(
            occurred_at=start + timedelta(days=1),
            currency="USD",
            account_id=account_id,
            category_id=category_id,
        ),
        _transaction(
            occurred_at=start + timedelta(days=1),
            account_id=uuid7(),
            category_id=category_id,
        ),
        _transaction(
            occurred_at=start + timedelta(days=1),
            account_id=account_id,
            category_id=uuid7(),
        ),
    )
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: (target, *excluded),
            other_owner_id: (target,),
        }
    )

    page = await ListTransactionsByCursor(repository)(
        owner_id,
        limit=100,
        filters=TransactionListFilters(
            start=start,
            end=end,
            kind=TransactionType.EXPENSE,
            account_id=account_id,
            category_id=category_id,
            currency="RUB",
        ),
    )

    assert tuple(item.transaction for item in page.items) == (target,)
    assert not page.has_more


def test_transaction_list_filters_are_immutable_bounded_and_repr_safe() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    filters = TransactionListFilters(
        start=start,
        end=start + timedelta(days=1),
        account_id=uuid7(),
        category_id=uuid7(),
        currency="RUB",
    )

    assert not filters.is_empty
    assert "2026" not in repr(filters)
    assert str(filters.account_id) not in repr(filters)
    assert TransactionListFilters().is_empty
    with pytest.raises(FrozenInstanceError):
        filters.currency = "USD"
    for invalid in (
        {"start": start},
        {"start": datetime(2026, 8, 1), "end": datetime(2026, 8, 2)},
        {"start": start, "end": start},
        {"currency": "rub"},
    ):
        with pytest.raises((TypeError, ValueError)):
            TransactionListFilters(**invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, False])
async def test_cursor_transactions_reject_invalid_limit(limit: int) -> None:
    with pytest.raises(ApplicationValidationError):
        await ListTransactionsByCursor(InMemoryQueryRepository())(uuid7(), limit=limit)


def test_transaction_cursor_requires_timezone() -> None:
    with pytest.raises(ValueError):
        TransactionCursor(datetime(2026, 8, 13), uuid7())


@pytest.mark.asyncio
async def test_catalog_and_owner_queries_return_only_requested_owner_state() -> None:
    owner_id = uuid7()
    other_owner_id = uuid7()
    active_account = AccountSnapshot(uuid7(), "Основной", "card", "RUB", None, 1)
    archived_account = AccountSnapshot(uuid7(), "Архив", "cash", "RUB", datetime.now(UTC), 2)
    expense = CategorySnapshot(uuid7(), TransactionType.EXPENSE, "Расход", "▫️", None, 1)
    income = CategorySnapshot(uuid7(), TransactionType.INCOME, "Доход", "▫️", None, 1)
    owner = OwnerSnapshot(owner_id, "ru_RU", "Europe/Moscow", "RUB", active_account.account_id)
    repository = InMemoryQueryRepository(
        owners={owner_id: owner},
        accounts={owner_id: (active_account, archived_account)},
        categories={owner_id: (expense, income)},
    )

    assert await ListAccounts(repository)(owner_id) == (active_account,)
    assert await ListAccounts(repository)(owner_id, archived=True) == (archived_account,)
    assert await ListCategories(repository)(owner_id, kind=TransactionType.EXPENSE) == (expense,)
    assert await GetOwnerSettings(repository)(owner_id) == owner
    assert await ListAccounts(repository)(other_owner_id) == ()
    with pytest.raises(EntityNotFoundError):
        await GetOwnerSettings(repository)(other_owner_id)
    with pytest.raises(ApplicationValidationError):
        await ListCategories(repository)(owner_id, kind="unknown")


@pytest.mark.asyncio
async def test_reports_and_comparison_keep_currency_totals_separate() -> None:
    owner_id = uuid7()
    current_start = datetime(2026, 8, 1, tzinfo=UTC)
    current_end = datetime(2026, 9, 1, tzinfo=UTC)
    previous_start = datetime(2026, 7, 1, tzinfo=UTC)
    previous_end = datetime(2026, 8, 1, tzinfo=UTC)
    account_id = uuid7()
    category_id = uuid7()
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: (
                _transaction(
                    occurred_at=current_start,
                    amount_minor=400,
                    account_id=account_id,
                    category_id=category_id,
                ),
                _transaction(
                    occurred_at=current_start + timedelta(days=1),
                    kind=TransactionType.INCOME,
                    amount_minor=900,
                    account_id=account_id,
                    category_id=category_id,
                ),
                _transaction(
                    occurred_at=current_start + timedelta(days=2),
                    currency="USD",
                    amount_minor=7,
                    account_id=account_id,
                    category_id=category_id,
                ),
                _transaction(
                    occurred_at=previous_start,
                    amount_minor=300,
                    account_id=account_id,
                    category_id=category_id,
                ),
            )
        }
    )

    report = await GetPeriodReport(repository)(owner_id, current_start, current_end)
    comparison = await ComparePeriods(repository)(
        owner_id,
        current_start,
        current_end,
        previous_start,
        previous_end,
    )

    assert [item.currency for item in report.totals] == ["RUB", "USD"]
    assert [(item.currency, item.net_minor) for item in report.totals] == [
        ("RUB", 500),
        ("USD", -7),
    ]
    assert [(item.currency, item.expense_minor) for item in comparison.previous_totals] == [
        ("RUB", 300)
    ]
    assert len(report.category_totals) == 2
    assert len(report.transactions) == 3


@pytest.mark.asyncio
async def test_timeseries_uses_local_calendar_buckets_counts_and_clipped_bounds() -> None:
    owner_id = uuid7()
    start = datetime(2026, 8, 10, 22, tzinfo=UTC)
    end = datetime(2026, 8, 12, 10, tzinfo=UTC)
    deleted = _transaction(
        occurred_at=datetime(2026, 8, 12, 8, tzinfo=UTC),
        amount_minor=999,
        deleted_at=end,
    )
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: (
                _transaction(occurred_at=start + timedelta(minutes=30), amount_minor=250),
                _transaction(
                    occurred_at=start + timedelta(hours=5),
                    amount_minor=900,
                    kind=TransactionType.INCOME,
                ),
                _transaction(
                    occurred_at=datetime(2026, 8, 12, 8, tzinfo=UTC),
                    amount_minor=7,
                    currency="USD",
                ),
                deleted,
            )
        }
    )

    result = await GetTimeSeries(repository)(
        owner_id,
        start,
        end,
        timezone="Europe/Moscow",
        grain=TimeSeriesGrain.DAY,
    )

    assert result.start == start
    assert result.end == end
    assert result.timezone == "Europe/Moscow"
    assert result.grain is TimeSeriesGrain.DAY
    assert [(bucket.start, bucket.end) for bucket in result.buckets] == [
        (start, datetime(2026, 8, 11, 21, tzinfo=UTC)),
        (datetime(2026, 8, 11, 21, tzinfo=UTC), end),
    ]
    first = result.buckets[0].totals[0]
    assert (
        first.currency,
        first.income_minor,
        first.expense_minor,
        first.net_minor,
        first.income_count,
        first.expense_count,
    ) == ("RUB", 900, 250, 650, 1, 1)
    assert tuple(item.currency for item in result.buckets[1].totals) == ("USD",)
    assert result.buckets[1].totals[0].expense_minor == 7


@pytest.mark.asyncio
async def test_timeseries_rejects_more_than_366_owner_local_buckets() -> None:
    start = datetime(2025, 1, 1, 12, tzinfo=UTC)

    with pytest.raises(ApplicationValidationError):
        await GetTimeSeries(InMemoryQueryRepository())(
            uuid7(),
            start,
            start + timedelta(days=366),
            timezone="UTC",
            grain="day",
        )


@pytest.mark.asyncio
async def test_timeseries_rejects_more_than_32_currencies_in_one_bucket() -> None:
    owner_id = uuid7()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    currencies = tuple(
        f"{chr(65 + first)}{chr(65 + second)}{chr(65 + third)}"
        for first in range(26)
        for second in range(26)
        for third in range(26)
    )[:33]
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: tuple(
                _transaction(occurred_at=start, currency=currency) for currency in currencies
            )
        }
    )

    with pytest.raises(ApplicationValidationError):
        await GetTimeSeries(repository)(
            owner_id,
            start,
            start + timedelta(days=1),
            timezone="UTC",
            grain="day",
        )


@pytest.mark.asyncio
async def test_timeseries_week_starts_on_owner_local_iso_monday() -> None:
    start = datetime(2026, 8, 12, tzinfo=UTC)
    end = datetime(2026, 8, 19, tzinfo=UTC)

    result = await GetTimeSeries(InMemoryQueryRepository())(
        uuid7(),
        start,
        end,
        timezone="Europe/Moscow",
        grain="week",
    )

    assert [(item.start, item.end, item.totals) for item in result.buckets] == [
        (start, datetime(2026, 8, 16, 21, tzinfo=UTC), ()),
        (datetime(2026, 8, 16, 21, tzinfo=UTC), end, ()),
    ]


@pytest.mark.asyncio
async def test_dashboard_reuses_comparable_period_and_applies_bounded_limits() -> None:
    owner_id = uuid7()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    previous_start = datetime(2026, 7, 1, tzinfo=UTC)
    previous_end = datetime(2026, 8, 1, tzinfo=UTC)
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: tuple(
                _transaction(occurred_at=start + timedelta(days=offset), amount_minor=offset + 1)
                for offset in range(3)
            )
        }
    )

    dashboard = await GetDashboard(repository)(
        owner_id,
        start,
        end,
        previous_start,
        previous_end,
        category_limit=1,
        recent_limit=2,
    )

    assert len(dashboard.top_categories) == 1
    assert len(dashboard.recent_transactions) == 2
    assert dashboard.previous_totals == ()


@pytest.mark.asyncio
async def test_reports_fail_closed_before_category_query_above_currency_cap() -> None:
    owner_id = uuid7()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    currencies = tuple(
        f"{chr(65 + first)}{chr(65 + second)}{chr(65 + third)}"
        for first in range(26)
        for second in range(26)
        for third in range(26)
    )[:33]
    repository = InMemoryQueryRepository(
        transactions={
            owner_id: tuple(
                _transaction(
                    occurred_at=start,
                    currency=currency,
                    category_id=uuid7(),
                )
                for currency in currencies
            )
        }
    )

    with pytest.raises(ApplicationValidationError) as too_many:
        await GetPeriodReport(repository)(owner_id, start, start + timedelta(days=1))

    assert too_many.value.code is ApplicationErrorCode.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_period_queries_reject_naive_or_reversed_bounds_before_reading() -> None:
    owner_id = uuid7()
    aware = datetime(2026, 8, 1, tzinfo=UTC)
    naive = datetime(2026, 8, 1)
    report = GetPeriodReport(InMemoryQueryRepository())

    with pytest.raises(ApplicationValidationError):
        await report(owner_id, naive, aware)
    with pytest.raises(ApplicationValidationError):
        await report(owner_id, aware + timedelta(days=1), aware)
