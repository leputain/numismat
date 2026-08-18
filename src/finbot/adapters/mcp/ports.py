from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from finbot.application.catalogs import BoundedCatalogReader
from finbot.application.ports import FinanceReader, OwnerReader


class McpQueryUnitOfWork(Protocol):
    """One fixed-owner, read-only database snapshot."""

    owner_id: UUID
    finance: FinanceReader
    catalogs: BoundedCatalogReader
    owners: OwnerReader


class McpQueryUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[McpQueryUnitOfWork]: ...


__all__ = ["McpQueryUnitOfWork", "McpQueryUnitOfWorkFactory"]
