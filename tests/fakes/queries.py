from collections.abc import Mapping
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from finbot.application.dto import (
    EMPTY_TRANSACTION_LIST_FILTERS,
    AccountSnapshot,
    CategorySnapshot,
    CategoryTotalSnapshot,
    CurrencyTotals,
    DeletedTransactionCursor,
    DeletedTransactionCursorItem,
    OwnerSnapshot,
    TimeSeriesAggregateRow,
    TimeSeriesCurrencyTotals,
    TimeSeriesGrain,
    TransactionCursor,
    TransactionCursorItem,
    TransactionListFilters,
    TransactionSnapshot,
)
from finbot.domain.transactions import TransactionType


class InMemoryQueryRepository:
    """Deterministic owner-scoped fake for shared application queries."""

    def __init__(
        self,
        *,
        owners: Mapping[UUID, OwnerSnapshot] | None = None,
        accounts: Mapping[UUID, tuple[AccountSnapshot, ...]] | None = None,
        categories: Mapping[UUID, tuple[CategorySnapshot, ...]] | None = None,
        transactions: Mapping[UUID, tuple[TransactionSnapshot, ...]] | None = None,
    ) -> None:
        self._owners = dict(owners or {})
        self._accounts = {owner_id: tuple(items) for owner_id, items in (accounts or {}).items()}
        self._categories = {
            owner_id: tuple(items) for owner_id, items in (categories or {}).items()
        }
        self._transactions = {
            owner_id: tuple(items) for owner_id, items in (transactions or {}).items()
        }

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        return self._owners.get(owner_id)

    async def list_accounts(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]:
        return tuple(
            account
            for account in self._accounts.get(owner_id, ())
            if (account.archived_at is not None) is archived
        )

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        return tuple(
            category
            for category in self._categories.get(owner_id, ())
            if (category.archived_at is not None) is archived
            and (kind is None or category.kind.value == kind)
        )

    async def get_transaction(
        self, owner_id: UUID, transaction_id: UUID
    ) -> TransactionSnapshot | None:
        return next(
            (
                transaction
                for transaction in self._transactions.get(owner_id, ())
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
        matching = tuple(
            transaction
            for transaction in self._transactions.get(owner_id, ())
            if (transaction.deleted_at is not None) is deleted
        )
        start = page * page_size
        return matching[start : start + page_size], len(matching)

    async def list_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: TransactionCursor | None,
        limit: int,
        filters: TransactionListFilters = EMPTY_TRANSACTION_LIST_FILTERS,
    ) -> tuple[TransactionCursorItem, ...]:
        matching = sorted(
            (
                TransactionCursorItem(
                    transaction=transaction,
                    cursor=TransactionCursor(
                        occurred_at=transaction.occurred_at,
                        transaction_id=transaction.transaction_id,
                    ),
                )
                for transaction in self._transactions.get(owner_id, ())
                if transaction.deleted_at is None
                and (filters.start is None or transaction.occurred_at >= filters.start)
                and (filters.end is None or transaction.occurred_at < filters.end)
                and (filters.kind is None or transaction.kind is filters.kind)
                and (filters.account_id is None or transaction.account_id == filters.account_id)
                and (filters.category_id is None or transaction.category_id == filters.category_id)
                and (filters.currency is None or transaction.currency == filters.currency)
            ),
            key=lambda item: (
                item.cursor.occurred_at,
                item.cursor.transaction_id.int,
            ),
            reverse=True,
        )
        if cursor is not None:
            anchor = (cursor.occurred_at, cursor.transaction_id.int)
            matching = [
                item
                for item in matching
                if (
                    item.cursor.occurred_at,
                    item.cursor.transaction_id.int,
                )
                < anchor
            ]
        return tuple(matching[:limit])

    async def list_deleted_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: DeletedTransactionCursor | None,
        limit: int,
    ) -> tuple[DeletedTransactionCursorItem, ...]:
        matching = sorted(
            (
                DeletedTransactionCursorItem(
                    transaction=transaction,
                    cursor=DeletedTransactionCursor(
                        deleted_at=transaction.deleted_at,
                        transaction_id=transaction.transaction_id,
                    ),
                )
                for transaction in self._transactions.get(owner_id, ())
                if transaction.deleted_at is not None
            ),
            key=lambda item: (
                item.cursor.deleted_at,
                item.cursor.transaction_id.int,
            ),
            reverse=True,
        )
        if cursor is not None:
            anchor = (cursor.deleted_at, cursor.transaction_id.int)
            matching = [
                item
                for item in matching
                if (item.cursor.deleted_at, item.cursor.transaction_id.int) < anchor
            ]
        return tuple(matching[:limit])

    def _in_period(
        self, owner_id: UUID, start: datetime, end: datetime
    ) -> tuple[TransactionSnapshot, ...]:
        return tuple(
            transaction
            for transaction in self._transactions.get(owner_id, ())
            if transaction.deleted_at is None and start <= transaction.occurred_at < end
        )

    async def totals_by_currency(
        self, owner_id: UUID, start: datetime, end: datetime
    ) -> tuple[CurrencyTotals, ...]:
        grouped: dict[str, dict[TransactionType, int]] = {}
        for transaction in self._in_period(owner_id, start, end):
            totals = grouped.setdefault(
                transaction.currency,
                {TransactionType.INCOME: 0, TransactionType.EXPENSE: 0},
            )
            totals[transaction.kind] += transaction.amount_minor
        return tuple(
            CurrencyTotals(
                currency=currency,
                income_minor=grouped[currency][TransactionType.INCOME],
                expense_minor=grouped[currency][TransactionType.EXPENSE],
            )
            for currency in sorted(grouped)
        )

    async def category_totals(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[CategoryTotalSnapshot, ...]:
        grouped: dict[tuple[UUID, str, str, str], int] = {}
        for transaction in self._in_period(owner_id, start, end):
            if transaction.kind is not TransactionType.EXPENSE:
                continue
            key = (
                transaction.category_id,
                transaction.category_name,
                transaction.category_emoji,
                transaction.currency,
            )
            grouped[key] = grouped.get(key, 0) + transaction.amount_minor
        result = tuple(
            CategoryTotalSnapshot(
                category_id=category_id,
                name=name,
                emoji=emoji,
                currency=currency,
                amount_minor=amount_minor,
            )
            for (category_id, name, emoji, currency), amount_minor in grouped.items()
        )
        ordered = sorted(
            result,
            key=lambda item: (
                item.currency,
                -item.amount_minor,
                item.name,
                item.category_id.int,
            ),
        )
        selected: list[CategoryTotalSnapshot] = []
        currency_counts: dict[str, int] = {}
        for item in ordered:
            count = currency_counts.get(item.currency, 0)
            if count >= limit:
                continue
            selected.append(item)
            currency_counts[item.currency] = count + 1
        return tuple(selected)

    async def period_transactions(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]:
        ordered = sorted(
            self._in_period(owner_id, start, end),
            key=lambda item: (item.occurred_at, item.transaction_id.int),
            reverse=True,
        )
        return tuple(ordered[:limit])

    async def timeseries_by_currency(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        timezone: str,
        grain: TimeSeriesGrain,
        row_limit: int,
    ) -> tuple[TimeSeriesAggregateRow, ...]:
        zone = ZoneInfo(timezone)
        grouped: dict[tuple[date, str], list[int]] = {}
        for transaction in self._in_period(owner_id, start, end):
            local_date = transaction.occurred_at.astimezone(zone).date()
            if grain is TimeSeriesGrain.WEEK:
                local_date -= timedelta(days=local_date.weekday())
            elif grain is TimeSeriesGrain.MONTH:
                local_date = local_date.replace(day=1)
            values = grouped.setdefault((local_date, transaction.currency), [0, 0, 0, 0])
            if transaction.kind is TransactionType.INCOME:
                values[0] += transaction.amount_minor
                values[2] += 1
            else:
                values[1] += transaction.amount_minor
                values[3] += 1
        return tuple(
            TimeSeriesAggregateRow(
                bucket_local_date=bucket_local_date,
                totals=TimeSeriesCurrencyTotals(
                    currency=currency,
                    income_minor=values[0],
                    expense_minor=values[1],
                    income_count=values[2],
                    expense_count=values[3],
                ),
            )
            for (bucket_local_date, currency), values in sorted(grouped.items())
        )[:row_limit]
