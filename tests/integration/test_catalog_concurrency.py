import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, User
from finbot.adapters.database.services.catalogs import (
    archive_account,
    create_account,
    rename_account,
    set_default_account,
)
from finbot.domain.errors import StaleObjectError


def _safe_database_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL", "")
    if not raw:
        raise pytest.UsageError("TEST_DATABASE_URL is required for integration tests")
    try:
        database = make_url(raw).database or ""
    except Exception as exc:
        raise pytest.UsageError("TEST_DATABASE_URL is invalid") from exc
    if not database.endswith("_test"):
        raise pytest.UsageError("TEST_DATABASE_URL database name must end with _test")
    return raw


DATABASE_URL = _safe_database_url()


def _synthetic_telegram_user_id() -> int:
    return 8_000_000_000 + uuid4().int % 1_000_000_000


async def _remove_test_catalog(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(Account).where(Account.user_id == user_id))
        await connection.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_catalog_locks_refresh_preloaded_identity_map_values() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    user_id: UUID | None = None

    try:
        async with factory() as setup:
            user = User(telegram_user_id=_synthetic_telegram_user_id())
            setup.add(user)
            await setup.flush()
            primary = Account(user_id=user.id, name="Основной", slug="основной")
            promoted = Account(user_id=user.id, name="Резервный", slug="резервный")
            setup.add_all((primary, promoted))
            await setup.flush()
            user.default_account_id = primary.id
            await setup.commit()
            user_id = user.id
            primary_id = primary.id
            promoted_id = promoted.id

        async with factory() as session_a, factory() as session_b:
            cached_user = await session_a.scalar(select(User).where(User.id == user_id))
            cached_promoted = await session_a.scalar(
                select(Account).where(Account.id == promoted_id)
            )
            assert cached_user is not None
            assert cached_promoted is not None
            assert cached_user.default_account_id == primary_id
            assert cached_promoted.version == 1

            changed = await set_default_account(
                session_b,
                user_id,
                promoted_id,
                expected_version=1,
            )
            assert changed.version == 2
            await session_b.commit()

            # Session A still holds the old Python objects. The service must force
            # locked reads to refresh both objects before checking invariants.
            assert cached_user.default_account_id == primary_id
            assert cached_promoted.version == 1
            with pytest.raises(ValueError, match="основной"):
                await archive_account(
                    session_a,
                    user_id,
                    promoted_id,
                    cached_user.default_account_id,
                    expected_version=2,
                )
            assert cached_user.default_account_id == promoted_id
            assert cached_promoted.version == 2

            with pytest.raises(StaleObjectError, match="уже изменён"):
                await rename_account(
                    session_a,
                    user_id,
                    promoted_id,
                    "Устаревшее переименование",
                    expected_version=1,
                )
            await session_a.rollback()
    finally:
        if user_id is not None:
            await _remove_test_catalog(engine, user_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_create_and_rename_same_slug_return_domain_error() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    user_id: UUID | None = None

    try:
        async with factory() as setup:
            user = User(telegram_user_id=_synthetic_telegram_user_id())
            setup.add(user)
            await setup.flush()
            original = Account(user_id=user.id, name="Старое имя", slug="старое-имя")
            setup.add(original)
            await setup.flush()
            user.default_account_id = original.id
            await setup.commit()
            user_id = user.id
            original_id = original.id

        created_and_locked = asyncio.Event()
        rename_started = asyncio.Event()

        async def create_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await create_account(session, user_id, "Общее имя", "RUB")
                created_and_locked.set()
                await asyncio.wait_for(rename_started.wait(), timeout=2)
                # Give the competing coroutine a turn to reach the owner-row lock.
                await asyncio.sleep(0)
                await session.commit()

        async def rename_contender() -> None:
            await asyncio.wait_for(created_and_locked.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                rename_started.set()
                with pytest.raises(ValueError, match="уже существует"):
                    await rename_account(
                        session,
                        user_id,
                        original_id,
                        "Общее имя",
                        expected_version=1,
                    )
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(create_winner(), rename_contender()),
            timeout=8,
        )

        async with factory() as verification:
            shared_slug_count = await verification.scalar(
                select(func.count(Account.id)).where(
                    Account.user_id == user_id,
                    Account.slug == "общее-имя",
                )
            )
            unchanged = await verification.scalar(select(Account).where(Account.id == original_id))
            assert shared_slug_count == 1
            assert unchanged is not None
            assert unchanged.slug == "старое-имя"
            assert unchanged.version == 1
    finally:
        if user_id is not None:
            await _remove_test_catalog(engine, user_id)
        await engine.dispose()
