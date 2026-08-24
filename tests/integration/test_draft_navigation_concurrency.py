import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, Category, Draft, User
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.adapters.database.repositories.draft_navigation import (
    SqlAlchemyDraftNavigationCatalogRepository,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.application.catalogs import (
    ArchiveCategoryCommand,
)
from finbot.application.draft_navigation import (
    DraftCatalogRef,
    DraftNavigationAction,
    DraftNavigationCommand,
)
from finbot.application.dto import DraftRef
from finbot.application.errors import (
    DraftRevisionConflictError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _synthetic_telegram_user_id() -> int:
    return 7_500_000_000 + uuid4().int % 1_000_000_000


def _navigation(session: AsyncSession) -> DraftNavigationUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return DraftNavigationUseCases(
        DraftUseCases(SqlAlchemyDraftRepository(session)),
        GetOwnerSettings(reader),
        ListAccounts(reader),
        ListCategories(reader),
        SqlAlchemyDraftNavigationCatalogRepository(session),
    )


async def _setup(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, DraftRef]:
    async with factory() as session:
        telegram_id = _synthetic_telegram_user_id()
        owner = User(telegram_user_id=telegram_id, telegram_chat_id=telegram_id)
        session.add(owner)
        await session.flush()
        primary = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            currency="RUB",
        )
        reserve = Account(
            user_id=owner.id,
            name="Резервный",
            slug="резервный",
            currency="RUB",
        )
        fallback = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Другое",
            slug="другое",
            emoji="▫️",
        )
        selected = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Выбранная",
            slug="выбранная",
            emoji="▫️",
        )
        session.add_all((primary, reserve, fallback, selected))
        await session.flush()
        owner.default_account_id = primary.id
        draft = await SqlAlchemyDraftRepository(session).create_if_absent(
            owner.id,
            "wizard_category",
            {
                "flow": "wizard",
                "type": TransactionType.EXPENSE.value,
                "amount_minor": 12_345,
            },
        )
        await session.commit()
        return owner.id, selected.id, draft.ref


async def _cleanup(engine: AsyncEngine, owner_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Draft).where(Draft.user_id == owner_id))
        await connection.execute(delete(Category).where(Category.user_id == owner_id))
        await connection.execute(delete(Account).where(Account.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


@pytest.mark.asyncio
async def test_selection_rejects_a_category_archived_by_the_serialized_winner() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id, category_id, draft_ref = await _setup(factory)
    archived = asyncio.Event()
    selector_started = asyncio.Event()

    try:

        async def archive_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await CatalogUseCases(
                    SqlAlchemyCatalogRepository(session)
                ).archive_category(ArchiveCategoryCommand(owner_id, category_id, 1))
                assert result.version == 2
                archived.set()
                await asyncio.wait_for(selector_started.wait(), timeout=2)
                await session.commit()

        async def select_loser() -> None:
            await asyncio.wait_for(archived.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                selector_started.set()
                with pytest.raises(ObjectVersionConflictError) as conflict:
                    await _navigation(session).execute(
                        DraftNavigationCommand(
                            owner_id,
                            draft_ref,
                            DraftNavigationAction.SELECT_CATEGORY,
                            DraftCatalogRef(category_id, 1),
                        )
                    )
                assert conflict.value.current_version == 2
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(archive_winner(), select_loser()),
            timeout=8,
        )

        async with factory() as verification:
            draft = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            category = await verification.scalar(select(Category).where(Category.id == category_id))
            assert draft is not None and draft.revision == draft_ref.revision
            assert category is not None and category.archived_at is not None
    finally:
        await _cleanup(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_double_click_applies_one_exact_draft_transition() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id, category_id, draft_ref = await _setup(factory)
    winner_updated = asyncio.Event()
    contender_preloaded = asyncio.Event()

    try:

        async def select_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await _navigation(session).execute(
                    DraftNavigationCommand(
                        owner_id,
                        draft_ref,
                        DraftNavigationAction.SELECT_CATEGORY,
                        DraftCatalogRef(category_id, 1),
                    )
                )
                assert result.draft is not None and result.draft.revision == 2
                winner_updated.set()
                await asyncio.wait_for(contender_preloaded.wait(), timeout=2)
                await session.commit()

        async def select_contender() -> None:
            await asyncio.wait_for(winner_updated.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                cached = await session.scalar(select(Draft).where(Draft.user_id == owner_id))
                assert cached is not None and cached.revision == draft_ref.revision
                contender_preloaded.set()
                with pytest.raises(DraftRevisionConflictError) as conflict:
                    await _navigation(session).execute(
                        DraftNavigationCommand(
                            owner_id,
                            draft_ref,
                            DraftNavigationAction.SELECT_CATEGORY,
                            DraftCatalogRef(category_id, 1),
                        )
                    )
                assert conflict.value.current_revision == 2
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(select_winner(), select_contender()),
            timeout=8,
        )

        async with factory() as verification:
            draft = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert draft is not None
            assert draft.revision == 2
            assert draft.state == "wizard_account"
            assert draft.payload["category_id"] == str(category_id)
    finally:
        await _cleanup(engine, owner_id)
        await engine.dispose()
