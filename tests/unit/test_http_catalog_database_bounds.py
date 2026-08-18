from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ClauseElement

import finbot.adapters.database.repositories.http_catalogs as http_catalog_repository
import finbot.adapters.database.services.catalogs as catalog_services
import finbot.adapters.database.services.transactions as transaction_services
from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import Account, Category, CategoryRule, User
from finbot.adapters.database.repositories.http_catalogs import (
    SqlAlchemyCatalogQueryUnitOfWork,
)
from finbot.application.errors import CatalogUnavailableError
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000002")
CATEGORY_ID = UUID("018f0000-0000-7000-8000-000000000003")


def _sql(statement: object) -> str:
    assert isinstance(statement, ClauseElement)
    return str(
        statement.compile(
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


class ScalarSession:
    def __init__(self, *results: object) -> None:
        self.results = iter(results)
        self.statements: list[object] = []
        self.flushes = 0
        self.added: list[object] = []

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return next(self.results)

    async def flush(self) -> None:
        self.flushes += 1

    def add(self, value: object) -> None:
        self.added.append(value)


class ReturningScalar:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class CategoryRuleSession(ScalarSession):
    def __init__(self, rule_id: UUID, *results: object) -> None:
        super().__init__(*results)
        self.rule_id = rule_id
        self.execute_calls = 0

    async def execute(self, statement: object) -> ReturningScalar:
        self.statements.append(statement)
        self.execute_calls += 1
        return ReturningScalar(self.rule_id)


@pytest.mark.asyncio
async def test_destination_capacity_probes_stop_at_the_200th_owner_row_without_sort() -> None:
    account_session = ScalarSession(None)
    category_session = ScalarSession(None)

    await catalog_services.ensure_account_destination_capacity(
        cast(AsyncSession, account_session),
        OWNER_ID,
        archived=False,
    )
    await catalog_services.ensure_category_destination_capacity(
        cast(AsyncSession, category_session),
        OWNER_ID,
        archived=True,
    )

    for statement in (*account_session.statements, *category_session.statements):
        rendered = _sql(statement)
        assert "offset 199" in rendered
        assert "limit 1" in rendered
        assert "order by" not in rendered
        assert OWNER_ID.hex in rendered

    with pytest.raises(CatalogUnavailableError):
        await catalog_services.ensure_account_destination_capacity(
            cast(AsyncSession, ScalarSession(ACCOUNT_ID)),
            OWNER_ID,
            archived=False,
        )
    with pytest.raises(CatalogUnavailableError):
        await catalog_services.ensure_category_destination_capacity(
            cast(AsyncSession, ScalarSession(CATEGORY_ID)),
            OWNER_ID,
            archived=False,
        )


@pytest.mark.asyncio
async def test_category_rule_capacity_allows_existing_upsert_but_blocks_a_new_rule() -> None:
    rule_id = uuid7()
    saved = CategoryRule(
        id=rule_id,
        user_id=OWNER_ID,
        kind=TransactionType.EXPENSE.value,
        pattern="кофе",
        normalized_pattern="кофе",
        category_id=CATEGORY_ID,
        account_id=None,
        version=2,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    existing_session = CategoryRuleSession(rule_id, OWNER_ID, rule_id, saved)

    result = await SqlAlchemyCategoryRuleRepository(cast(AsyncSession, existing_session)).upsert(
        OWNER_ID,
        TransactionType.EXPENSE,
        CATEGORY_ID,
        "кофе",
        None,
    )

    assert result.id == rule_id
    assert existing_session.execute_calls == 1
    assert "for update" in _sql(existing_session.statements[0])
    assert "offset 511" not in _sql(existing_session.statements[1])

    full_session = CategoryRuleSession(rule_id, OWNER_ID, None, rule_id)
    with pytest.raises(CatalogUnavailableError):
        await SqlAlchemyCategoryRuleRepository(cast(AsyncSession, full_session)).upsert(
            OWNER_ID,
            TransactionType.EXPENSE,
            CATEGORY_ID,
            "новое правило",
            None,
        )

    assert "for update" in _sql(full_session.statements[0])
    assert "offset 511" in _sql(full_session.statements[2])
    assert "limit 1" in _sql(full_session.statements[2])
    assert full_session.execute_calls == 0


@pytest.mark.asyncio
async def test_finance_create_or_get_honors_active_cap_but_returns_existing_slug() -> None:
    existing = Account(
        id=ACCOUNT_ID,
        user_id=OWNER_ID,
        name="Существующий",
        slug="existing",
        type="other",
        currency="RUB",
        archived_at=None,
        version=1,
    )
    existing_session = ScalarSession(existing)
    returned = await transaction_services.create_or_get_account(
        cast(AsyncSession, existing_session),
        OWNER_ID,
        "  existing  ",
        "RUB",
    )

    assert returned is existing
    assert len(existing_session.statements) == 1
    assert existing_session.added == []
    assert existing_session.flushes == 0

    full_session = ScalarSession(None, ACCOUNT_ID)
    with pytest.raises(CatalogUnavailableError):
        await transaction_services.create_or_get_account(
            cast(AsyncSession, full_session),
            OWNER_ID,
            "new account",
            "RUB",
        )

    assert len(full_session.statements) == 2
    assert "offset 199" in _sql(full_session.statements[1])
    assert full_session.added == []
    assert full_session.flushes == 0


def _user() -> User:
    return User(
        id=OWNER_ID,
        telegram_user_id=42,
        telegram_chat_id=None,
        locale="ru_RU",
        timezone="Europe/Moscow",
        base_currency="RUB",
        fast_mode=False,
        default_account_id=None,
    )


@pytest.mark.asyncio
async def test_account_archive_uses_bounded_other_active_exists_before_destination_cap() -> None:
    account = Account(
        id=ACCOUNT_ID,
        user_id=OWNER_ID,
        name="Счёт",
        slug="account",
        type="card",
        currency="RUB",
        archived_at=None,
        version=3,
    )
    session = ScalarSession(_user(), account, uuid7(), None)

    result = await catalog_services.archive_account(
        cast(AsyncSession, session),
        OWNER_ID,
        ACCOUNT_ID,
        None,
        expected_version=3,
    )

    active_exists = _sql(session.statements[2])
    destination_cap = _sql(session.statements[3])
    assert "count(" not in active_exists
    assert "accounts.id !=" in active_exists
    assert "limit 1" in active_exists
    assert "offset 199" in destination_cap
    assert result.version == 4
    assert result.archived_at is not None
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_category_archive_uses_same_kind_bounded_exists_and_total_destination_cap() -> None:
    category = Category(
        id=CATEGORY_ID,
        user_id=OWNER_ID,
        kind="expense",
        name="Категория",
        slug="category",
        emoji="▫️",
        archived_at=None,
        version=5,
    )
    session = ScalarSession(_user(), category, uuid7(), None)

    result = await catalog_services.archive_category(
        cast(AsyncSession, session),
        OWNER_ID,
        CATEGORY_ID,
        expected_version=5,
    )

    active_exists = _sql(session.statements[2])
    destination_cap = _sql(session.statements[3])
    assert "count(" not in active_exists
    assert "categories.id !=" in active_exists
    assert "categories.kind = 'expense'" in active_exists
    assert "limit 1" in active_exists
    assert "categories.kind" not in destination_cap
    assert "offset 199" in destination_cap
    assert result.version == 6
    assert result.archived_at is not None
    assert session.flushes == 1


class EnteredContext:
    def __init__(self, session: object) -> None:
        self.session = session
        self.exit_args: tuple[object, object, object] | None = None

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.exit_args = (exc_type, exc, traceback)


class QuerySession:
    def __init__(self) -> None:
        self.connection_options: dict[str, str] | None = None
        self.statements: list[object] = []

    async def connection(self, *, execution_options: dict[str, str]) -> object:
        self.connection_options = execution_options
        return object()

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return object()


class QuerySessions:
    def __init__(self, context: EnteredContext) -> None:
        self.context = context

    def begin(self) -> EnteredContext:
        return self.context


@pytest.mark.asyncio
async def test_catalog_query_uow_closes_entered_read_only_snapshot_if_wiring_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = RuntimeError("synthetic catalog wiring failure")
    session = QuerySession()
    context = EnteredContext(session)

    def fail_wiring(_session: object) -> None:
        raise marker

    constructor: Callable[[object], None] = fail_wiring
    monkeypatch.setattr(http_catalog_repository, "SqlAlchemyQueryRepository", constructor)
    uow = SqlAlchemyCatalogQueryUnitOfWork(cast(Any, QuerySessions(context)))

    with pytest.raises(RuntimeError) as caught:
        await uow.__aenter__()

    assert caught.value is marker
    assert session.connection_options == {"isolation_level": "REPEATABLE READ"}
    assert len(session.statements) == 1
    assert "set transaction read only" in _sql(session.statements[0])
    assert context.exit_args is not None
    assert context.exit_args[0] is RuntimeError
    assert context.exit_args[1] is marker
