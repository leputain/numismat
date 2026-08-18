from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import User
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.mcp.ports import McpQueryUnitOfWork
from finbot.application.catalogs import BoundedCatalogReader
from finbot.application.errors import EntityNotFoundError
from finbot.application.ports import FinanceReader, OwnerReader


class SqlAlchemyMcpQueryUnitOfWork(McpQueryUnitOfWork):
    """Resolve one configured owner inside a read-only repeatable snapshot."""

    __slots__ = (
        "_context",
        "_owner_telegram_user_id",
        "catalogs",
        "finance",
        "owner_id",
        "owners",
        "session",
    )

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        owner_telegram_user_id: int,
    ) -> None:
        if (
            isinstance(owner_telegram_user_id, bool)
            or not isinstance(owner_telegram_user_id, int)
            or not 1 <= owner_telegram_user_id <= 2**52 - 1
        ):
            raise ValueError("MCP owner identity is invalid")
        self._context = sessions.begin()
        self._owner_telegram_user_id = owner_telegram_user_id
        self.session: AsyncSession

    async def __aenter__(self) -> SqlAlchemyMcpQueryUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            await self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self.session.execute(text("SET TRANSACTION READ ONLY"))
            owner_id = await self.session.scalar(
                select(User.id).where(User.telegram_user_id == self._owner_telegram_user_id)
            )
            if owner_id is None:
                raise EntityNotFoundError("Настроенный владелец не найден")
            reader = SqlAlchemyQueryRepository(self.session)
            self.owner_id = owner_id
            self.finance: FinanceReader = reader
            self.catalogs: BoundedCatalogReader = reader
            self.owners: OwnerReader = reader
        except BaseException as exc:
            await self._context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None:
        await self._context.__aexit__(exc_type, exc, traceback)
        return None


class SqlAlchemyMcpQueryUnitOfWorkFactory:
    __slots__ = ("_owner_telegram_user_id", "_sessions")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        owner_telegram_user_id: int,
    ) -> None:
        self._sessions = sessions
        self._owner_telegram_user_id = owner_telegram_user_id

    def __call__(self) -> SqlAlchemyMcpQueryUnitOfWork:
        return SqlAlchemyMcpQueryUnitOfWork(
            self._sessions,
            self._owner_telegram_user_id,
        )


__all__ = [
    "SqlAlchemyMcpQueryUnitOfWork",
    "SqlAlchemyMcpQueryUnitOfWorkFactory",
]
