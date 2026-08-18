from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.bank_imports import BankImportReader


class BankImportQueryUnitOfWork(Protocol):
    auth: AuthPersistence
    bank_imports: BankImportReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None: ...


class BankImportQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[BankImportQueryUnitOfWork]: ...


__all__ = ["BankImportQueryUnitOfWork", "BankImportQueryUnitOfWorkFactory"]
