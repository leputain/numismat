import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import Account, Category, Transaction, User
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.application.errors import EntityNotFoundError
from finbot.application.use_cases.queries import (
    GetDashboard,
    GetOwnerSettings,
    GetTransaction,
    ListAccounts,
    ListCategories,
    ListTransactions,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.mark.asyncio
async def test_shared_query_repository_is_owner_scoped_and_currency_safe() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(
            telegram_user_id=900000061,
            locale="ru_RU",
            timezone="Europe/Moscow",
            base_currency="RUB",
            fast_mode=True,
        )
        other_owner = User(telegram_user_id=900000062)
        session.add_all([owner, other_owner])
        await session.flush()

        rub_account = Account(
            user_id=owner.id,
            name="Синтетический RUB",
            slug="synthetic-rub",
            currency="RUB",
        )
        usd_account = Account(
            user_id=owner.id,
            name="Синтетический USD",
            slug="synthetic-usd",
            currency="USD",
        )
        archived_account = Account(
            user_id=owner.id,
            name="Синтетический архив",
            slug="synthetic-archive",
            currency="RUB",
            archived_at=datetime.now(UTC),
        )
        expense_category = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Синтетические расходы",
            slug="synthetic-expense",
            emoji="▫️",
        )
        income_category = Category(
            user_id=owner.id,
            kind=TransactionType.INCOME.value,
            name="Синтетические доходы",
            slug="synthetic-income",
            emoji="▫️",
        )
        other_account = Account(
            user_id=other_owner.id,
            name="Чужой счёт",
            slug="other-account",
            currency="RUB",
        )
        other_category = Category(
            user_id=other_owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Чужая категория",
            slug="other-category",
            emoji="▫️",
        )
        session.add_all(
            [
                rub_account,
                usd_account,
                archived_account,
                expense_category,
                income_category,
                other_account,
                other_category,
            ]
        )
        await session.flush()
        owner.default_account_id = rub_account.id
        other_owner.default_account_id = other_account.id

        current_start = datetime(2026, 8, 1, tzinfo=UTC)
        current_end = datetime(2026, 9, 1, tzinfo=UTC)
        previous_start = datetime(2026, 7, 1, tzinfo=UTC)
        previous_end = datetime(2026, 8, 1, tzinfo=UTC)
        current_expense = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=1200,
            currency="RUB",
            account_id=rub_account.id,
            category_id=expense_category.id,
            occurred_at=current_start + timedelta(days=1),
            description="",
        )
        current_income = Transaction(
            user_id=owner.id,
            type=TransactionType.INCOME.value,
            amount_minor=3000,
            currency="RUB",
            account_id=rub_account.id,
            category_id=income_category.id,
            occurred_at=current_start + timedelta(days=2),
            description="",
        )
        current_usd_expense = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=700,
            currency="USD",
            account_id=usd_account.id,
            category_id=expense_category.id,
            occurred_at=current_start + timedelta(days=3),
            description="",
        )
        previous_expense = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=500,
            currency="RUB",
            account_id=rub_account.id,
            category_id=expense_category.id,
            occurred_at=previous_start + timedelta(days=1),
            description="",
        )
        deleted_expense = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=600,
            currency="RUB",
            account_id=rub_account.id,
            category_id=expense_category.id,
            occurred_at=current_start + timedelta(days=4),
            description="",
            deleted_at=datetime.now(UTC),
        )
        other_transaction = Transaction(
            user_id=other_owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=900,
            currency="RUB",
            account_id=other_account.id,
            category_id=other_category.id,
            occurred_at=current_start + timedelta(days=1),
            description="",
        )
        session.add_all(
            [
                current_expense,
                current_income,
                current_usd_expense,
                previous_expense,
                deleted_expense,
                other_transaction,
            ]
        )
        await session.flush()

        repository = SqlAlchemyQueryRepository(session)

        owner_settings = await GetOwnerSettings(repository)(owner.id)
        assert owner_settings.default_account_id == rub_account.id
        assert owner_settings.fast_mode is True
        assert {item.account_id for item in await ListAccounts(repository)(owner.id)} == {
            rub_account.id,
            usd_account.id,
        }
        archived_accounts = await ListAccounts(repository)(owner.id, archived=True)
        assert tuple(item.account_id for item in archived_accounts) == (archived_account.id,)
        expense_categories = await ListCategories(repository)(
            owner.id, kind=TransactionType.EXPENSE
        )
        assert tuple(item.category_id for item in expense_categories) == (expense_category.id,)

        page = await ListTransactions(repository)(owner.id, page_size=10)
        deleted_page = await ListTransactions(repository)(owner.id, page_size=10, deleted=True)
        assert page.total == 4
        assert deleted_page.total == 1
        assert all(item.transaction_id != other_transaction.id for item in page.items)
        assert await GetTransaction(repository)(owner.id, current_expense.id)
        with pytest.raises(EntityNotFoundError):
            await GetTransaction(repository)(other_owner.id, current_expense.id)

        dashboard = await GetDashboard(repository)(
            owner.id,
            current_start,
            current_end,
            previous_start,
            previous_end,
            category_limit=10,
            recent_limit=10,
        )
        assert [item.currency for item in dashboard.totals] == ["RUB", "USD"]
        assert [(item.currency, item.expense_minor) for item in dashboard.totals] == [
            ("RUB", 1200),
            ("USD", 700),
        ]
        assert [(item.currency, item.expense_minor) for item in dashboard.previous_totals] == [
            ("RUB", 500)
        ]
        assert {(item.currency, item.amount_minor) for item in dashboard.top_categories} == {
            ("RUB", 1200),
            ("USD", 700),
        }
        assert len(dashboard.recent_transactions) == 3

        await session.rollback()
    await engine.dispose()
