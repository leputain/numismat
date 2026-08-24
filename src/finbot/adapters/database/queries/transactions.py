from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from finbot.adapters.database.models import Account, Category, Transaction
from finbot.application.dto import (
    EMPTY_TRANSACTION_LIST_FILTERS,
    DeletedTransactionCursor,
    TransactionCursor,
    TransactionListFilters,
)
from finbot.application.queries.transactions import TransactionDetails

DetailRow = Row[tuple[Transaction, str, str, str]]


def _details(row: DetailRow) -> TransactionDetails:
    transaction, category_name, category_emoji, account_name = row._t
    return TransactionDetails(
        id=transaction.id,
        type=transaction.type,
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


def _base_query(user_id: UUID) -> Select[tuple[Transaction, str, str, str]]:
    return (
        select(Transaction, Category.name, Category.emoji, Account.name)
        .join(Category, Transaction.category_id == Category.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(Transaction.user_id == user_id)
    )


async def get_transaction_details(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
) -> TransactionDetails | None:
    query = _base_query(user_id)
    row = (await session.execute(query.where(Transaction.id == transaction_id))).first()
    return _details(row) if row is not None else None


async def list_transaction_details(
    session: AsyncSession,
    user_id: UUID,
    *,
    page: int = 0,
    page_size: int = 5,
) -> tuple[list[TransactionDetails], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))
        )
        or 0
    )
    query = _base_query(user_id)
    rows = await session.execute(
        query.where(Transaction.deleted_at.is_(None))
        .order_by(
            Transaction.occurred_at.desc(),
            Transaction.created_at.desc(),
            Transaction.id.desc(),
        )
        .offset(max(page, 0) * page_size)
        .limit(page_size)
    )
    return [_details(row) for row in rows.all()], total


async def list_transaction_details_after(
    session: AsyncSession,
    user_id: UUID,
    *,
    cursor: TransactionCursor | None,
    limit: int,
    filters: TransactionListFilters = EMPTY_TRANSACTION_LIST_FILTERS,
) -> list[TransactionDetails]:
    if type(limit) is not int or not 1 <= limit <= 101:
        raise ValueError("Cursor query limit must be between 1 and 101")
    query = _base_query(user_id).where(Transaction.deleted_at.is_(None))
    if filters.start is not None and filters.end is not None:
        query = query.where(
            Transaction.occurred_at >= filters.start,
            Transaction.occurred_at < filters.end,
        )
    if filters.kind is not None:
        query = query.where(Transaction.type == filters.kind.value)
    if filters.account_id is not None:
        query = query.where(Transaction.account_id == filters.account_id)
    if filters.category_id is not None:
        query = query.where(Transaction.category_id == filters.category_id)
    if filters.currency is not None:
        query = query.where(Transaction.currency == filters.currency)
    if cursor is not None:
        query = query.where(
            or_(
                Transaction.occurred_at < cursor.occurred_at,
                and_(
                    Transaction.occurred_at == cursor.occurred_at,
                    Transaction.id < cursor.transaction_id,
                ),
            )
        )
    rows = await session.execute(
        query.order_by(
            Transaction.occurred_at.desc(),
            Transaction.id.desc(),
        ).limit(limit)
    )
    return [_details(row) for row in rows.all()]


async def list_deleted_transaction_details_after(
    session: AsyncSession,
    user_id: UUID,
    *,
    cursor: DeletedTransactionCursor | None,
    limit: int,
) -> list[TransactionDetails]:
    if type(limit) is not int or not 1 <= limit <= 101:
        raise ValueError("Deleted cursor query limit must be between 1 and 101")
    query = _base_query(user_id).where(Transaction.deleted_at.is_not(None))
    if cursor is not None:
        query = query.where(
            or_(
                Transaction.deleted_at < cursor.deleted_at,
                and_(
                    Transaction.deleted_at == cursor.deleted_at,
                    Transaction.id < cursor.transaction_id,
                ),
            )
        )
    rows = await session.execute(
        query.order_by(
            Transaction.deleted_at.desc(),
            Transaction.id.desc(),
        ).limit(limit)
    )
    return [_details(row) for row in rows.all()]


async def list_deleted_transaction_details(
    session: AsyncSession,
    user_id: UUID,
    *,
    page: int = 0,
    page_size: int = 5,
) -> tuple[list[TransactionDetails], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.user_id == user_id, Transaction.deleted_at.is_not(None))
        )
        or 0
    )
    query = _base_query(user_id)
    rows = await session.execute(
        query.where(Transaction.deleted_at.is_not(None))
        .order_by(
            Transaction.deleted_at.desc(),
            Transaction.updated_at.desc(),
            Transaction.id.desc(),
        )
        .offset(max(page, 0) * page_size)
        .limit(page_size)
    )
    return [_details(row) for row in rows.all()], total


async def export_transaction_details(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int | None = None,
) -> list[TransactionDetails]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("Export limit must be positive")
    query = _base_query(user_id)
    query = query.where(Transaction.deleted_at.is_(None)).order_by(
        Transaction.occurred_at, Transaction.created_at, Transaction.id
    )
    if limit is not None:
        query = query.limit(limit)
    rows = await session.execute(query)
    return [_details(row) for row in rows.all()]
