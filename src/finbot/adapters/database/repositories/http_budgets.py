from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthPersistence
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.budgets import BudgetReader


class SqlAlchemyBudgetQueryUnitOfWork:
    """One authenticated, read-only repeatable snapshot for budget progress."""

    __slots__ = ("_context", "_sessions", "auth", "budgets", "session")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.auth: AuthPersistence
        self.budgets: BudgetReader

    async def __aenter__(self) -> SqlAlchemyBudgetQueryUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            await self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self.session.execute(text("SET TRANSACTION READ ONLY"))
            self.auth = SqlAlchemyAuthPersistence(self.session)
            self.budgets = SqlAlchemyBudgetRepository(self.session)
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


class SqlAlchemyBudgetQueryUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyBudgetQueryUnitOfWork:
        return SqlAlchemyBudgetQueryUnitOfWork(self._sessions)


__all__ = [
    "SqlAlchemyBudgetQueryUnitOfWork",
    "SqlAlchemyBudgetQueryUnitOfWorkFactory",
]
