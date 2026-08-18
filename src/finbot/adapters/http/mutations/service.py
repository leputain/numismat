from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaimStatus,
    IdempotencyKeyConflictError,
    IdempotencyResult,
    IdempotencyResultKind,
    IdempotencyStateError,
)
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator
from finbot.adapters.http.mutations.ports import MutationUnitOfWork, MutationUnitOfWorkFactory
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.errors import EntityNotFoundError
from finbot.application.revision_mutations import (
    DraftExistingSelection,
    DraftPatchResult,
    DraftPatchSpec,
)

IDEMPOTENCY_TTL = timedelta(hours=24)


class MutationOperation(StrEnum):
    ACCOUNT_CREATE = "account.create"
    ACCOUNT_UPDATE = "account.update"
    ACCOUNT_ARCHIVE = "account.archive"
    ACCOUNT_RESTORE = "account.restore"
    ACCOUNT_DEFAULT = "account.default"
    CATEGORY_CREATE = "category.create"
    CATEGORY_UPDATE = "category.update"
    CATEGORY_ARCHIVE = "category.archive"
    CATEGORY_RESTORE = "category.restore"
    BUDGET_CREATE = "budget.create"
    BUDGET_REPLACE = "budget.replace"
    BUDGET_DELETE = "budget.delete"
    BUDGET_RESTORE = "budget.restore"
    RECURRING_SCHEDULE_CREATE = "recurring_schedule.create"
    RECURRING_SCHEDULE_REPLACE = "recurring_schedule.replace"
    RECURRING_SCHEDULE_PAUSE = "recurring_schedule.pause"
    RECURRING_SCHEDULE_RESUME = "recurring_schedule.resume"
    RECURRING_SCHEDULE_DELETE = "recurring_schedule.delete"
    RECURRING_SCHEDULE_RESTORE = "recurring_schedule.restore"
    RECURRING_INSTANCE_SKIP = "recurring_instance.skip"
    RECURRING_INSTANCE_RETRY = "recurring_instance.retry"
    EXCHANGE_RATE_VERSION_PUBLISH = "exchange_rate_version.publish"
    BANK_IMPORT_CREATE = "bank_import.create"
    BANK_IMPORT_DRAFT = "bank_import.draft"
    BANK_IMPORT_LINK = "bank_import.link"
    BANK_IMPORT_SKIP = "bank_import.skip"
    BANK_IMPORT_CANCEL = "bank_import.cancel"
    DRAFT_CREATE = "draft.create"
    DRAFT_UPDATE = "draft.update"
    DRAFT_CONFIRM = "draft.confirm"
    DRAFT_CANCEL = "draft.cancel"
    DRAFT_RESUME = "draft.resume"
    DRAFT_REPLACE = "draft.replace"
    TRANSACTION_REPEAT = "transaction.repeat"
    TRANSACTION_EDIT_DRAFT = "transaction.edit_draft"
    TRANSACTION_DELETE = "transaction.delete"
    TRANSACTION_RESTORE = "transaction.restore"


class IdempotencyKeyReuseError(RuntimeError):
    pass


class IdempotencyInProgressError(RuntimeError):
    pass


class InvalidStoredMutationResultError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class MutationCredentials:
    session_token: str = field(repr=False)
    csrf_cookie: str = field(repr=False)
    csrf_header: str = field(repr=False)
    idempotency_key: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class MutationReceipt:
    http_status: int
    kind: IdempotencyResultKind
    result_id: UUID | None = field(default=None, repr=False)
    revision: int | None = field(default=None, repr=False)
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IdempotencyResultKind):
            raise TypeError("mutation receipt kind is invalid")
        IdempotencyResult(
            http_status=self.http_status,
            kind=self.kind,
            result_id=self.result_id,
            revision=self.revision,
        )
        if type(self.replayed) is not bool:
            raise TypeError("mutation replay flag must be boolean")
        if self.revision is not None and self.revision > 2**31 - 1:
            raise ValueError("mutation receipt revision is out of range")

    def stored(self) -> IdempotencyResult:
        return IdempotencyResult(
            http_status=self.http_status,
            kind=self.kind,
            result_id=self.result_id,
            revision=self.revision,
        )


type MutationCallback = Callable[[MutationUnitOfWork, UUID], Awaitable[MutationReceipt]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_request(
    operation: MutationOperation,
    semantic_request: Mapping[str, object],
) -> bytes:
    try:
        return json.dumps(
            {
                "body": semantic_request,
                "operation": operation.value,
                "version": 1,
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("mutation fingerprint input is not canonical JSON") from exc


class HttpMutationExecutor:
    """Authenticate, claim, mutate and complete inside one database transaction."""

    __slots__ = ("_authenticator", "_clock", "_digester", "_uow_factory")

    def __init__(
        self,
        *,
        digester: HttpSecurityDigester,
        uow_factory: MutationUnitOfWorkFactory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._digester = digester
        self._authenticator = SessionAuthenticator(digester)
        self._uow_factory = uow_factory
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mutation clock must be timezone-aware")
        return value.astimezone(UTC)

    async def execute(
        self,
        credentials: MutationCredentials,
        *,
        operation: MutationOperation,
        semantic_request: Mapping[str, object],
        allowed_results: frozenset[tuple[int, IdempotencyResultKind]],
        mutate: MutationCallback,
    ) -> MutationReceipt:
        now = self._now()
        key = self._digester.idempotency_key(credentials.idempotency_key)
        fingerprint = self._digester.request_fingerprint(
            _canonical_request(operation, semantic_request)
        )
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_mutation(
                uow.auth,
                credentials.session_token,
                credentials.csrf_cookie,
                credentials.csrf_header,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            # Every existing domain repository locks owner before draft/transaction rows.
            # Taking it before the idempotency row keeps one global lock order.
            await uow.lock_owner(owner_id)
            try:
                claim = await uow.idempotency.claim(
                    owner_id,
                    key,
                    fingerprint,
                    operation=operation.value,
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            except IdempotencyKeyConflictError as exc:
                raise IdempotencyKeyReuseError from exc
            except (IdempotencyStateError, TypeError, ValueError) as exc:
                raise InvalidStoredMutationResultError from exc

            if claim.status is IdempotencyClaimStatus.REPLAY:
                if (
                    claim.result is None
                    or (
                        claim.result.http_status,
                        claim.result.kind,
                    )
                    not in allowed_results
                ):
                    raise InvalidStoredMutationResultError
                try:
                    return MutationReceipt(
                        http_status=claim.result.http_status,
                        kind=claim.result.kind,
                        result_id=claim.result.result_id,
                        revision=claim.result.revision,
                        replayed=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise InvalidStoredMutationResultError from exc
            if claim.status is IdempotencyClaimStatus.IN_PROGRESS:
                raise IdempotencyInProgressError

            receipt = await mutate(uow, owner_id)
            if (receipt.http_status, receipt.kind) not in allowed_results or receipt.replayed:
                raise InvalidStoredMutationResultError
            try:
                await uow.idempotency.complete(
                    owner_id,
                    claim,
                    receipt.stored(),
                    completed_at=self._now(),
                )
            except IdempotencyStateError as exc:
                raise InvalidStoredMutationResultError from exc
        return receipt

    async def active_draft(self, raw_session_token: str) -> DraftSnapshot | None:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=now,
            )
            return await uow.drafts.get_active(authenticated.owner.owner_id)

    async def draft(self, raw_session_token: str, draft_id: UUID) -> DraftSnapshot:
        current = await self.active_draft(raw_session_token)
        if current is None or current.draft_id != draft_id:
            raise EntityNotFoundError("Черновик не найден")
        return current


_DRAFT_CREATE_RESULTS = frozenset(
    {
        (200, IdempotencyResultKind.DRAFT),
        (201, IdempotencyResultKind.DRAFT),
    }
)
_DRAFT_PATCH_RESULTS = frozenset(
    {
        (200, IdempotencyResultKind.DRAFT),
        (200, IdempotencyResultKind.TRANSACTION),
        (204, IdempotencyResultKind.NONE),
    }
)
_DRAFT_RESULTS = frozenset({(200, IdempotencyResultKind.DRAFT)})
_CONFIRM_RESULTS = frozenset({(201, IdempotencyResultKind.TRANSACTION)})
_NONE_RESULTS = frozenset({(204, IdempotencyResultKind.NONE)})
_TRANSACTION_RESULTS = frozenset({(200, IdempotencyResultKind.TRANSACTION)})


def _draft_receipt(status: int, draft: DraftSnapshot) -> MutationReceipt:
    return MutationReceipt(
        http_status=status,
        kind=IdempotencyResultKind.DRAFT,
        result_id=draft.draft_id,
        revision=draft.revision,
    )


def _stored_receipt(result: IdempotencyResult) -> MutationReceipt:
    return MutationReceipt(
        http_status=result.http_status,
        kind=result.kind,
        result_id=result.result_id,
        revision=result.revision,
    )


def _patch_semantics(spec: DraftPatchSpec) -> dict[str, object]:
    semantic: dict[str, object] = {
        "action": spec.action.value,
        "draft_id": str(spec.expected.draft_id),
        "revision": spec.expected.revision,
    }
    if spec.text is not None:
        semantic["text"] = spec.text
    if spec.value is not None:
        semantic["value"] = spec.value.value
    if isinstance(spec.selection, DraftExistingSelection):
        semantic["selection"] = {
            "id": str(spec.selection.entity_id),
            "kind": "existing",
            "version": spec.selection.version,
        }
    elif spec.selection is not None:
        semantic["selection"] = {"kind": spec.selection.value}
    return semantic


class HttpRevisionMutationService:
    """Closed Task 14 facade; routes cannot invoke generic domain mutations."""

    __slots__ = ("_executor",)

    def __init__(self, executor: HttpMutationExecutor) -> None:
        self._executor = executor

    async def active_draft(self, raw_session_token: str) -> DraftSnapshot | None:
        return await self._executor.active_draft(raw_session_token)

    async def draft(self, raw_session_token: str, draft_id: UUID) -> DraftSnapshot:
        return await self._executor.draft(raw_session_token, draft_id)

    async def create_draft(self, credentials: MutationCredentials) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            status, draft = await uow.commands.create_draft(owner_id)
            return _draft_receipt(status, draft)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_CREATE,
            semantic_request={},
            allowed_results=_DRAFT_CREATE_RESULTS,
            mutate=mutate,
        )

    async def patch_draft(
        self,
        credentials: MutationCredentials,
        spec: DraftPatchSpec,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.commands.patch_draft(spec.bind(owner_id))
            return _patch_receipt(result)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_UPDATE,
            semantic_request=_patch_semantics(spec),
            allowed_results=_DRAFT_PATCH_RESULTS,
            mutate=mutate,
        )

    async def confirm_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            result = await uow.commands.confirm_draft(
                owner_id,
                expected.draft_id,
                expected.revision,
            )
            return _stored_receipt(result)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_CONFIRM,
            semantic_request=_draft_ref_semantics(expected),
            allowed_results=_CONFIRM_RESULTS,
            mutate=mutate,
        )

    async def cancel_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            await uow.commands.cancel_draft(
                owner_id,
                expected.draft_id,
                expected.revision,
            )
            return MutationReceipt(204, IdempotencyResultKind.NONE)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_CANCEL,
            semantic_request=_draft_ref_semantics(expected),
            allowed_results=_NONE_RESULTS,
            mutate=mutate,
        )

    async def resume_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        return await self._resolve_draft(credentials, expected, replace=False)

    async def replace_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
    ) -> MutationReceipt:
        return await self._resolve_draft(credentials, expected, replace=True)

    async def _resolve_draft(
        self,
        credentials: MutationCredentials,
        expected: DraftRef,
        *,
        replace: bool,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            draft = await uow.commands.resolve_draft(
                owner_id,
                expected.draft_id,
                expected.revision,
                replace=replace,
            )
            return _draft_receipt(200, draft)

        return await self._executor.execute(
            credentials,
            operation=(
                MutationOperation.DRAFT_REPLACE if replace else MutationOperation.DRAFT_RESUME
            ),
            semantic_request=_draft_ref_semantics(expected),
            allowed_results=_DRAFT_RESULTS,
            mutate=mutate,
        )

    async def repeat_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            status, draft = await uow.commands.repeat_transaction(
                owner_id,
                transaction_id,
                version,
            )
            return _draft_receipt(status, draft)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.TRANSACTION_REPEAT,
            semantic_request=_transaction_semantics(transaction_id, version),
            allowed_results=_DRAFT_CREATE_RESULTS,
            mutate=mutate,
        )

    async def begin_transaction_edit(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            status, draft = await uow.commands.begin_transaction_edit(
                owner_id,
                transaction_id,
                version,
            )
            return _draft_receipt(status, draft)

        return await self._executor.execute(
            credentials,
            operation=MutationOperation.TRANSACTION_EDIT_DRAFT,
            semantic_request=_transaction_semantics(transaction_id, version),
            allowed_results=_DRAFT_CREATE_RESULTS,
            mutate=mutate,
        )

    async def delete_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._transaction_lifecycle(
            credentials,
            transaction_id,
            version,
            restore=False,
        )

    async def restore_transaction(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._transaction_lifecycle(
            credentials,
            transaction_id,
            version,
            restore=True,
        )

    async def _transaction_lifecycle(
        self,
        credentials: MutationCredentials,
        transaction_id: UUID,
        version: int,
        *,
        restore: bool,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            command = (
                uow.commands.restore_transaction if restore else uow.commands.delete_transaction
            )
            result = await command(owner_id, transaction_id, version)
            return _stored_receipt(result)

        return await self._executor.execute(
            credentials,
            operation=(
                MutationOperation.TRANSACTION_RESTORE
                if restore
                else MutationOperation.TRANSACTION_DELETE
            ),
            semantic_request=_transaction_semantics(transaction_id, version),
            allowed_results=_TRANSACTION_RESULTS,
            mutate=mutate,
        )


def _patch_receipt(result: DraftPatchResult) -> MutationReceipt:
    if result.closed:
        return MutationReceipt(204, IdempotencyResultKind.NONE)
    if result.draft is not None:
        return _draft_receipt(200, result.draft)
    if result.transaction is not None:
        return MutationReceipt(
            200,
            IdempotencyResultKind.TRANSACTION,
            result.transaction.entity_id,
            result.transaction.version,
        )
    raise InvalidStoredMutationResultError


def _draft_ref_semantics(expected: DraftRef) -> dict[str, object]:
    return {
        "draft_id": str(expected.draft_id),
        "revision": expected.revision,
    }


def _transaction_semantics(transaction_id: UUID, version: int) -> dict[str, object]:
    return {
        "transaction_id": str(transaction_id),
        "version": version,
    }
