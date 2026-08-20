from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyClaimStatus,
    IdempotencyKeyConflictError,
    IdempotencyResult,
    IdempotencyResultKind,
)
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthOwner,
    SessionCheck,
    SessionCheckStatus,
)
from finbot.adapters.http.auth.service import SessionCredentials
from finbot.adapters.http.catalogs.ports import CatalogQueryUnitOfWorkFactory
from finbot.adapters.http.catalogs.service import HttpCatalogService
from finbot.adapters.http.mutations.ports import (
    MutationUnitOfWork,
    MutationUnitOfWorkFactory,
)
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    IdempotencyKeyReuseError,
    MutationCallback,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.application.catalogs import (
    BOUNDED_CATALOG_FETCH_LIMIT,
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
from finbot.domain.transactions import TransactionType

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000002")
CATEGORY_ID = UUID("018f0000-0000-7000-8000-000000000003")
SECURITY_KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
SESSION_BINDING = HttpSecurityDigester(SECURITY_KEY).session_binding(SESSION_TOKEN)
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
IDEMPOTENCY_KEY = base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode("ascii")
CREDENTIALS = MutationCredentials(
    session_token=SESSION_TOKEN,
    session_binding=SESSION_BINDING,
    csrf_cookie=CSRF_TOKEN,
    csrf_header=CSRF_TOKEN,
    idempotency_key=IDEMPOTENCY_KEY,
)
READ_CREDENTIALS = SessionCredentials(SESSION_TOKEN, SESSION_BINDING)


class RecordingCatalogCommands:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def create_account(self, command: CreateAccountCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(ACCOUNT_ID, 1, "active")

    async def update_account(self, command: UpdateAccountCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.account_id, command.expected_version + 1, "active")

    async def archive_account(self, command: ArchiveAccountCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.account_id, command.expected_version + 1, "archived")

    async def restore_account(self, command: RestoreAccountCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.account_id, command.expected_version + 1, "active")

    async def set_default_account(self, command: SetDefaultAccountCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.account_id, command.expected_version + 1, "default")

    async def create_category(self, command: CreateCategoryCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(CATEGORY_ID, 1, "active")

    async def update_category(self, command: UpdateCategoryCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.category_id, command.expected_version + 1, "active")

    async def archive_category(self, command: ArchiveCategoryCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.category_id, command.expected_version + 1, "archived")

    async def restore_category(self, command: RestoreCategoryCommand) -> MutationResult:
        self.calls.append(command)
        return MutationResult(command.category_id, command.expected_version + 1, "active")


class RecordingMutationUnitOfWork:
    def __init__(self, catalogs: RecordingCatalogCommands) -> None:
        self.catalogs = catalogs


class RecordingMutationExecutor:
    def __init__(self, catalogs: RecordingCatalogCommands) -> None:
        self.uow = RecordingMutationUnitOfWork(catalogs)
        self.calls: list[
            tuple[
                MutationOperation,
                dict[str, object],
                frozenset[tuple[int, IdempotencyResultKind]],
            ]
        ] = []

    async def execute(
        self,
        _credentials: MutationCredentials,
        *,
        operation: MutationOperation,
        semantic_request: Mapping[str, object],
        allowed_results: frozenset[tuple[int, IdempotencyResultKind]],
        mutate: MutationCallback,
    ) -> MutationReceipt:
        self.calls.append((operation, dict(semantic_request), allowed_results))
        return await mutate(cast(MutationUnitOfWork, self.uow), OWNER_ID)


def _service(executor: object) -> HttpCatalogService:
    return HttpCatalogService(
        digester=HttpSecurityDigester(SECURITY_KEY),
        query_uow_factory=cast(CatalogQueryUnitOfWorkFactory, object()),
        mutation_executor=cast(HttpMutationExecutor, executor),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_catalog_facade_dispatches_all_nine_typed_operations_and_closed_receipts() -> None:
    catalogs = RecordingCatalogCommands()
    executor = RecordingMutationExecutor(catalogs)
    service = _service(executor)

    receipts = (
        await service.create_account(CREDENTIALS, name="  Новый   счёт ", currency=" rub "),
        await service.update_account(
            CREDENTIALS,
            ACCOUNT_ID,
            name="  Запасной   счёт ",
            version=4,
        ),
        await service.archive_account(CREDENTIALS, ACCOUNT_ID, 5),
        await service.restore_account(CREDENTIALS, ACCOUNT_ID, 6),
        await service.set_default_account(CREDENTIALS, ACCOUNT_ID, 7),
        await service.create_category(
            CREDENTIALS,
            name="  Покупки   дома ",
            kind=TransactionType.EXPENSE,
        ),
        await service.update_category(
            CREDENTIALS,
            CATEGORY_ID,
            name="  Домашние   покупки ",
            version=8,
        ),
        await service.archive_category(CREDENTIALS, CATEGORY_ID, 9),
        await service.restore_category(CREDENTIALS, CATEGORY_ID, 10),
    )

    assert [type(command) for command in catalogs.calls] == [
        CreateAccountCommand,
        UpdateAccountCommand,
        ArchiveAccountCommand,
        RestoreAccountCommand,
        SetDefaultAccountCommand,
        CreateCategoryCommand,
        UpdateCategoryCommand,
        ArchiveCategoryCommand,
        RestoreCategoryCommand,
    ]
    created_account = cast(CreateAccountCommand, catalogs.calls[0])
    updated_account = cast(UpdateAccountCommand, catalogs.calls[1])
    created_category = cast(CreateCategoryCommand, catalogs.calls[5])
    updated_category = cast(UpdateCategoryCommand, catalogs.calls[6])
    assert (created_account.owner_id, created_account.name, created_account.currency) == (
        OWNER_ID,
        "Новый счёт",
        "RUB",
    )
    assert updated_account.name == "Запасной счёт"
    assert (created_category.name, created_category.kind) == (
        "Покупки дома",
        TransactionType.EXPENSE,
    )
    assert updated_category.name == "Домашние покупки"

    assert [call[0] for call in executor.calls] == list(MutationOperation)[:9]
    assert executor.calls[0][1] == {"currency": "RUB", "name": "Новый счёт"}
    assert executor.calls[1][1] == {
        "account_id": str(ACCOUNT_ID),
        "name": "Запасной счёт",
        "version": 4,
    }
    assert executor.calls[5][1] == {"kind": "expense", "name": "Покупки дома"}
    assert executor.calls[6][1] == {
        "category_id": str(CATEGORY_ID),
        "name": "Домашние покупки",
        "version": 8,
    }
    assert executor.calls[0][2] == frozenset({(201, IdempotencyResultKind.ACCOUNT)})
    assert all(
        allowed == frozenset({(200, IdempotencyResultKind.ACCOUNT)})
        for _operation, _semantic, allowed in executor.calls[1:5]
    )
    assert executor.calls[5][2] == frozenset({(201, IdempotencyResultKind.CATEGORY)})
    assert all(
        allowed == frozenset({(200, IdempotencyResultKind.CATEGORY)})
        for _operation, _semantic, allowed in executor.calls[6:]
    )
    assert [(receipt.http_status, receipt.kind, receipt.result_id) for receipt in receipts] == [
        (201, IdempotencyResultKind.ACCOUNT, ACCOUNT_ID),
        *((200, IdempotencyResultKind.ACCOUNT, ACCOUNT_ID),) * 4,
        (201, IdempotencyResultKind.CATEGORY, CATEGORY_ID),
        *((200, IdempotencyResultKind.CATEGORY, CATEGORY_ID),) * 3,
    ]


class ReadAuth:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def read_session(
        self,
        _session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        assert now == NOW
        self.events.append("session")
        return SessionCheck(
            SessionCheckStatus.ACTIVE,
            AuthenticatedSession(
                AuthOwner(OWNER_ID, "ru_RU", "Europe/Moscow", "RUB"),
                NOW + timedelta(hours=1),
            ),
        )


class ReadOwners:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("owner")
        return OwnerSnapshot(owner_id, "ru_RU", "Europe/Moscow", "RUB", ACCOUNT_ID)


class ReadCatalogs:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def list_accounts_bounded(
        self,
        owner_id: UUID,
        *,
        archived: bool,
        limit: int,
    ) -> tuple[AccountSnapshot, ...]:
        assert (owner_id, archived, limit) == (OWNER_ID, True, BOUNDED_CATALOG_FETCH_LIMIT)
        self.events.append("accounts")
        return (AccountSnapshot(ACCOUNT_ID, "Счёт", "card", "RUB", NOW, 3),)

    async def list_categories_bounded(
        self,
        owner_id: UUID,
        *,
        kind: str | None,
        archived: bool,
        limit: int,
    ) -> tuple[CategorySnapshot, ...]:
        assert (owner_id, kind, archived, limit) == (
            OWNER_ID,
            "income",
            False,
            BOUNDED_CATALOG_FETCH_LIMIT,
        )
        self.events.append("categories")
        return (
            CategorySnapshot(
                CATEGORY_ID,
                TransactionType.INCOME,
                "Доход",
                "▫️",
                None,
                2,
            ),
        )


class ReadUnitOfWork:
    def __init__(self, events: list[str]) -> None:
        self.auth = ReadAuth(events)
        self.owners = ReadOwners(events)
        self.catalogs = ReadCatalogs(events)


class ReadUnitOfWorkFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.uow = ReadUnitOfWork(events)

    def __call__(self) -> AbstractAsyncContextManager[ReadUnitOfWork]:
        return self._context()

    @asynccontextmanager
    async def _context(self) -> AsyncIterator[ReadUnitOfWork]:
        self.events.append("enter")
        try:
            yield self.uow
        finally:
            self.events.append("exit")


@pytest.mark.asyncio
async def test_catalog_reads_authenticate_and_project_inside_one_query_uow() -> None:
    events: list[str] = []
    factory = ReadUnitOfWorkFactory(events)
    service = HttpCatalogService(
        digester=HttpSecurityDigester(SECURITY_KEY),
        query_uow_factory=cast(CatalogQueryUnitOfWorkFactory, factory),
        mutation_executor=cast(HttpMutationExecutor, object()),
        clock=lambda: NOW,
    )

    accounts = await service.accounts(READ_CREDENTIALS, archived=True)
    categories = await service.categories(
        READ_CREDENTIALS,
        kind=TransactionType.INCOME,
        archived=False,
    )

    assert accounts.default_account_id == ACCOUNT_ID
    assert accounts.items[0].account_id == ACCOUNT_ID
    assert categories[0].category_id == CATEGORY_ID
    assert events == [
        "enter",
        "session",
        "owner",
        "accounts",
        "exit",
        "enter",
        "session",
        "categories",
        "exit",
    ]


class MutationAuth:
    async def lock_session_for_mutation(
        self,
        _session_token: SessionTokenDigest,
        _csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        assert now == NOW
        return SessionCheck(
            SessionCheckStatus.ACTIVE,
            AuthenticatedSession(
                AuthOwner(OWNER_ID, "ru_RU", "Europe/Moscow", "RUB"),
                NOW + timedelta(hours=1),
            ),
        )


class StatefulIdempotency:
    def __init__(self) -> None:
        self.operation: str | None = None
        self.fingerprint: bytes | None = None
        self.result: IdempotencyResult | None = None
        self.record_id = uuid7()

    async def claim(
        self,
        _owner_id: UUID,
        _key: IdempotencyKeyDigest,
        fingerprint: RequestFingerprintDigest,
        *,
        operation: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> IdempotencyClaim:
        assert (created_at, expires_at) == (NOW, NOW + timedelta(hours=24))
        candidate = fingerprint.database_value()
        if self.fingerprint is None:
            self.fingerprint = candidate
            self.operation = operation
            return IdempotencyClaim(self.record_id, IdempotencyClaimStatus.NEW)
        if self.operation != operation or self.fingerprint != candidate:
            raise IdempotencyKeyConflictError
        assert self.result is not None
        return IdempotencyClaim(self.record_id, IdempotencyClaimStatus.REPLAY, self.result)

    async def complete(
        self,
        _owner_id: UUID,
        _claim: IdempotencyClaim,
        result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None:
        assert completed_at == NOW
        self.result = result


class ReplayCatalogs:
    def __init__(self) -> None:
        self.calls = 0

    async def create_account(self, command: CreateAccountCommand) -> MutationResult:
        self.calls += 1
        assert (command.name, command.currency) == ("Новый счёт", "RUB")
        return MutationResult(ACCOUNT_ID, 1, "active")


class StatefulMutationUnitOfWork:
    def __init__(self) -> None:
        self.auth = MutationAuth()
        self.catalogs = ReplayCatalogs()
        self.idempotency = StatefulIdempotency()
        self.commands = object()
        self.drafts = object()
        self.owner_locks = 0

    async def lock_owner(self, owner_id: UUID) -> None:
        assert owner_id == OWNER_ID
        self.owner_locks += 1


class StatefulMutationUnitOfWorkFactory:
    def __init__(self, uow: StatefulMutationUnitOfWork) -> None:
        self.uow = uow

    def __call__(self) -> AbstractAsyncContextManager[StatefulMutationUnitOfWork]:
        return self._context()

    @asynccontextmanager
    async def _context(self) -> AsyncIterator[StatefulMutationUnitOfWork]:
        yield self.uow


@pytest.mark.asyncio
async def test_normalized_fingerprint_replays_stored_catalog_receipt_without_domain_read() -> None:
    uow = StatefulMutationUnitOfWork()
    executor = HttpMutationExecutor(
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=cast(MutationUnitOfWorkFactory, StatefulMutationUnitOfWorkFactory(uow)),
        clock=lambda: NOW,
    )
    service = _service(executor)

    first = await service.create_account(
        CREDENTIALS,
        name="  Новый   счёт ",
        currency=" rub ",
    )
    replay = await service.create_account(
        CREDENTIALS,
        name="Новый счёт",
        currency="RUB",
    )

    assert first == MutationReceipt(201, IdempotencyResultKind.ACCOUNT, ACCOUNT_ID, 1)
    assert replay == MutationReceipt(
        201,
        IdempotencyResultKind.ACCOUNT,
        ACCOUNT_ID,
        1,
        replayed=True,
    )
    assert uow.catalogs.calls == 1
    assert uow.owner_locks == 2

    with pytest.raises(IdempotencyKeyReuseError):
        await service.create_account(CREDENTIALS, name="Другой счёт", currency="RUB")
    assert uow.catalogs.calls == 1


def test_catalog_boundary_representations_redact_credentials_names_and_ids() -> None:
    command = CreateAccountCommand(OWNER_ID, "private-account-name", "RUB")
    catalog = _service(RecordingMutationExecutor(RecordingCatalogCommands()))
    rendered = " ".join((repr(CREDENTIALS), repr(command), repr(catalog)))

    for sensitive in (
        SESSION_TOKEN,
        CSRF_TOKEN,
        IDEMPOTENCY_KEY,
        "private-account-name",
        str(OWNER_ID),
    ):
        assert sensitive not in rendered
