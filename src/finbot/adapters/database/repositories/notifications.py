from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    NotificationJob,
    NotificationPreference,
    RecurringInstance,
    User,
)
from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.application.errors import EntityNotFoundError, ObjectVersionConflictError
from finbot.application.notifications import (
    MAX_NOTIFICATION_ATTEMPTS,
    MAX_NOTIFICATION_CLAIM_BATCH,
    MAX_NOTIFICATION_CLEANUP_BATCH,
    MAX_NOTIFICATION_OWNERS_PER_TICK,
    MAX_NOTIFICATION_REFS_PER_OWNER,
    NOTIFICATION_LEASE_SECONDS,
    NOTIFICATION_RETENTION_DAYS,
    NotificationDeliveryContext,
    NotificationDigest,
    NotificationFailureCode,
    NotificationIntent,
    NotificationJobSnapshot,
    NotificationJobStatus,
    NotificationPreferencesSnapshot,
    ReplaceNotificationPreferences,
    budget_notification_kind,
)
from finbot.application.use_cases.budgets import GetBudgetProgress
from finbot.domain.notifications import (
    NotificationKind,
    NotificationPreferences,
    quiet_until,
)

_MAX_VERSION = 2**31 - 1
_BASE_RETRY_DELAY = timedelta(seconds=30)
_MAX_RETRY_DELAY = timedelta(minutes=15)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("notification repository time must contain a timezone")
    return value.astimezone(UTC)


def _settings(row: NotificationPreference | None) -> NotificationPreferences:
    if row is None:
        return NotificationPreferences()
    return NotificationPreferences(
        budget_80_enabled=row.budget_80_enabled,
        budget_100_enabled=row.budget_100_enabled,
        recurring_ready_enabled=row.recurring_ready_enabled,
        weekly_digest_enabled=row.weekly_digest_enabled,
        quiet_start=row.quiet_start,
        quiet_end=row.quiet_end,
        weekly_weekday=row.weekly_weekday,
        weekly_time=row.weekly_time,
    )


def _preferences_snapshot(
    owner: User,
    row: NotificationPreference | None,
) -> NotificationPreferencesSnapshot:
    return NotificationPreferencesSnapshot(
        owner_id=owner.id,
        timezone=owner.timezone,
        preferences=_settings(row),
        version=row.version if row is not None else 0,
    )


def _job_snapshot(row: NotificationJob) -> NotificationJobSnapshot:
    return NotificationJobSnapshot(
        job_id=row.id,
        owner_id=row.user_id,
        kind=NotificationKind(row.kind),
        available_at=row.available_at,
        reference_id=row.reference_id,
        status=NotificationJobStatus(row.status),
        attempt_count=row.attempt_count,
        lease_token=row.lease_token,
    )


def _retry_delay(attempt_count: int) -> timedelta:
    delay = _BASE_RETRY_DELAY * (2 ** min(max(attempt_count - 1, 0), 5))
    return delay if delay <= _MAX_RETRY_DELAY else _MAX_RETRY_DELAY


class SqlAlchemyNotificationRepository:
    """Owner-scoped preferences and a privacy-safe lease queue; caller owns commit."""

    __slots__ = ("_digest", "_session")

    def __init__(
        self,
        session: AsyncSession,
        digest: NotificationDigest | None = None,
    ) -> None:
        self._session = session
        self._digest = digest

    async def get_preferences(self, owner_id: UUID) -> NotificationPreferencesSnapshot:
        owner = await self._session.get(User, owner_id)
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        row = await self._session.get(NotificationPreference, owner_id)
        return _preferences_snapshot(owner, row)

    async def replace_preferences(
        self,
        command: ReplaceNotificationPreferences,
    ) -> NotificationPreferencesSnapshot:
        owner = await self._session.scalar(
            select(User).where(User.id == command.owner_id).with_for_update()
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        row = await self._session.scalar(
            select(NotificationPreference)
            .where(NotificationPreference.user_id == command.owner_id)
            .with_for_update()
        )
        current_version = row.version if row is not None else 0
        if command.expected_version != current_version:
            raise ObjectVersionConflictError(current_version=current_version)
        if current_version >= _MAX_VERSION:
            raise RuntimeError("notification preference version exhausted")
        settings = command.preferences
        now = datetime.now(UTC)
        if row is None:
            row = NotificationPreference(user_id=command.owner_id, version=1, created_at=now)
            self._session.add(row)
        else:
            row.version += 1
        row.budget_80_enabled = settings.budget_80_enabled
        row.budget_100_enabled = settings.budget_100_enabled
        row.recurring_ready_enabled = settings.recurring_ready_enabled
        row.weekly_digest_enabled = settings.weekly_digest_enabled
        row.quiet_start = settings.quiet_start
        row.quiet_end = settings.quiet_end
        row.weekly_weekday = settings.weekly_weekday
        row.weekly_time = settings.weekly_time
        row.updated_at = now
        await self._session.flush()
        return _preferences_snapshot(owner, row)

    async def list_scheduler_owners(
        self,
        *,
        after_owner_id: UUID | None,
    ) -> tuple[NotificationPreferencesSnapshot, ...]:
        statement = (
            select(User, NotificationPreference)
            .join(NotificationPreference, NotificationPreference.user_id == User.id)
            .where(
                or_(
                    NotificationPreference.budget_80_enabled.is_(True),
                    NotificationPreference.budget_100_enabled.is_(True),
                    NotificationPreference.recurring_ready_enabled.is_(True),
                    NotificationPreference.weekly_digest_enabled.is_(True),
                ),
            )
            .order_by(User.id)
            .limit(MAX_NOTIFICATION_OWNERS_PER_TICK)
        )
        if after_owner_id is not None:
            statement = statement.where(User.id > after_owner_id)
        rows = tuple((await self._session.execute(statement)).all())
        return tuple(_preferences_snapshot(owner, preference) for owner, preference in rows)

    async def list_recurring_ready_refs(self, owner_id: UUID) -> tuple[UUID, ...]:
        rows = tuple(
            await self._session.scalars(
                select(RecurringInstance.id)
                .where(
                    RecurringInstance.user_id == owner_id,
                    RecurringInstance.status == "generated",
                    RecurringInstance.draft_id.is_not(None),
                )
                .order_by(RecurringInstance.generated_at, RecurringInstance.id)
                .limit(MAX_NOTIFICATION_REFS_PER_OWNER + 1)
            )
        )
        if len(rows) > MAX_NOTIFICATION_REFS_PER_OWNER:
            raise RuntimeError("notification recurring batch exceeded its bound")
        return rows

    async def enqueue(self, intent: NotificationIntent, *, available_at: datetime) -> bool:
        queued_at = _utc(available_at)
        if self._digest is None:
            raise RuntimeError("notification digest is required for enqueue")
        statement = (
            insert(NotificationJob)
            .values(
                id=uuid7(),
                user_id=intent.owner_id,
                kind=intent.kind.value,
                reference_id=intent.reference_id,
                dedupe_digest=self._digest.for_intent(intent),
                status=NotificationJobStatus.PENDING.value,
                attempt_count=0,
                available_at=queued_at,
                created_at=queued_at,
                updated_at=queued_at,
            )
            .on_conflict_do_nothing(index_elements=[NotificationJob.dedupe_digest])
            .returning(NotificationJob.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def claim_due(self, *, now: datetime) -> tuple[NotificationJobSnapshot, ...]:
        claimed_at = _utc(now)
        rows = tuple(
            await self._session.scalars(
                select(NotificationJob)
                .where(
                    or_(
                        (
                            (NotificationJob.status == NotificationJobStatus.PENDING.value)
                            & (NotificationJob.available_at <= claimed_at)
                        ),
                        (
                            (NotificationJob.status == NotificationJobStatus.LEASED.value)
                            & (NotificationJob.lease_until <= claimed_at)
                        ),
                    )
                )
                .order_by(NotificationJob.available_at, NotificationJob.id)
                .with_for_update(skip_locked=True)
                .limit(MAX_NOTIFICATION_CLAIM_BATCH)
                .execution_options(populate_existing=True)
            ),
        )
        lease_until = claimed_at + timedelta(seconds=NOTIFICATION_LEASE_SECONDS)
        for row in rows:
            row.status = NotificationJobStatus.LEASED.value
            row.lease_token = uuid7()
            row.lease_until = lease_until
            row.updated_at = claimed_at
        await self._session.flush()
        return tuple(_job_snapshot(row) for row in rows)

    async def prepare_delivery(
        self,
        job: NotificationJobSnapshot,
        *,
        now: datetime,
        allowed_telegram_user_ids: frozenset[int],
    ) -> NotificationDeliveryContext | None:
        prepared_at = _utc(now)
        owner = await self._session.scalar(
            select(User).where(User.id == job.owner_id).with_for_update(read=True)
        )
        row = await self._leased_row(job)
        if row is None or owner is None:
            return None
        preference = await self._session.scalar(
            select(NotificationPreference)
            .where(NotificationPreference.user_id == row.user_id)
            .with_for_update(read=True)
        )
        if owner.telegram_user_id not in allowed_telegram_user_ids:
            self._terminal(row, NotificationFailureCode.OWNER_REVOKED, prepared_at)
            await self._session.flush()
            return None
        if (
            owner.telegram_chat_id is None
            or owner.telegram_user_id <= 0
            or owner.telegram_chat_id != owner.telegram_user_id
        ):
            self._terminal(row, NotificationFailureCode.CHAT_INVALID, prepared_at)
            await self._session.flush()
            return None
        settings = _settings(preference)
        if not settings.enabled_for(NotificationKind(row.kind)):
            self._terminal(row, NotificationFailureCode.PREFERENCE_DISABLED, prepared_at)
            await self._session.flush()
            return None
        if not await self._reference_is_current(row, settings=settings, now=prepared_at):
            self._terminal(row, NotificationFailureCode.REFERENCE_INVALID, prepared_at)
            await self._session.flush()
            return None
        if row.attempt_count >= MAX_NOTIFICATION_ATTEMPTS:
            self._terminal(row, NotificationFailureCode.RETRY_EXHAUSTED, prepared_at)
            await self._session.flush()
            return None
        postponed_until = quiet_until(settings, timezone=owner.timezone, now=prepared_at)
        if postponed_until is not None:
            row.status = NotificationJobStatus.PENDING.value
            row.available_at = postponed_until
            row.lease_token = None
            row.lease_until = None
            row.updated_at = prepared_at
            await self._session.flush()
            return None
        row.attempt_count += 1
        row.updated_at = prepared_at
        await self._session.flush()
        return NotificationDeliveryContext(
            job=_job_snapshot(row),
            telegram_user_id=owner.telegram_user_id,
            telegram_chat_id=owner.telegram_chat_id,
            timezone=owner.timezone,
            preferences=settings,
        )

    async def mark_delivered(
        self,
        job: NotificationJobSnapshot,
        *,
        now: datetime,
    ) -> bool:
        completed_at = _utc(now)
        row = await self._leased_row(job)
        if row is None:
            return False
        row.status = NotificationJobStatus.DELIVERED.value
        row.lease_token = None
        row.lease_until = None
        row.failure_code = None
        row.delivered_at = completed_at
        row.updated_at = completed_at
        await self._session.flush()
        return True

    async def defer_prepared_for_quiet_hours(
        self,
        job: NotificationJobSnapshot,
        *,
        now: datetime,
        available_at: datetime,
    ) -> bool:
        deferred_at = _utc(now)
        resume_at = _utc(available_at)
        if resume_at <= deferred_at:
            raise ValueError("notification quiet-hours deferral must be in the future")
        row = await self._leased_row(job)
        if row is None:
            return False
        if row.attempt_count != job.attempt_count or row.attempt_count <= 0:
            return False
        row.status = NotificationJobStatus.PENDING.value
        row.attempt_count -= 1
        row.available_at = resume_at
        row.lease_token = None
        row.lease_until = None
        row.failure_code = None
        row.updated_at = deferred_at
        await self._session.flush()
        return True

    async def mark_failed(
        self,
        job: NotificationJobSnapshot,
        *,
        now: datetime,
        retryable: bool,
    ) -> bool:
        failed_at = _utc(now)
        row = await self._leased_row(job)
        if row is None:
            return False
        row.lease_token = None
        row.lease_until = None
        row.updated_at = failed_at
        if retryable and row.attempt_count < MAX_NOTIFICATION_ATTEMPTS:
            row.status = NotificationJobStatus.PENDING.value
            row.available_at = failed_at + _retry_delay(row.attempt_count)
            row.failure_code = NotificationFailureCode.TELEGRAM_RETRYABLE.value
        else:
            row.status = NotificationJobStatus.FAILED.value
            row.failure_code = (
                NotificationFailureCode.RETRY_EXHAUSTED.value
                if retryable
                else NotificationFailureCode.TELEGRAM_REJECTED.value
            )
        await self._session.flush()
        return True

    async def cleanup_terminal_jobs(self, *, now: datetime) -> int:
        cutoff = _utc(now) - timedelta(days=NOTIFICATION_RETENTION_DAYS)
        terminal = (
            NotificationJobStatus.DELIVERED.value,
            NotificationJobStatus.FAILED.value,
        )
        candidate_ids = tuple(
            await self._session.scalars(
                select(NotificationJob.id)
                .where(
                    NotificationJob.status.in_(terminal),
                    NotificationJob.updated_at < cutoff,
                )
                .order_by(NotificationJob.updated_at, NotificationJob.id)
                .with_for_update(skip_locked=True)
                .limit(MAX_NOTIFICATION_CLEANUP_BATCH)
            )
        )
        if not candidate_ids:
            return 0
        deleted_ids = tuple(
            await self._session.scalars(
                delete(NotificationJob)
                .where(
                    NotificationJob.id.in_(candidate_ids),
                    NotificationJob.status.in_(terminal),
                    NotificationJob.updated_at < cutoff,
                )
                .returning(NotificationJob.id)
            )
        )
        if len(deleted_ids) > MAX_NOTIFICATION_CLEANUP_BATCH:
            raise RuntimeError("notification cleanup exceeded its bound")
        return len(deleted_ids)

    async def _leased_row(self, job: NotificationJobSnapshot) -> NotificationJob | None:
        if job.lease_token is None:
            return None
        return cast(
            NotificationJob | None,
            await self._session.scalar(
                select(NotificationJob)
                .where(
                    NotificationJob.id == job.job_id,
                    NotificationJob.user_id == job.owner_id,
                    NotificationJob.status == NotificationJobStatus.LEASED.value,
                    NotificationJob.lease_token == job.lease_token,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def _reference_is_current(
        self,
        row: NotificationJob,
        *,
        settings: NotificationPreferences,
        now: datetime,
    ) -> bool:
        kind = NotificationKind(row.kind)
        if kind is NotificationKind.WEEKLY_DIGEST:
            return row.reference_id is None
        if row.reference_id is None:
            return False
        if kind in (NotificationKind.BUDGET_80, NotificationKind.BUDGET_100):
            try:
                progress = await GetBudgetProgress(SqlAlchemyBudgetRepository(self._session))(
                    row.user_id,
                    row.reference_id,
                    as_of=now,
                )
            except EntityNotFoundError:
                return False
            if progress.budget.deleted_at is not None:
                return False
            current_kind = budget_notification_kind(
                spent_minor=progress.progress.spent_minor,
                limit_minor=progress.budget.definition.limit_minor,
                preferences=settings,
            )
            return current_kind is kind
        return (
            await self._session.scalar(
                select(RecurringInstance.id).where(
                    RecurringInstance.id == row.reference_id,
                    RecurringInstance.user_id == row.user_id,
                    RecurringInstance.status == "generated",
                    RecurringInstance.draft_id.is_not(None),
                )
            )
        ) is not None

    @staticmethod
    def _terminal(
        row: NotificationJob,
        failure: NotificationFailureCode,
        now: datetime,
    ) -> None:
        row.status = NotificationJobStatus.FAILED.value
        row.lease_token = None
        row.lease_until = None
        row.failure_code = failure.value
        row.updated_at = now
