from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from finbot.adapters.database.models import Account, Category, Transaction
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
        .order_by(Transaction.occurred_at.desc(), Transaction.created_at.desc())
        .offset(max(page, 0) * page_size)
        .limit(page_size)
    )
    return [_details(row) for row in rows.all()], total


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
        .order_by(Transaction.deleted_at.desc(), Transaction.updated_at.desc())
        .offset(max(page, 0) * page_size)
        .limit(page_size)
    )
    return [_details(row) for row in rows.all()], total


async def export_transaction_details(
    session: AsyncSession,
    user_id: UUID,
) -> list[TransactionDetails]:
    query = _base_query(user_id)
    rows = await session.execute(
        query.where(Transaction.deleted_at.is_(None)).order_by(Transaction.occurred_at)
    )
    return [_details(row) for row in rows.all()]
