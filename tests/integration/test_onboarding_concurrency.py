import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, Category, User
from finbot.adapters.database.services.onboarding import (
    OwnerChatBindingError,
    ensure_owner_user,
)
from finbot.application.services.onboarding import (
    DEFAULT_ACCOUNT_SLUG,
    INITIAL_CATEGORIES,
)


def _safe_database_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL", "")
    if not raw:
        raise pytest.UsageError("TEST_DATABASE_URL is required for integration tests")
    database = make_url(raw).database or ""
    if not database.endswith("_test"):
        raise pytest.UsageError("TEST_DATABASE_URL database name must end with _test")
    return raw


DATABASE_URL = _safe_database_url()


def _synthetic_telegram_user_id() -> int:
    return 8_000_000_000 + uuid4().int % 1_000_000_000


async def _remove_owner(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(User).where(User.id == user_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == user_id))
        await connection.execute(delete(Account).where(Account.user_id == user_id))
        await connection.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_concurrent_first_use_creates_one_complete_catalog() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = _synthetic_telegram_user_id()
    first_initialized = asyncio.Event()
    contender_started = asyncio.Event()
    returned_ids: list[UUID] = []
    user_id: UUID | None = None

    async def initialize_first() -> None:
        async with factory() as session:
            user = await ensure_owner_user(
                session,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_user_id,
                locale="ru_RU",
                timezone="Europe/Moscow",
                currency=" rub ",
            )
            first_initialized.set()
            await asyncio.wait_for(contender_started.wait(), timeout=2)
            await asyncio.sleep(0)
            await session.commit()
            returned_ids.append(user.id)

    async def initialize_contender() -> None:
        await asyncio.wait_for(first_initialized.wait(), timeout=2)
        async with factory() as session:
            contender_started.set()
            user = await ensure_owner_user(
                session,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_user_id,
                locale="ru_RU",
                timezone="Europe/Moscow",
                currency="RUB",
            )
            await session.commit()
            returned_ids.append(user.id)

    try:
        await asyncio.wait_for(
            asyncio.gather(initialize_first(), initialize_contender()),
            timeout=8,
        )
        assert len(set(returned_ids)) == 1
        user_id = returned_ids[0]

        async with factory() as verification:
            user = await verification.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            accounts = list(
                await verification.scalars(select(Account).where(Account.user_id == user_id))
            )
            categories = list(
                await verification.scalars(select(Category).where(Category.user_id == user_id))
            )
            user_count = await verification.scalar(
                select(func.count(User.id)).where(User.telegram_user_id == telegram_user_id)
            )

            assert user is not None
            assert user_count == 1
            assert user.telegram_chat_id == telegram_user_id
            assert user.base_currency == "RUB"
            assert len(accounts) == 1
            assert accounts[0].slug == DEFAULT_ACCOUNT_SLUG
            assert accounts[0].currency == "RUB"
            assert user.default_account_id == accounts[0].id
            assert len(categories) == len(INITIAL_CATEGORIES)
            assert {(item.kind, item.slug) for item in categories} == {
                (item.kind, item.slug) for item in INITIAL_CATEGORIES
            }
    finally:
        if user_id is not None:
            await _remove_owner(engine, user_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_owner_is_refreshed_without_allowing_chat_rebind() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = _synthetic_telegram_user_id()
    user_id: UUID | None = None

    try:
        async with factory() as setup:
            user = User(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_user_id,
                base_currency="RUB",
            )
            setup.add(user)
            await setup.flush()
            account = Account(
                user_id=user.id,
                name="Тестовый",
                slug="тестовый",
                currency="RUB",
            )
            setup.add(account)
            await setup.flush()
            user.default_account_id = account.id
            await setup.commit()
            user_id = user.id

        async with factory() as stale_session, factory() as updater:
            cached = await stale_session.scalar(select(User).where(User.id == user_id))
            assert cached is not None
            assert cached.base_currency == "RUB"

            await updater.execute(
                update(User).where(User.id == user_id).values(base_currency="USD")
            )
            await updater.commit()

            loaded = await ensure_owner_user(
                stale_session,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_user_id,
                locale="ru_RU",
                timezone="Europe/Moscow",
                currency="EUR",
            )
            assert loaded is cached
            assert loaded.base_currency == "USD"
            assert loaded.default_account_id == account.id
            await stale_session.rollback()

        async with factory() as rejected:
            with pytest.raises(OwnerChatBindingError):
                await ensure_owner_user(
                    rejected,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_user_id + 1,
                    locale="ru_RU",
                    timezone="Europe/Moscow",
                    currency="RUB",
                )
            await rejected.rollback()
    finally:
        if user_id is not None:
            await _remove_owner(engine, user_id)
        await engine.dispose()
