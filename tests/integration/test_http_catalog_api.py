from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import (
    Account,
    Category,
    HttpIdempotencyRecord,
    User,
    WebSession,
)
from finbot.adapters.database.repositories.http_catalogs import (
    SqlAlchemyCatalogQueryUnitOfWork,
    SqlAlchemyCatalogQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_finance import (
    SqlAlchemyFinanceQueryUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.security_values import SessionTokenDigest
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import AuthPersistence, SessionCheck
from finbot.adapters.http.catalogs.ports import CatalogQueryUnitOfWork
from finbot.adapters.http.catalogs.service import HttpCatalogService
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.adapters.http.mutations.ports import MutationUnitOfWork, MutationUnitOfWorkFactory
from finbot.adapters.http.mutations.service import HttpMutationExecutor
from finbot.application.catalogs import BoundedCatalogReader
from finbot.application.dto import AccountSnapshot, CategorySnapshot, OwnerSnapshot
from finbot.application.ports import OwnerReader

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ORIGIN = "https://miniapp.catalogs.integration.test"
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True, repr=False)
class _OwnerFixture:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _AuthTokens:
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


class _ReadinessStub:
    async def check(self) -> None:
        return None


class _FailOnceAfterBodyFactory:
    """Rollback one otherwise-successful mutation after its body and receipt are written."""

    __slots__ = ("_base", "_should_fail")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._base = SqlAlchemyHttpMutationUnitOfWorkFactory(sessions)
        self._should_fail = True

    def __call__(self) -> AbstractAsyncContextManager[MutationUnitOfWork]:
        return _FailOnceAfterBodyContext(self)

    def take_failure(self) -> bool:
        should_fail = self._should_fail
        self._should_fail = False
        return should_fail

    def open(self) -> AbstractAsyncContextManager[MutationUnitOfWork]:
        return cast(AbstractAsyncContextManager[MutationUnitOfWork], self._base())


class _FailOnceAfterBodyContext:
    __slots__ = ("_factory", "_inner")

    def __init__(self, factory: _FailOnceAfterBodyFactory) -> None:
        self._factory = factory
        self._inner: AbstractAsyncContextManager[MutationUnitOfWork] | None = None

    async def __aenter__(self) -> MutationUnitOfWork:
        self._inner = self._factory.open()
        return await self._inner.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._inner is None:  # pragma: no cover - context manager protocol guard
            raise RuntimeError("catalog rollback test context was not entered")
        if exc_type is None and self._factory.take_failure():
            failure = RuntimeError("synthetic catalog transaction rollback")
            await self._inner.__aexit__(type(failure), failure, failure.__traceback__)
            raise failure
        result = await self._inner.__aexit__(exc_type, exc, traceback)
        return bool(result) if result is not None else None


class _ReadOrder:
    __slots__ = ("calls", "session_ids")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.session_ids: list[int] = []

    def record(self, call: str, session: AsyncSession) -> None:
        assert session.in_transaction()
        self.calls.append(call)
        self.session_ids.append(id(session))


class _RecordingAuth:
    __slots__ = ("_delegate", "_order", "_session")

    def __init__(
        self,
        delegate: AuthPersistence,
        order: _ReadOrder,
        session: AsyncSession,
    ) -> None:
        self._delegate = delegate
        self._order = order
        self._session = session

    async def read_session(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        self._order.record("authenticate", self._session)
        return await self._delegate.read_session(session_token, now=now)


class _RecordingOwnerReader:
    __slots__ = ("_delegate", "_order", "_session")

    def __init__(
        self,
        delegate: OwnerReader,
        order: _ReadOrder,
        session: AsyncSession,
    ) -> None:
        self._delegate = delegate
        self._order = order
        self._session = session

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self._order.record("owner", self._session)
        return await self._delegate.get_owner(owner_id)


class _RecordingCatalogReader:
    __slots__ = ("_delegate", "_order", "_session")

    def __init__(
        self,
        delegate: BoundedCatalogReader,
        order: _ReadOrder,
        session: AsyncSession,
    ) -> None:
        self._delegate = delegate
        self._order = order
        self._session = session

    async def list_accounts_bounded(
        self,
        owner_id: UUID,
        *,
        archived: bool,
        limit: int,
    ) -> tuple[AccountSnapshot, ...]:
        self._order.record("catalog", self._session)
        result: tuple[AccountSnapshot, ...] = await self._delegate.list_accounts_bounded(
            owner_id,
            archived=archived,
            limit=limit,
        )
        return result

    async def list_categories_bounded(
        self,
        owner_id: UUID,
        *,
        kind: str | None,
        archived: bool,
        limit: int,
    ) -> tuple[CategorySnapshot, ...]:
        self._order.record("catalog", self._session)
        result: tuple[CategorySnapshot, ...] = await self._delegate.list_categories_bounded(
            owner_id,
            kind=kind,
            archived=archived,
            limit=limit,
        )
        return result


class _RecordingCatalogQueryFactory:
    __slots__ = ("_base", "order")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._base = SqlAlchemyCatalogQueryUnitOfWorkFactory(sessions)
        self.order = _ReadOrder()

    def __call__(self) -> AbstractAsyncContextManager[CatalogQueryUnitOfWork]:
        return _RecordingCatalogQueryContext(self._base(), self.order)


class _RecordingCatalogQueryContext:
    __slots__ = ("_inner", "_order")

    def __init__(
        self,
        inner: SqlAlchemyCatalogQueryUnitOfWork,
        order: _ReadOrder,
    ) -> None:
        self._inner = inner
        self._order = order

    async def __aenter__(self) -> CatalogQueryUnitOfWork:
        uow = await self._inner.__aenter__()
        uow.auth = cast(AuthPersistence, _RecordingAuth(uow.auth, self._order, uow.session))
        uow.owners = cast(
            OwnerReader,
            _RecordingOwnerReader(uow.owners, self._order, uow.session),
        )
        uow.catalogs = cast(
            BoundedCatalogReader,
            _RecordingCatalogReader(uow.catalogs, self._order, uow.session),
        )
        return uow

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        result = await self._inner.__aexit__(exc_type, exc, traceback)
        return bool(result) if result is not None else None


def _opaque_token() -> str:
    raw = uuid4().bytes + uuid4().bytes
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _synthetic_telegram_user_id() -> int:
    return 9_910_000_000 + uuid4().int % 80_000_000


async def _setup_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    timezone: str = "Europe/Moscow",
) -> _OwnerFixture:
    async with factory.begin() as session:
        telegram_user_id = _synthetic_telegram_user_id()
        owner = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
            locale="ru_RU",
            timezone=timezone,
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Baseline Account",
            slug="baseline-account",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind="expense",
            name="Baseline Category",
            slug="baseline-category",
            emoji="▫️",
        )
        session.add_all((account, category))
        await session.flush()
        owner.default_account_id = account.id
        return _OwnerFixture(owner.id, account.id, category.id)


async def _create_auth_session(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
    digester: HttpSecurityDigester,
) -> _AuthTokens:
    tokens = _AuthTokens(_opaque_token(), _opaque_token())
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            owner_id,
            digester.session(tokens.session_token),
            digester.csrf(tokens.csrf_token),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    return tokens


def _app(
    factory: async_sessionmaker[AsyncSession],
    digester: HttpSecurityDigester,
    *,
    mutation_uow_factory: MutationUnitOfWorkFactory | None = None,
) -> FastAPI:
    actual_mutation_uow_factory: MutationUnitOfWorkFactory = (
        mutation_uow_factory or SqlAlchemyHttpMutationUnitOfWorkFactory(factory)
    )
    executor = HttpMutationExecutor(
        digester=digester,
        uow_factory=actual_mutation_uow_factory,
        clock=lambda: NOW,
    )
    app: FastAPI = create_app(
        readiness_probe=_ReadinessStub(),
        finance_service=FinanceQueryService(
            digester=digester,
            cursor_codec=TransactionCursorCodec(SECURITY_KEY),
            uow_factory=SqlAlchemyFinanceQueryUnitOfWorkFactory(factory),
            clock=lambda: NOW,
        ),
        catalog_service=HttpCatalogService(
            digester=digester,
            query_uow_factory=SqlAlchemyCatalogQueryUnitOfWorkFactory(factory),
            mutation_executor=executor,
            clock=lambda: NOW,
        ),
        catalog_origin=ORIGIN,
    )
    return app


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ORIGIN,
    )


def _cookie_header(tokens: _AuthTokens) -> str:
    return f"{SESSION_COOKIE}={tokens.session_token}; {CSRF_COOKIE}={tokens.csrf_token}"


def _read_headers(tokens: _AuthTokens) -> dict[str, str]:
    return {"Cookie": _cookie_header(tokens)}


def _mutation_headers(tokens: _AuthTokens, key: str) -> dict[str, str]:
    return {
        "Cookie": _cookie_header(tokens),
        "Idempotency-Key": key,
        "Origin": ORIGIN,
        "X-CSRF-Token": tokens.csrf_token,
    }


async def _cleanup_owners(engine: AsyncEngine, *fixtures: _OwnerFixture) -> None:
    owner_ids = tuple(fixture.owner_id for fixture in fixtures)
    async with engine.begin() as connection:
        await connection.execute(
            delete(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id.in_(owner_ids))
        )
        await connection.execute(delete(WebSession).where(WebSession.user_id.in_(owner_ids)))
        await connection.execute(
            update(User).where(User.id.in_(owner_ids)).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id.in_(owner_ids)))
        await connection.execute(delete(Account).where(Account.user_id.in_(owner_ids)))
        await connection.execute(delete(User).where(User.id.in_(owner_ids)))


async def _seed_active_accounts(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
    *,
    count: int,
) -> tuple[UUID, ...]:
    accounts = [
        Account(
            user_id=owner_id,
            name=f"Capacity Account {index:03d}",
            slug=f"capacity-account-{index:03d}",
            type="other",
            currency="RUB",
        )
        for index in range(count)
    ]
    async with factory.begin() as session:
        session.add_all(accounts)
        await session.flush()
        return tuple(account.id for account in accounts)


def _result(response: httpx2.Response) -> dict[str, object]:
    result = response.json()["result"]
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_catalog_http_lifecycle_is_bounded_owner_scoped_and_report_today_works() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    foreign = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)
    try:
        async with _client(app) as client:
            unauthorized = await client.get("/api/v1/accounts")
            accounts_before = await client.get(
                "/api/v1/accounts?archived=false",
                headers=_read_headers(tokens),
            )
            categories_before = await client.get(
                "/api/v1/categories?kind=expense&archived=false",
                headers=_read_headers(tokens),
            )
            duplicate_query = await client.get(
                "/api/v1/accounts?archived=false&archived=false",
                headers=_read_headers(tokens),
            )
            unknown_query = await client.get(
                "/api/v1/categories?unexpected=true",
                headers=_read_headers(tokens),
            )

            create_account = await client.post(
                "/api/v1/accounts",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Lifecycle Account", "currency": "USD"},
            )
            account_id = str(_result(create_account)["account_id"])
            update_account = await client.patch(
                f"/api/v1/accounts/{account_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Renamed Lifecycle Account", "version": 1},
            )
            archive_account = await client.post(
                f"/api/v1/accounts/{account_id}/archive",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 2},
            )
            archived_accounts = await client.get(
                "/api/v1/accounts?archived=true",
                headers=_read_headers(tokens),
            )
            restore_account = await client.post(
                f"/api/v1/accounts/{account_id}/restore",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 3},
            )
            default_account = await client.post(
                f"/api/v1/accounts/{account_id}/default",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 4},
            )

            create_category = await client.post(
                "/api/v1/categories",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Lifecycle Category", "kind": "expense"},
            )
            category_id = str(_result(create_category)["category_id"])
            update_category = await client.patch(
                f"/api/v1/categories/{category_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Renamed Lifecycle Category", "version": 1},
            )
            archive_category = await client.post(
                f"/api/v1/categories/{category_id}/archive",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 2},
            )
            archived_categories = await client.get(
                "/api/v1/categories?kind=expense&archived=true",
                headers=_read_headers(tokens),
            )
            restore_category = await client.post(
                f"/api/v1/categories/{category_id}/restore",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 3},
            )

            accounts_after = await client.get(
                "/api/v1/accounts",
                headers=_read_headers(tokens),
            )
            categories_after = await client.get(
                "/api/v1/categories?kind=expense",
                headers=_read_headers(tokens),
            )
            today = await client.get(
                "/api/v1/reports/today",
                headers=_read_headers(tokens),
            )

        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "auth_session_invalid"
        assert accounts_before.status_code == 200
        assert accounts_before.json()["default_account_id"] == str(owner.account_id)
        assert [item["id"] for item in accounts_before.json()["items"]] == [str(owner.account_id)]
        assert categories_before.status_code == 200
        assert [item["id"] for item in categories_before.json()["items"]] == [
            str(owner.category_id)
        ]
        assert duplicate_query.status_code == 422
        assert unknown_query.status_code == 422

        assert create_account.status_code == 201
        assert _result(create_account) == {
            "kind": "account",
            "account_id": account_id,
            "version": 1,
        }
        assert update_account.status_code == 200
        assert _result(update_account)["version"] == 2
        assert archive_account.status_code == 200
        assert _result(archive_account)["version"] == 3
        assert archived_accounts.status_code == 200
        assert [item["id"] for item in archived_accounts.json()["items"]] == [account_id]
        assert restore_account.status_code == 200
        assert _result(restore_account)["version"] == 4
        assert default_account.status_code == 200
        assert _result(default_account)["version"] == 5

        assert create_category.status_code == 201
        assert _result(create_category) == {
            "kind": "category",
            "category_id": category_id,
            "version": 1,
        }
        assert update_category.status_code == 200
        assert _result(update_category)["version"] == 2
        assert archive_category.status_code == 200
        assert _result(archive_category)["version"] == 3
        assert archived_categories.status_code == 200
        assert [item["id"] for item in archived_categories.json()["items"]] == [category_id]
        assert restore_category.status_code == 200
        assert _result(restore_category)["version"] == 4

        assert accounts_after.status_code == 200
        assert accounts_after.json()["default_account_id"] == account_id
        account_ids = {item["id"] for item in accounts_after.json()["items"]}
        assert account_ids == {str(owner.account_id), account_id}
        assert str(foreign.account_id) not in account_ids
        assert categories_after.status_code == 200
        category_ids = {item["id"] for item in categories_after.json()["items"]}
        assert category_ids == {str(owner.category_id), category_id}
        assert str(foreign.category_id) not in category_ids
        assert today.status_code == 200
        today_payload = today.json()
        assert today_payload["period"] == {
            "start": "2026-08-13T21:00:00Z",
            "end": "2026-08-14T21:00:00Z",
            "totals": [],
        }
        assert len(today_payload["top_categories"]) <= 20
        assert len(today_payload["transactions"]) <= 20
    finally:
        await _cleanup_owners(engine, owner, foreign)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_query_uow_is_read_only_repeatable_and_authenticates_first() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    base_query_factory = SqlAlchemyCatalogQueryUnitOfWorkFactory(factory)
    try:
        async with base_query_factory() as uow:
            isolation = await uow.session.scalar(text("SHOW transaction_isolation"))
            read_only = await uow.session.scalar(text("SHOW transaction_read_only"))

        assert isolation == "repeatable read"
        assert read_only == "on"

        recording_query_factory = _RecordingCatalogQueryFactory(factory)
        service = HttpCatalogService(
            digester=digester,
            query_uow_factory=recording_query_factory,
            mutation_executor=HttpMutationExecutor(
                digester=digester,
                uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(factory),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )
        result = await service.accounts(tokens.session_token, archived=False)

        assert [item.account_id for item in result.items] == [owner.account_id]
        assert recording_query_factory.order.calls == ["authenticate", "owner", "catalog"]
        assert len(recording_query_factory.order.session_ids) == 3
        assert len(set(recording_query_factory.order.session_ids)) == 1
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_same_key_replays_minimal_receipt_and_mismatch_is_typed_409() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    key = _opaque_token()
    app = _app(factory, digester)
    try:
        headers = _mutation_headers(tokens, key)
        async with _client(app) as client:
            first = await client.post(
                "/api/v1/accounts",
                headers=headers,
                json={"name": "  Replay   Account  ", "currency": "RUB"},
            )
            replay = await client.post(
                "/api/v1/accounts",
                headers={**headers, "Content-Type": "application/json"},
                content=b'{ "currency": "RUB", "name": "Replay Account" }',
            )
            mismatch = await client.post(
                "/api/v1/accounts",
                headers=headers,
                json={"name": "Different Account", "currency": "RUB"},
            )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert set(first.json()) == {"result"}
        assert set(first.json()["result"]) == {"kind", "account_id", "version"}
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "idempotency_key_conflict"

        async with factory() as session:
            created_count = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.user_id == owner.owner_id, Account.slug == "replay-account")
            )
            records = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(
                        HttpIdempotencyRecord.user_id == owner.owner_id
                    )
                )
            ).all()
        assert created_count == 1
        assert len(records) == 1
        record = records[0]
        assert record.status == "completed"
        assert record.operation == "account.create"
        assert record.http_status == 201
        assert record.result_kind == "account"
        assert record.result_revision == 1
        assert len(record.idempotency_key_hash) == 32
        assert len(record.request_fingerprint) == 32
        assert record.idempotency_key_hash != key.encode("ascii")
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_different_keys_same_version_race_mutates_once() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)

    async def rename(name: str) -> httpx2.Response:
        async with _client(app) as client:
            return await client.patch(
                f"/api/v1/accounts/{owner.account_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": name, "version": 1},
            )

    try:
        responses = await asyncio.gather(rename("Race Winner A"), rename("Race Winner B"))

        assert sorted(response.status_code for response in responses) == [200, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["error"]["code"] == "object_version_conflict"
        async with factory() as session:
            account = await session.get(Account, owner.account_id)
            records = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(
                        HttpIdempotencyRecord.user_id == owner.owner_id
                    )
                )
            ).all()
        assert account is not None
        assert account.version == 2
        assert account.name in {"Race Winner A", "Race Winner B"}
        assert len(records) == 1
        assert records[0].status == "completed"
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_post_body_failure_rolls_back_claim_and_same_key_retry_succeeds() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    key = _opaque_token()
    app = _app(
        factory,
        digester,
        mutation_uow_factory=_FailOnceAfterBodyFactory(factory),
    )
    try:
        async with _client(app) as client:
            failed = await client.post(
                "/api/v1/categories",
                headers=_mutation_headers(tokens, key),
                json={"name": "Rollback Category", "kind": "income"},
            )
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"

        async with factory() as session:
            rolled_back_categories = await session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.user_id == owner.owner_id, Category.slug == "rollback-category")
            )
            rolled_back_claims = await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(HttpIdempotencyRecord.user_id == owner.owner_id)
            )
        assert rolled_back_categories == 0
        assert rolled_back_claims == 0

        async with _client(app) as client:
            retried = await client.post(
                "/api/v1/categories",
                headers=_mutation_headers(tokens, key),
                json={"name": "Rollback Category", "kind": "income"},
            )
        assert retried.status_code == 201
        assert _result(retried)["kind"] == "category"
        assert _result(retried)["version"] == 1

        async with factory() as session:
            committed_categories = await session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.user_id == owner.owner_id, Category.slug == "rollback-category")
            )
            committed_claims = await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(HttpIdempotencyRecord.user_id == owner.owner_id)
            )
        assert committed_categories == 1
        assert committed_claims == 1
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_foreign_and_missing_entities_are_identical_and_unmodified() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    foreign = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)
    missing_account_id = uuid4()
    missing_category_id = uuid4()
    try:
        async with _client(app) as client:
            foreign_account = await client.patch(
                f"/api/v1/accounts/{foreign.account_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Forbidden Rename", "version": 1},
            )
            missing_account = await client.patch(
                f"/api/v1/accounts/{missing_account_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Forbidden Rename", "version": 1},
            )
            foreign_category = await client.patch(
                f"/api/v1/categories/{foreign.category_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Forbidden Category Rename", "version": 1},
            )
            missing_category = await client.patch(
                f"/api/v1/categories/{missing_category_id}",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": "Forbidden Category Rename", "version": 1},
            )

        responses = (foreign_account, missing_account, foreign_category, missing_category)
        assert all(response.status_code == 404 for response in responses)
        assert len({response.text for response in responses}) == 1
        assert foreign_account.json()["error"]["code"] == "not_found"
        async with factory() as session:
            account = await session.get(Account, foreign.account_id)
            category = await session.get(Category, foreign.category_id)
            owner_claims = await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(HttpIdempotencyRecord.user_id == owner.owner_id)
            )
        assert account is not None
        assert account.name == "Baseline Account"
        assert account.version == 1
        assert category is not None
        assert category.name == "Baseline Category"
        assert category.version == 1
        assert owner_claims == 0
    finally:
        await _cleanup_owners(engine, owner, foreign)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_destination_cap_rolls_back_claim_and_retries_after_slot_is_freed() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    seeded_ids = await _seed_active_accounts(factory, owner.owner_id, count=198)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)
    first_key = _opaque_token()
    blocked_key = _opaque_token()
    archive_key = _opaque_token()
    blocked_key_hash = digester.idempotency_key(blocked_key).database_value()
    try:
        async with _client(app) as client:
            fill_last_slot = await client.post(
                "/api/v1/accounts",
                headers=_mutation_headers(tokens, first_key),
                json={"name": "Capacity Account 199", "currency": "RUB"},
            )
            rejected_at_cap = await client.post(
                "/api/v1/accounts",
                headers=_mutation_headers(tokens, blocked_key),
                json={"name": "Capacity Retry", "currency": "RUB"},
            )

        assert fill_last_slot.status_code == 201
        assert rejected_at_cap.status_code == 409
        assert rejected_at_cap.json()["error"]["code"] == "catalog_unavailable"
        async with factory() as session:
            active_at_cap = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.user_id == owner.owner_id, Account.archived_at.is_(None))
            )
            rejected_claims = await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(
                    HttpIdempotencyRecord.user_id == owner.owner_id,
                    HttpIdempotencyRecord.idempotency_key_hash == blocked_key_hash,
                )
            )
        assert active_at_cap == 200
        assert rejected_claims == 0

        async with _client(app) as client:
            free_slot = await client.post(
                f"/api/v1/accounts/{seeded_ids[0]}/archive",
                headers=_mutation_headers(tokens, archive_key),
                json={"version": 1},
            )
            retried = await client.post(
                "/api/v1/accounts",
                headers=_mutation_headers(tokens, blocked_key),
                json={"name": "Capacity Retry", "currency": "RUB"},
            )

        assert free_slot.status_code == 200
        assert retried.status_code == 201
        assert _result(retried)["version"] == 1
        async with factory() as session:
            active_after_retry = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.user_id == owner.owner_id, Account.archived_at.is_(None))
            )
            retried_claims = await session.scalar(
                select(func.count())
                .select_from(HttpIdempotencyRecord)
                .where(
                    HttpIdempotencyRecord.user_id == owner.owner_id,
                    HttpIdempotencyRecord.idempotency_key_hash == blocked_key_hash,
                    HttpIdempotencyRecord.status == "completed",
                )
            )
        assert active_after_retry == 200
        assert retried_claims == 1
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_concurrent_creates_from_199_allow_only_one_destination_slot() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    await _seed_active_accounts(factory, owner.owner_id, count=198)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)

    async def create_account(name: str) -> httpx2.Response:
        async with _client(app) as client:
            return await client.post(
                "/api/v1/accounts",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"name": name, "currency": "RUB"},
            )

    try:
        responses = await asyncio.gather(
            create_account("Concurrent Capacity A"),
            create_account("Concurrent Capacity B"),
        )

        assert sorted(response.status_code for response in responses) == [201, 409]
        rejected = next(response for response in responses if response.status_code == 409)
        assert rejected.json()["error"]["code"] == "catalog_unavailable"
        async with factory() as session:
            active_count = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.user_id == owner.owner_id, Account.archived_at.is_(None))
            )
            records = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(
                        HttpIdempotencyRecord.user_id == owner.owner_id
                    )
                )
            ).all()
        assert active_count == 200
        assert len(records) == 1
        assert records[0].status == "completed"
        assert records[0].http_status == 201
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()


def _parameter_values(parameters: object) -> tuple[object, ...]:
    if isinstance(parameters, Mapping):
        return tuple(parameters.values())
    if isinstance(parameters, Sequence) and not isinstance(parameters, (str, bytes, bytearray)):
        values: list[object] = []
        for item in parameters:
            if isinstance(item, Mapping):
                values.extend(item.values())
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                values.extend(item)
            else:
                values.append(item)
        return tuple(values)
    return ()


@pytest.mark.asyncio
async def test_catalog_sql_limit_201_returns_200_complete_and_409_on_overflow() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)
    try:
        async with factory.begin() as session:
            await session.execute(
                insert(Account),
                [
                    {
                        "user_id": owner.owner_id,
                        "name": f"Bounded Account {index:03d}",
                        "slug": f"bounded-account-{index:03d}",
                        "type": "other",
                        "currency": "RUB",
                        "initial_balance_minor": 0,
                        "version": 1,
                    }
                    for index in range(199)
                ],
            )
            await session.execute(
                insert(Category),
                [
                    {
                        "user_id": owner.owner_id,
                        "kind": "expense",
                        "name": f"Bounded Category {index:03d}",
                        "slug": f"bounded-category-{index:03d}",
                        "emoji": "",
                        "version": 1,
                    }
                    for index in range(199)
                ],
            )

        observed_limits: dict[str, list[tuple[object, ...]]] = {
            "accounts": [],
            "categories": [],
        }

        def capture_catalog_limit(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if "FROM accounts" in statement and " LIMIT " in statement:
                observed_limits["accounts"].append(_parameter_values(parameters))
            if "FROM categories" in statement and " LIMIT " in statement:
                observed_limits["categories"].append(_parameter_values(parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", capture_catalog_limit)
        try:
            async with _client(app) as client:
                accounts_at_limit = await client.get(
                    "/api/v1/accounts",
                    headers=_read_headers(tokens),
                )
                categories_at_limit = await client.get(
                    "/api/v1/categories?kind=expense",
                    headers=_read_headers(tokens),
                )
            assert accounts_at_limit.status_code == 200
            assert len(accounts_at_limit.json()["items"]) == 200
            assert categories_at_limit.status_code == 200
            assert len(categories_at_limit.json()["items"]) == 200

            overflow_account = Account(
                user_id=owner.owner_id,
                name="Overflow Account",
                slug="overflow-account",
                type="other",
                currency="RUB",
            )
            overflow_category = Category(
                user_id=owner.owner_id,
                kind="expense",
                name="Overflow Category",
                slug="overflow-category",
                emoji="",
            )
            async with factory.begin() as session:
                session.add_all(
                    (
                        overflow_account,
                        overflow_category,
                    )
                )

            async with _client(app) as client:
                accounts_overflow = await client.get(
                    "/api/v1/accounts",
                    headers=_read_headers(tokens),
                )
                categories_overflow = await client.get(
                    "/api/v1/categories?kind=expense",
                    headers=_read_headers(tokens),
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_catalog_limit)

        assert accounts_overflow.status_code == 409
        assert accounts_overflow.json()["error"]["code"] == "catalog_unavailable"
        assert categories_overflow.status_code == 409
        assert categories_overflow.json()["error"]["code"] == "catalog_unavailable"
        assert observed_limits["accounts"]
        assert observed_limits["categories"]
        assert all(201 in values for values in observed_limits["accounts"])
        assert all(201 in values for values in observed_limits["categories"])

        async with _client(app) as client:
            recover_accounts = await client.post(
                f"/api/v1/accounts/{overflow_account.id}/archive",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 1},
            )
            recover_categories = await client.post(
                f"/api/v1/categories/{overflow_category.id}/archive",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 1},
            )
            accounts_recovered = await client.get(
                "/api/v1/accounts",
                headers=_read_headers(tokens),
            )
            categories_recovered = await client.get(
                "/api/v1/categories?kind=expense",
                headers=_read_headers(tokens),
            )

        assert recover_accounts.status_code == 200
        assert recover_categories.status_code == 200
        assert accounts_recovered.status_code == 200
        assert len(accounts_recovered.json()["items"]) == 200
        assert categories_recovered.status_code == 200
        assert len(categories_recovered.json()["items"]) == 200
    finally:
        await _cleanup_owners(engine, owner)
        await engine.dispose()
