from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Transaction, User
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.application.dto import PreparedTransactionDraft, PrepareRepeatDraftCommand
from finbot.application.errors import (
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)


class SqlAlchemyDraftConflictReplacementTargets:
    """Lock and validate transaction targets for a conflict replacement."""

    __slots__ = ("_session", "_transactions")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._transactions = SqlAlchemyTransactionCommandRepository(session)

    async def _lock_owner(self, owner_id: UUID) -> None:
        owner_exists = await self._session.scalar(
            select(User.id)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner_exists is None:
            raise EntityNotFoundError("Владелец не найден")

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        return await self._transactions.prepare_repeat(command)

    async def validate_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> None:
        await self._lock_owner(owner_id)
        transaction = cast(
            Transaction | None,
            await self._session.scalar(
                select(Transaction)
                .where(
                    Transaction.id == transaction_id,
                    Transaction.user_id == owner_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if transaction is None:
            raise EntityNotFoundError("Операция не найдена")
        if transaction.version != expected_version:
            raise ObjectVersionConflictError(current_version=transaction.version)
        if transaction.deleted_at is not None:
            raise InvalidStateError("Удалённую операцию сначала нужно восстановить")
