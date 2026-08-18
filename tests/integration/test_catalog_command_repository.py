import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import Account, Category, User
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.application.catalogs import (
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _synthetic_telegram_user_id() -> int:
    return 7_000_000_000 + uuid4().int % 1_000_000_000


@pytest.mark.asyncio
async def test_catalog_repository_flushes_without_committing() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = None

    try:
        async with factory() as setup:
            user = User(telegram_user_id=_synthetic_telegram_user_id())
            setup.add(user)
            await setup.commit()
            user_id = user.id

        async with factory() as session:
            result = await CatalogUseCases(SqlAlchemyCatalogRepository(session)).create_account(
                CreateAccountCommand(user_id, "Откатываемый", "rub")
            )
            staged_count = await session.scalar(
                select(func.count(Account.id)).where(Account.id == result.entity_id)
            )
            assert staged_count == 1
            await session.rollback()

        async with factory() as verification:
            persisted_count = await verification.scalar(
                select(func.count(Account.id)).where(Account.id == result.entity_id)
            )
            assert persisted_count == 0
    finally:
        if user_id is not None:
            async with factory() as cleanup:
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_use_cases_preserve_owner_versions_and_archive_invariants() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            owner = User(telegram_user_id=_synthetic_telegram_user_id())
            other_owner = User(telegram_user_id=_synthetic_telegram_user_id())
            session.add_all((owner, other_owner))
            await session.flush()
            primary = Account(user_id=owner.id, name="Основной", slug="основной")
            reserve = Account(user_id=owner.id, name="Резервный", slug="резервный")
            base_category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Базовая",
                slug="базовая",
                emoji="▫️",
            )
            mutable_category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Изменяемая",
                slug="изменяемая",
                emoji="▫️",
            )
            session.add_all((primary, reserve, base_category, mutable_category))
            await session.flush()
            owner.default_account_id = primary.id
            await session.flush()

            use_cases = CatalogUseCases(SqlAlchemyCatalogRepository(session))
            renamed = await use_cases.update_account(
                UpdateAccountCommand(owner.id, reserve.id, "Запасной", 1)
            )
            assert renamed.version == 2

            with pytest.raises(ObjectVersionConflictError):
                await use_cases.update_account(
                    UpdateAccountCommand(owner.id, reserve.id, "Устаревшее", 1)
                )
            with pytest.raises(EntityNotFoundError):
                await use_cases.update_account(
                    UpdateAccountCommand(other_owner.id, reserve.id, "Чужое", 2)
                )

            selected = await use_cases.set_default_account(
                SetDefaultAccountCommand(owner.id, reserve.id, 2)
            )
            assert (selected.version, selected.resulting_state) == (3, "default")
            with pytest.raises(ApplicationValidationError):
                await use_cases.archive_account(ArchiveAccountCommand(owner.id, reserve.id, 3))

            primary_selected = await use_cases.set_default_account(
                SetDefaultAccountCommand(owner.id, primary.id, 1)
            )
            assert primary_selected.version == 2
            archived = await use_cases.archive_account(
                ArchiveAccountCommand(owner.id, reserve.id, 3)
            )
            restored = await use_cases.restore_account(
                RestoreAccountCommand(owner.id, reserve.id, 4)
            )
            assert (archived.version, archived.resulting_state) == (4, "archived")
            assert (restored.version, restored.resulting_state) == (5, "active")

            created_category = await use_cases.create_category(
                CreateCategoryCommand(owner.id, "Доход", TransactionType.INCOME)
            )
            assert created_category.version == 1
            with pytest.raises(ApplicationValidationError):
                await use_cases.create_category(
                    CreateCategoryCommand(owner.id, "Доход", TransactionType.INCOME)
                )

            updated_category = await use_cases.update_category(
                UpdateCategoryCommand(owner.id, mutable_category.id, "Обновлённая", 1)
            )
            archived_category = await use_cases.archive_category(
                ArchiveCategoryCommand(owner.id, mutable_category.id, 2)
            )
            restored_category = await use_cases.restore_category(
                RestoreCategoryCommand(owner.id, mutable_category.id, 3)
            )
            assert updated_category.version == 2
            assert (archived_category.version, archived_category.resulting_state) == (
                3,
                "archived",
            )
            assert (restored_category.version, restored_category.resulting_state) == (4, "active")

            base_archived = await use_cases.archive_category(
                ArchiveCategoryCommand(owner.id, base_category.id, 1)
            )
            assert base_archived.version == 2
            with pytest.raises(ApplicationValidationError):
                await use_cases.archive_category(
                    ArchiveCategoryCommand(owner.id, mutable_category.id, 4)
                )

            await session.rollback()
    finally:
        await engine.dispose()
