from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
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
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    MutationResult,
    OwnerSnapshot,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType


class CatalogOperation(StrEnum):
    ACCOUNT_CREATED = "account_created"
    ACCOUNT_UPDATED = "account_updated"
    ACCOUNT_ARCHIVED = "account_archived"
    ACCOUNT_RESTORED = "account_restored"
    ACCOUNT_DEFAULT_SET = "account_default_set"
    CATEGORY_CREATED = "category_created"
    CATEGORY_UPDATED = "category_updated"
    CATEGORY_ARCHIVED = "category_archived"
    CATEGORY_RESTORED = "category_restored"


@dataclass(frozen=True, slots=True)
class TelegramCatalogContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class CreateAccountInput:
    name: str = field(repr=False)
    currency: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UpdateAccountInput:
    account_id: UUID = field(repr=False)
    name: str = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class VersionedAccountInput:
    account_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class CreateCategoryInput:
    name: str = field(repr=False)
    kind: TransactionType = field(repr=False)


@dataclass(frozen=True, slots=True)
class UpdateCategoryInput:
    category_id: UUID = field(repr=False)
    name: str = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class VersionedCategoryInput:
    category_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class AccountCatalogReceiptSnapshot:
    """Pure renderer input captured after an account mutation in the same transaction."""

    operation: CatalogOperation
    mutation: MutationResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    active_accounts: tuple[AccountSnapshot, ...] = field(repr=False)
    archived_accounts: tuple[AccountSnapshot, ...] = field(repr=False)
    message_id: int | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CategoryCatalogReceiptSnapshot:
    """Pure renderer input captured after a category mutation in the same transaction."""

    operation: CatalogOperation
    mutation: MutationResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    active_categories: tuple[CategorySnapshot, ...] = field(repr=False)
    archived_categories: tuple[CategorySnapshot, ...] = field(repr=False)
    message_id: int | None = field(default=None, repr=False)


type CatalogReceiptSnapshot = AccountCatalogReceiptSnapshot | CategoryCatalogReceiptSnapshot


@dataclass(frozen=True, slots=True)
class CatalogSessionUseCases:
    """Session-scoped application facade built by the composition root."""

    catalogs: CatalogUseCases = field(repr=False)
    list_accounts: ListAccounts = field(repr=False)
    list_categories: ListCategories = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)


class CatalogUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession) -> CatalogSessionUseCases: ...


type CatalogReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, CatalogReceiptSnapshot],
    Awaitable[None],
]
type AccountMutation = Callable[[CatalogUseCases, UUID], Awaitable[MutationResult]]
type CategoryMutation = Callable[[CatalogUseCases, UUID], Awaitable[MutationResult]]


def _same_receipt[ReceiptValue](value: ReceiptValue) -> ReceiptValue:
    return value


class CatalogController:
    """Map neutral catalog inputs to application commands and durable Telegram receipts."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_case_factory")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: CatalogUseCaseFactory,
        enqueue_receipt: CatalogReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_case_factory = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def _execute_account(
        self,
        context: TelegramCatalogContext,
        operation: CatalogOperation,
        mutation: AccountMutation,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> AccountCatalogReceiptSnapshot:
            use_cases = self._use_case_factory(session)
            result = await mutation(use_cases.catalogs, owner_id)
            owner = await use_cases.get_owner_settings(owner_id)
            active = await use_cases.list_accounts(owner_id, archived=False)
            archived = await use_cases.list_accounts(owner_id, archived=True)
            return AccountCatalogReceiptSnapshot(
                operation=operation,
                mutation=result,
                owner=owner,
                active_accounts=active,
                archived_accounts=archived,
                message_id=context.message_id,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_same_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def _execute_category(
        self,
        context: TelegramCatalogContext,
        operation: CatalogOperation,
        mutation: CategoryMutation,
    ) -> CategoryCatalogReceiptSnapshot | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> CategoryCatalogReceiptSnapshot:
            use_cases = self._use_case_factory(session)
            result = await mutation(use_cases.catalogs, owner_id)
            owner = await use_cases.get_owner_settings(owner_id)
            active = await use_cases.list_categories(owner_id, archived=False)
            archived = await use_cases.list_categories(owner_id, archived=True)
            return CategoryCatalogReceiptSnapshot(
                operation=operation,
                mutation=result,
                owner=owner,
                active_categories=active,
                archived_categories=archived,
                message_id=context.message_id,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_same_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def create_account(
        self,
        context: TelegramCatalogContext,
        values: CreateAccountInput,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.create_account(
                CreateAccountCommand(owner_id, values.name, values.currency)
            )

        return await self._execute_account(
            context,
            CatalogOperation.ACCOUNT_CREATED,
            mutation,
        )

    async def update_account(
        self,
        context: TelegramCatalogContext,
        values: UpdateAccountInput,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.update_account(
                UpdateAccountCommand(
                    owner_id,
                    values.account_id,
                    values.name,
                    values.expected_version,
                )
            )

        return await self._execute_account(
            context,
            CatalogOperation.ACCOUNT_UPDATED,
            mutation,
        )

    async def archive_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.archive_account(
                ArchiveAccountCommand(owner_id, values.account_id, values.expected_version)
            )

        return await self._execute_account(
            context,
            CatalogOperation.ACCOUNT_ARCHIVED,
            mutation,
        )

    async def restore_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.restore_account(
                RestoreAccountCommand(owner_id, values.account_id, values.expected_version)
            )

        return await self._execute_account(
            context,
            CatalogOperation.ACCOUNT_RESTORED,
            mutation,
        )

    async def set_default_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> AccountCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.set_default_account(
                SetDefaultAccountCommand(owner_id, values.account_id, values.expected_version)
            )

        return await self._execute_account(
            context,
            CatalogOperation.ACCOUNT_DEFAULT_SET,
            mutation,
        )

    async def create_category(
        self,
        context: TelegramCatalogContext,
        values: CreateCategoryInput,
    ) -> CategoryCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.create_category(
                CreateCategoryCommand(owner_id, values.name, values.kind)
            )

        return await self._execute_category(
            context,
            CatalogOperation.CATEGORY_CREATED,
            mutation,
        )

    async def update_category(
        self,
        context: TelegramCatalogContext,
        values: UpdateCategoryInput,
    ) -> CategoryCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.update_category(
                UpdateCategoryCommand(
                    owner_id,
                    values.category_id,
                    values.name,
                    values.expected_version,
                )
            )

        return await self._execute_category(
            context,
            CatalogOperation.CATEGORY_UPDATED,
            mutation,
        )

    async def archive_category(
        self,
        context: TelegramCatalogContext,
        values: VersionedCategoryInput,
    ) -> CategoryCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.archive_category(
                ArchiveCategoryCommand(owner_id, values.category_id, values.expected_version)
            )

        return await self._execute_category(
            context,
            CatalogOperation.CATEGORY_ARCHIVED,
            mutation,
        )

    async def restore_category(
        self,
        context: TelegramCatalogContext,
        values: VersionedCategoryInput,
    ) -> CategoryCatalogReceiptSnapshot | None:
        async def mutation(catalogs: CatalogUseCases, owner_id: UUID) -> MutationResult:
            return await catalogs.restore_category(
                RestoreCategoryCommand(owner_id, values.category_id, values.expected_version)
            )

        return await self._execute_category(
            context,
            CatalogOperation.CATEGORY_RESTORED,
            mutation,
        )
