from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    RecurringInstance,
    RecurringSchedule,
    User,
)
from finbot.application.dto import ReviewedTransactionInput
from finbot.application.recurring import (
    MAX_DUE_SCHEDULES_PER_TICK,
    MAX_PENDING_RECURRING_INSTANCES_PER_OWNER,
    MAX_STAGE_OWNERS_PER_TICK,
    RecurringFailureCode,
    RecurringInstanceStatus,
)
from finbot.domain.recurrence import (
    RecurrenceCadence,
    RecurrenceRule,
    recurrence_occurrence,
)
from finbot.domain.transactions import TransactionType

_MATERIALIZE_LOCK_KEY = 5_644_489_200_043_922_225
_STAGE_LOCK_KEY = 5_644_489_200_043_922_226
_PENDING_PROBE_LIMIT = MAX_PENDING_RECURRING_INSTANCES_PER_OWNER + 1
_BASE_BACKOFF = timedelta(minutes=15)
_MAX_BACKOFF = timedelta(hours=24)
_MAX_VERSION = 2**31 - 1
_LOGGER = logging.getLogger("finbot.database.recurring_runner")


@dataclass(frozen=True, slots=True)
class RecurringTickResult:
    materialized: int
    staged: int
    deferred: int
    blocked: int

    def __post_init__(self) -> None:
        for value in (self.materialized, self.staged, self.deferred, self.blocked):
            if type(value) is not int or value < 0:
                raise ValueError("recurring tick counters must be non-negative integers")


@dataclass(frozen=True, slots=True)
class _StageResult:
    staged: int = 0
    deferred: int = 0
    blocked: int = 0


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("recurring runner time must contain a timezone")
    return value.astimezone(UTC)


def _next_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise RuntimeError("recurring object version exhausted")
    return current + 1


async def _try_phase_lock(session: AsyncSession, key: int) -> bool:
    value = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": key},
    )
    return value is True


async def _set_phase_limits(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    await session.execute(text("SET LOCAL statement_timeout = '10s'"))


async def _lock_owner(session: AsyncSession, owner_id: UUID) -> User | None:
    return cast(
        User | None,
        await session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ),
    )


async def _pending_probe(session: AsyncSession, owner_id: UUID) -> int:
    rows = await session.scalars(
        select(RecurringInstance.id)
        .where(
            RecurringInstance.user_id == owner_id,
            RecurringInstance.status == RecurringInstanceStatus.PENDING.value,
        )
        .limit(_PENDING_PROBE_LIMIT)
    )
    return len(tuple(rows))


def _rule(schedule: RecurringSchedule) -> RecurrenceRule:
    return RecurrenceRule(
        cadence=RecurrenceCadence(schedule.cadence),
        interval=schedule.interval,
        anchor_date=schedule.anchor_date,
        local_time=schedule.local_time,
        timezone=schedule.timezone,
        ends_on=schedule.ends_on,
    )


def _advance_schedule(schedule: RecurringSchedule, now: datetime) -> None:
    schedule.next_occurrence_index += 1
    try:
        occurrence = recurrence_occurrence(_rule(schedule), schedule.next_occurrence_index)
    except ValueError:
        # There is no representable date after year 9999.  Treat that as a
        # completed finite calendar rather than retrying an impossible due item.
        occurrence = None
    if occurrence is None:
        schedule.next_due_local = None
        schedule.next_due_at = None
    else:
        schedule.next_due_local = occurrence.nominal_local
        schedule.next_due_at = occurrence.scheduled_for
    # Every materialized occurrence changes the client-visible schedule snapshot.
    # Exhaustion raises and rolls back the phase instead of accepting stale writes.
    schedule.version = _next_version(schedule.version)
    schedule.updated_at = now


async def _materialize_one(
    session: AsyncSession,
    schedule_id: UUID,
    now: datetime,
) -> bool:
    owner_id = await session.scalar(
        select(RecurringSchedule.user_id).where(RecurringSchedule.id == schedule_id)
    )
    if owner_id is None or await _lock_owner(session, owner_id) is None:
        return False
    schedule = await session.scalar(
        select(RecurringSchedule)
        .where(RecurringSchedule.id == schedule_id, RecurringSchedule.user_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        schedule is None
        or schedule.deleted_at is not None
        or schedule.paused_at is not None
        or schedule.next_due_at is None
        or schedule.next_due_at > now
    ):
        return False
    if await _pending_probe(session, owner_id) >= MAX_PENDING_RECURRING_INSTANCES_PER_OWNER:
        return False

    occurrence = recurrence_occurrence(_rule(schedule), schedule.next_occurrence_index)
    if (
        occurrence is None
        or occurrence.nominal_local != schedule.next_due_local
        or occurrence.scheduled_for != schedule.next_due_at
    ):
        schedule.paused_at = now
        schedule.pause_reason = RecurringFailureCode.SCHEDULE_INVALID.value
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = now
        return False

    instance = RecurringInstance(
        id=uuid7(),
        schedule_id=schedule.id,
        user_id=schedule.user_id,
        occurrence_index=occurrence.index,
        nominal_local=occurrence.nominal_local,
        scheduled_for=occurrence.scheduled_for,
        timezone=schedule.timezone,
        dst_adjusted=occurrence.dst_adjusted,
        type=schedule.type,
        amount_minor=schedule.amount_minor,
        currency=schedule.currency,
        account_id=schedule.account_id,
        category_id=schedule.category_id,
        description=schedule.description,
        status=RecurringInstanceStatus.PENDING.value,
        next_attempt_at=now,
        attempt_count=0,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(instance)
    _advance_schedule(schedule, now)
    await session.flush()
    return True


def _fair_due_schedule_query(now: datetime) -> Select[tuple[UUID]]:
    ranked_due = (
        select(
            RecurringSchedule.id.label("schedule_id"),
            RecurringSchedule.next_due_at.label("next_due_at"),
            func.row_number()
            .over(
                partition_by=RecurringSchedule.user_id,
                order_by=(RecurringSchedule.next_due_at, RecurringSchedule.id),
            )
            .label("owner_rank"),
        )
        .where(
            RecurringSchedule.deleted_at.is_(None),
            RecurringSchedule.paused_at.is_(None),
            RecurringSchedule.next_due_at.is_not(None),
            RecurringSchedule.next_due_at <= now,
        )
        .subquery("ranked_due_schedules")
    )
    return cast(
        Select[tuple[UUID]],
        select(ranked_due.c.schedule_id)
        .where(ranked_due.c.owner_rank == 1)
        .order_by(ranked_due.c.next_due_at, ranked_due.c.schedule_id)
        .limit(MAX_DUE_SCHEDULES_PER_TICK),
    )


def _fair_stage_owner_query(now: datetime) -> Select[tuple[UUID]]:
    oldest_actionable = func.min(RecurringInstance.next_attempt_at).label("oldest_actionable")
    return (
        select(RecurringInstance.user_id)
        .where(
            RecurringInstance.status == RecurringInstanceStatus.PENDING.value,
            RecurringInstance.next_attempt_at <= now,
        )
        .group_by(RecurringInstance.user_id)
        .order_by(oldest_actionable, RecurringInstance.user_id)
        .limit(MAX_STAGE_OWNERS_PER_TICK)
    )


def _backoff(attempt_count: int) -> timedelta:
    exponent = min(max(attempt_count - 1, 0), 7)
    delay = _BASE_BACKOFF * (2**exponent)
    return delay if delay <= _MAX_BACKOFF else _MAX_BACKOFF


def _increment_attempt(instance: RecurringInstance) -> None:
    instance.attempt_count = min(instance.attempt_count + 1, 32_767)


async def _active_catalogs(
    session: AsyncSession,
    instance: RecurringInstance,
) -> tuple[Account | None, Category | None, RecurringFailureCode | None]:
    account = await session.scalar(
        select(Account)
        .where(Account.id == instance.account_id, Account.user_id == instance.user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if account is None or account.archived_at is not None:
        return account, None, RecurringFailureCode.ACCOUNT_UNAVAILABLE
    if account.currency != instance.currency:
        return account, None, RecurringFailureCode.CURRENCY_MISMATCH
    category = await session.scalar(
        select(Category)
        .where(
            Category.id == instance.category_id,
            Category.user_id == instance.user_id,
            Category.kind == instance.type,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if category is None or category.archived_at is not None:
        return account, category, RecurringFailureCode.CATEGORY_UNAVAILABLE
    return account, category, None


async def _block_instance(
    session: AsyncSession,
    instance: RecurringInstance,
    failure: RecurringFailureCode,
    now: datetime,
) -> None:
    instance.status = RecurringInstanceStatus.BLOCKED.value
    instance.failure_code = failure.value
    _increment_attempt(instance)
    instance.version = _next_version(instance.version)
    instance.updated_at = now
    schedule = await session.scalar(
        select(RecurringSchedule)
        .where(
            RecurringSchedule.id == instance.schedule_id,
            RecurringSchedule.user_id == instance.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if schedule is not None and schedule.deleted_at is None:
        schedule.paused_at = now
        schedule.pause_reason = failure.value
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = now


async def _stage_one(
    session: AsyncSession,
    owner_id: UUID,
    now: datetime,
) -> _StageResult:
    if await _lock_owner(session, owner_id) is None:
        return _StageResult()
    instance = await session.scalar(
        select(RecurringInstance)
        .where(
            RecurringInstance.user_id == owner_id,
            RecurringInstance.status == RecurringInstanceStatus.PENDING.value,
            RecurringInstance.next_attempt_at <= now,
        )
        .order_by(RecurringInstance.scheduled_for, RecurringInstance.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if instance is None:
        return _StageResult()

    active_draft = await session.scalar(
        select(Draft)
        .where(Draft.user_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if active_draft is not None:
        _increment_attempt(instance)
        instance.next_attempt_at = now + _backoff(instance.attempt_count)
        instance.version = _next_version(instance.version)
        instance.updated_at = now
        await session.flush()
        return _StageResult(deferred=1)

    account, category, failure = await _active_catalogs(session, instance)
    if failure is not None:
        await _block_instance(session, instance, failure, now)
        await session.flush()
        return _StageResult(blocked=1)
    if account is None or category is None:  # pragma: no cover - guarded above
        raise RuntimeError("validated recurring catalogs disappeared")

    reviewed = ReviewedTransactionInput(
        kind=TransactionType(instance.type),
        amount_minor=instance.amount_minor,
        account_id=instance.account_id,
        category_id=instance.category_id,
        occurred_at=instance.scheduled_for,
        description=instance.description,
    )
    payload = dict(reviewed.to_payload())
    payload.update(
        {
            "flow": "recurring",
            "currency": instance.currency,
            "account_name": account.name,
            "category_name": category.name,
            "category_emoji": category.emoji,
        }
    )
    draft = Draft(
        id=uuid7(),
        user_id=owner_id,
        state="review",
        payload=payload,
        schema_version=1,
        revision=1,
        suspended=False,
        updated_at=now,
    )
    session.add(draft)
    await session.flush()
    instance.status = RecurringInstanceStatus.GENERATED.value
    instance.draft_id = draft.id
    instance.generated_at = now
    _increment_attempt(instance)
    instance.version = _next_version(instance.version)
    instance.updated_at = now
    await session.flush()
    return _StageResult(staged=1)


async def _materialize_schedules_isolated(
    session: AsyncSession,
    schedule_ids: tuple[UUID, ...],
    now: datetime,
) -> tuple[int, int]:
    materialized = failures = 0
    for schedule_id in schedule_ids:
        try:
            async with session.begin_nested():
                result = await _materialize_one(session, schedule_id, now)
        except Exception:
            # A corrupt or contended tenant must not roll back work already
            # completed for other owners in this bounded phase.
            failures += 1
            continue
        materialized += int(result)
    return materialized, failures


async def _stage_owners_isolated(
    session: AsyncSession,
    owner_ids: tuple[UUID, ...],
    now: datetime,
) -> tuple[_StageResult, int]:
    staged = deferred = blocked = failures = 0
    for owner_id in owner_ids:
        try:
            async with session.begin_nested():
                result = await _stage_one(session, owner_id, now)
        except Exception:
            # Roll back only this owner's savepoint and keep the phase fair for
            # every remaining selected tenant.
            failures += 1
            continue
        staged += result.staged
        deferred += result.deferred
        blocked += result.blocked
    return _StageResult(staged=staged, deferred=deferred, blocked=blocked), failures


def _log_phase_completion(event: str, failures: int) -> None:
    if failures:
        _LOGGER.error(event, extra={"result": "error"})
    else:
        _LOGGER.info(event, extra={"result": "success"})


class SqlAlchemyRecurringRunner:
    """Two committed, transaction-advisory-locked phases with bounded work."""

    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _materialize_due(self, now: datetime) -> int:
        _LOGGER.debug("recurring_materialization_started")
        async with self._sessions.begin() as session:
            await _set_phase_limits(session)
            if not await _try_phase_lock(session, _MATERIALIZE_LOCK_KEY):
                _LOGGER.info(
                    "recurring_materialization_completed",
                    extra={"result": "ignored"},
                )
                return 0
            schedule_ids = tuple(await session.scalars(_fair_due_schedule_query(now)))
            materialized, failures = await _materialize_schedules_isolated(
                session,
                schedule_ids,
                now,
            )
        _log_phase_completion("recurring_materialization_completed", failures)
        return materialized

    async def _stage_pending(self, now: datetime) -> _StageResult:
        _LOGGER.debug("recurring_staging_started")
        async with self._sessions.begin() as session:
            await _set_phase_limits(session)
            if not await _try_phase_lock(session, _STAGE_LOCK_KEY):
                _LOGGER.info("recurring_staging_completed", extra={"result": "ignored"})
                return _StageResult()
            owner_ids = tuple(await session.scalars(_fair_stage_owner_query(now)))
            result, failures = await _stage_owners_isolated(session, owner_ids, now)
        _log_phase_completion("recurring_staging_completed", failures)
        return result

    async def tick(self, *, now: datetime | None = None) -> RecurringTickResult:
        current = _now(now or datetime.now(UTC))
        try:
            materialized = await self._materialize_due(current)
            staged = await self._stage_pending(current)
        except BaseException:
            _LOGGER.error("recurring_runner_tick_completed", extra={"result": "error"})
            raise
        _LOGGER.info("recurring_runner_tick_completed", extra={"result": "success"})
        return RecurringTickResult(
            materialized=materialized,
            staged=staged.staged,
            deferred=staged.deferred,
            blocked=staged.blocked,
        )


__all__ = ["RecurringTickResult", "SqlAlchemyRecurringRunner"]
