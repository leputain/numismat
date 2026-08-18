import asyncio
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import psycopg
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import Account, HttpIdempotencyRecord, User, WebSession
from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaimStatus,
    IdempotencyKeyConflictError,
    IdempotencyResult,
    IdempotencyResultKind,
    SqlAlchemyHttpIdempotencyRepository,
)
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _keyed_digest(label: bytes, value: bytes) -> bytes:
    return hmac.new(b"synthetic-test-key", label + b"\0" + value, hashlib.sha256).digest()


@pytest.mark.asyncio
async def test_web_session_repository_uses_only_keyed_digests_and_bounded_state() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    raw_session = b"raw-session-token-never-persisted"
    raw_csrf = b"raw-csrf-token-never-persisted"
    session_digest = SessionTokenDigest(_keyed_digest(b"session", raw_session))
    csrf_digest = CsrfTokenDigest(_keyed_digest(b"csrf", raw_csrf))
    wrong_csrf = CsrfTokenDigest(_keyed_digest(b"csrf", b"wrong"))

    async with factory() as session:
        owner = User(telegram_user_id=99_700_101, telegram_chat_id=99_700_101)
        session.add(owner)
        await session.flush()
        repository = SqlAlchemyWebSessionRepository(session)

        created = await repository.create(
            owner.id,
            session_digest,
            csrf_digest,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

        assert created.owner_id == owner.id
        assert await repository.get_active(session_digest, now=now) == created
        assert (
            await repository.lock_active_for_mutation(
                session_digest,
                wrong_csrf,
                now=now,
            )
            is None
        )
        assert not await repository.revoke(
            session_digest,
            wrong_csrf,
            revoked_at=now + timedelta(seconds=30),
        )
        assert await repository.get_active(session_digest, now=now) == created
        assert (
            await repository.lock_active_for_mutation(
                session_digest,
                csrf_digest,
                now=now,
            )
            == created
        )
        stored = await session.scalar(select(WebSession).where(WebSession.id == created.session_id))
        assert stored is not None
        assert stored.session_token_hash == session_digest.database_value()
        assert stored.csrf_token_hash == csrf_digest.database_value()
        assert raw_session not in (stored.session_token_hash, stored.csrf_token_hash)
        assert raw_csrf not in (stored.session_token_hash, stored.csrf_token_hash)

        assert await repository.revoke(
            session_digest,
            csrf_digest,
            revoked_at=now + timedelta(minutes=1),
        )
        assert await repository.get_active(session_digest, now=now + timedelta(minutes=1)) is None
        assert await repository.cleanup(now=now + timedelta(minutes=1), limit=10) == 1
        assert await session.scalar(select(func.count()).select_from(WebSession)) == 0
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_logout_waits_for_a_locked_state_change_before_revoking_session() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    session_digest = SessionTokenDigest(_keyed_digest(b"session", b"concurrent"))
    csrf_digest = CsrfTokenDigest(_keyed_digest(b"csrf", b"concurrent"))
    owner_id: UUID
    async with factory.begin() as session:
        owner = User(telegram_user_id=99_700_106, telegram_chat_id=99_700_106)
        session.add(owner)
        await session.flush()
        owner_id = owner.id
        await SqlAlchemyWebSessionRepository(session).create(
            owner_id,
            session_digest,
            csrf_digest,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

    mutation_locked = asyncio.Event()
    release_mutation = asyncio.Event()
    logout_started = asyncio.Event()

    async def state_change() -> None:
        async with factory.begin() as session:
            active = await SqlAlchemyWebSessionRepository(session).lock_active_for_mutation(
                session_digest,
                csrf_digest,
                now=now,
            )
            assert active is not None
            mutation_locked.set()
            await release_mutation.wait()

    async def logout() -> bool:
        await mutation_locked.wait()
        async with factory.begin() as session:
            logout_started.set()
            return await SqlAlchemyWebSessionRepository(session).revoke(
                session_digest,
                csrf_digest,
                revoked_at=now + timedelta(seconds=1),
            )

    mutation_task = asyncio.create_task(state_change())
    logout_task = asyncio.create_task(logout())
    await logout_started.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(logout_task), timeout=0.2)
    finally:
        release_mutation.set()

    await mutation_task
    assert await logout_task
    async with factory.begin() as session:
        repository = SqlAlchemyWebSessionRepository(session)
        assert await repository.get_active(session_digest, now=now + timedelta(seconds=1)) is None
        await session.execute(delete(User).where(User.id == owner_id))
    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotency_claim_replays_and_conflicting_reuse_fails_closed() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    key = IdempotencyKeyDigest(_keyed_digest(b"idempotency", b"same-key"))
    fingerprint = RequestFingerprintDigest(_keyed_digest(b"request", b"same-request"))
    conflicting = RequestFingerprintDigest(_keyed_digest(b"request", b"other-request"))
    result_id = uuid7()

    async with factory() as session:
        owner = User(telegram_user_id=99_700_111, telegram_chat_id=99_700_111)
        session.add(owner)
        await session.flush()
        repository = SqlAlchemyHttpIdempotencyRepository(session)

        first = await repository.claim(
            owner.id,
            key,
            fingerprint,
            operation="draft.update",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        assert first.status is IdempotencyClaimStatus.NEW
        result = IdempotencyResult(
            http_status=200,
            kind=IdempotencyResultKind.DRAFT,
            result_id=result_id,
            revision=3,
        )
        await repository.complete(
            owner.id,
            first,
            result,
            completed_at=now + timedelta(seconds=1),
        )

        replay = await repository.claim(
            owner.id,
            key,
            fingerprint,
            operation="draft.update",
            created_at=now + timedelta(seconds=2),
            expires_at=now + timedelta(hours=1),
        )
        assert replay.status is IdempotencyClaimStatus.REPLAY
        assert replay.result == result
        with pytest.raises(IdempotencyKeyConflictError, match="another request"):
            await repository.claim(
                owner.id,
                key,
                conflicting,
                operation="draft.update",
                created_at=now + timedelta(seconds=2),
                expires_at=now + timedelta(hours=1),
            )
        with pytest.raises(IdempotencyKeyConflictError, match="another request"):
            await repository.claim(
                owner.id,
                key,
                fingerprint,
                operation="transaction.restore",
                created_at=now + timedelta(seconds=2),
                expires_at=now + timedelta(hours=1),
            )

        stored = await session.scalar(
            select(HttpIdempotencyRecord).where(HttpIdempotencyRecord.id == first.record_id)
        )
        assert stored is not None
        assert stored.idempotency_key_hash == key.database_value()
        assert stored.request_fingerprint == fingerprint.database_value()
        assert stored.http_status == 200
        assert stored.result_id == result_id
        assert await repository.cleanup(now=now + timedelta(minutes=30), limit=10) == 0
        assert await repository.cleanup(now=now + timedelta(hours=1), limit=10) == 1
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_rejects_malformed_digest_and_unbounded_expiry_state() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)

    async with factory() as session:
        owner = User(telegram_user_id=99_700_116, telegram_chat_id=99_700_116)
        session.add(owner)
        await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    WebSession(
                        id=uuid7(),
                        user_id=owner.id,
                        session_token_hash=b"s" * 31,
                        csrf_token_hash=b"c" * 32,
                        created_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
                await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    HttpIdempotencyRecord(
                        id=uuid7(),
                        user_id=owner.id,
                        idempotency_key_hash=b"i" * 32,
                        request_fingerprint=b"f" * 32,
                        operation="draft.update",
                        status="in_progress",
                        created_at=now,
                        expires_at=now + timedelta(hours=24, seconds=1),
                    )
                )
                await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    HttpIdempotencyRecord(
                        id=uuid7(),
                        user_id=owner.id,
                        idempotency_key_hash=b"n" * 32,
                        request_fingerprint=b"h" * 32,
                        operation="draft.confirm",
                        status="completed",
                        http_status=None,
                        result_kind="none",
                        result_id=None,
                        result_revision=None,
                        created_at=now,
                        completed_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
                await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    HttpIdempotencyRecord(
                        id=uuid7(),
                        user_id=owner.id,
                        idempotency_key_hash=b"r" * 32,
                        request_fingerprint=b"v" * 32,
                        operation="draft.update",
                        status="completed",
                        http_status=200,
                        result_kind="draft",
                        result_id=uuid7(),
                        result_revision=None,
                        created_at=now,
                        completed_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
                await session.flush()

        assert await session.scalar(select(func.count()).select_from(WebSession)) == 0
        assert await session.scalar(select(func.count()).select_from(HttpIdempotencyRecord)) == 0
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_exclusive_downgrade_lock_blocks_new_security_state_writes() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    owner_id: UUID
    async with factory.begin() as session:
        owner = User(telegram_user_id=99_700_117, telegram_chat_id=99_700_117)
        session.add(owner)
        await session.flush()
        owner_id = owner.id

    locker = factory()
    writer = factory()
    try:
        await locker.execute(
            text("LOCK TABLE web_sessions, http_idempotency IN ACCESS EXCLUSIVE MODE")
        )
        await writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
        writer.add(
            WebSession(
                id=uuid7(),
                user_id=owner_id,
                session_token_hash=b"l" * 32,
                csrf_token_hash=b"c" * 32,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )

        with pytest.raises(OperationalError) as caught:
            await writer.flush()

        assert isinstance(caught.value.orig, psycopg.errors.LockNotAvailable)
    finally:
        await writer.rollback()
        await locker.rollback()
        await writer.close()
        await locker.close()

    async with factory.begin() as session:
        await session.execute(delete(User).where(User.id == owner_id))
    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotency_claim_rolls_back_with_business_mutation() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)

    async with factory() as session:
        owner = User(telegram_user_id=99_700_121, telegram_chat_id=99_700_121)
        session.add(owner)
        await session.flush()
        nested = await session.begin_nested()
        repository = SqlAlchemyHttpIdempotencyRepository(session)
        claim = await repository.claim(
            owner.id,
            IdempotencyKeyDigest(_keyed_digest(b"idempotency", b"rolled-back")),
            RequestFingerprintDigest(_keyed_digest(b"request", b"rolled-back")),
            operation="account.create",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(Account(user_id=owner.id, name="Synthetic", slug="synthetic"))
        await session.flush()

        await nested.rollback()

        assert (
            await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(HttpIdempotencyRecord.id == claim.record_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(Account).where(Account.user_id == owner.id)
            )
            == 0
        )
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_idempotency_claim_has_one_winner_and_one_replay() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    owner_id: UUID
    async with factory.begin() as session:
        owner = User(telegram_user_id=99_700_131, telegram_chat_id=99_700_131)
        session.add(owner)
        await session.flush()
        owner_id = owner.id

    key = IdempotencyKeyDigest(_keyed_digest(b"idempotency", b"concurrent"))
    fingerprint = RequestFingerprintDigest(_keyed_digest(b"request", b"concurrent"))
    result = IdempotencyResult(http_status=204, kind=IdempotencyResultKind.NONE)

    async def worker() -> IdempotencyClaimStatus:
        async with factory.begin() as session:
            repository = SqlAlchemyHttpIdempotencyRepository(session)
            claim = await repository.claim(
                owner_id,
                key,
                fingerprint,
                operation="draft.cancel",
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
            if claim.status is IdempotencyClaimStatus.NEW:
                await repository.complete(
                    owner_id,
                    claim,
                    result,
                    completed_at=now + timedelta(seconds=1),
                )
            return claim.status

    statuses = await asyncio.gather(worker(), worker())

    assert sorted(statuses) == sorted((IdempotencyClaimStatus.NEW, IdempotencyClaimStatus.REPLAY))
    async with factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(HttpIdempotencyRecord.user_id == owner_id)
            )
            == 1
        )
        await session.execute(delete(User).where(User.id == owner_id))
    await engine.dispose()
