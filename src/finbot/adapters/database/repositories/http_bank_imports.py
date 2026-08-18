from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.bank_imports import (
    SqlAlchemyBankImportRepository,
)
from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthPersistence
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.bank_imports import BankImportReader


class SqlAlchemyBankImportQueryUnitOfWork:
    """Authenticated read-only repeatable snapshot for bounded import reads."""

    __slots__ = ("_context", "_sessions", "auth", "bank_imports", "session")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.auth: AuthPersistence
        self.bank_imports: BankImportReader

    async def __aenter__(self) -> SqlAlchemyBankImportQueryUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            await self.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self.session.execute(text("SET TRANSACTION READ ONLY"))
            self.auth = SqlAlchemyAuthPersistence(self.session)
            self.bank_imports = SqlAlchemyBankImportRepository(self.session)
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


class SqlAlchemyBankImportQueryUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyBankImportQueryUnitOfWork:
        return SqlAlchemyBankImportQueryUnitOfWork(self._sessions)


__all__ = [
    "SqlAlchemyBankImportQueryUnitOfWork",
    "SqlAlchemyBankImportQueryUnitOfWorkFactory",
]
