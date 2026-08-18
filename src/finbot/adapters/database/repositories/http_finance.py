from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthPersistence
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.ports import FinanceReader


class SqlAlchemyFinanceQueryUnitOfWork:
    """One read-only snapshot for session authentication and finance queries."""

    __slots__ = ("_context", "_sessions", "auth", "finance", "session")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.auth: AuthPersistence
        self.finance: FinanceReader

    async def __aenter__(self) -> SqlAlchemyFinanceQueryUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            await self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self.session.execute(text("SET TRANSACTION READ ONLY"))
        except BaseException as exc:
            await self._context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        self.auth = SqlAlchemyAuthPersistence(self.session)
        self.finance = SqlAlchemyQueryRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None:
        await self._context.__aexit__(exc_type, exc, traceback)
        return None


class SqlAlchemyFinanceQueryUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyFinanceQueryUnitOfWork:
        return SqlAlchemyFinanceQueryUnitOfWork(self._sessions)
