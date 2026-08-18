from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

from finbot.application.catalogs import (
    BOUNDED_CATALOG_FETCH_LIMIT,
    MAX_BOUNDED_CATALOG_ITEMS,
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    BoundedCatalogReader,
    CatalogCommandRepository,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import AccountSnapshot, CategorySnapshot, MutationResult
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    EntityNotFoundError,
    ObjectVersionConflictError,
)
from finbot.application.services.onboarding import normalize_currency_code
from finbot.domain.errors import ObjectNotFoundError, StaleObjectError
from finbot.domain.transactions import TransactionType


def _validate_expected_version(expected_version: int) -> None:
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ApplicationValidationError("Версия объекта должна быть положительным числом")


def _validate_category_kind(kind: TransactionType) -> None:
    if not isinstance(kind, TransactionType):
        raise ApplicationValidationError("Неизвестный тип категории")


async def _translate_legacy_errors[Result](
    operation: Callable[[], Awaitable[Result]],
) -> Result:
    """Expose stable application failures while legacy services are migrated.

    Only documented, owner-recoverable failures are translated. Unexpected
    database or programming errors keep their original type and reach the
    outer transaction/error boundary instead of being disguised as input
    mistakes.
    """

    try:
        return await operation()
    except StaleObjectError as error:
        raise ObjectVersionConflictError() from error
    except ObjectNotFoundError as error:
        raise EntityNotFoundError(str(error)) from error
    except ValueError as error:
        raise ApplicationValidationError(str(error)) from error


def _account_result(account: AccountSnapshot, *, state: str | None = None) -> MutationResult:
    return MutationResult(
        entity_id=account.account_id,
        version=account.version,
        resulting_state=state or ("archived" if account.archived_at is not None else "active"),
    )


def _category_result(category: CategorySnapshot) -> MutationResult:
    return MutationResult(
        entity_id=category.category_id,
        version=category.version,
        resulting_state="archived" if category.archived_at is not None else "active",
    )


def _bounded_catalog[SnapshotT](items: tuple[SnapshotT, ...]) -> tuple[SnapshotT, ...]:
    if len(items) > MAX_BOUNDED_CATALOG_ITEMS:
        raise CatalogUnavailableError("Справочник превышает допустимый размер")
    return items


class ListBoundedAccounts:
    """Return a complete bounded account catalog or fail instead of truncating."""

    __slots__ = ("_reader",)

    def __init__(self, reader: BoundedCatalogReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        if type(archived) is not bool:
            raise ApplicationValidationError("Признак архива не прошёл проверку")
        return _bounded_catalog(
            await self._reader.list_accounts_bounded(
                owner_id,
                archived=archived,
                limit=BOUNDED_CATALOG_FETCH_LIMIT,
            )
        )


class ListBoundedCategories:
    """Return a complete bounded category catalog or fail instead of truncating."""

    __slots__ = ("_reader",)

    def __init__(self, reader: BoundedCatalogReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        if type(archived) is not bool:
            raise ApplicationValidationError("Признак архива не прошёл проверку")
        if kind is not None and not isinstance(kind, TransactionType):
            raise ApplicationValidationError("Неизвестный тип категории")
        return _bounded_catalog(
            await self._reader.list_categories_bounded(
                owner_id,
                kind=kind.value if kind is not None else None,
                archived=archived,
                limit=BOUNDED_CATALOG_FETCH_LIMIT,
            )
        )


class CatalogUseCases:
    """Framework-neutral account and category command API."""

    __slots__ = ("_repository",)

    def __init__(self, repository: CatalogCommandRepository) -> None:
        self._repository = repository

    async def create_account(self, command: CreateAccountCommand) -> MutationResult:
        try:
            currency = normalize_currency_code(command.currency)
        except ValueError as error:
            raise ApplicationValidationError(str(error)) from error
        normalized = replace(command, currency=currency)
        account = await _translate_legacy_errors(
            lambda: self._repository.create_account(normalized)
        )
        return _account_result(account)

    async def update_account(self, command: UpdateAccountCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        account = await _translate_legacy_errors(lambda: self._repository.update_account(command))
        return _account_result(account)

    async def archive_account(self, command: ArchiveAccountCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        account = await _translate_legacy_errors(lambda: self._repository.archive_account(command))
        return _account_result(account)

    async def restore_account(self, command: RestoreAccountCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        account = await _translate_legacy_errors(lambda: self._repository.restore_account(command))
        return _account_result(account)

    async def set_default_account(self, command: SetDefaultAccountCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        account = await _translate_legacy_errors(
            lambda: self._repository.set_default_account(command)
        )
        return _account_result(account, state="default")

    async def create_category(self, command: CreateCategoryCommand) -> MutationResult:
        _validate_category_kind(command.kind)
        category = await _translate_legacy_errors(lambda: self._repository.create_category(command))
        return _category_result(category)

    async def update_category(self, command: UpdateCategoryCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        category = await _translate_legacy_errors(lambda: self._repository.update_category(command))
        return _category_result(category)

    async def archive_category(self, command: ArchiveCategoryCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        category = await _translate_legacy_errors(
            lambda: self._repository.archive_category(command)
        )
        return _category_result(category)

    async def restore_category(self, command: RestoreCategoryCommand) -> MutationResult:
        _validate_expected_version(command.expected_version)
        category = await _translate_legacy_errors(
            lambda: self._repository.restore_category(command)
        )
        return _category_result(category)
