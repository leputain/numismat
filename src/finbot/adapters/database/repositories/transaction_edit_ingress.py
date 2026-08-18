from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, Transaction, User
from finbot.application.dto import TransactionSnapshot
from finbot.application.errors import (
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.domain.transactions import TransactionType


class SqlAlchemyTransactionEditTargetReader:
    """Lock and return one authoritative active transaction for edit ingress."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __call__(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionSnapshot:
        owner_exists = await self._session.scalar(
            select(User.id)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner_exists is None:
            raise EntityNotFoundError("Владелец не найден")

        row = (
            await self._session.execute(
                select(Transaction, Category.name, Category.emoji, Account.name)
                .join(Category, Transaction.category_id == Category.id)
                .join(Account, Transaction.account_id == Account.id)
                .where(
                    Transaction.id == transaction_id,
                    Transaction.user_id == owner_id,
                )
                .with_for_update(of=Transaction)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise EntityNotFoundError("Операция не найдена")
        transaction, category_name, category_emoji, account_name = row._t
        if transaction.version != expected_version:
            raise ObjectVersionConflictError(current_version=transaction.version)
        if transaction.deleted_at is not None:
            raise InvalidStateError("Удалённую операцию сначала нужно восстановить")

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
