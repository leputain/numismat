from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.notifications import (
    SqlAlchemyNotificationRepository,
)
from finbot.application.notifications import (
    STATIC_NOTIFICATION_TEXT,
    NotificationSender,
    NotificationSendFailure,
)
from finbot.domain.notifications import quiet_until

NOTIFICATION_CLEANUP_LOCK_KEY = 5_644_489_200_043_922_229


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("notification delivery time must contain a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class NotificationDeliveryTickResult:
    claimed: int
    delivered: int
    deferred: int
    failed: int


async def _set_limits(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))


async def _try_cleanup_lock(session: AsyncSession) -> bool:
    return (
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": NOTIFICATION_CLEANUP_LOCK_KEY},
        )
    ) is True


class SqlAlchemyNotificationDelivery:
    """Lease jobs in PostgreSQL and perform Telegram I/O outside transactions."""

    __slots__ = ("_allowed", "_clock", "_sender", "_sessions")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        sender: NotificationSender,
        *,
        allowed_telegram_user_ids: frozenset[int],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not allowed_telegram_user_ids or any(
            type(user_id) is not int or user_id <= 0 for user_id in allowed_telegram_user_ids
        ):
            raise ValueError("notification delivery allowlist is invalid")
        self._sessions = sessions
        self._sender = sender
        self._allowed = allowed_telegram_user_ids
        self._clock = clock

    async def tick(self, now: datetime | None = None) -> NotificationDeliveryTickResult:
        fixed_at = _as_utc(now) if now is not None else None
        await self.cleanup(fixed_at)
        claimed_at = self._timestamp(fixed_at)
        async with self._sessions() as session, session.begin():
            await _set_limits(session)
            claimed = await SqlAlchemyNotificationRepository(
                session,
            ).claim_due(now=claimed_at)

        delivered = 0
        deferred = 0
        failed = 0
        for job in claimed:
            prepared_at = self._timestamp(fixed_at)
            async with self._sessions() as session, session.begin():
                await _set_limits(session)
                prepared = await SqlAlchemyNotificationRepository(
                    session,
                ).prepare_delivery(
                    job,
                    now=prepared_at,
                    allowed_telegram_user_ids=self._allowed,
                )
            if prepared is None or prepared.telegram_chat_id is None:
                deferred += 1
                continue
            send_at = self._timestamp(fixed_at)
            postponed_until = quiet_until(
                prepared.preferences,
                timezone=prepared.timezone,
                now=send_at,
            )
            if postponed_until is not None:
                async with self._sessions() as session, session.begin():
                    await _set_limits(session)
                    await SqlAlchemyNotificationRepository(
                        session,
                    ).defer_prepared_for_quiet_hours(
                        prepared.job,
                        now=send_at,
                        available_at=postponed_until,
                    )
                deferred += 1
                continue
            try:
                await self._sender.send(
                    chat_id=prepared.telegram_chat_id,
                    text=STATIC_NOTIFICATION_TEXT[prepared.job.kind],
                )
            except NotificationSendFailure as exc:
                async with self._sessions() as session, session.begin():
                    await _set_limits(session)
                    changed = await SqlAlchemyNotificationRepository(
                        session,
                    ).mark_failed(
                        prepared.job,
                        now=self._timestamp(fixed_at),
                        retryable=exc.retryable,
                    )
                failed += int(changed)
                continue

            async with self._sessions() as session, session.begin():
                await _set_limits(session)
                changed = await SqlAlchemyNotificationRepository(
                    session,
                ).mark_delivered(prepared.job, now=self._timestamp(fixed_at))
            delivered += int(changed)
        return NotificationDeliveryTickResult(
            claimed=len(claimed),
            delivered=delivered,
            deferred=deferred,
            failed=failed,
        )

    async def cleanup(self, now: datetime | None = None) -> int:
        fixed_at = _as_utc(now) if now is not None else None
        cleaned_at = self._timestamp(fixed_at)
        async with self._sessions() as session, session.begin():
            await _set_limits(session)
            if not await _try_cleanup_lock(session):
                return 0
            return await SqlAlchemyNotificationRepository(session).cleanup_terminal_jobs(
                now=cleaned_at
            )

    def _timestamp(self, fixed_at: datetime | None) -> datetime:
        return fixed_at if fixed_at is not None else _as_utc(self._clock())
