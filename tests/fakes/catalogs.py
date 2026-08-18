from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from finbot.application.catalogs import (
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.application.services.catalogs import (
    catalog_slug,
    ensure_expected_version,
    normalize_catalog_name,
)
from finbot.domain.errors import ObjectNotFoundError

_ARCHIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class InMemoryCatalogRepository:
    """Deterministic fake mirroring catalog owner/version/archive invariants."""

    def __init__(
        self,
        *,
        owners: Iterable[UUID] = (),
        accounts: Mapping[UUID, tuple[AccountSnapshot, ...]] | None = None,
        categories: Mapping[UUID, tuple[CategorySnapshot, ...]] | None = None,
        default_account_ids: Mapping[UUID, UUID | None] | None = None,
    ) -> None:
        account_items = accounts or {}
        category_items = categories or {}
        self._owners = set(owners) | set(account_items) | set(category_items)
        self._accounts = {
            owner_id: {account.account_id: account for account in items}
            for owner_id, items in account_items.items()
        }
        self._categories = {
            owner_id: {category.category_id: category for category in items}
            for owner_id, items in category_items.items()
        }
        self._default_account_ids = dict(default_account_ids or {})
        existing_ids = [
            item_id.int
            for catalogs in (self._accounts, self._categories)
            for owner_catalog in catalogs.values()
            for item_id in owner_catalog
        ]
        self._next_id = max(existing_ids, default=0) + 1

    def _new_id(self) -> UUID:
        entity_id = UUID(int=self._next_id)
        self._next_id += 1
        return entity_id

    def _require_owner(self, owner_id: UUID) -> None:
        if owner_id not in self._owners:
            raise ObjectNotFoundError("Пользователь не найден")

    def _account(self, owner_id: UUID, account_id: UUID) -> AccountSnapshot:
        self._require_owner(owner_id)
        account = self._accounts.get(owner_id, {}).get(account_id)
        if account is None:
            raise ObjectNotFoundError("Счёт не найден")
        return account

    def _category(self, owner_id: UUID, category_id: UUID) -> CategorySnapshot:
        self._require_owner(owner_id)
        category = self._categories.get(owner_id, {}).get(category_id)
        if category is None:
            raise ObjectNotFoundError("Категория не найдена")
        return category

    def accounts_for(self, owner_id: UUID) -> tuple[AccountSnapshot, ...]:
        return tuple(self._accounts.get(owner_id, {}).values())

    def categories_for(self, owner_id: UUID) -> tuple[CategorySnapshot, ...]:
        return tuple(self._categories.get(owner_id, {}).values())

    def default_account_id_for(self, owner_id: UUID) -> UUID | None:
        return self._default_account_ids.get(owner_id)

    async def create_account(self, command: CreateAccountCommand) -> AccountSnapshot:
        self._require_owner(command.owner_id)
        clean = normalize_catalog_name(command.name)
        slug = catalog_slug(clean)
        if any(
            catalog_slug(account.name) == slug
            for account in self._accounts.get(command.owner_id, {}).values()
        ):
            raise ValueError("Счёт с таким названием уже существует")
        account = AccountSnapshot(
            account_id=self._new_id(),
            name=clean,
            account_type="other",
            currency=command.currency,
            archived_at=None,
            version=1,
        )
        self._accounts.setdefault(command.owner_id, {})[account.account_id] = account
        return account

    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot:
        account = self._account(command.owner_id, command.account_id)
        ensure_expected_version("Счёт", account.version, command.expected_version)
        if account.archived_at is not None:
            raise ObjectNotFoundError("Счёт не найден")
        clean = normalize_catalog_name(command.name)
        slug = catalog_slug(clean)
        if any(
            other.account_id != account.account_id and catalog_slug(other.name) == slug
            for other in self._accounts[command.owner_id].values()
        ):
            raise ValueError("Счёт с таким названием уже существует")
        updated = replace(account, name=clean, version=account.version + 1)
        self._accounts[command.owner_id][account.account_id] = updated
        return updated

    async def archive_account(self, command: ArchiveAccountCommand) -> AccountSnapshot:
        account = self._account(command.owner_id, command.account_id)
        ensure_expected_version("Счёт", account.version, command.expected_version)
        if account.archived_at is not None:
            raise ObjectNotFoundError("Счёт не найден")
        if self._default_account_ids.get(command.owner_id) == account.account_id:
            raise ValueError("Сначала выберите другой основной счёт")
        active_count = sum(
            item.archived_at is None for item in self._accounts[command.owner_id].values()
        )
        if active_count <= 1:
            raise ValueError("Нельзя архивировать последний активный счёт")
        archived = replace(
            account,
            archived_at=_ARCHIVED_AT,
            version=account.version + 1,
        )
        self._accounts[command.owner_id][account.account_id] = archived
        return archived

    async def restore_account(self, command: RestoreAccountCommand) -> AccountSnapshot:
        account = self._account(command.owner_id, command.account_id)
        ensure_expected_version("Счёт", account.version, command.expected_version)
        if account.archived_at is None:
            raise ObjectNotFoundError("Архивный счёт не найден")
        restored = replace(account, archived_at=None, version=account.version + 1)
        self._accounts[command.owner_id][account.account_id] = restored
        return restored

    async def set_default_account(self, command: SetDefaultAccountCommand) -> AccountSnapshot:
        account = self._account(command.owner_id, command.account_id)
        ensure_expected_version("Счёт", account.version, command.expected_version)
        if account.archived_at is not None:
            raise ObjectNotFoundError("Счёт не найден")
        if self._default_account_ids.get(command.owner_id) == account.account_id:
            return account
        updated = replace(account, version=account.version + 1)
        self._accounts[command.owner_id][account.account_id] = updated
        self._default_account_ids[command.owner_id] = account.account_id
        return updated

    async def create_category(self, command: CreateCategoryCommand) -> CategorySnapshot:
        self._require_owner(command.owner_id)
        clean = normalize_catalog_name(command.name)
        slug = catalog_slug(clean)
        if any(
            category.kind is command.kind and catalog_slug(category.name) == slug
            for category in self._categories.get(command.owner_id, {}).values()
        ):
            raise ValueError("Категория с таким названием уже существует")
        category = CategorySnapshot(
            category_id=self._new_id(),
            kind=command.kind,
            name=clean,
            emoji="▫️",
            archived_at=None,
            version=1,
        )
        self._categories.setdefault(command.owner_id, {})[category.category_id] = category
        return category

    async def update_category(self, command: UpdateCategoryCommand) -> CategorySnapshot:
        category = self._category(command.owner_id, command.category_id)
        ensure_expected_version("Категория", category.version, command.expected_version)
        if category.archived_at is not None:
            raise ObjectNotFoundError("Категория не найдена")
        clean = normalize_catalog_name(command.name)
        slug = catalog_slug(clean)
        if any(
            other.category_id != category.category_id
            and other.kind is category.kind
            and catalog_slug(other.name) == slug
            for other in self._categories[command.owner_id].values()
        ):
            raise ValueError("Категория с таким названием уже существует")
        updated = replace(category, name=clean, version=category.version + 1)
        self._categories[command.owner_id][category.category_id] = updated
        return updated

    async def archive_category(self, command: ArchiveCategoryCommand) -> CategorySnapshot:
        category = self._category(command.owner_id, command.category_id)
        ensure_expected_version("Категория", category.version, command.expected_version)
        if category.archived_at is not None:
            raise ObjectNotFoundError("Категория не найдена")
        active_count = sum(
            item.kind is category.kind and item.archived_at is None
            for item in self._categories[command.owner_id].values()
        )
        if active_count <= 1:
            raise ValueError("Нельзя архивировать последнюю категорию этого типа")
        archived = replace(
            category,
            archived_at=_ARCHIVED_AT,
            version=category.version + 1,
        )
        self._categories[command.owner_id][category.category_id] = archived
        return archived

    async def restore_category(self, command: RestoreCategoryCommand) -> CategorySnapshot:
        category = self._category(command.owner_id, command.category_id)
        ensure_expected_version("Категория", category.version, command.expected_version)
        if category.archived_at is None:
            raise ObjectNotFoundError("Архивная категория не найдена")
        restored = replace(category, archived_at=None, version=category.version + 1)
        self._categories[command.owner_id][category.category_id] = restored
        return restored
