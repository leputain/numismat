from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category
from finbot.adapters.database.services.catalogs import (
    archive_account as archive_account_service,
)
from finbot.adapters.database.services.catalogs import (
    archive_category as archive_category_service,
)
from finbot.adapters.database.services.catalogs import (
    create_account as create_account_service,
)
from finbot.adapters.database.services.catalogs import (
    create_category as create_category_service,
)
from finbot.adapters.database.services.catalogs import (
    rename_account as update_account_service,
)
from finbot.adapters.database.services.catalogs import (
    rename_category as update_category_service,
)
from finbot.adapters.database.services.catalogs import (
    restore_account as restore_account_service,
)
from finbot.adapters.database.services.catalogs import (
    restore_category as restore_category_service,
)
from finbot.adapters.database.services.catalogs import (
    set_default_account as set_default_account_service,
)
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


class SqlAlchemyCatalogRepository:
    """Compatibility adapter over the proven, lock-safe catalog services.

    Methods flush through the wrapped services but deliberately never commit;
    the presentation adapter retains the atomic unit-of-work boundary.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_account(self, command: CreateAccountCommand) -> AccountSnapshot:
        account = await create_account_service(
            self._session,
            command.owner_id,
            command.name,
            command.currency,
        )
        return _account_snapshot(account)

    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot:
        account = await update_account_service(
            self._session,
            command.owner_id,
            command.account_id,
            command.name,
            command.expected_version,
        )
        return _account_snapshot(account)

    async def archive_account(self, command: ArchiveAccountCommand) -> AccountSnapshot:
        account = await archive_account_service(
            self._session,
            command.owner_id,
            command.account_id,
            None,
            command.expected_version,
        )
        return _account_snapshot(account)

    async def restore_account(self, command: RestoreAccountCommand) -> AccountSnapshot:
        account = await restore_account_service(
            self._session,
            command.owner_id,
            command.account_id,
            command.expected_version,
        )
        return _account_snapshot(account)

    async def set_default_account(self, command: SetDefaultAccountCommand) -> AccountSnapshot:
        account = await set_default_account_service(
            self._session,
            command.owner_id,
            command.account_id,
            command.expected_version,
        )
        return _account_snapshot(account)

    async def create_category(self, command: CreateCategoryCommand) -> CategorySnapshot:
        category = await create_category_service(
            self._session,
            command.owner_id,
            command.name,
            command.kind.value,
        )
        return _category_snapshot(category)

    async def update_category(self, command: UpdateCategoryCommand) -> CategorySnapshot:
        category = await update_category_service(
            self._session,
            command.owner_id,
            command.category_id,
            command.name,
            command.expected_version,
        )
        return _category_snapshot(category)

    async def archive_category(self, command: ArchiveCategoryCommand) -> CategorySnapshot:
        category = await archive_category_service(
            self._session,
            command.owner_id,
            command.category_id,
            command.expected_version,
        )
        return _category_snapshot(category)

    async def restore_category(self, command: RestoreCategoryCommand) -> CategorySnapshot:
        category = await restore_category_service(
            self._session,
            command.owner_id,
            command.category_id,
            command.expected_version,
        )
        return _category_snapshot(category)
