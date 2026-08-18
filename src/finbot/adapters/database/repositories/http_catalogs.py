from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthPersistence
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.catalogs import BoundedCatalogReader
from finbot.application.ports import OwnerReader


class SqlAlchemyCatalogQueryUnitOfWork:
    """One read-only repeatable snapshot for authenticated catalog lists."""

    __slots__ = ("_context", "_sessions", "auth", "catalogs", "owners", "session")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.auth: AuthPersistence
        self.catalogs: BoundedCatalogReader
        self.owners: OwnerReader

    async def __aenter__(self) -> SqlAlchemyCatalogQueryUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            await self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self.session.execute(text("SET TRANSACTION READ ONLY"))
            reader = SqlAlchemyQueryRepository(self.session)
            self.auth = SqlAlchemyAuthPersistence(self.session)
            self.catalogs = reader
            self.owners = reader
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


class SqlAlchemyCatalogQueryUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyCatalogQueryUnitOfWork:
        return SqlAlchemyCatalogQueryUnitOfWork(self._sessions)
