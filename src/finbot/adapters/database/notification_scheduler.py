from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.adapters.database.repositories.notifications import (
    SqlAlchemyNotificationRepository,
)
from finbot.application.budgets import MAX_BUDGET_PAGE_SIZE, BudgetCursor
from finbot.application.errors import EntityNotFoundError
from finbot.application.notifications import (
    MAX_NOTIFICATION_OWNERS_PER_TICK,
    NotificationDigest,
    NotificationIntent,
    budget_notification_kind,
)
from finbot.application.use_cases.budgets import ListBudgetProgress
from finbot.domain.notifications import NotificationKind, latest_weekly_slot

_SCHEDULER_LOCK_KEY = 5_644_489_200_043_922_227
_MAX_BUDGET_PAGES_PER_OWNER = 4


@dataclass(frozen=True, slots=True)
class NotificationSchedulerTickResult:
    owners: int
    queued: int
    skipped: int


async def _set_limits(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    await session.execute(text("SET LOCAL statement_timeout = '10s'"))


async def _try_lock(session: AsyncSession) -> bool:
    return (
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": _SCHEDULER_LOCK_KEY},
        )
    ) is True


class SqlAlchemyNotificationScheduler:
    """Bounded producer; financial values remain in the read transaction only."""

    __slots__ = ("_cursor", "_digest", "_sessions")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        digest: NotificationDigest,
    ) -> None:
        self._sessions = sessions
        self._digest = digest
        self._cursor: UUID | None = None

    async def tick(self, now: datetime | None = None) -> NotificationSchedulerTickResult:
        measured_at = (now or datetime.now(UTC)).astimezone(UTC)
        async with self._sessions() as session, session.begin():
            await _set_limits(session)
            if not await _try_lock(session):
                return NotificationSchedulerTickResult(owners=0, queued=0, skipped=1)
            repository = SqlAlchemyNotificationRepository(
                session,
                self._digest,
            )
            owners = await repository.list_scheduler_owners(after_owner_id=self._cursor)
            if not owners and self._cursor is not None:
                self._cursor = None
                owners = await repository.list_scheduler_owners(after_owner_id=None)

        if owners:
            self._cursor = (
                owners[-1].owner_id if len(owners) == MAX_NOTIFICATION_OWNERS_PER_TICK else None
            )

        queued = 0
        skipped = 0
        for candidate in owners:
            try:
                queued += await self._schedule_owner(candidate.owner_id, measured_at)
            except EntityNotFoundError, RuntimeError, ValueError:
                skipped += 1
        return NotificationSchedulerTickResult(
            owners=len(owners),
            queued=queued,
            skipped=skipped,
        )

    async def _schedule_owner(self, owner_id: UUID, now: datetime) -> int:
        async with self._sessions() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await _set_limits(session)
            notifications = SqlAlchemyNotificationRepository(session, self._digest)
            snapshot = await notifications.get_preferences(owner_id)
            preferences = snapshot.preferences
            queued = 0

            if preferences.budget_80_enabled or preferences.budget_100_enabled:
                try:
                    timezone = ZoneInfo(snapshot.timezone)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError("notification owner timezone is invalid") from exc
                local_day = now.astimezone(timezone).date()
                cursor: BudgetCursor | None = None
                for page_number in range(_MAX_BUDGET_PAGES_PER_OWNER):
                    page = await ListBudgetProgress(SqlAlchemyBudgetRepository(session))(
                        owner_id,
                        window_start=local_day,
                        window_end=local_day,
                        deleted=False,
                        cursor=cursor,
                        limit=MAX_BUDGET_PAGE_SIZE,
                        as_of=now,
                    )
                    for progress in page.items:
                        kind = budget_notification_kind(
                            spent_minor=progress.progress.spent_minor,
                            limit_minor=progress.budget.definition.limit_minor,
                            preferences=preferences,
                        )
                        if kind is None:
                            continue
                        period = (
                            f"{progress.budget.definition.starts_on.isoformat()}_"
                            f"{progress.budget.definition.ends_on.isoformat()}"
                        )
                        queued += await notifications.enqueue(
                            NotificationIntent(
                                owner_id=owner_id,
                                kind=kind,
                                reference_id=progress.budget.budget_id,
                                period_key=period,
                            ),
                            available_at=now,
                        )
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if page_number == _MAX_BUDGET_PAGES_PER_OWNER - 1:
                        raise RuntimeError("notification budget scan exceeded its bound")

            if preferences.recurring_ready_enabled:
                for instance_id in await notifications.list_recurring_ready_refs(owner_id):
                    queued += await notifications.enqueue(
                        NotificationIntent(
                            owner_id=owner_id,
                            kind=NotificationKind.RECURRING_READY,
                            reference_id=instance_id,
                            period_key="generated",
                        ),
                        available_at=now,
                    )

            if preferences.weekly_digest_enabled:
                slot = latest_weekly_slot(
                    preferences,
                    timezone=snapshot.timezone,
                    now=now,
                )
                queued += await notifications.enqueue(
                    NotificationIntent(
                        owner_id=owner_id,
                        kind=NotificationKind.WEEKLY_DIGEST,
                        period_key=slot.period_key,
                    ),
                    available_at=slot.due_at,
                )
            return queued
