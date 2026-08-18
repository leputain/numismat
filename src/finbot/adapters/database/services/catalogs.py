from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, User
from finbot.application.catalogs import MAX_BOUNDED_CATALOG_ITEMS
from finbot.application.errors import CatalogUnavailableError
from finbot.application.services.catalogs import (
    catalog_slug,
    ensure_expected_version,
    normalize_catalog_name,
)
from finbot.domain.errors import ObjectNotFoundError


async def _lock_owner(session: AsyncSession, user_id: UUID) -> User:
    """Serialize mutations within one owner's catalog namespace."""
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ObjectNotFoundError("Пользователь не найден")
    return user


async def ensure_account_destination_capacity(
    session: AsyncSession,
    user_id: UUID,
    *,
    archived: bool,
) -> None:
    """Fail closed at the catalog cap; caller must already hold the owner row lock."""

    archive_filter = Account.archived_at.is_not(None) if archived else Account.archived_at.is_(None)
    at_capacity = await session.scalar(
        select(Account.id)
        .where(Account.user_id == user_id, archive_filter)
        .offset(MAX_BOUNDED_CATALOG_ITEMS - 1)
        .limit(1)
    )
    if at_capacity is not None:
        raise CatalogUnavailableError("Справочник счетов достиг допустимого размера")


async def ensure_category_destination_capacity(
    session: AsyncSession,
    user_id: UUID,
    *,
    archived: bool,
) -> None:
    """Fail closed at the catalog cap; caller must already hold the owner row lock."""

    archive_filter = (
        Category.archived_at.is_not(None) if archived else Category.archived_at.is_(None)
    )
    at_capacity = await session.scalar(
        select(Category.id)
        .where(Category.user_id == user_id, archive_filter)
        .offset(MAX_BOUNDED_CATALOG_ITEMS - 1)
        .limit(1)
    )
    if at_capacity is not None:
        raise CatalogUnavailableError("Справочник категорий достиг допустимого размера")


async def create_account(session: AsyncSession, user_id: UUID, name: str, currency: str) -> Account:
    clean = normalize_catalog_name(name)
    slug = catalog_slug(clean)
    await _lock_owner(session, user_id)
    existing = await session.scalar(
        select(Account).where(Account.user_id == user_id, Account.slug == slug)
    )
    if existing is not None:
        location = "в архиве" if existing.archived_at is not None else "в списке счетов"
        raise ValueError(f"Счёт с таким названием уже есть {location}")
    await ensure_account_destination_capacity(session, user_id, archived=False)
    account = Account(
        user_id=user_id,
        name=clean,
        slug=slug,
        type="other",
        currency=currency,
    )
    session.add(account)
    await session.flush()
    return account


async def rename_account(
    session: AsyncSession,
    user_id: UUID,
    account_id: UUID,
    name: str,
    expected_version: int | None = None,
) -> Account:
    await _lock_owner(session, user_id)
    account = await session.scalar(
        select(Account)
        .where(
            Account.id == account_id,
            Account.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ObjectNotFoundError("Счёт не найден")
    ensure_expected_version("Счёт", account.version, expected_version)
    if account.archived_at is not None:
        raise ObjectNotFoundError("Счёт не найден")
    clean = normalize_catalog_name(name)
    slug = catalog_slug(clean)
    duplicate = await session.scalar(
        select(Account.id).where(
            Account.user_id == user_id,
            Account.slug == slug,
            Account.id != account.id,
        )
    )
    if duplicate is not None:
        raise ValueError("Счёт с таким названием уже существует")
    account.name = clean
    account.slug = slug
    account.version += 1
    await session.flush()
    return account


async def archive_account(
    session: AsyncSession,
    user_id: UUID,
    account_id: UUID,
    default_account_id: UUID | None,
    expected_version: int | None = None,
) -> Account:
    user = await _lock_owner(session, user_id)
    account = await session.scalar(
        select(Account)
        .where(
            Account.id == account_id,
            Account.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ObjectNotFoundError("Счёт не найден")
    ensure_expected_version("Счёт", account.version, expected_version)
    if account.archived_at is not None:
        raise ObjectNotFoundError("Счёт не найден")
    if account.id == user.default_account_id:
        raise ValueError("Сначала выберите другой основной счёт")
    another_active = await session.scalar(
        select(Account.id)
        .where(
            Account.user_id == user_id,
            Account.archived_at.is_(None),
            Account.id != account.id,
        )
        .limit(1)
    )
    if another_active is None:
        raise ValueError("Нельзя архивировать последний активный счёт")
    await ensure_account_destination_capacity(session, user_id, archived=True)
    account.archived_at = datetime.now(UTC)
    account.version += 1
    await session.flush()
    return account


async def restore_account(
    session: AsyncSession,
    user_id: UUID,
    account_id: UUID,
    expected_version: int | None = None,
) -> Account:
    await _lock_owner(session, user_id)
    account = await session.scalar(
        select(Account)
        .where(
            Account.id == account_id,
            Account.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ObjectNotFoundError("Счёт не найден")
    ensure_expected_version("Счёт", account.version, expected_version)
    if account.archived_at is None:
        raise ObjectNotFoundError("Архивный счёт не найден")
    await ensure_account_destination_capacity(session, user_id, archived=False)
    account.archived_at = None
    account.version += 1
    await session.flush()
    return account


async def set_default_account(
    session: AsyncSession,
    user_id: UUID,
    account_id: UUID,
    expected_version: int | None = None,
) -> Account:
    """Select an active default account with optimistic concurrency control."""

    user = await _lock_owner(session, user_id)
    account = await session.scalar(
        select(Account)
        .where(
            Account.id == account_id,
            Account.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise ObjectNotFoundError("Счёт не найден")
    ensure_expected_version("Счёт", account.version, expected_version)
    if account.archived_at is not None:
        raise ObjectNotFoundError("Счёт не найден")
    if user.default_account_id != account.id:
        user.default_account_id = account.id
        account.version += 1
        await session.flush()
    return account


async def create_category(session: AsyncSession, user_id: UUID, name: str, kind: str) -> Category:
    if kind not in {"expense", "income"}:
        raise ValueError("Неизвестный тип категории")
    clean = normalize_catalog_name(name)
    slug = catalog_slug(clean)
    await _lock_owner(session, user_id)
    existing = await session.scalar(
        select(Category).where(
            Category.user_id == user_id,
            Category.kind == kind,
            Category.slug == slug,
        )
    )
    if existing is not None:
        location = "в архиве" if existing.archived_at is not None else "в списке категорий"
        raise ValueError(f"Категория с таким названием уже есть {location}")
    await ensure_category_destination_capacity(session, user_id, archived=False)
    category = Category(
        user_id=user_id,
        kind=kind,
        name=clean,
        slug=slug,
        emoji="▫️",
    )
    session.add(category)
    await session.flush()
    return category


async def rename_category(
    session: AsyncSession,
    user_id: UUID,
    category_id: UUID,
    name: str,
    expected_version: int | None = None,
) -> Category:
    await _lock_owner(session, user_id)
    category = await session.scalar(
        select(Category)
        .where(
            Category.id == category_id,
            Category.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if category is None:
        raise ObjectNotFoundError("Категория не найдена")
    ensure_expected_version("Категория", category.version, expected_version)
    if category.archived_at is not None:
        raise ObjectNotFoundError("Категория не найдена")
    clean = normalize_catalog_name(name)
    slug = catalog_slug(clean)
    duplicate = await session.scalar(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.kind == category.kind,
            Category.slug == slug,
            Category.id != category.id,
        )
    )
    if duplicate is not None:
        raise ValueError("Категория с таким названием уже существует")
    category.name = clean
    category.slug = slug
    category.version += 1
    await session.flush()
    return category


async def archive_category(
    session: AsyncSession,
    user_id: UUID,
    category_id: UUID,
    expected_version: int | None = None,
) -> Category:
    # Serialize catalog invariant checks per owner. Locking only the selected
    # category lets two concurrent requests archive the last two rows.
    await _lock_owner(session, user_id)
    category = await session.scalar(
        select(Category)
        .where(
            Category.id == category_id,
            Category.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if category is None:
        raise ObjectNotFoundError("Категория не найдена")
    ensure_expected_version("Категория", category.version, expected_version)
    if category.archived_at is not None:
        raise ObjectNotFoundError("Категория не найдена")
    another_active = await session.scalar(
        select(Category.id)
        .where(
            Category.user_id == user_id,
            Category.kind == category.kind,
            Category.archived_at.is_(None),
            Category.id != category.id,
        )
        .limit(1)
    )
    if another_active is None:
        raise ValueError("Нельзя архивировать последнюю категорию этого типа")
    await ensure_category_destination_capacity(session, user_id, archived=True)
    category.archived_at = datetime.now(UTC)
    category.version += 1
    await session.flush()
    return category


async def restore_category(
    session: AsyncSession,
    user_id: UUID,
    category_id: UUID,
    expected_version: int | None = None,
) -> Category:
    await _lock_owner(session, user_id)
    category = await session.scalar(
        select(Category)
        .where(
            Category.id == category_id,
            Category.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if category is None:
        raise ObjectNotFoundError("Категория не найдена")
    ensure_expected_version("Категория", category.version, expected_version)
    if category.archived_at is None:
        raise ObjectNotFoundError("Архивная категория не найдена")
    await ensure_category_destination_capacity(session, user_id, archived=False)
    category.archived_at = None
    category.version += 1
    await session.flush()
    return category
