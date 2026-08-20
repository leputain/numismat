from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator, SessionCredentials
from finbot.adapters.http.catalogs.ports import CatalogQueryUnitOfWorkFactory
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
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
from finbot.application.dto import AccountSnapshot, CategorySnapshot, MutationResult
from finbot.application.errors import ApplicationValidationError
from finbot.application.services.catalogs import normalize_catalog_name
from finbot.application.services.onboarding import normalize_currency_code
from finbot.application.use_cases.catalogs import ListBoundedAccounts, ListBoundedCategories
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.domain.transactions import TransactionType

_ACCOUNT_CREATED = frozenset({(201, IdempotencyResultKind.ACCOUNT)})
_ACCOUNT_UPDATED = frozenset({(200, IdempotencyResultKind.ACCOUNT)})
_CATEGORY_CREATED = frozenset({(201, IdempotencyResultKind.CATEGORY)})
_CATEGORY_UPDATED = frozenset({(200, IdempotencyResultKind.CATEGORY)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_name(value: str) -> str:
    try:
        return normalize_catalog_name(value)
    except ValueError as exc:
        raise ApplicationValidationError("Название не прошло проверку") from exc


def _normalized_currency(value: str) -> str:
    try:
        return normalize_currency_code(value)
    except ValueError as exc:
        raise ApplicationValidationError("Валюта не прошла проверку") from exc


def _receipt(
    result: MutationResult,
    *,
    status: int,
    kind: IdempotencyResultKind,
) -> MutationReceipt:
    return MutationReceipt(
        http_status=status,
        kind=kind,
        result_id=result.entity_id,
        revision=result.version,
    )


@dataclass(frozen=True, slots=True, repr=False)
class AccountCatalog:
    default_account_id: UUID | None = field(repr=False)
    items: tuple[AccountSnapshot, ...] = field(repr=False)


class HttpCatalogService:
    """Closed owner-scoped catalog facade for Task 15 HTTP routes."""

    __slots__ = ("_authenticator", "_clock", "_mutation_executor", "_query_uow_factory")

    def __init__(
        self,
        *,
        digester: HttpSecurityDigester,
        query_uow_factory: CatalogQueryUnitOfWorkFactory,
        mutation_executor: HttpMutationExecutor,
        allowed_telegram_user_ids: frozenset[int] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._authenticator = SessionAuthenticator(digester, allowed_telegram_user_ids)
        self._query_uow_factory = query_uow_factory
        self._mutation_executor = mutation_executor
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog query clock must be timezone-aware")
        return value.astimezone(UTC)

    async def accounts(self, credentials: SessionCredentials, *, archived: bool) -> AccountCatalog:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            owner = await GetOwnerSettings(uow.owners)(owner_id)
            items = await ListBoundedAccounts(uow.catalogs)(owner_id, archived=archived)
            return AccountCatalog(default_account_id=owner.default_account_id, items=items)

    async def categories(
        self,
        credentials: SessionCredentials,
        *,
        kind: TransactionType | None,
        archived: bool,
    ) -> tuple[CategorySnapshot, ...]:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await ListBoundedCategories(uow.catalogs)(
                authenticated.owner.owner_id,
                kind=kind,
                archived=archived,
            )

    async def create_account(
        self,
        credentials: MutationCredentials,
        *,
        name: str,
        currency: str,
    ) -> MutationReceipt:
        normalized_name = _normalized_name(name)
        normalized_currency = _normalized_currency(currency)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.catalogs.create_account(
                CreateAccountCommand(owner_id, normalized_name, normalized_currency)
            )
            return _receipt(result, status=201, kind=IdempotencyResultKind.ACCOUNT)

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.ACCOUNT_CREATE,
            semantic_request={"currency": normalized_currency, "name": normalized_name},
            allowed_results=_ACCOUNT_CREATED,
            mutate=mutate,
        )

    async def update_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        *,
        name: str,
        version: int,
    ) -> MutationReceipt:
        normalized_name = _normalized_name(name)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.catalogs.update_account(
                UpdateAccountCommand(owner_id, account_id, normalized_name, version)
            )
            return _receipt(result, status=200, kind=IdempotencyResultKind.ACCOUNT)

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.ACCOUNT_UPDATE,
            semantic_request={
                "account_id": str(account_id),
                "name": normalized_name,
                "version": version,
            },
            allowed_results=_ACCOUNT_UPDATED,
            mutate=mutate,
        )

    async def archive_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._account_action(
            credentials,
            account_id,
            version,
            operation=MutationOperation.ACCOUNT_ARCHIVE,
        )

    async def restore_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._account_action(
            credentials,
            account_id,
            version,
            operation=MutationOperation.ACCOUNT_RESTORE,
        )

    async def set_default_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._account_action(
            credentials,
            account_id,
            version,
            operation=MutationOperation.ACCOUNT_DEFAULT,
        )

    async def _account_action(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
        *,
        operation: MutationOperation,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            if operation is MutationOperation.ACCOUNT_ARCHIVE:
                archive = ArchiveAccountCommand(owner_id, account_id, version)
                result = await uow.catalogs.archive_account(archive)
            elif operation is MutationOperation.ACCOUNT_RESTORE:
                restore = RestoreAccountCommand(owner_id, account_id, version)
                result = await uow.catalogs.restore_account(restore)
            elif operation is MutationOperation.ACCOUNT_DEFAULT:
                select_default = SetDefaultAccountCommand(owner_id, account_id, version)
                result = await uow.catalogs.set_default_account(select_default)
            else:  # pragma: no cover - private exhaustive dispatch
                raise TypeError("unsupported account operation")
            return _receipt(result, status=200, kind=IdempotencyResultKind.ACCOUNT)

        return await self._mutation_executor.execute(
            credentials,
            operation=operation,
            semantic_request={"account_id": str(account_id), "version": version},
            allowed_results=_ACCOUNT_UPDATED,
            mutate=mutate,
        )

    async def create_category(
        self,
        credentials: MutationCredentials,
        *,
        name: str,
        kind: TransactionType,
    ) -> MutationReceipt:
        normalized_name = _normalized_name(name)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.catalogs.create_category(
                CreateCategoryCommand(owner_id, normalized_name, kind)
            )
            return _receipt(result, status=201, kind=IdempotencyResultKind.CATEGORY)

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.CATEGORY_CREATE,
            semantic_request={"kind": kind.value, "name": normalized_name},
            allowed_results=_CATEGORY_CREATED,
            mutate=mutate,
        )

    async def update_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        *,
        name: str,
        version: int,
    ) -> MutationReceipt:
        normalized_name = _normalized_name(name)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.catalogs.update_category(
                UpdateCategoryCommand(owner_id, category_id, normalized_name, version)
            )
            return _receipt(result, status=200, kind=IdempotencyResultKind.CATEGORY)

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.CATEGORY_UPDATE,
            semantic_request={
                "category_id": str(category_id),
                "name": normalized_name,
                "version": version,
            },
            allowed_results=_CATEGORY_UPDATED,
            mutate=mutate,
        )

    async def archive_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._category_action(
            credentials,
            category_id,
            version,
            operation=MutationOperation.CATEGORY_ARCHIVE,
        )

    async def restore_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._category_action(
            credentials,
            category_id,
            version,
            operation=MutationOperation.CATEGORY_RESTORE,
        )

    async def _category_action(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
        *,
        operation: MutationOperation,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            if operation is MutationOperation.CATEGORY_ARCHIVE:
                archive = ArchiveCategoryCommand(owner_id, category_id, version)
                result = await uow.catalogs.archive_category(archive)
            elif operation is MutationOperation.CATEGORY_RESTORE:
                restore = RestoreCategoryCommand(owner_id, category_id, version)
                result = await uow.catalogs.restore_category(restore)
            else:  # pragma: no cover - private exhaustive dispatch
                raise TypeError("unsupported category operation")
            return _receipt(result, status=200, kind=IdempotencyResultKind.CATEGORY)

        return await self._mutation_executor.execute(
            credentials,
            operation=operation,
            semantic_request={"category_id": str(category_id), "version": version},
            allowed_results=_CATEGORY_UPDATED,
            mutate=mutate,
        )
