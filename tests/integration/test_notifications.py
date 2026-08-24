from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid7

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import (
    Account,
    Budget,
    Category,
    NotificationJob,
    NotificationPreference,
    Transaction,
    User,
)
from finbot.adapters.database.notification_delivery import (
    NOTIFICATION_CLEANUP_LOCK_KEY,
    SqlAlchemyNotificationDelivery,
)
from finbot.adapters.database.notification_scheduler import SqlAlchemyNotificationScheduler
from finbot.adapters.database.repositories.notifications import (
    SqlAlchemyNotificationRepository,
)
from finbot.application.notifications import (
    MAX_NOTIFICATION_CLEANUP_BATCH,
    NOTIFICATION_RETENTION_DAYS,
    STATIC_NOTIFICATION_TEXT,
    NotificationDigest,
    NotificationFailureCode,
    NotificationIntent,
    NotificationJobStatus,
)
from finbot.domain.notifications import NotificationKind

OWNER_A = UUID("00000000-0000-7000-8000-000000004101")
OWNER_B = UUID("00000000-0000-7000-8000-000000004102")
TELEGRAM_A = 9_000_004_101
TELEGRAM_B = 9_000_004_102
KEY = bytes(range(32))
NOW = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send(self, *, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class _SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = values
        self.calls = 0

    def __call__(self) -> datetime:
        if self.calls >= len(self._values):
            raise AssertionError("notification delivery requested an unexpected timestamp")
        value = self._values[self.calls]
        self.calls += 1
        return value


async def _job(session_factory: async_sessionmaker, owner_id: UUID) -> NotificationJob:
    async with session_factory() as session:
        row = await session.scalar(
            select(NotificationJob)
            .where(NotificationJob.user_id == owner_id)
            .order_by(NotificationJob.created_at.desc(), NotificationJob.id.desc())
        )
        assert row is not None
        return row


async def test_queue_is_tenant_safe_bounded_and_privacy_preserving() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    digest = NotificationDigest(KEY)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                (
                    User(
                        id=OWNER_A,
                        telegram_user_id=TELEGRAM_A,
                        telegram_chat_id=TELEGRAM_A,
                        timezone="UTC",
                    ),
                    User(
                        id=OWNER_B,
                        telegram_user_id=TELEGRAM_B,
                        telegram_chat_id=TELEGRAM_B,
                        timezone="UTC",
                    ),
                )
            )
            await session.flush()
            session.add_all(
                (
                    NotificationPreference(
                        user_id=OWNER_A,
                        weekly_digest_enabled=True,
                        quiet_start=time(22),
                        quiet_end=time(7),
                        weekly_weekday=0,
                        weekly_time=time(9),
                    ),
                    NotificationPreference(
                        user_id=OWNER_B,
                        weekly_digest_enabled=True,
                        weekly_weekday=0,
                        weekly_time=time(9),
                    ),
                )
            )

        scheduler = SqlAlchemyNotificationScheduler(sessions, digest)
        first = await scheduler.tick(NOW)
        duplicate = await scheduler.tick(NOW)
        assert first.queued == 2
        assert duplicate.queued == 0

        async with sessions() as session:
            rows = tuple(
                await session.scalars(select(NotificationJob).order_by(NotificationJob.user_id))
            )
        assert len(rows) == 2
        assert {row.user_id for row in rows} == {OWNER_A, OWNER_B}
        assert all(len(row.dedupe_digest) == 32 for row in rows)
        assert not {
            "amount_minor",
            "currency",
            "description",
            "message_text",
            "telegram_chat_id",
        } & set(NotificationJob.__table__.c.keys())

        async with sessions() as session, session.begin():
            claimed = await SqlAlchemyNotificationRepository(session).claim_due(now=NOW)
        assert len(claimed) == 2
        quiet_job = next(job for job in claimed if job.owner_id == OWNER_A)
        recovery_job = next(job for job in claimed if job.owner_id == OWNER_B)

        async with sessions() as session, session.begin():
            prepared = await SqlAlchemyNotificationRepository(session).prepare_delivery(
                quiet_job,
                now=NOW,
                allowed_telegram_user_ids=frozenset((TELEGRAM_A, TELEGRAM_B)),
            )
        assert prepared is None
        quiet_row = await _job(sessions, OWNER_A)
        assert quiet_row.status == NotificationJobStatus.PENDING.value
        assert quiet_row.attempt_count == 0
        assert quiet_row.available_at == datetime(2026, 8, 25, 7, tzinfo=UTC)

        recovery_at = NOW.replace(minute=33)
        async with sessions() as session, session.begin():
            recovered = await SqlAlchemyNotificationRepository(session).claim_due(now=recovery_at)
        assert len(recovered) == 1
        assert recovered[0].job_id == recovery_job.job_id
        assert recovered[0].lease_token != recovery_job.lease_token
        assert recovered[0].attempt_count == 0

        async with sessions() as session, session.begin():
            revoked = await SqlAlchemyNotificationRepository(session).prepare_delivery(
                recovered[0],
                now=recovery_at,
                allowed_telegram_user_ids=frozenset((TELEGRAM_A,)),
            )
        assert revoked is None
        revoked_row = await _job(sessions, OWNER_B)
        assert revoked_row.status == NotificationJobStatus.FAILED.value
        assert revoked_row.failure_code == NotificationFailureCode.OWNER_REVOKED.value

        async with sessions() as session, session.begin():
            await session.execute(delete(NotificationJob).where(NotificationJob.user_id == OWNER_A))
            queued = await SqlAlchemyNotificationRepository(session, digest).enqueue(
                NotificationIntent(
                    owner_id=OWNER_B,
                    kind=NotificationKind.WEEKLY_DIGEST,
                    period_key="retry-contract",
                ),
                available_at=recovery_at,
            )
        assert queued

        attempt_at = recovery_at
        for attempt in range(1, 6):
            async with sessions() as session, session.begin():
                pending = await SqlAlchemyNotificationRepository(session).claim_due(now=attempt_at)
            assert len(pending) == 1
            async with sessions() as session, session.begin():
                prepared = await SqlAlchemyNotificationRepository(session).prepare_delivery(
                    pending[0],
                    now=attempt_at,
                    allowed_telegram_user_ids=frozenset((TELEGRAM_B,)),
                )
            assert prepared is not None and prepared.job.attempt_count == attempt
            async with sessions() as session, session.begin():
                assert await SqlAlchemyNotificationRepository(session).mark_failed(
                    prepared.job,
                    now=attempt_at,
                    retryable=True,
                )
            retry_row = await _job(sessions, OWNER_B)
            if attempt < 5:
                assert retry_row.status == NotificationJobStatus.PENDING.value
                attempt_at = retry_row.available_at
            else:
                assert retry_row.status == NotificationJobStatus.FAILED.value
                assert retry_row.failure_code == NotificationFailureCode.RETRY_EXHAUSTED.value

        async with sessions() as session, session.begin():
            queued = await SqlAlchemyNotificationRepository(session, digest).enqueue(
                NotificationIntent(
                    owner_id=OWNER_B,
                    kind=NotificationKind.WEEKLY_DIGEST,
                    period_key="success-contract",
                ),
                available_at=attempt_at,
            )
        assert queued
        sender = _Sender()
        result = await SqlAlchemyNotificationDelivery(
            sessions,
            sender,
            allowed_telegram_user_ids=frozenset((TELEGRAM_B,)),
        ).tick(attempt_at)
        assert result.delivered == 1
        assert sender.calls == (
            [(TELEGRAM_B, STATIC_NOTIFICATION_TEXT[NotificationKind.WEEKLY_DIGEST])]
        )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(User).where(User.id.in_((OWNER_A, OWNER_B))))
        await engine.dispose()


async def test_delivery_rechecks_quiet_hours_for_each_job_in_a_batch() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid7()
    telegram_id = 9_000_004_103
    before_quiet = datetime(2026, 8, 24, 21, 59, 58, tzinfo=UTC)
    quiet_boundary = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)
    job_ids = tuple(sorted((uuid7(), uuid7())))
    try:
        async with sessions() as session, session.begin():
            session.add(
                User(
                    id=owner_id,
                    telegram_user_id=telegram_id,
                    telegram_chat_id=telegram_id,
                    timezone="UTC",
                )
            )
            await session.flush()
            session.add(
                NotificationPreference(
                    user_id=owner_id,
                    weekly_digest_enabled=True,
                    quiet_start=time(22),
                    quiet_end=time(7),
                    weekly_weekday=0,
                    weekly_time=time(9),
                )
            )
            session.add_all(
                NotificationJob(
                    id=job_id,
                    user_id=owner_id,
                    kind=NotificationKind.WEEKLY_DIGEST.value,
                    dedupe_digest=job_id.bytes * 2,
                    status=NotificationJobStatus.PENDING.value,
                    attempt_count=0,
                    available_at=before_quiet,
                    created_at=before_quiet,
                    updated_at=before_quiet,
                )
                for job_id in job_ids
            )

        clock = _SequenceClock(
            before_quiet,
            before_quiet,
            before_quiet + timedelta(seconds=1),
            before_quiet + timedelta(seconds=1),
            quiet_boundary,
            quiet_boundary,
        )
        sender = _Sender()
        result = await SqlAlchemyNotificationDelivery(
            sessions,
            sender,
            allowed_telegram_user_ids=frozenset((telegram_id,)),
            clock=clock,
        ).tick()

        assert result.claimed == 2
        assert result.delivered == 1
        assert result.deferred == 1
        assert result.failed == 0
        assert clock.calls == 6
        assert sender.calls == [
            (telegram_id, STATIC_NOTIFICATION_TEXT[NotificationKind.WEEKLY_DIGEST])
        ]

        async with sessions() as session:
            jobs = tuple(
                await session.scalars(
                    select(NotificationJob)
                    .where(NotificationJob.user_id == owner_id)
                    .order_by(NotificationJob.id)
                )
            )
        assert jobs[0].status == NotificationJobStatus.DELIVERED.value
        assert jobs[0].delivered_at == quiet_boundary
        assert jobs[1].status == NotificationJobStatus.PENDING.value
        assert jobs[1].attempt_count == 0
        assert jobs[1].available_at == datetime(2026, 8, 25, 7, tzinfo=UTC)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(User).where(User.id == owner_id))
        await engine.dispose()


async def test_delivery_rechecks_quiet_hours_immediately_before_telegram_io() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid7()
    telegram_id = 9_000_004_104
    before_quiet = datetime(2026, 8, 24, 21, 59, 58, tzinfo=UTC)
    quiet_boundary = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)
    job_id = uuid7()
    try:
        async with sessions() as session, session.begin():
            session.add(
                User(
                    id=owner_id,
                    telegram_user_id=telegram_id,
                    telegram_chat_id=telegram_id,
                    timezone="UTC",
                )
            )
            await session.flush()
            session.add(
                NotificationPreference(
                    user_id=owner_id,
                    weekly_digest_enabled=True,
                    quiet_start=time(22),
                    quiet_end=time(7),
                    weekly_weekday=0,
                    weekly_time=time(9),
                )
            )
            session.add(
                NotificationJob(
                    id=job_id,
                    user_id=owner_id,
                    kind=NotificationKind.WEEKLY_DIGEST.value,
                    dedupe_digest=job_id.bytes * 2,
                    status=NotificationJobStatus.PENDING.value,
                    attempt_count=0,
                    available_at=before_quiet,
                    created_at=before_quiet,
                    updated_at=before_quiet,
                )
            )

        clock = _SequenceClock(
            before_quiet,
            before_quiet,
            before_quiet + timedelta(seconds=1),
            quiet_boundary,
        )
        sender = _Sender()
        result = await SqlAlchemyNotificationDelivery(
            sessions,
            sender,
            allowed_telegram_user_ids=frozenset((telegram_id,)),
            clock=clock,
        ).tick()

        assert result.claimed == 1
        assert result.delivered == 0
        assert result.deferred == 1
        assert result.failed == 0
        assert clock.calls == 4
        assert sender.calls == []
        row = await _job(sessions, owner_id)
        assert row.status == NotificationJobStatus.PENDING.value
        assert row.attempt_count == 0
        assert row.available_at == datetime(2026, 8, 25, 7, tzinfo=UTC)
        assert row.lease_token is None
        assert row.lease_until is None
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(User).where(User.id == owner_id))
        await engine.dispose()


async def test_scheduler_owner_pages_are_bounded_and_advance_past_64_tenants() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_ids = tuple(uuid7() for _ in range(65))
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                User(
                    id=owner_id,
                    telegram_user_id=9_100_000_000 + index,
                    telegram_chat_id=9_100_000_000 + index,
                    timezone="UTC",
                )
                for index, owner_id in enumerate(owner_ids)
            )
            await session.flush()
            session.add_all(
                NotificationPreference(
                    user_id=owner_id,
                    weekly_digest_enabled=True,
                    weekly_weekday=0,
                    weekly_time=time(9),
                )
                for owner_id in owner_ids
            )

        async with sessions() as session:
            repository = SqlAlchemyNotificationRepository(session)
            first = await repository.list_scheduler_owners(after_owner_id=None)
            second = await repository.list_scheduler_owners(after_owner_id=first[-1].owner_id)
            exhausted = await repository.list_scheduler_owners(after_owner_id=second[-1].owner_id)

        assert len(first) == 64
        assert len(second) == 1
        assert exhausted == ()
        assert {item.owner_id for item in first}.isdisjoint({item.owner_id for item in second})
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(User).where(User.id.in_(owner_ids)))
        await engine.dispose()


async def test_budget_delivery_preflight_drops_alerts_after_current_data_changes() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_ids = (uuid7(), uuid7())
    account_ids = (uuid7(), uuid7())
    category_ids = (uuid7(), uuid7())
    budget_ids = (uuid7(), uuid7())
    transaction_ids = (uuid7(), uuid7())
    telegram_ids = (9_200_000_001, 9_200_000_002)
    digest = NotificationDigest(KEY)
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                User(
                    id=owner_id,
                    telegram_user_id=telegram_id,
                    telegram_chat_id=telegram_id,
                    timezone="UTC",
                )
                for owner_id, telegram_id in zip(owner_ids, telegram_ids, strict=True)
            )
            await session.flush()
            session.add_all(
                Account(
                    id=account_id,
                    user_id=owner_id,
                    name="Основной",
                    slug="основной",
                    currency="RUB",
                )
                for owner_id, account_id in zip(owner_ids, account_ids, strict=True)
            )
            session.add_all(
                Category(
                    id=category_id,
                    user_id=owner_id,
                    kind="expense",
                    name="Покупки",
                    slug="покупки",
                )
                for owner_id, category_id in zip(owner_ids, category_ids, strict=True)
            )
            session.add_all(
                Budget(
                    id=budget_id,
                    user_id=owner_id,
                    name="Месячный лимит",
                    limit_minor=1_000,
                    currency="RUB",
                    starts_on=date(2026, 8, 1),
                    ends_on=date(2026, 8, 31),
                    timezone="UTC",
                )
                for owner_id, budget_id in zip(owner_ids, budget_ids, strict=True)
            )
            session.add_all(
                Transaction(
                    id=transaction_id,
                    user_id=owner_id,
                    type="expense",
                    amount_minor=800,
                    currency="RUB",
                    account_id=account_id,
                    category_id=category_id,
                    occurred_at=NOW - timedelta(hours=1),
                    description="",
                )
                for owner_id, account_id, category_id, transaction_id in zip(
                    owner_ids,
                    account_ids,
                    category_ids,
                    transaction_ids,
                    strict=True,
                )
            )
            session.add_all(
                NotificationPreference(
                    user_id=owner_id,
                    budget_80_enabled=True,
                    budget_100_enabled=True,
                )
                for owner_id in owner_ids
            )

        scheduled = await SqlAlchemyNotificationScheduler(sessions, digest).tick(NOW)
        assert scheduled.queued == 2

        async with sessions() as session, session.begin():
            await session.execute(
                update(Transaction)
                .where(Transaction.id == transaction_ids[0])
                .values(amount_minor=700, version=2, updated_at=NOW)
            )
            await session.execute(
                update(Budget)
                .where(Budget.id == budget_ids[1])
                .values(limit_minor=1_100, version=2, updated_at=NOW)
            )

        sender = _Sender()
        delivered = await SqlAlchemyNotificationDelivery(
            sessions,
            sender,
            allowed_telegram_user_ids=frozenset(telegram_ids),
        ).tick(NOW)

        assert delivered.claimed == 2
        assert delivered.delivered == 0
        assert sender.calls == []
        async with sessions() as session:
            jobs = tuple(
                await session.scalars(
                    select(NotificationJob).where(NotificationJob.user_id.in_(owner_ids))
                )
            )
        assert len(jobs) == 2
        assert all(job.status == NotificationJobStatus.FAILED.value for job in jobs)
        assert all(
            job.failure_code == NotificationFailureCode.REFERENCE_INVALID.value for job in jobs
        )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(Transaction).where(Transaction.user_id.in_(owner_ids)))
            await session.execute(delete(Budget).where(Budget.user_id.in_(owner_ids)))
            await session.execute(delete(Category).where(Category.user_id.in_(owner_ids)))
            await session.execute(delete(Account).where(Account.user_id.in_(owner_ids)))
            await session.execute(delete(User).where(User.id.in_(owner_ids)))
        await engine.dispose()


async def test_terminal_job_cleanup_is_singleton_bounded_and_never_touches_live_jobs() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid7()
    telegram_id = 9_300_000_001
    old = NOW - timedelta(days=NOTIFICATION_RETENTION_DAYS + 1)
    boundary = NOW - timedelta(days=NOTIFICATION_RETENTION_DAYS)
    try:
        async with sessions() as session, session.begin():
            session.add(
                User(
                    id=owner_id,
                    telegram_user_id=telegram_id,
                    telegram_chat_id=telegram_id,
                    timezone="UTC",
                )
            )
            await session.flush()
            terminal_jobs = []
            for index in range(MAX_NOTIFICATION_CLEANUP_BATCH + 1):
                delivered = index % 2 == 0
                terminal_jobs.append(
                    NotificationJob(
                        id=uuid7(),
                        user_id=owner_id,
                        kind=NotificationKind.WEEKLY_DIGEST.value,
                        dedupe_digest=index.to_bytes(32, "big"),
                        status=(
                            NotificationJobStatus.DELIVERED.value
                            if delivered
                            else NotificationJobStatus.FAILED.value
                        ),
                        attempt_count=1,
                        available_at=old,
                        failure_code=(
                            None if delivered else NotificationFailureCode.REFERENCE_INVALID.value
                        ),
                        delivered_at=old if delivered else None,
                        created_at=old,
                        updated_at=old,
                    )
                )
            protected = (
                NotificationJob(
                    id=uuid7(),
                    user_id=owner_id,
                    kind=NotificationKind.WEEKLY_DIGEST.value,
                    dedupe_digest=(MAX_NOTIFICATION_CLEANUP_BATCH + 1).to_bytes(32, "big"),
                    status=NotificationJobStatus.PENDING.value,
                    attempt_count=0,
                    available_at=old,
                    created_at=old,
                    updated_at=old,
                ),
                NotificationJob(
                    id=uuid7(),
                    user_id=owner_id,
                    kind=NotificationKind.WEEKLY_DIGEST.value,
                    dedupe_digest=(MAX_NOTIFICATION_CLEANUP_BATCH + 2).to_bytes(32, "big"),
                    status=NotificationJobStatus.LEASED.value,
                    attempt_count=0,
                    available_at=old,
                    lease_token=uuid7(),
                    lease_until=NOW + timedelta(minutes=1),
                    created_at=old,
                    updated_at=old,
                ),
                NotificationJob(
                    id=uuid7(),
                    user_id=owner_id,
                    kind=NotificationKind.WEEKLY_DIGEST.value,
                    dedupe_digest=(MAX_NOTIFICATION_CLEANUP_BATCH + 3).to_bytes(32, "big"),
                    status=NotificationJobStatus.DELIVERED.value,
                    attempt_count=1,
                    available_at=boundary,
                    delivered_at=boundary,
                    created_at=boundary,
                    updated_at=boundary,
                ),
            )
            session.add_all((*terminal_jobs, *protected))

        delivery = SqlAlchemyNotificationDelivery(
            sessions,
            _Sender(),
            allowed_telegram_user_ids=frozenset((telegram_id,)),
        )
        assert await delivery.cleanup(NOW) == MAX_NOTIFICATION_CLEANUP_BATCH

        async with sessions() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": NOTIFICATION_CLEANUP_LOCK_KEY},
            )
            assert await delivery.cleanup(NOW) == 0
            async with sessions() as verification:
                assert (
                    await verification.scalar(
                        select(func.count(NotificationJob.id)).where(
                            NotificationJob.user_id == owner_id,
                            NotificationJob.updated_at < boundary,
                            NotificationJob.status.in_(
                                (
                                    NotificationJobStatus.DELIVERED.value,
                                    NotificationJobStatus.FAILED.value,
                                )
                            ),
                        )
                    )
                    == 1
                )

        assert await delivery.cleanup(NOW) == 1
        async with sessions() as verification:
            remaining = tuple(
                await verification.scalars(
                    select(NotificationJob).where(NotificationJob.user_id == owner_id)
                )
            )
        assert {job.status for job in remaining} == {
            NotificationJobStatus.PENDING.value,
            NotificationJobStatus.LEASED.value,
            NotificationJobStatus.DELIVERED.value,
        }
        assert len(remaining) == 3
        assert (
            next(
                job for job in remaining if job.status == NotificationJobStatus.DELIVERED.value
            ).updated_at
            == boundary
        )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(User).where(User.id == owner_id))
        await engine.dispose()
