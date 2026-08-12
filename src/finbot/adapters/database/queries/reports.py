from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, Transaction
from finbot.application.queries.reports import CategoryTotal, ReportTransaction


async def totals(
    session: AsyncSession, user_id: object, start: datetime, end: datetime
) -> dict[str, int]:
    """Compatibility totals for the user's base-currency-only MVP."""
    rows = await session.execute(
        select(Transaction.type, func.sum(Transaction.amount_minor))
        .where(
            Transaction.user_id == user_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.deleted_at.is_(None),
        )
        .group_by(Transaction.type)
    )
    return {kind: int(total or 0) for kind, total in rows}


async def totals_by_currency(
    session: AsyncSession, user_id: UUID, start: datetime, end: datetime
) -> dict[str, dict[str, int]]:
    rows = await session.execute(
        select(Transaction.currency, Transaction.type, func.sum(Transaction.amount_minor))
        .where(
            Transaction.user_id == user_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.deleted_at.is_(None),
        )
        .group_by(Transaction.currency, Transaction.type)
    )
    result: dict[str, dict[str, int]] = {}
    for currency, kind, total in rows:
        result.setdefault(str(currency), {})[str(kind)] = int(total or 0)
    return result


async def by_category(
    session: AsyncSession, user_id: object, start: datetime, end: datetime
) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(Category.name, func.sum(Transaction.amount_minor))
        .join(Transaction, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.deleted_at.is_(None),
        )
        .group_by(Category.name)
        .order_by(func.sum(Transaction.amount_minor).desc())
    )
    return [(str(row[0]), int(row[1] or 0)) for row in rows.all()]


async def category_totals(
    session: AsyncSession, user_id: UUID, start: datetime, end: datetime
) -> list[CategoryTotal]:
    rows = await session.execute(
        select(
            Category.name,
            Category.emoji,
            Transaction.currency,
            func.sum(Transaction.amount_minor),
        )
        .join(Transaction, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.deleted_at.is_(None),
        )
        .group_by(Category.name, Category.emoji, Transaction.currency)
        .order_by(func.sum(Transaction.amount_minor).desc())
    )
    return [
        CategoryTotal(str(name), str(emoji), str(currency), int(amount or 0))
        for name, emoji, currency, amount in rows
    ]


ReportRow = Row[tuple[Transaction, str, str, str]]


def _report_transaction(row: ReportRow) -> ReportTransaction:
    transaction, category_name, category_emoji, account_name = row._t
    return ReportTransaction(
        id=transaction.id,
        type=transaction.type,
        amount_minor=transaction.amount_minor,
        currency=transaction.currency,
        occurred_at=transaction.occurred_at,
        description=transaction.description,
        version=transaction.version,
        category_name=str(category_name),
        category_emoji=str(category_emoji),
        account_name=str(account_name),
    )


async def period_transactions(
    session: AsyncSession,
    user_id: UUID,
    start: datetime,
    end: datetime,
    *,
    limit: int = 20,
) -> list[ReportTransaction]:
    rows = await session.execute(
        select(Transaction, Category.name, Category.emoji, Account.name)
        .join(Category, Transaction.category_id == Category.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.occurred_at.desc(), Transaction.created_at.desc())
        .limit(limit)
    )
    return [_report_transaction(row) for row in rows.all()]
