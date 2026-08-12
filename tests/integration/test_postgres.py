import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import Account, Category, User
from finbot.adapters.database.queries.reports import totals_by_currency
from finbot.adapters.database.queries.transactions import get_transaction_details
from finbot.adapters.database.services.catalogs import (
    archive_account,
    archive_category,
    create_account,
    create_category,
    rename_account,
    rename_category,
    restore_account,
    restore_category,
    set_default_account,
)
from finbot.adapters.database.services.transactions import (
    clear_draft,
    edit_transaction,
    get_draft,
    put_draft,
    restore_transaction,
    save_transaction,
    soft_delete_transaction,
    start_draft,
    undo_last_action,
)
from finbot.adapters.database.services.updates import claim_update
from finbot.application.rules import resolve_learned_category
from finbot.application.services.csv_export import build_csv
from finbot.domain.errors import StaleObjectError
from finbot.domain.transactions import TransactionDraft, TransactionType, period_bounds

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.mark.asyncio
async def test_postgres_test_database_is_safe() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
        tables = await connection.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        assert "transactions" in set(tables)
    await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_lifecycle_reports_csv_and_undo() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000001, locale="ru_RU", timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        # Legacy installations used slugs with spaces; they must remain writable.
        account = Account(user_id=user.id, name="Тестовая карта", slug="тестовая карта")
        category = Category(
            user_id=user.id,
            kind="expense",
            name="Тестовая категория",
            slug="тестовая-категория",
            emoji="🧪",
        )
        session.add_all([account, category])
        await session.flush()
        user.default_account_id = account.id

        assert await claim_update(session, 990000001)
        assert not await claim_update(session, 990000001)
        transaction = await save_transaction(
            session,
            user.id,
            TransactionDraft(
                amount_minor=10000,
                type=TransactionType.EXPENSE,
                category_hint=category.slug,
                account_hint=account.slug,
                description="интеграционный тест",
            ),
            update_id=990000001,
            default_account_id=account.id,
        )
        assert transaction.version == 1

        deleted = await soft_delete_transaction(session, user.id, transaction.id, 1)
        assert deleted.deleted_at is not None
        with pytest.raises(StaleObjectError):
            await soft_delete_transaction(session, user.id, transaction.id, 1)
        restored = await restore_transaction(session, user.id, transaction.id, 2)
        assert restored.deleted_at is None

        changed = await edit_transaction(session, user.id, transaction.id, 3, amount_minor=25000)
        assert changed.amount_minor == 25000
        undone = await undo_last_action(session, user.id)
        assert undone is not None and undone.action == "update"
        assert transaction.amount_minor == 10000

        start, end = period_bounds(datetime.now(UTC).date(), "UTC")
        summary = await totals_by_currency(session, user.id, start, end)
        assert summary["RUB"]["expense"] == 10000
        details = await get_transaction_details(session, user.id, transaction.id)
        assert details is not None
        exported = build_csv([details]).decode("utf-8-sig")
        assert "100,00" in exported
        assert "Тестовая карта" in exported
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_and_category_catalog_lifecycle() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000003, locale="ru_RU", timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        primary = Account(user_id=user.id, name="Основная", slug="основная")
        base_category = Category(
            user_id=user.id,
            kind="expense",
            name="Другое",
            slug="другое",
            emoji="📦",
        )
        session.add_all([primary, base_category])
        await session.flush()
        user.default_account_id = primary.id

        cash = await create_account(session, user.id, "Наличные", "RUB")
        assert cash.name == "Наличные"
        cash = await rename_account(session, user.id, cash.id, "Кошелёк")
        assert cash.slug == "кошелёк"
        with pytest.raises(ValueError, match="уже"):
            await create_account(session, user.id, "Кошелёк", "RUB")

        archived_cash = await archive_account(session, user.id, cash.id, user.default_account_id)
        assert archived_cash.archived_at is not None
        restored_cash = await restore_account(session, user.id, cash.id)
        assert restored_cash.archived_at is None
        with pytest.raises(ValueError, match="основной"):
            await archive_account(session, user.id, primary.id, user.default_account_id)

        user.default_account_id = cash.id
        await archive_account(session, user.id, primary.id, user.default_account_id)
        user.default_account_id = None
        with pytest.raises(ValueError, match="последний"):
            await archive_account(session, user.id, cash.id, user.default_account_id)
        await restore_account(session, user.id, primary.id)

        pets = await create_category(session, user.id, "Питомцы", "expense")
        pets = await rename_category(session, user.id, pets.id, "Домашние животные")
        assert pets.slug == "домашние-животные"
        archived_pets = await archive_category(session, user.id, pets.id)
        assert archived_pets.archived_at is not None
        restored_pets = await restore_category(session, user.id, pets.id)
        assert restored_pets.archived_at is None
        await archive_category(session, user.id, base_category.id)
        with pytest.raises(ValueError, match="последнюю"):
            await archive_category(session, user.id, pets.id)
        await restore_category(session, user.id, base_category.id)

        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_mutations_reject_stale_and_aba_commands() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000006, locale="ru_RU", timezone="Europe/Moscow")
        session.add(user)
        await session.flush()
        primary = Account(user_id=user.id, name="Основной", slug="основной")
        mutable = Account(user_id=user.id, name="Запасной", slug="запасной")
        base_category = Category(
            user_id=user.id,
            kind="expense",
            name="Базовая",
            slug="базовая",
            emoji="📦",
        )
        mutable_category = Category(
            user_id=user.id,
            kind="expense",
            name="Изменяемая",
            slug="изменяемая",
            emoji="🧪",
        )
        session.add_all((primary, mutable, base_category, mutable_category))
        await session.flush()
        user.default_account_id = primary.id

        renamed_account = await rename_account(
            session,
            user.id,
            mutable.id,
            "Резервный",
            expected_version=1,
        )
        assert renamed_account.version == 2
        with pytest.raises(StaleObjectError):
            await rename_account(
                session,
                user.id,
                mutable.id,
                "Устаревшее имя",
                expected_version=1,
            )

        default_account = await set_default_account(
            session, user.id, mutable.id, expected_version=2
        )
        assert user.default_account_id == mutable.id
        assert default_account.version == 3
        with pytest.raises(StaleObjectError):
            await archive_account(
                session,
                user.id,
                mutable.id,
                user.default_account_id,
                expected_version=2,
            )

        primary = await set_default_account(session, user.id, primary.id, expected_version=1)
        assert primary.version == 2
        unchanged_default = await set_default_account(
            session, user.id, primary.id, expected_version=2
        )
        assert unchanged_default.version == 2
        archived_account = await archive_account(
            session,
            user.id,
            mutable.id,
            user.default_account_id,
            expected_version=3,
        )
        assert archived_account.version == 4
        with pytest.raises(StaleObjectError):
            await archive_account(
                session,
                user.id,
                mutable.id,
                user.default_account_id,
                expected_version=3,
            )
        restored_account = await restore_account(session, user.id, mutable.id, expected_version=4)
        assert restored_account.version == 5
        with pytest.raises(StaleObjectError):
            await restore_account(session, user.id, mutable.id, expected_version=4)
        with pytest.raises(StaleObjectError):
            await rename_account(
                session,
                user.id,
                mutable.id,
                "ABA не прошло",
                expected_version=3,
            )

        renamed_category = await rename_category(
            session,
            user.id,
            mutable_category.id,
            "Обновлённая",
            expected_version=1,
        )
        assert renamed_category.version == 2
        with pytest.raises(StaleObjectError):
            await archive_category(session, user.id, mutable_category.id, expected_version=1)
        archived_category = await archive_category(
            session, user.id, mutable_category.id, expected_version=2
        )
        assert archived_category.version == 3
        with pytest.raises(StaleObjectError):
            await archive_category(session, user.id, mutable_category.id, expected_version=2)
        restored_category = await restore_category(
            session, user.id, mutable_category.id, expected_version=3
        )
        assert restored_category.version == 4
        with pytest.raises(StaleObjectError):
            await restore_category(session, user.id, mutable_category.id, expected_version=3)
        with pytest.raises(StaleObjectError):
            await rename_category(
                session,
                user.id,
                mutable_category.id,
                "ABA не прошло",
                expected_version=2,
            )

        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_draft_revision_and_flow_replacement() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000004)
        session.add(user)
        await session.flush()

        first = await start_draft(session, user.id, "amount", {"type": "expense"})
        first_id = first.id
        assert first.revision == 1

        changed = await put_draft(
            session,
            user.id,
            "category",
            {"type": "expense", "amount_minor": 100},
            expected_revision=1,
        )
        assert changed.id == first_id
        assert changed.revision == 2
        with pytest.raises(StaleObjectError, match="Черновик"):
            await put_draft(
                session,
                user.id,
                "account",
                {},
                expected_revision=1,
            )

        replacement = await start_draft(session, user.id, "amount", {"type": "income"})
        assert replacement.id != first_id
        assert replacement.revision == 1
        await clear_draft(session, user.id, expected_revision=1)
        assert await get_draft(session, user.id) is None
        with pytest.raises(StaleObjectError, match="Черновик"):
            await put_draft(
                session,
                user.id,
                "category",
                {},
                expected_revision=1,
            )
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_category_rule_repository_respects_scope_and_upserts() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000005)
        session.add(user)
        await session.flush()
        account = Account(user_id=user.id, name="Карта", slug="карта")
        groceries = Category(
            user_id=user.id,
            kind="expense",
            name="Продукты",
            slug="продукты",
        )
        cafes = Category(
            user_id=user.id,
            kind="expense",
            name="Кафе",
            slug="кафе",
        )
        session.add_all((account, groceries, cafes))
        await session.flush()

        repository = SqlAlchemyCategoryRuleRepository(session)
        global_rule = await repository.upsert(
            user.id,
            TransactionType.EXPENSE,
            groceries.id,
            "кофе",
            None,
        )
        scoped_rule = await repository.upsert(
            user.id,
            TransactionType.EXPENSE,
            cafes.id,
            "кофе",
            account.id,
        )
        decision = await resolve_learned_category(
            repository,
            user_id=user.id,
            kind=TransactionType.EXPENSE,
            account_id=account.id,
            description="Кофе по дороге",
        )
        assert decision is not None
        assert decision.rule_id == scoped_rule.id

        updated = await repository.upsert(
            user.id,
            TransactionType.EXPENSE,
            cafes.id,
            "кофе",
            None,
        )
        assert updated.id == global_rule.id
        assert updated.category_id == cafes.id
        assert updated.version == 2
        await session.rollback()
    await engine.dispose()
