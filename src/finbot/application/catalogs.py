from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.domain.transactions import TransactionType

MAX_BOUNDED_CATALOG_ITEMS = 200
BOUNDED_CATALOG_FETCH_LIMIT = MAX_BOUNDED_CATALOG_ITEMS + 1


@dataclass(frozen=True, slots=True)
class CreateAccountCommand:
    owner_id: UUID = field(repr=False)
    name: str = field(repr=False)
    currency: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UpdateAccountCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    name: str = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArchiveAccountCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class RestoreAccountCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class SetDefaultAccountCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class CreateCategoryCommand:
    owner_id: UUID = field(repr=False)
    name: str = field(repr=False)
    kind: TransactionType = field(repr=False)


@dataclass(frozen=True, slots=True)
class UpdateCategoryCommand:
    owner_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    name: str = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArchiveCategoryCommand:
    owner_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class RestoreCategoryCommand:
    owner_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


class CatalogCommandRepository(Protocol):
    """Owner-scoped catalog mutations implemented by persistence adapters.

    The repository returns immutable application snapshots and never owns the
    transaction boundary.  A caller may therefore include catalog changes in
    the same unit of work as update claims, audit events, and response outbox
    writes.
    """

    async def create_account(self, command: CreateAccountCommand) -> AccountSnapshot: ...

    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot: ...

    async def archive_account(self, command: ArchiveAccountCommand) -> AccountSnapshot: ...

    async def restore_account(self, command: RestoreAccountCommand) -> AccountSnapshot: ...

    async def set_default_account(self, command: SetDefaultAccountCommand) -> AccountSnapshot: ...

    async def create_category(self, command: CreateCategoryCommand) -> CategorySnapshot: ...

    async def update_category(self, command: UpdateCategoryCommand) -> CategorySnapshot: ...

    async def archive_category(self, command: ArchiveCategoryCommand) -> CategorySnapshot: ...

    async def restore_category(self, command: RestoreCategoryCommand) -> CategorySnapshot: ...


class BoundedCatalogReader(Protocol):
    """Owner-scoped catalog reads that must enforce the supplied SQL limit."""

    async def list_accounts_bounded(
        self,
        owner_id: UUID,
        *,
        archived: bool,
        limit: int,
    ) -> tuple[AccountSnapshot, ...]: ...

    async def list_categories_bounded(
        self,
        owner_id: UUID,
        *,
        kind: str | None,
        archived: bool,
        limit: int,
    ) -> tuple[CategorySnapshot, ...]: ...
