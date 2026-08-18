from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.budgets import BudgetReader


class BudgetQueryUnitOfWork(Protocol):
    auth: AuthPersistence
    budgets: BudgetReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None: ...


class BudgetQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[BudgetQueryUnitOfWork]: ...
