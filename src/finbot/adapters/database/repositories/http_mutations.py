from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import User
from finbot.adapters.database.repositories.bank_imports import SqlAlchemyBankImportRepository
from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.exchange_rates import (
    SqlAlchemyExchangeRateRepository,
)
from finbot.adapters.database.repositories.http_auth import SqlAlchemyAuthPersistence
from finbot.adapters.database.repositories.http_idempotency import (
    SqlAlchemyHttpIdempotencyRepository,
)
from finbot.adapters.database.repositories.http_mutation_commands import (
    SqlAlchemyRevisionMutationCommands,
)
from finbot.adapters.database.repositories.recurring import SqlAlchemyRecurringRepository
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.adapters.http.mutations.ports import (
    CatalogMutationCommands,
    MutationIdempotency,
    RevisionMutationCommands,
)
from finbot.application.errors import EntityNotFoundError
from finbot.application.ports import DraftRepository
from finbot.application.use_cases.bank_imports import BankImportUseCases
from finbot.application.use_cases.budgets import BudgetUseCases
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.exchange_rates import ExchangeRateUseCases
from finbot.application.use_cases.recurring import RecurringUseCases


class SqlAlchemyHttpMutationUnitOfWork:
    """Session, owner, idempotency and domain locks in one external transaction."""

    __slots__ = (
        "_context",
        "_sessions",
        "auth",
        "bank_imports",
        "budgets",
        "catalogs",
        "commands",
        "drafts",
        "exchange_rates",
        "idempotency",
        "owner_timezone",
        "recurring",
        "session",
    )

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.auth: AuthPersistence
        self.bank_imports: BankImportUseCases
        self.budgets: BudgetUseCases
        self.catalogs: CatalogMutationCommands
        self.commands: RevisionMutationCommands
        self.drafts: DraftRepository
        self.exchange_rates: ExchangeRateUseCases
        self.idempotency: MutationIdempotency
        self.owner_timezone: str
        self.recurring: RecurringUseCases

    async def __aenter__(self) -> SqlAlchemyHttpMutationUnitOfWork:
        self.session = await self._context.__aenter__()
        try:
            self.auth = SqlAlchemyAuthPersistence(self.session)
            self.bank_imports = BankImportUseCases(SqlAlchemyBankImportRepository(self.session))
            self.budgets = BudgetUseCases(SqlAlchemyBudgetRepository(self.session))
            self.catalogs = CatalogUseCases(SqlAlchemyCatalogRepository(self.session))
            self.commands = SqlAlchemyRevisionMutationCommands(self.session)
            self.drafts = SqlAlchemyDraftRepository(self.session)
            self.exchange_rates = ExchangeRateUseCases(
                SqlAlchemyExchangeRateRepository(self.session)
            )
            self.idempotency = SqlAlchemyHttpIdempotencyRepository(self.session)
            self.recurring = RecurringUseCases(SqlAlchemyRecurringRepository(self.session))
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

    async def lock_owner(self, owner_id: UUID) -> None:
        owner = await self.session.scalar(select(User).where(User.id == owner_id).with_for_update())
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        self.owner_timezone = owner.timezone


class SqlAlchemyHttpMutationUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyHttpMutationUnitOfWork:
        return SqlAlchemyHttpMutationUnitOfWork(self._sessions)
