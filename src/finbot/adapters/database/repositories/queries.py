from datetime import datetime
from uuid import UUID

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, Transaction, User
from finbot.adapters.database.queries.reports import totals_by_currency
from finbot.adapters.database.queries.transactions import (
    get_transaction_details,
    list_deleted_transaction_details,
    list_deleted_transaction_details_after,
    list_transaction_details,
    list_transaction_details_after,
)
from finbot.application.catalogs import BOUNDED_CATALOG_FETCH_LIMIT
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
from finbot.application.queries.transactions import TransactionDetails
from finbot.domain.transactions import TransactionType

_MAX_TIMESERIES_QUERY_ROW_LIMIT = 366 * 32 + 1


def _transaction_snapshot(details: TransactionDetails) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=details.id,
        kind=TransactionType(details.type),
        amount_minor=details.amount_minor,
        currency=details.currency,
        account_id=details.account_id,
        account_name=details.account_name,
        category_id=details.category_id,
        category_name=details.category_name,
        category_emoji=details.category_emoji,
        occurred_at=details.occurred_at,
        description=details.description,
        source=details.source,
        deleted_at=details.deleted_at,
        version=details.version,
    )


TransactionRow = Row[tuple[Transaction, str, str, str]]


def _transaction_row_snapshot(row: TransactionRow) -> TransactionSnapshot:
    transaction, category_name, category_emoji, account_name = row._t
    return TransactionSnapshot(
        transaction_id=transaction.id,
        kind=TransactionType(transaction.type),
        amount_minor=transaction.amount_minor,
        currency=transaction.currency,
        account_id=transaction.account_id,
        account_name=str(account_name),
        category_id=transaction.category_id,
        category_name=str(category_name),
        category_emoji=str(category_emoji),
        occurred_at=transaction.occurred_at,
        description=transaction.description,
        source=transaction.source,
        deleted_at=transaction.deleted_at,
        version=transaction.version,
    )


class SqlAlchemyQueryRepository:
    """Owner-scoped read model shared by presentation adapters."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        row = (
            await self._session.execute(
                select(
                    User.id,
                    User.locale,
                    User.timezone,
                    User.base_currency,
                    User.default_account_id,
                    User.fast_mode,
                    User.settings_version,
                ).where(User.id == owner_id)
            )
        ).one_or_none()
        if row is None:
            return None
        (
            user_id,
            locale,
            timezone,
            base_currency,
            default_account_id,
            fast_mode,
            settings_version,
        ) = row._t
        return OwnerSnapshot(
            owner_id=user_id,
            locale=locale,
            timezone=timezone,
            base_currency=base_currency,
            default_account_id=default_account_id,
            fast_mode=fast_mode,
            settings_version=settings_version,
        )

    async def list_accounts(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]:
        archive_filter = (
            Account.archived_at.is_not(None) if archived else Account.archived_at.is_(None)
        )
        rows = await self._session.execute(
            select(
                Account.id,
                Account.name,
                Account.type,
                Account.currency,
                Account.archived_at,
                Account.version,
            )
            .where(Account.user_id == owner_id, archive_filter)
            .order_by(Account.name, Account.id)
        )
        return tuple(
            AccountSnapshot(
                account_id=account_id,
                name=name,
                account_type=account_type,
                currency=currency,
                archived_at=archived_at,
                version=version,
            )
            for account_id, name, account_type, currency, archived_at, version in rows
        )

    async def list_accounts_bounded(
        self,
        owner_id: UUID,
        *,
        archived: bool,
        limit: int,
    ) -> tuple[AccountSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= BOUNDED_CATALOG_FETCH_LIMIT:
            raise ValueError("catalog query limit is invalid")
        if type(archived) is not bool:
            raise ValueError("catalog archive selector is invalid")
        archive_filter = (
            Account.archived_at.is_not(None) if archived else Account.archived_at.is_(None)
        )
        rows = await self._session.execute(
            select(
                Account.id,
                Account.name,
                Account.type,
                Account.currency,
                Account.archived_at,
                Account.version,
            )
            .where(Account.user_id == owner_id, archive_filter)
            .order_by(Account.name, Account.id)
            .limit(limit)
        )
        return tuple(
            AccountSnapshot(
                account_id=account_id,
                name=name,
                account_type=account_type,
                currency=currency,
                archived_at=archived_at,
                version=version,
            )
            for account_id, name, account_type, currency, archived_at, version in rows
        )

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        archive_filter = (
            Category.archived_at.is_not(None) if archived else Category.archived_at.is_(None)
        )
        query = select(
            Category.id,
            Category.kind,
            Category.name,
            Category.emoji,
            Category.archived_at,
            Category.version,
        ).where(Category.user_id == owner_id, archive_filter)
        if kind is not None:
            query = query.where(Category.kind == kind)
        rows = await self._session.execute(query.order_by(Category.name, Category.id))
        return tuple(
            CategorySnapshot(
                category_id=category_id,
                kind=TransactionType(category_kind),
                name=name,
                emoji=emoji,
                archived_at=archived_at,
                version=version,
            )
            for category_id, category_kind, name, emoji, archived_at, version in rows
        )

    async def list_categories_bounded(
        self,
        owner_id: UUID,
        *,
        kind: str | None,
        archived: bool,
        limit: int,
    ) -> tuple[CategorySnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= BOUNDED_CATALOG_FETCH_LIMIT:
            raise ValueError("catalog query limit is invalid")
        if type(archived) is not bool:
            raise ValueError("catalog archive selector is invalid")
        if kind not in {None, TransactionType.EXPENSE.value, TransactionType.INCOME.value}:
            raise ValueError("catalog kind selector is invalid")
        archive_filter = (
            Category.archived_at.is_not(None) if archived else Category.archived_at.is_(None)
        )
        query = select(
            Category.id,
            Category.kind,
            Category.name,
            Category.emoji,
            Category.archived_at,
            Category.version,
        ).where(Category.user_id == owner_id, archive_filter)
        if kind is not None:
            query = query.where(Category.kind == kind)
        rows = await self._session.execute(query.order_by(Category.name, Category.id).limit(limit))
        return tuple(
            CategorySnapshot(
                category_id=category_id,
                kind=TransactionType(category_kind),
                name=name,
                emoji=emoji,
                archived_at=archived_at,
                version=version,
            )
            for category_id, category_kind, name, emoji, archived_at, version in rows
        )

    async def get_transaction(
        self, owner_id: UUID, transaction_id: UUID
    ) -> TransactionSnapshot | None:
        details = await get_transaction_details(self._session, owner_id, transaction_id)
        return _transaction_snapshot(details) if details is not None else None

    async def list_transactions(
        self,
        owner_id: UUID,
        *,
        page: int,
        page_size: int,
        deleted: bool = False,
    ) -> tuple[tuple[TransactionSnapshot, ...], int]:
        loader = list_deleted_transaction_details if deleted else list_transaction_details
        details, total = await loader(
            self._session,
            owner_id,
            page=page,
            page_size=page_size,
        )
        return tuple(_transaction_snapshot(item) for item in details), total

    async def list_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: TransactionCursor | None,
        limit: int,
        filters: TransactionListFilters = EMPTY_TRANSACTION_LIST_FILTERS,
    ) -> tuple[TransactionCursorItem, ...]:
        rows = await list_transaction_details_after(
            self._session,
            owner_id,
            cursor=cursor,
            limit=limit,
            filters=filters,
        )
        return tuple(
            TransactionCursorItem(
                transaction=_transaction_snapshot(details),
                cursor=TransactionCursor(
                    occurred_at=details.occurred_at,
                    transaction_id=details.id,
                ),
            )
            for details in rows
        )

    async def list_deleted_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: DeletedTransactionCursor | None,
        limit: int,
    ) -> tuple[DeletedTransactionCursorItem, ...]:
        rows = await list_deleted_transaction_details_after(
            self._session,
            owner_id,
            cursor=cursor,
            limit=limit,
        )
        return tuple(
            DeletedTransactionCursorItem(
                transaction=_transaction_snapshot(details),
                cursor=DeletedTransactionCursor(
                    deleted_at=details.deleted_at,
                    transaction_id=details.id,
                ),
            )
            for details in rows
            if details.deleted_at is not None
        )

    async def totals_by_currency(
        self, owner_id: UUID, start: datetime, end: datetime
    ) -> tuple[CurrencyTotals, ...]:
        grouped = await totals_by_currency(
            self._session,
            owner_id,
            start,
            end,
            currency_limit=33,
        )
        return tuple(
            CurrencyTotals(
                currency=currency,
                income_minor=grouped[currency].get(TransactionType.INCOME.value, 0),
                expense_minor=grouped[currency].get(TransactionType.EXPENSE.value, 0),
            )
            for currency in sorted(grouped)
        )

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
        if type(row_limit) is not int or not 1 <= row_limit <= _MAX_TIMESERIES_QUERY_ROW_LIMIT:
            raise ValueError("time-series query row limit is invalid")
        local_timestamp = func.timezone(timezone, Transaction.occurred_at)
        bucket_local_date = cast(
            func.date_trunc(grain.value, local_timestamp),
            Date,
        ).label("bucket_local_date")
        income = Transaction.type == TransactionType.INCOME.value
        expense = Transaction.type == TransactionType.EXPENSE.value
        rows = await self._session.execute(
            select(
                bucket_local_date,
                Transaction.currency,
                func.coalesce(
                    func.sum(case((income, Transaction.amount_minor), else_=0)),
                    0,
                ).label("income_minor"),
                func.coalesce(
                    func.sum(case((expense, Transaction.amount_minor), else_=0)),
                    0,
                ).label("expense_minor"),
                func.count().filter(income).label("income_count"),
                func.count().filter(expense).label("expense_count"),
            )
            .where(
                Transaction.user_id == owner_id,
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Transaction.deleted_at.is_(None),
            )
            .group_by(bucket_local_date, Transaction.currency)
            .order_by(bucket_local_date, Transaction.currency)
            .limit(row_limit)
        )
        return tuple(
            TimeSeriesAggregateRow(
                bucket_local_date=local_date,
                totals=TimeSeriesCurrencyTotals(
                    currency=currency,
                    income_minor=int(income_minor),
                    expense_minor=int(expense_minor),
                    income_count=int(income_count),
                    expense_count=int(expense_count),
                ),
            )
            for (
                local_date,
                currency,
                income_minor,
                expense_minor,
                income_count,
                expense_count,
            ) in rows
        )

    async def category_totals(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[CategoryTotalSnapshot, ...]:
        aggregated = (
            select(
                Category.id.label("category_id"),
                Category.name.label("name"),
                Category.emoji.label("emoji"),
                Transaction.currency.label("currency"),
                func.sum(Transaction.amount_minor).label("amount_minor"),
            )
            .join(Transaction, Transaction.category_id == Category.id)
            .where(
                Transaction.user_id == owner_id,
                Transaction.type == TransactionType.EXPENSE.value,
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Transaction.deleted_at.is_(None),
            )
            .group_by(Category.id, Category.name, Category.emoji, Transaction.currency)
            .subquery()
        )
        ranked = select(
            aggregated,
            func.row_number()
            .over(
                partition_by=aggregated.c.currency,
                order_by=(
                    aggregated.c.amount_minor.desc(),
                    aggregated.c.name,
                    aggregated.c.category_id,
                ),
            )
            .label("currency_rank"),
        ).subquery()
        rows = await self._session.execute(
            select(
                ranked.c.category_id,
                ranked.c.name,
                ranked.c.emoji,
                ranked.c.currency,
                ranked.c.amount_minor,
            )
            .where(ranked.c.currency_rank <= limit)
            .order_by(ranked.c.currency, ranked.c.currency_rank)
            .limit(3201)
        )
        return tuple(
            CategoryTotalSnapshot(
                category_id=category_id,
                name=name,
                emoji=emoji,
                currency=currency,
                amount_minor=int(amount_minor or 0),
            )
            for category_id, name, emoji, currency, amount_minor in rows
        )

    async def period_transactions(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]:
        rows = await self._session.execute(
            select(Transaction, Category.name, Category.emoji, Account.name)
            .join(Category, Transaction.category_id == Category.id)
            .join(Account, Transaction.account_id == Account.id)
            .where(
                Transaction.user_id == owner_id,
                Transaction.occurred_at >= start,
                Transaction.occurred_at < end,
                Transaction.deleted_at.is_(None),
            )
            .order_by(
                Transaction.occurred_at.desc(),
                Transaction.id.desc(),
            )
            .limit(limit)
        )
        return tuple(_transaction_row_snapshot(row) for row in rows.all())
