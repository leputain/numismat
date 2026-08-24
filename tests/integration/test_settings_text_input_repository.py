import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.database.models import Account, Category, Draft, User
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.settings_text_input import (
    SqlAlchemySettingsTextInputRepository,
)
from finbot.application.settings_text_input import SettingsTextInputCommand
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.settings_text_input import SettingsTextInputUseCase
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _synthetic_telegram_user_id() -> int:
    return 8_900_000_000 + uuid4().int % 90_000_000


async def _cleanup_owner(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
) -> None:
    async with factory() as session:
        await session.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await session.execute(delete(Draft).where(Draft.user_id == owner_id))
        await session.execute(delete(Category).where(Category.user_id == owner_id))
        await session.execute(delete(Account).where(Account.user_id == owner_id))
        await session.execute(delete(User).where(User.id == owner_id))
        await session.commit()


def _use_case(session: AsyncSession) -> SettingsTextInputUseCase:
    targets = SqlAlchemySettingsTextInputRepository(session)
    return SettingsTextInputUseCase(
        targets,
        CatalogUseCases(SqlAlchemyCatalogRepository(session)),
        DraftUseCases(SqlAlchemyDraftRepository(session)),
    )


@pytest.mark.asyncio
async def test_catalog_mutation_and_exact_draft_delete_share_rollback_boundary() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id: UUID | None = None

    try:
        async with factory() as setup:
            telegram_id = _synthetic_telegram_user_id()
            owner = User(
                telegram_user_id=telegram_id,
                telegram_chat_id=telegram_id,
                locale="ru",
                timezone="Europe/Moscow",
                base_currency="RUB",
            )
            setup.add(owner)
            await setup.flush()
            owner_id = owner.id
            account = Account(
                user_id=owner.id,
                name="Исходный",
                slug="исходный",
                currency="RUB",
            )
            setup.add(account)
            await setup.flush()
            draft = Draft(
                user_id=owner.id,
                state="settings_account_rename",
                payload={"account_id": str(account.id), "object_version": 1},
            )
            setup.add(draft)
            await setup.commit()
            original_ref = (draft.id, draft.revision)

        async with factory() as session:
            targets = SqlAlchemySettingsTextInputRepository(session)
            active = await targets.lock_active(owner_id)
            result = await _use_case(session).execute(
                SettingsTextInputCommand(owner_id, active.ref, "Откатываемое имя")
            )
            assert result.account is not None and result.account.version == 2
            assert await session.scalar(select(Draft.id).where(Draft.user_id == owner_id)) is None
            await session.rollback()

        async with factory() as verification:
            stored_draft = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            stored_account = await verification.scalar(
                select(Account).where(Account.user_id == owner_id)
            )
            assert stored_draft is not None
            assert (stored_draft.id, stored_draft.revision) == original_ref
            assert stored_account is not None
            assert (stored_account.name, stored_account.version) == ("Исходный", 1)
    finally:
        if owner_id is not None:
            await _cleanup_owner(factory, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_revision_rename_has_one_success_and_one_cas_failure() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id: UUID | None = None

    try:
        async with factory() as setup:
            telegram_id = _synthetic_telegram_user_id()
            owner = User(
                telegram_user_id=telegram_id,
                telegram_chat_id=telegram_id,
                locale="ru",
                timezone="Europe/Moscow",
                base_currency="RUB",
            )
            setup.add(owner)
            await setup.flush()
            owner_id = owner.id
            category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Исходная",
                slug="исходная",
                emoji="▫️",
            )
            setup.add(category)
            await setup.flush()
            draft = Draft(
                user_id=owner.id,
                state="settings_category_rename",
                payload={
                    "category_id": str(category.id),
                    "kind": "expense",
                    "object_version": 1,
                },
            )
            setup.add(draft)
            await setup.commit()
            expected = draft.id, draft.revision

        async def submit(name: str) -> str:
            async with factory() as session:
                try:
                    targets = SqlAlchemySettingsTextInputRepository(session)
                    active = await targets.lock_active(owner_id)
                    assert (active.draft_id, active.revision) == expected
                    result = await _use_case(session).execute(
                        SettingsTextInputCommand(owner_id, active.ref, name)
                    )
                    await session.commit()
                    return result.category.name if result.category is not None else "missing"
                except Exception as error:
                    await session.rollback()
                    return type(error).__name__

        first, second = await asyncio.gather(submit("Первое имя"), submit("Второе имя"))
        assert {first, second}.intersection({"Первое имя", "Второе имя"})
        assert [first, second].count("SettingsTextInputNotApplicableError") == 1

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Draft.id)).where(Draft.user_id == owner_id)
                )
                == 0
            )
            stored_category = await verification.scalar(
                select(Category).where(Category.user_id == owner_id)
            )
            assert stored_category is not None
            assert stored_category.name in {"Первое имя", "Второе имя"}
            assert stored_category.version == 2
    finally:
        if owner_id is not None:
            await _cleanup_owner(factory, owner_id)
        await engine.dispose()
