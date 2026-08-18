from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.ports import FinanceReader


class FinanceQueryUnitOfWork(Protocol):
    auth: AuthPersistence
    finance: FinanceReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None: ...


class FinanceQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[FinanceQueryUnitOfWork]: ...
