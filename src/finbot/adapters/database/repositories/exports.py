from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.queries.transactions import export_transaction_details
from finbot.application.queries.transactions import TransactionDetails


class SqlAlchemyCsvExportRepository:
    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def export(
        self,
        owner_id: UUID,
        *,
        limit: int,
    ) -> list[TransactionDetails]:
        return await export_transaction_details(self._session, owner_id, limit=limit)
