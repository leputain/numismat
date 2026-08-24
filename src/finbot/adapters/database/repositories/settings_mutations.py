from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, User
from finbot.application.dto import AccountSnapshot, CategorySnapshot, OwnerSnapshot
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
    ObjectVersionConflictError,
)
from finbot.application.settings_mutations import MAX_SETTINGS_VERSION
from finbot.domain.transactions import TransactionType


def _owner_snapshot(owner: User) -> OwnerSnapshot:
    return OwnerSnapshot(
        owner_id=owner.id,
        locale=owner.locale,
        timezone=owner.timezone,
        base_currency=owner.base_currency,
        default_account_id=owner.default_account_id,
        fast_mode=owner.fast_mode,
        settings_version=owner.settings_version,
    )


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


class SqlAlchemySettingsMutationRepository:
    """Authoritative settings target locks; the caller owns commit/rollback."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner

    async def lock_owner(self, owner_id: UUID) -> OwnerSnapshot:
        return _owner_snapshot(await self._owner(owner_id))

    async def lock_account(
        self,
        owner_id: UUID,
        account_id: UUID,
        expected_version: int,
    ) -> AccountSnapshot:
        await self._owner(owner_id)
        account = await self._session.scalar(
            select(Account)
            .where(Account.id == account_id, Account.user_id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account is None or account.archived_at is not None:
            raise EntityNotFoundError("Счёт не найден")
        if account.version != expected_version:
            raise ObjectVersionConflictError(current_version=account.version)
        return _account_snapshot(account)

    async def lock_category(
        self,
        owner_id: UUID,
        category_id: UUID,
        expected_version: int,
    ) -> CategorySnapshot:
        await self._owner(owner_id)
        category = await self._session.scalar(
            select(Category)
            .where(Category.id == category_id, Category.user_id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if category is None or category.archived_at is not None:
            raise EntityNotFoundError("Категория не найдена")
        if category.version != expected_version:
            raise ObjectVersionConflictError(current_version=category.version)
        return _category_snapshot(category)

    async def change_timezone(
        self,
        owner_id: UUID,
        expected_version: int,
        timezone: str,
    ) -> OwnerSnapshot:
        owner = await self._owner(owner_id)
        if owner.settings_version != expected_version:
            raise ObjectVersionConflictError(current_version=owner.settings_version)
        if owner.settings_version >= MAX_SETTINGS_VERSION:
            raise ApplicationValidationError("Достигнут предел версий настроек")
        owner.timezone = timezone
        owner.settings_version += 1
        await self._session.flush()
        return _owner_snapshot(owner)
