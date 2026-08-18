from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import AuthUnitOfWorkFactory
from finbot.adapters.http.auth.service import SessionAuthenticator
from finbot.adapters.http.bank_imports.cursor import BankImportCursorCodec
from finbot.adapters.http.bank_imports.ports import BankImportQueryUnitOfWorkFactory
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.application.bank_imports import (
    BankImportBatchPage,
    BankImportBatchRef,
    BankImportBatchSnapshot,
    BankImportBatchState,
    BankImportEncoding,
    BankImportProfile,
    BankImportRequestDigest,
    BankImportRowPage,
    BankImportRowRef,
    BankImportRowSnapshot,
    BankImportRowState,
    CancelBankImportBatchCommand,
    CreateBankImportCommand,
    LinkBankImportRowCommand,
    ReconciliationCandidate,
    StageBankImportBatchCommand,
    VersionedBankImportRowCommand,
)
from finbot.application.errors import InvalidStateError
from finbot.application.use_cases.bank_imports import (
    BankImportPreparer,
    GetBankImportBatch,
    GetBankImportRow,
    ListBankImportBatches,
    ListBankImportRows,
    ListReconciliationCandidates,
)

DEFAULT_BANK_IMPORT_LIMIT = 20
_BATCH_CREATED = frozenset({(201, IdempotencyResultKind.BANK_IMPORT_BATCH)})
_BATCH_UPDATED = frozenset({(200, IdempotencyResultKind.BANK_IMPORT_BATCH)})
_ROW_UPDATED = frozenset({(200, IdempotencyResultKind.BANK_IMPORT_ROW)})
_DRAFT_CREATED = frozenset({(201, IdempotencyResultKind.DRAFT)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedHttpBankImport:
    command: StageBankImportBatchCommand = field(repr=False)
    request_digest: BankImportRequestDigest = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class HttpBankImportBatchPage:
    page: BankImportBatchPage = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class HttpBankImportRowPage:
    page: BankImportRowPage = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


class HttpBankImportService:
    """Admission-gated upload plus bounded owner-scoped import reconciliation."""

    __slots__ = (
        "_admission_uow_factory",
        "_authenticator",
        "_clock",
        "_cursor_codec",
        "_mutation_executor",
        "_preparer",
        "_query_uow_factory",
    )

    def __init__(
        self,
        *,
        digester: HttpSecurityDigester,
        preparer: BankImportPreparer,
        cursor_codec: BankImportCursorCodec,
        admission_uow_factory: AuthUnitOfWorkFactory,
        query_uow_factory: BankImportQueryUnitOfWorkFactory,
        mutation_executor: HttpMutationExecutor,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._authenticator = SessionAuthenticator(digester)
        self._preparer = preparer
        self._cursor_codec = cursor_codec
        self._admission_uow_factory = admission_uow_factory
        self._query_uow_factory = query_uow_factory
        self._mutation_executor = mutation_executor
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bank import clock must be timezone-aware")
        return value.astimezone(UTC)

    async def admit_upload(self, credentials: MutationCredentials) -> UUID:
        """Short DB-backed session+CSRF gate before reading a potentially large body."""

        async with self._admission_uow_factory() as auth:
            authenticated = await self._authenticator.authenticate_mutation(
                auth,
                credentials.session_token,
                credentials.csrf_cookie,
                credentials.csrf_header,
                now=self._now(),
            )
            return authenticated.owner.owner_id

    def prepare_upload(
        self,
        owner_id: UUID,
        account_id: UUID,
        account_version: int,
        content: bytes,
        *,
        encoding: BankImportEncoding | None,
    ) -> PreparedHttpBankImport:
        command = self._preparer.prepare(
            CreateBankImportCommand(
                owner_id=owner_id,
                account_id=account_id,
                expected_account_version=account_version,
                profile=BankImportProfile.CANONICAL_V1,
                content=content,
                encoding=encoding,
            )
        )
        return PreparedHttpBankImport(
            command=command,
            request_digest=self._preparer.request_digest(command),
        )

    async def create(
        self,
        credentials: MutationCredentials,
        prepared: PreparedHttpBankImport,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            if owner_id != prepared.command.owner_id:
                raise InvalidStateError("Владелец импорта изменился")
            batch = await uow.bank_imports.create_prepared(prepared.command)
            return MutationReceipt(
                http_status=201,
                kind=IdempotencyResultKind.BANK_IMPORT_BATCH,
                result_id=batch.batch_id,
                revision=batch.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BANK_IMPORT_CREATE,
            semantic_request={
                "batch_digest": prepared.request_digest.canonical_token(),
            },
            allowed_results=_BATCH_CREATED,
            mutate=mutate,
        )

    async def list_batches(
        self,
        raw_session_token: str,
        *,
        state: BankImportBatchState | None,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpBankImportBatchPage:
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=self._now(),
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode_batch(owner_id, raw_cursor, state=state)
                if raw_cursor is not None
                else None
            )
            page = await ListBankImportBatches(uow.bank_imports)(
                owner_id,
                state=state,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = (
                self._cursor_codec.encode_batch(owner_id, page.next_cursor, state=state)
                if page.next_cursor is not None
                else None
            )
            return HttpBankImportBatchPage(page, next_cursor)

    async def get_batch(
        self,
        raw_session_token: str,
        batch_id: UUID,
    ) -> BankImportBatchSnapshot:
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=self._now(),
            )
            return await GetBankImportBatch(uow.bank_imports)(
                authenticated.owner.owner_id,
                batch_id,
            )

    async def list_rows(
        self,
        raw_session_token: str,
        batch_id: UUID,
        *,
        state: BankImportRowState | None,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpBankImportRowPage:
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=self._now(),
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode_row(
                    owner_id,
                    batch_id,
                    raw_cursor,
                    state=state,
                )
                if raw_cursor is not None
                else None
            )
            page = await ListBankImportRows(uow.bank_imports)(
                owner_id,
                batch_id,
                state=state,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = (
                self._cursor_codec.encode_row(
                    owner_id,
                    batch_id,
                    page.next_cursor,
                    state=state,
                )
                if page.next_cursor is not None
                else None
            )
            return HttpBankImportRowPage(page, next_cursor)

    async def get_row(
        self,
        raw_session_token: str,
        batch_id: UUID,
        row_id: UUID,
    ) -> BankImportRowSnapshot:
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=self._now(),
            )
            return await GetBankImportRow(uow.bank_imports)(
                authenticated.owner.owner_id,
                batch_id,
                row_id,
            )

    async def candidates(
        self,
        raw_session_token: str,
        batch_id: UUID,
        row_id: UUID,
    ) -> tuple[ReconciliationCandidate, ...]:
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=self._now(),
            )
            return await ListReconciliationCandidates(uow.bank_imports)(
                authenticated.owner.owner_id,
                batch_id,
                row_id,
            )

    async def stage_draft(
        self,
        credentials: MutationCredentials,
        batch_id: UUID,
        batch_version: int,
        row_id: UUID,
        row_version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.bank_imports.stage_draft(
                VersionedBankImportRowCommand(
                    owner_id,
                    BankImportBatchRef(batch_id, batch_version),
                    BankImportRowRef(row_id, row_version),
                )
            )
            return MutationReceipt(
                http_status=201,
                kind=IdempotencyResultKind.DRAFT,
                result_id=result.draft.draft_id,
                revision=result.draft.revision,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BANK_IMPORT_DRAFT,
            semantic_request=_row_semantics(
                batch_id,
                batch_version,
                row_id,
                row_version,
            ),
            allowed_results=_DRAFT_CREATED,
            mutate=mutate,
        )

    async def link(
        self,
        credentials: MutationCredentials,
        batch_id: UUID,
        batch_version: int,
        row_id: UUID,
        row_version: int,
        transaction_id: UUID,
        transaction_version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.bank_imports.link(
                LinkBankImportRowCommand(
                    owner_id=owner_id,
                    batch=BankImportBatchRef(batch_id, batch_version),
                    row=BankImportRowRef(row_id, row_version),
                    transaction_id=transaction_id,
                    expected_transaction_version=transaction_version,
                )
            )
            return MutationReceipt(
                http_status=200,
                kind=IdempotencyResultKind.BANK_IMPORT_ROW,
                result_id=result.row.row_id,
                revision=result.row.version,
            )

        semantics = _row_semantics(batch_id, batch_version, row_id, row_version)
        semantics.update(
            {
                "transaction_id": str(transaction_id),
                "transaction_version": transaction_version,
            }
        )
        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BANK_IMPORT_LINK,
            semantic_request=semantics,
            allowed_results=_ROW_UPDATED,
            mutate=mutate,
        )

    async def skip(
        self,
        credentials: MutationCredentials,
        batch_id: UUID,
        batch_version: int,
        row_id: UUID,
        row_version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.bank_imports.skip(
                VersionedBankImportRowCommand(
                    owner_id,
                    BankImportBatchRef(batch_id, batch_version),
                    BankImportRowRef(row_id, row_version),
                )
            )
            return MutationReceipt(
                http_status=200,
                kind=IdempotencyResultKind.BANK_IMPORT_ROW,
                result_id=result.row.row_id,
                revision=result.row.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BANK_IMPORT_SKIP,
            semantic_request=_row_semantics(
                batch_id,
                batch_version,
                row_id,
                row_version,
            ),
            allowed_results=_ROW_UPDATED,
            mutate=mutate,
        )

    async def cancel(
        self,
        credentials: MutationCredentials,
        batch_id: UUID,
        version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            batch = await uow.bank_imports.cancel_batch(
                CancelBankImportBatchCommand(
                    owner_id,
                    BankImportBatchRef(batch_id, version),
                )
            )
            return MutationReceipt(
                http_status=200,
                kind=IdempotencyResultKind.BANK_IMPORT_BATCH,
                result_id=batch.batch_id,
                revision=batch.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BANK_IMPORT_CANCEL,
            semantic_request={"batch_id": str(batch_id), "version": version},
            allowed_results=_BATCH_UPDATED,
            mutate=mutate,
        )


def _row_semantics(
    batch_id: UUID,
    batch_version: int,
    row_id: UUID,
    row_version: int,
) -> dict[str, object]:
    return {
        "batch_id": str(batch_id),
        "batch_version": batch_version,
        "row_id": str(row_id),
        "row_version": row_version,
    }


__all__ = [
    "DEFAULT_BANK_IMPORT_LIMIT",
    "HttpBankImportBatchPage",
    "HttpBankImportRowPage",
    "HttpBankImportService",
    "PreparedHttpBankImport",
]
