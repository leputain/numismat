import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid7

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import HttpIdempotencyRecord
from finbot.adapters.database.repositories.security_values import (
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
)

MAX_HTTP_IDEMPOTENCY_TTL = timedelta(hours=24)
MAX_CLEANUP_BATCH = 500
_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


class IdempotencyKeyConflictError(RuntimeError):
    pass


class IdempotencyStateError(RuntimeError):
    pass


class IdempotencyClaimStatus(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"


class IdempotencyResultKind(StrEnum):
    NONE = "none"
    DRAFT = "draft"
    TRANSACTION = "transaction"
    ACCOUNT = "account"
    CATEGORY = "category"
    BUDGET = "budget"
    RECURRING_SCHEDULE = "recurring_schedule"
    RECURRING_INSTANCE = "recurring_instance"
    EXCHANGE_RATE_VERSION = "exchange_rate_version"
    BANK_IMPORT_BATCH = "bank_import_batch"
    BANK_IMPORT_ROW = "bank_import_row"


@dataclass(frozen=True, slots=True, repr=False)
class IdempotencyResult:
    http_status: int
    kind: IdempotencyResultKind
    result_id: UUID | None = None
    revision: int | None = None

    def __post_init__(self) -> None:
        if type(self.http_status) is not int or not 200 <= self.http_status <= 299:
            raise ValueError("idempotency result status must be successful HTTP status")
        if self.kind is IdempotencyResultKind.NONE:
            if self.result_id is not None or self.revision is not None:
                raise ValueError("empty idempotency result cannot reference an entity")
            return
        if self.result_id is None or type(self.revision) is not int or self.revision < 1:
            raise ValueError("entity idempotency result requires id and positive revision")


@dataclass(frozen=True, slots=True, repr=False)
class IdempotencyClaim:
    record_id: UUID
    status: IdempotencyClaimStatus
    result: IdempotencyResult | None = None

    def __post_init__(self) -> None:
        if (self.status is IdempotencyClaimStatus.REPLAY) != (self.result is not None):
            raise ValueError("only replay claims carry a stored result")


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_lifetime(created_at: datetime, expires_at: datetime) -> None:
    _require_aware(created_at, field="created_at")
    _require_aware(expires_at, field="expires_at")
    if expires_at <= created_at or expires_at > created_at + MAX_HTTP_IDEMPOTENCY_TTL:
        raise ValueError("idempotency expiry must be within 24 hours")


def _validate_operation(operation: str) -> None:
    if type(operation) is not str or _OPERATION_PATTERN.fullmatch(operation) is None:
        raise ValueError("invalid idempotency operation code")


def _validate_cleanup_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_CLEANUP_BATCH:
        raise ValueError(f"cleanup limit must be between 1 and {MAX_CLEANUP_BATCH}")


def _stored_result(record: HttpIdempotencyRecord) -> IdempotencyResult:
    if record.http_status is None or record.result_kind is None:
        raise IdempotencyStateError("completed idempotency record is inconsistent")
    return IdempotencyResult(
        http_status=record.http_status,
        kind=IdempotencyResultKind(record.result_kind),
        result_id=record.result_id,
        revision=record.result_revision,
    )


class SqlAlchemyHttpIdempotencyRepository:
    """No-commit claim/replay storage for atomic HTTP mutations."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        owner_id: UUID,
        key: IdempotencyKeyDigest,
        fingerprint: RequestFingerprintDigest,
        *,
        operation: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> IdempotencyClaim:
        _validate_operation(operation)
        _validate_lifetime(created_at, expires_at)
        key_hash = key.database_value()
        await self._session.execute(
            delete(HttpIdempotencyRecord).where(
                HttpIdempotencyRecord.user_id == owner_id,
                HttpIdempotencyRecord.idempotency_key_hash == key_hash,
                HttpIdempotencyRecord.expires_at <= created_at,
            )
        )

        record_id = uuid7()
        inserted_id = (
            await self._session.execute(
                insert(HttpIdempotencyRecord)
                .values(
                    id=record_id,
                    user_id=owner_id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=fingerprint.database_value(),
                    operation=operation,
                    status="in_progress",
                    created_at=created_at,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(constraint="uq_http_idempotency_owner_key_hash")
                .returning(HttpIdempotencyRecord.id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            return IdempotencyClaim(record_id=inserted_id, status=IdempotencyClaimStatus.NEW)

        existing = await self._session.scalar(
            select(HttpIdempotencyRecord)
            .where(
                HttpIdempotencyRecord.user_id == owner_id,
                HttpIdempotencyRecord.idempotency_key_hash == key_hash,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is None:  # pragma: no cover - protected by the unique constraint
            raise IdempotencyStateError("idempotency claim disappeared")
        if existing.operation != operation or not fingerprint.matches(existing.request_fingerprint):
            raise IdempotencyKeyConflictError("idempotency key was reused for another request")
        if existing.status == "completed":
            return IdempotencyClaim(
                record_id=existing.id,
                status=IdempotencyClaimStatus.REPLAY,
                result=_stored_result(existing),
            )
        if existing.status != "in_progress":  # pragma: no cover - database constraint
            raise IdempotencyStateError("idempotency record has unknown state")
        return IdempotencyClaim(
            record_id=existing.id,
            status=IdempotencyClaimStatus.IN_PROGRESS,
        )

    async def complete(
        self,
        owner_id: UUID,
        claim: IdempotencyClaim,
        result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None:
        _require_aware(completed_at, field="completed_at")
        if claim.status is not IdempotencyClaimStatus.NEW:
            raise IdempotencyStateError("only a new idempotency claim can be completed")
        record = await self._session.scalar(
            select(HttpIdempotencyRecord)
            .where(
                HttpIdempotencyRecord.id == claim.record_id,
                HttpIdempotencyRecord.user_id == owner_id,
            )
            .with_for_update()
        )
        if record is None or record.status != "in_progress":
            raise IdempotencyStateError("idempotency claim is not completable")
        if completed_at < record.created_at or completed_at > record.expires_at:
            raise IdempotencyStateError("idempotency claim expired before completion")
        record.status = "completed"
        record.http_status = result.http_status
        record.result_kind = result.kind.value
        record.result_id = result.result_id
        record.result_revision = result.revision
        record.completed_at = completed_at
        await self._session.flush()

    async def cleanup(self, *, now: datetime, limit: int = MAX_CLEANUP_BATCH) -> int:
        _require_aware(now, field="now")
        _validate_cleanup_limit(limit)
        candidates = (
            select(HttpIdempotencyRecord.id)
            .where(HttpIdempotencyRecord.expires_at <= now)
            .order_by(HttpIdempotencyRecord.expires_at, HttpIdempotencyRecord.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        deleted = await self._session.scalars(
            delete(HttpIdempotencyRecord)
            .where(HttpIdempotencyRecord.id.in_(candidates))
            .returning(HttpIdempotencyRecord.id)
        )
        return len(deleted.all())
