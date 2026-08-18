from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, User
from finbot.adapters.database.services.transactions import resolve_account, resolve_category
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.application.errors import (
    CatalogUnavailableError,
    ObjectVersionConflictError,
)
from finbot.domain.errors import UnknownAccountError, UnknownCategoryError
from finbot.domain.transactions import TransactionType


def _account_snapshot(account: Account) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=account.id,
        name=account.name,
        account_type=account.type,
        currency=account.currency,
        archived_at=account.archived_at,
        version=account.version,
    )


def _category_snapshot(category: Category) -> CategorySnapshot:
    return CategorySnapshot(
        category_id=category.id,
        kind=TransactionType(category.kind),
        name=category.name,
        emoji=category.emoji,
        archived_at=category.archived_at,
        version=category.version,
    )


class SqlAlchemyDraftNavigationCatalogRepository:
    """Lock-safe owner catalog boundary for a draft selection transaction.

    Catalog mutations serialize on the owner row before locking an entity.  A
    selection takes the same locks in the same order, so a concurrent archive
    either precedes validation and makes the version stale, or follows a fully
    committed draft transition.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise CatalogUnavailableError("Каталог владельца недоступен")
        return owner

    async def _lock_account(self, owner_id: UUID, account_id: UUID) -> Account | None:
        return cast(
            Account | None,
            await self._session.scalar(
                select(Account)
                .where(Account.id == account_id, Account.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def _lock_category(self, owner_id: UUID, category_id: UUID) -> Category | None:
        return cast(
            Category | None,
            await self._session.scalar(
                select(Category)
                .where(Category.id == category_id, Category.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def select_account(
        self,
        owner_id: UUID,
        reference: DraftCatalogRef,
    ) -> AccountSnapshot:
        await self._lock_owner(owner_id)
        account = await self._lock_account(owner_id, reference.entity_id)
        if account is None:
            raise CatalogUnavailableError("Счёт больше недоступен")
        if account.version != reference.version:
            raise ObjectVersionConflictError(current_version=account.version)
        if account.archived_at is not None:
            raise CatalogUnavailableError("Счёт больше недоступен")
        return _account_snapshot(account)

    async def select_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        reference: DraftCatalogRef,
    ) -> CategorySnapshot:
        await self._lock_owner(owner_id)
        category = await self._lock_category(owner_id, reference.entity_id)
        if category is None or category.kind != kind.value:
            raise CatalogUnavailableError("Категория больше недоступна")
        if category.version != reference.version:
            raise ObjectVersionConflictError(current_version=category.version)
        if category.archived_at is not None:
            raise CatalogUnavailableError("Категория больше недоступна")
        return _category_snapshot(category)

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        owner = await self._lock_owner(owner_id)
        try:
            resolved = await resolve_account(
                self._session,
                owner_id,
                hint,
                owner.default_account_id,
            )
        except UnknownAccountError:
            return None
        account = await self._lock_account(owner_id, resolved.id)
        if account is None or account.archived_at is not None:
            return None
        return _account_snapshot(account)

    async def resolve_fallback_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
    ) -> CategorySnapshot:
        await self._lock_owner(owner_id)
        try:
            resolved = await resolve_category(self._session, owner_id, kind.value, None)
        except UnknownCategoryError:
            raise CatalogUnavailableError("Резервная категория недоступна") from None
        category = await self._lock_category(owner_id, resolved.id)
        if category is None or category.kind != kind.value or category.archived_at is not None:
            raise CatalogUnavailableError("Резервная категория недоступна")
        return _category_snapshot(category)
