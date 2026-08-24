from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.database.models import Account, Category, Transaction, User
from finbot.adapters.database.repositories.http_finance import (
    SqlAlchemyFinanceQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.web_sessions import SqlAlchemyWebSessionRepository
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import SessionCheckStatus
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
ORIGIN = "https://miniapp.integration.test"


class ReadinessStub:
    async def check(self) -> None:
        pass


def _token(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).rstrip(b"=").decode("ascii")


async def _create_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int,
    timezone: str = "Europe/Moscow",
) -> User:
    async with factory.begin() as session:
        owner = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
            locale="ru_RU",
            timezone=timezone,
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        return owner


async def _delete_owners(
    factory: async_sessionmaker[AsyncSession],
    *owners: User,
) -> None:
    owner_ids = tuple(owner.id for owner in owners)
    async with factory.begin() as session:
        await session.execute(delete(Transaction).where(Transaction.user_id.in_(owner_ids)))
        await session.execute(delete(Account).where(Account.user_id.in_(owner_ids)))
        await session.execute(delete(Category).where(Category.user_id.in_(owner_ids)))
        await session.execute(delete(User).where(User.id.in_(owner_ids)))


async def _create_session(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    raw_session_token: str,
) -> None:
    digester = HttpSecurityDigester(SECURITY_KEY)
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            owner.id,
            digester.session(raw_session_token),
            digester.csrf(raw_session_token),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )


def _finance_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    digester = HttpSecurityDigester(SECURITY_KEY)
    return create_app(
        readiness_probe=ReadinessStub(),
        finance_service=FinanceQueryService(
            digester=digester,
            cursor_codec=TransactionCursorCodec(SECURITY_KEY),
            uow_factory=SqlAlchemyFinanceQueryUnitOfWorkFactory(factory),
            clock=lambda: NOW,
        ),
    )


@pytest.mark.asyncio
async def test_http_finance_is_owner_scoped_keyset_bounded_and_currency_safe() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _create_owner(factory, telegram_user_id=99_713_101)
    other_owner = await _create_owner(factory, telegram_user_id=99_713_102)
    session_token = _token(41)
    await _create_session(factory, owner, raw_session_token=session_token)
    occurred_at = NOW - timedelta(hours=1)
    try:
        async with factory.begin() as session:
            account = Account(
                user_id=owner.id,
                name="Synthetic owner account",
                slug="synthetic-owner-account",
                currency="RUB",
            )
            category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Synthetic owner category",
                slug="synthetic-owner-category",
                emoji="▫️",
            )
            other_account = Account(
                user_id=other_owner.id,
                name="Synthetic other account",
                slug="synthetic-other-account",
                currency="RUB",
            )
            other_category = Category(
                user_id=other_owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Synthetic other category",
                slug="synthetic-other-category",
                emoji="▫️",
            )
            session.add_all([account, category, other_account, other_category])
            await session.flush()
            transaction_ids = (
                UUID("018f0000-0000-7000-8000-000000000003"),
                UUID("018f0000-0000-7000-8000-000000000002"),
                UUID("018f0000-0000-7000-8000-000000000001"),
            )
            transactions = tuple(
                Transaction(
                    id=transaction_id,
                    user_id=owner.id,
                    type=TransactionType.EXPENSE.value,
                    amount_minor=2**53 + index,
                    currency="RUB",
                    account_id=account.id,
                    category_id=category.id,
                    occurred_at=occurred_at,
                    description="",
                )
                for index, transaction_id in enumerate(transaction_ids, start=1)
            )
            deleted = Transaction(
                user_id=owner.id,
                type=TransactionType.EXPENSE.value,
                amount_minor=700,
                currency="RUB",
                account_id=account.id,
                category_id=category.id,
                occurred_at=occurred_at + timedelta(seconds=1),
                description="",
                deleted_at=NOW - timedelta(seconds=1),
            )
            other_transaction = Transaction(
                user_id=other_owner.id,
                type=TransactionType.EXPENSE.value,
                amount_minor=800,
                currency="RUB",
                account_id=other_account.id,
                category_id=other_category.id,
                occurred_at=occurred_at,
                description="",
            )
            session.add_all([*transactions, deleted, other_transaction])
            await session.flush()

        app = _finance_app(factory)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
            cookies={"__Host-numismat_session": session_token},
            headers={
                "X-Session-Binding": HttpSecurityDigester(SECURITY_KEY).session_binding(
                    session_token
                )
            },
        ) as client:
            dashboard = await client.get("/api/v1/dashboard")
            first = await client.get("/api/v1/transactions?limit=2")
            cursor = first.json()["next_cursor"]
            second = await client.get(
                "/api/v1/transactions",
                params={"limit": 2, "cursor": cursor},
            )
            deleted_detail = await client.get(f"/api/v1/transactions/{deleted.id}")
            foreign_detail = await client.get(f"/api/v1/transactions/{other_transaction.id}")

        assert dashboard.status_code == 200
        totals = dashboard.json()["current_period"]["totals"]
        assert totals == [
            {
                "currency": "RUB",
                "expense_minor": str(sum(item.amount_minor for item in transactions)),
                "income_minor": "0",
                "net_minor": str(-sum(item.amount_minor for item in transactions)),
            }
        ]
        assert first.status_code == 200
        assert [item["id"] for item in first.json()["items"]] == [
            str(transaction_ids[0]),
            str(transaction_ids[1]),
        ]
        assert len(cursor) == 76
        assert [item["id"] for item in second.json()["items"]] == [str(transaction_ids[2])]
        assert second.json()["next_cursor"] is None
        assert deleted_detail.status_code == 200
        assert deleted_detail.json()["deleted_at"] is not None
        assert foreign_detail.status_code == 404
        assert foreign_detail.json()["error"]["message"] == "Объект не найден"
        assert all(
            response.headers["cache-control"] == "no-store, no-cache"
            for response in (dashboard, first, second, deleted_detail, foreign_detail)
        )
    finally:
        await _delete_owners(factory, owner, other_owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_filters_are_owner_scoped_and_cursor_bound_in_postgresql() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _create_owner(factory, telegram_user_id=99_713_105)
    other_owner = await _create_owner(factory, telegram_user_id=99_713_106)
    session_token = _token(61)
    await _create_session(factory, owner, raw_session_token=session_token)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    try:
        async with factory.begin() as session:
            account = Account(
                user_id=owner.id,
                name="Filter target account",
                slug="filter-target-account",
                currency="RUB",
            )
            other_account = Account(
                user_id=owner.id,
                name="Filter other account",
                slug="filter-other-account",
                currency="RUB",
            )
            expense_category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Filter target category",
                slug="filter-target-category",
                emoji="▫️",
            )
            other_expense_category = Category(
                user_id=owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Filter other category",
                slug="filter-other-category",
                emoji="▫️",
            )
            income_category = Category(
                user_id=owner.id,
                kind=TransactionType.INCOME.value,
                name="Filter income category",
                slug="filter-income-category",
                emoji="▫️",
            )
            foreign_account = Account(
                user_id=other_owner.id,
                name="Filter foreign account",
                slug="filter-foreign-account",
                currency="RUB",
            )
            foreign_category = Category(
                user_id=other_owner.id,
                kind=TransactionType.EXPENSE.value,
                name="Filter foreign category",
                slug="filter-foreign-category",
                emoji="▫️",
            )
            session.add_all(
                [
                    account,
                    other_account,
                    expense_category,
                    other_expense_category,
                    income_category,
                    foreign_account,
                    foreign_category,
                ]
            )
            await session.flush()

            def owner_transaction(
                *,
                occurred_at: datetime,
                kind: TransactionType = TransactionType.EXPENSE,
                currency: str = "RUB",
                account_id: UUID = account.id,
                category_id: UUID = expense_category.id,
                amount_minor: int = 101,
                deleted_at: datetime | None = None,
            ) -> Transaction:
                return Transaction(
                    user_id=owner.id,
                    type=kind.value,
                    amount_minor=amount_minor,
                    currency=currency,
                    account_id=account_id,
                    category_id=category_id,
                    occurred_at=occurred_at,
                    description="",
                    deleted_at=deleted_at,
                )

            matching = (
                owner_transaction(
                    occurred_at=start + timedelta(days=10),
                    amount_minor=2**53 + 201,
                ),
                owner_transaction(
                    occurred_at=start,
                    amount_minor=2**53 + 202,
                ),
            )
            excluded = (
                owner_transaction(occurred_at=start - timedelta(seconds=1)),
                owner_transaction(occurred_at=end),
                owner_transaction(
                    occurred_at=start + timedelta(days=1),
                    kind=TransactionType.INCOME,
                    category_id=income_category.id,
                ),
                owner_transaction(
                    occurred_at=start + timedelta(days=1),
                    account_id=other_account.id,
                ),
                owner_transaction(
                    occurred_at=start + timedelta(days=1),
                    category_id=other_expense_category.id,
                ),
                owner_transaction(
                    occurred_at=start + timedelta(days=1),
                    currency="USD",
                ),
            )
            deleted = owner_transaction(
                occurred_at=start + timedelta(days=1),
                deleted_at=NOW,
            )
            foreign = Transaction(
                user_id=other_owner.id,
                type=TransactionType.EXPENSE.value,
                amount_minor=303,
                currency="RUB",
                account_id=foreign_account.id,
                category_id=foreign_category.id,
                occurred_at=start + timedelta(days=5),
                description="",
            )
            session.add_all([*matching, *excluded, deleted, foreign])
            await session.flush()

        filters = {
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
            "type": "expense",
            "account_id": str(account.id),
            "category_id": str(expense_category.id),
            "currency": "RUB",
        }
        equivalent_filters = {
            **filters,
            "start": "2026-08-01T00:00:00.000000Z",
            "end": "2026-09-01T00:00:00.0Z",
        }
        app = _finance_app(factory)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
            cookies={"__Host-numismat_session": session_token},
            headers={
                "X-Session-Binding": HttpSecurityDigester(SECURITY_KEY).session_binding(
                    session_token
                )
            },
        ) as client:
            first = await client.get(
                "/api/v1/transactions",
                params={**filters, "limit": "1"},
            )
            cursor = first.json()["next_cursor"]
            second = await client.get(
                "/api/v1/transactions",
                params={**equivalent_filters, "limit": "1", "cursor": cursor},
            )
            changed_filter = await client.get(
                "/api/v1/transactions",
                params={**filters, "currency": "USD", "cursor": cursor},
            )
            removed_filters = await client.get(
                "/api/v1/transactions",
                params={"cursor": cursor},
            )
            unfiltered = await client.get("/api/v1/transactions")

        assert first.status_code == 200
        assert [item["id"] for item in first.json()["items"]] == [str(matching[0].id)]
        assert first.json()["items"][0]["amount_minor"] == str(matching[0].amount_minor)
        assert isinstance(cursor, str) and len(cursor) == 76 and "=" not in cursor
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["items"]] == [str(matching[1].id)]
        assert second.json()["next_cursor"] is None
        for rejected in (changed_filter, removed_filters):
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "invalid_cursor"
        assert unfiltered.status_code == 200
        unfiltered_ids = {item["id"] for item in unfiltered.json()["items"]}
        assert len(unfiltered_ids) == len(matching) + len(excluded)
        assert str(foreign.id) not in unfiltered_ids
        assert str(deleted.id) not in unfiltered_ids
        assert all(
            response.headers["cache-control"] == "no-store, no-cache"
            for response in (first, second, changed_filter, removed_filters, unfiltered)
        )
    finally:
        await _delete_owners(factory, owner, other_owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_finance_uow_is_repeatable_read_read_only_and_authenticates_first() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _create_owner(factory, telegram_user_id=99_713_103)
    session_token = _token(51)
    await _create_session(factory, owner, raw_session_token=session_token)
    digester = HttpSecurityDigester(SECURITY_KEY)
    uow_factory = SqlAlchemyFinanceQueryUnitOfWorkFactory(factory)
    try:
        async with uow_factory() as uow:
            isolation = await uow.session.scalar(text("SHOW transaction_isolation"))
            read_only = await uow.session.scalar(text("SHOW transaction_read_only"))
            checked = await uow.auth.read_session(digester.session(session_token), now=NOW)
            initial_owner_count = await uow.session.scalar(
                select(func.count()).select_from(User).where(User.id == owner.id)
            )
            async with factory.begin() as concurrent_session:
                concurrent_session.add(
                    User(
                        telegram_user_id=99_713_104,
                        telegram_chat_id=99_713_104,
                        locale="ru_RU",
                        timezone="Europe/Moscow",
                        base_currency="RUB",
                    )
                )
            snapshot_owner_count = await uow.session.scalar(
                select(func.count()).select_from(User).where(User.telegram_user_id >= 99_713_103)
            )

        assert isolation == "repeatable read"
        assert read_only == "on"
        assert checked.status is SessionCheckStatus.ACTIVE
        assert checked.authenticated is not None
        assert checked.authenticated.owner.owner_id == owner.id
        assert initial_owner_count == 1
        assert snapshot_owner_count == 1

        with pytest.raises(DBAPIError):
            async with uow_factory() as uow:
                uow.session.add(
                    Account(
                        user_id=owner.id,
                        name="Must not persist",
                        slug="must-not-persist",
                        currency="RUB",
                    )
                )
                await uow.session.flush()

        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Account).where(Account.user_id == owner.id)
                )
                == 0
            )
    finally:
        async with factory.begin() as session:
            await session.execute(delete(User).where(User.telegram_user_id == 99_713_104))
        await _delete_owners(factory, owner)
        await engine.dispose()
