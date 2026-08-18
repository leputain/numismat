from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import WebSession
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    SessionTokenDigest,
)

MAX_WEB_SESSION_TTL = timedelta(hours=24)
MAX_CLEANUP_BATCH = 500


@dataclass(frozen=True, slots=True, repr=False)
class WebSessionSnapshot:
    session_id: UUID
    owner_id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_lifetime(created_at: datetime, expires_at: datetime) -> None:
    _require_aware(created_at, field="created_at")
    _require_aware(expires_at, field="expires_at")
    if expires_at <= created_at or expires_at > created_at + MAX_WEB_SESSION_TTL:
        raise ValueError("web session expiry must be within 24 hours")


def _validate_cleanup_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_CLEANUP_BATCH:
        raise ValueError(f"cleanup limit must be between 1 and {MAX_CLEANUP_BATCH}")


def _snapshot(record: WebSession) -> WebSessionSnapshot:
    return WebSessionSnapshot(
        session_id=record.id,
        owner_id=record.user_id,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
    )


class SqlAlchemyWebSessionRepository:
    """No-commit repository for short-lived opaque web sessions."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        owner_id: UUID,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> WebSessionSnapshot:
        _validate_lifetime(created_at, expires_at)
        session_hash = session_token.database_value()
        csrf_hash = csrf_token.database_value()
        if session_token.matches(csrf_hash):
            raise ValueError("session and CSRF digests must be distinct")
        record = WebSession(
            id=uuid7(),
            user_id=owner_id,
            session_token_hash=session_hash,
            csrf_token_hash=csrf_hash,
            created_at=created_at,
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        return _snapshot(record)

    async def get_active(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> WebSessionSnapshot | None:
        _require_aware(now, field="now")
        record = await self._session.scalar(
            select(WebSession).where(
                WebSession.session_token_hash == session_token.database_value(),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
        )
        return _snapshot(record) if record is not None else None

    async def lock_active_for_mutation(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> WebSessionSnapshot | None:
        _require_aware(now, field="now")
        record = await self._session.scalar(
            select(WebSession)
            .where(
                WebSession.session_token_hash == session_token.database_value(),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
            .with_for_update(read=True)
        )
        if record is None or not csrf_token.matches(record.csrf_token_hash):
            return None
        return _snapshot(record)

    async def revoke(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        revoked_at: datetime,
    ) -> bool:
        _require_aware(revoked_at, field="revoked_at")
        record = await self._session.scalar(
            select(WebSession)
            .where(
                WebSession.session_token_hash == session_token.database_value(),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > revoked_at,
            )
            .with_for_update()
        )
        if record is None or not csrf_token.matches(record.csrf_token_hash):
            return False
        record.revoked_at = revoked_at
        await self._session.flush()
        return True

    async def cleanup(self, *, now: datetime, limit: int = MAX_CLEANUP_BATCH) -> int:
        _require_aware(now, field="now")
        _validate_cleanup_limit(limit)
        candidates = (
            select(WebSession.id)
            .where(or_(WebSession.expires_at <= now, WebSession.revoked_at.is_not(None)))
            .order_by(WebSession.expires_at, WebSession.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        deleted = await self._session.scalars(
            delete(WebSession).where(WebSession.id.in_(candidates)).returning(WebSession.id)
        )
        return len(deleted.all())
