import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    TelegramDraftPresentation,
    User,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.finance_draft_text_input import (
    SqlAlchemyFinanceDraftTextInputCatalogRepository,
    SqlAlchemyFinanceDraftTextTargetRepository,
)
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.application.finance_draft_text_input import FinanceDraftTextInputCommand
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.finance_draft_text_input import (
    FinanceDraftTextInputUseCase,
)
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _synthetic_telegram_user_id() -> int:
    return 8_500_000_000 + uuid4().int % 400_000_000


async def _cleanup_owner(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
) -> None:
    async with factory() as session:
        await session.execute(delete(Draft).where(Draft.user_id == owner_id))
        await session.execute(delete(Category).where(Category.user_id == owner_id))
        await session.execute(delete(Account).where(Account.user_id == owner_id))
        await session.execute(delete(User).where(User.id == owner_id))
        await session.commit()


@pytest.mark.asyncio
async def test_catalog_creation_draft_cas_and_projection_share_rollback_boundary() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id: UUID | None = None

    try:
        async with factory() as setup:
            owner = User(
                telegram_user_id=_synthetic_telegram_user_id(),
                telegram_chat_id=_synthetic_telegram_user_id(),
                locale="ru",
                timezone="Europe/Moscow",
                base_currency="RUB",
            )
            setup.add(owner)
            await setup.flush()
            owner_id = owner.id
            account = Account(
                user_id=owner.id,
                name="Основной",
                slug="основной",
                currency="RUB",
            )
            category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Исходная",
                slug="исходная",
                emoji="▫️",
            )
            setup.add_all((account, category))
            await setup.flush()
            owner.default_account_id = account.id
            draft = Draft(
                user_id=owner.id,
                state="custom_category",
                payload={
                    "flow": "wizard",
                    "type": "expense",
                    "amount_minor": 12_345,
                    "custom_back_state": "wizard_category",
                },
            )
            setup.add(draft)
            await setup.flush()
            setup.add(
                TelegramDraftPresentation(
                    draft_id=draft.id,
                    chat_id=93_000_003,
                    message_id=94_000_004,
                    rendered_revision=draft.revision,
                )
            )
            await setup.commit()
            original_draft_id = draft.id

        async with factory() as session:
            targets = SqlAlchemyFinanceDraftTextTargetRepository(session)
            active = await targets.lock_active(owner_id)
            assert active.draft_id == original_draft_id
            assert (
                await targets.presentation_message_id(
                    owner_id,
                    active.ref,
                    93_000_003,
                )
                == 94_000_004
            )

            queries = SqlAlchemyQueryRepository(session)
            catalogs = SqlAlchemyFinanceDraftTextInputCatalogRepository(session)
            use_case = FinanceDraftTextInputUseCase(
                DraftUseCases(SqlAlchemyDraftRepository(session)),
                GetOwnerSettings(queries),
                ListAccounts(queries),
                ListCategories(queries),
                catalogs,
            )
            result = await use_case.execute(
                FinanceDraftTextInputCommand(owner_id, active.ref, "Откатываемая")
            )
            assert result.draft.state == "wizard_account"
            assert result.draft.revision == active.revision + 1
            assert result.choices.accounts
            staged_categories = await session.scalar(
                select(func.count(Category.id)).where(
                    Category.user_id == owner_id,
                    Category.slug == "откатываемая",
                )
            )
            assert staged_categories == 1
            await session.rollback()

        async with factory() as verification:
            stored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert stored is not None
            assert stored.id == original_draft_id
            assert stored.state == "custom_category"
            assert stored.revision == 1
            persisted_categories = await verification.scalar(
                select(func.count(Category.id)).where(
                    Category.user_id == owner_id,
                    Category.slug == "откатываемая",
                )
            )
            assert persisted_categories == 0
    finally:
        if owner_id is not None:
            await _cleanup_owner(factory, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_or_get_reuses_active_catalog_rows_without_committing() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id: UUID | None = None

    try:
        async with factory() as setup:
            owner = User(telegram_user_id=_synthetic_telegram_user_id())
            setup.add(owner)
            await setup.commit()
            owner_id = owner.id

        async with factory() as session:
            repository = SqlAlchemyFinanceDraftTextInputCatalogRepository(session)
            first_category = await repository.create_or_get_category(
                owner_id,
                "Повторяемая",
                TransactionType.EXPENSE,
            )
            second_category = await repository.create_or_get_category(
                owner_id,
                "повторяемая",
                TransactionType.EXPENSE,
            )
            first_account = await repository.create_or_get_account(
                owner_id,
                "Кошелёк",
                "RUB",
            )
            second_account = await repository.create_or_get_account(
                owner_id,
                "кошелёк",
                "RUB",
            )
            assert first_category.category_id == second_category.category_id
            assert first_account.account_id == second_account.account_id
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Category.id)).where(Category.user_id == owner_id)
                )
                == 0
            )
            assert (
                await verification.scalar(
                    select(func.count(Account.id)).where(Account.user_id == owner_id)
                )
                == 0
            )
    finally:
        if owner_id is not None:
            await _cleanup_owner(factory, owner_id)
        await engine.dispose()
