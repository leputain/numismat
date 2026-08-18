from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.recurring import RecurringReader


class RecurringQueryUnitOfWork(Protocol):
    auth: AuthPersistence
    recurring: RecurringReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None: ...


class RecurringQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[RecurringQueryUnitOfWork]: ...
