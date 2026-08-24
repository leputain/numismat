import asyncio
import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, Category, Draft, User
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.settings_mutations import (
    SqlAlchemySettingsMutationRepository,
)
from finbot.application.errors import ActiveDraftConflictError
from finbot.application.settings_mutations import (
    BeginAccountRenameCommand,
    ChangeTimezoneCommand,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.settings_mutations import (
    BeginSettingsInput,
    ChangeSettingsTimezone,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)


def _synthetic_telegram_user_id() -> int:
    return 8_300_000_000 + uuid4().int % 600_000_000


def _ingress(session: AsyncSession) -> BeginSettingsInput:
    return BeginSettingsInput(
        SqlAlchemySettingsMutationRepository(session),
        DraftUseCases(SqlAlchemyDraftRepository(session)),
    )


async def _setup(factory: async_sessionmaker[AsyncSession]) -> _Fixture:
    async with factory() as session:
        owner = User(
            telegram_user_id=_synthetic_telegram_user_id(),
            timezone="Europe/Moscow",
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Базовая",
            slug="базовая",
            emoji="▫️",
        )
        session.add_all((account, category))
        await session.flush()
        fixture = _Fixture(owner.id, account.id)
        await session.commit()
        return fixture


async def _cleanup(engine: AsyncEngine, fixture: _Fixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(Draft).where(Draft.user_id == fixture.owner_id))
        await connection.execute(delete(Category).where(Category.user_id == fixture.owner_id))
        await connection.execute(delete(Account).where(Account.user_id == fixture.owner_id))
        await connection.execute(delete(User).where(User.id == fixture.owner_id))


@pytest.mark.asyncio
async def test_settings_ingress_draft_rolls_back_with_its_unit_of_work() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)

    try:
        async with factory() as session:
            result = await _ingress(session).account_rename(
                BeginAccountRenameCommand(fixture.owner_id, fixture.account_id, 1)
            )
            assert dict(result.draft.payload) == {
                "account_id": str(fixture.account_id),
                "object_version": 1,
            }
            assert "ui_message_id" not in result.draft.payload
            assert (
                await session.scalar(
                    select(func.count(Draft.id)).where(Draft.user_id == fixture.owner_id)
                )
                == 1
            )
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Draft.id)).where(Draft.user_id == fixture.owner_id)
                )
                == 0
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_settings_ingress_creates_one_canonical_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    winner_created = asyncio.Event()
    contender_started = asyncio.Event()

    try:

        async def winner() -> str:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await _ingress(session).account_rename(
                    BeginAccountRenameCommand(fixture.owner_id, fixture.account_id, 1)
                )
                winner_created.set()
                await asyncio.wait_for(contender_started.wait(), timeout=2)
                await session.commit()
                return result.draft.state

        async def contender() -> str:
            await asyncio.wait_for(winner_created.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                contender_started.set()
                try:
                    await _ingress(session).account_rename(
                        BeginAccountRenameCommand(fixture.owner_id, fixture.account_id, 1)
                    )
                except ActiveDraftConflictError:
                    await session.rollback()
                    return "conflict"
                raise AssertionError("concurrent settings ingress unexpectedly succeeded")

        assert set(
            await asyncio.wait_for(
                asyncio.gather(winner(), contender()),
                timeout=8,
            )
        ) == {"settings_account_rename", "conflict"}

        async with factory() as verification:
            drafts = (
                await verification.scalars(select(Draft).where(Draft.user_id == fixture.owner_id))
            ).all()
            assert len(drafts) == 1
            assert drafts[0].state == "settings_account_rename"
            assert drafts[0].payload == {
                "account_id": str(fixture.account_id),
                "object_version": 1,
            }
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_timezone_change_uses_cas_and_rolls_back_cleanly() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)

    try:
        async with factory() as session:
            result = await ChangeSettingsTimezone(
                SqlAlchemySettingsMutationRepository(session)
            ).execute(
                ChangeTimezoneCommand(
                    fixture.owner_id,
                    1,
                    "Asia/Yekaterinburg",
                )
            )
            assert result.timezone == "Asia/Yekaterinburg"
            assert result.settings_version == 2
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(select(User.timezone).where(User.id == fixture.owner_id))
                == "Europe/Moscow"
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()
