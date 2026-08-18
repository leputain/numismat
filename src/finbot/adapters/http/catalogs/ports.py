from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.catalogs import BoundedCatalogReader
from finbot.application.ports import OwnerReader


class CatalogQueryUnitOfWork(Protocol):
    """One read-only snapshot for authentication, owner and catalog reads."""

    auth: AuthPersistence
    catalogs: BoundedCatalogReader
    owners: OwnerReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None: ...


class CatalogQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[CatalogQueryUnitOfWork]: ...
