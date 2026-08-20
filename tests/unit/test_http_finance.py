from __future__ import annotations

import io
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid7

import httpx2
import pytest
from fakes.queries import InMemoryQueryRepository
from fastapi import FastAPI

from finbot.adapters.database.repositories.http_finance import (
    SqlAlchemyFinanceQueryUnitOfWork,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthOwner,
    SessionCheck,
    SessionCheckStatus,
)
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.application.dto import TransactionSnapshot
from finbot.domain.transactions import TransactionType
from finbot.observability.logging import JsonFormatter

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SESSION_TOKEN = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
SESSION_BINDING = HttpSecurityDigester(SECURITY_KEY).session_binding(SESSION_TOKEN)
BASE_URL = "https://miniapp.example.test"


class ReadinessStub:
    async def check(self) -> None:
        pass


@dataclass
class FinanceAuthFake:
    authenticated: AuthenticatedSession | None

    async def read_session(self, _token: object, *, now: datetime) -> SessionCheck:
        if self.authenticated is None or self.authenticated.expires_at <= now:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        return SessionCheck(
            status=SessionCheckStatus.ACTIVE,
            authenticated=self.authenticated,
        )


@dataclass
class FinanceUowContext:
    auth: Any
    finance: Any


class FinanceUowFactory:
    def __init__(self, auth: FinanceAuthFake, finance: InMemoryQueryRepository) -> None:
        self.auth = auth
        self.finance = finance
        self.entries = 0

    @asynccontextmanager
    async def __call__(self) -> Any:
        self.entries += 1
        yield FinanceUowContext(auth=self.auth, finance=self.finance)


class FailingSetupSession:
    async def connection(self, **_kwargs: object) -> None:
        raise RuntimeError("synthetic setup failure")


class FailingSetupContext:
    def __init__(self) -> None:
        self.exited: tuple[object, object, object] | None = None

    async def __aenter__(self) -> FailingSetupSession:
        return FailingSetupSession()

    async def __aexit__(self, *args: object) -> None:
        assert len(args) == 3
        self.exited = (args[0], args[1], args[2])


class FailingSetupSessions:
    def __init__(self) -> None:
        self.context = FailingSetupContext()

    def begin(self) -> FailingSetupContext:
        return self.context


def _transaction(
    *,
    transaction_id: UUID | None = None,
    occurred_at: datetime = NOW,
    amount_minor: int = 2**53 + 17,
    kind: TransactionType = TransactionType.EXPENSE,
    currency: str = "RUB",
    deleted_at: datetime | None = None,
    description: str = "private-description-marker",
) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=transaction_id or uuid7(),
        kind=kind,
        amount_minor=amount_minor,
        currency=currency,
        account_id=uuid7(),
        account_name="Private account marker",
        category_id=uuid7(),
        category_name="Private category marker",
        category_emoji="▫️",
        occurred_at=occurred_at,
        description=description,
        deleted_at=deleted_at,
        version=3,
    )


def _app(
    transactions: tuple[TransactionSnapshot, ...],
    *,
    authenticated: bool = True,
) -> tuple[FastAPI, FinanceUowFactory, UUID]:
    owner_id = uuid7()
    session = (
        AuthenticatedSession(
            owner=AuthOwner(owner_id, "ru_RU", "Europe/Moscow", "RUB"),
            expires_at=NOW + timedelta(hours=1),
        )
        if authenticated
        else None
    )
    factory = FinanceUowFactory(
        FinanceAuthFake(session),
        InMemoryQueryRepository(transactions={owner_id: transactions}),
    )
    service = FinanceQueryService(
        digester=HttpSecurityDigester(SECURITY_KEY),
        cursor_codec=TransactionCursorCodec(SECURITY_KEY),
        uow_factory=factory,
        clock=lambda: NOW,
    )
    return (
        create_app(
            readiness_probe=ReadinessStub(),
            finance_service=service,
        ),
        factory,
        owner_id,
    )


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=BASE_URL,
        cookies={"__Host-numismat_session": SESSION_TOKEN},
        headers={"X-Session-Binding": SESSION_BINDING},
    )


def _security_headers(response: httpx2.Response) -> None:
    assert response.headers["cache-control"] == "no-store, no-cache"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_finance_uow_cleans_inner_context_when_setup_fails() -> None:
    sessions = FailingSetupSessions()
    uow = SqlAlchemyFinanceQueryUnitOfWork(sessions)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await uow.__aenter__()

    assert sessions.context.exited is not None
    assert sessions.context.exited[0] is RuntimeError
    assert isinstance(sessions.context.exited[1], RuntimeError)


@pytest.mark.asyncio
async def test_dashboard_uses_owner_month_and_serializes_money_as_strings() -> None:
    current_expense = _transaction(occurred_at=NOW - timedelta(days=1))
    current_income = _transaction(
        occurred_at=NOW - timedelta(days=2),
        amount_minor=2**53 + 99,
        kind=TransactionType.INCOME,
    )
    previous = _transaction(occurred_at=datetime(2026, 7, 5, tzinfo=UTC), amount_minor=500)
    app, factory, _owner_id = _app((current_expense, current_income, previous))

    async with _client(app) as client:
        response = await client.get("/api/v1/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_period"]["start"] == "2026-07-31T21:00:00Z"
    assert payload["current_period"]["end"] == "2026-08-13T12:00:00Z"
    assert payload["comparable_period"]["start"] == "2026-06-30T21:00:00Z"
    assert payload["current_period"]["totals"] == [
        {
            "currency": "RUB",
            "expense_minor": str(current_expense.amount_minor),
            "income_minor": str(current_income.amount_minor),
            "net_minor": str(current_income.amount_minor - current_expense.amount_minor),
        }
    ]
    transaction = payload["recent_transactions"][0]
    assert isinstance(transaction["amount_minor"], str)
    assert transaction["account"] == {
        "id": str(current_expense.account_id),
        "name": current_expense.account_name,
    }
    assert transaction["category"]["id"] == str(current_expense.category_id)
    assert "transaction_id" not in transaction
    assert factory.entries == 1
    _security_headers(response)


@pytest.mark.asyncio
async def test_today_report_uses_owner_timezone_and_fixed_bounded_limits() -> None:
    current = tuple(
        _transaction(
            occurred_at=datetime(2026, 8, 13, 8, minute, tzinfo=UTC),
            amount_minor=100 + minute,
        )
        for minute in range(25)
    )
    previous_local_day = _transaction(
        occurred_at=datetime(2026, 8, 12, 20, 59, 59, tzinfo=UTC),
    )
    app, factory, _owner_id = _app((*current, previous_local_day))

    async with _client(app) as client:
        response = await client.get("/api/v1/reports/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"]["start"] == "2026-08-12T21:00:00Z"
    assert payload["period"]["end"] == "2026-08-13T21:00:00Z"
    assert len(payload["top_categories"]) == 20
    assert len(payload["transactions"]) == 20
    assert str(previous_local_day.transaction_id) not in {
        item["id"] for item in payload["transactions"]
    }
    assert factory.entries == 1
    _security_headers(response)


@pytest.mark.asyncio
async def test_timeseries_is_owner_local_dense_and_excludes_deleted_details() -> None:
    first_expense = _transaction(
        occurred_at=datetime(2026, 8, 10, 21, 30, tzinfo=UTC),
        amount_minor=250,
        description="timeseries-private-marker",
    )
    first_income = _transaction(
        occurred_at=datetime(2026, 8, 11, 8, tzinfo=UTC),
        amount_minor=900,
        kind=TransactionType.INCOME,
    )
    second_currency = _transaction(
        occurred_at=datetime(2026, 8, 12, 8, tzinfo=UTC),
        amount_minor=7,
        currency="USD",
    )
    deleted = _transaction(
        occurred_at=datetime(2026, 8, 12, 9, tzinfo=UTC),
        amount_minor=999,
        deleted_at=NOW,
    )
    app, factory, _owner_id = _app((first_expense, first_income, second_currency, deleted))

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/reports/timeseries"
            "?start=2026-08-10T21%3A00%3A00Z"
            "&end=2026-08-13T21%3A00%3A00Z"
            "&grain=day"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == {
        "start": "2026-08-10T21:00:00Z",
        "end": "2026-08-13T21:00:00Z",
    }
    assert payload["timezone"] == "Europe/Moscow"
    assert payload["grain"] == "day"
    assert payload["buckets"] == [
        {
            "start": "2026-08-10T21:00:00Z",
            "end": "2026-08-11T21:00:00Z",
            "totals": [
                {
                    "currency": "RUB",
                    "income_minor": "900",
                    "expense_minor": "250",
                    "net_minor": "650",
                    "income_count": 1,
                    "expense_count": 1,
                }
            ],
        },
        {
            "start": "2026-08-11T21:00:00Z",
            "end": "2026-08-12T21:00:00Z",
            "totals": [
                {
                    "currency": "USD",
                    "income_minor": "0",
                    "expense_minor": "7",
                    "net_minor": "-7",
                    "income_count": 0,
                    "expense_count": 1,
                }
            ],
        },
        {
            "start": "2026-08-12T21:00:00Z",
            "end": "2026-08-13T21:00:00Z",
            "totals": [],
        },
    ]
    rendered = json.dumps(payload)
    assert "timeseries-private-marker" not in rendered
    assert str(first_expense.transaction_id) not in rendered
    assert "999" not in rendered
    assert factory.entries == 1
    _security_headers(response)


@pytest.mark.asyncio
async def test_period_and_comparison_enforce_bounded_canonical_contract() -> None:
    transaction = _transaction(occurred_at=NOW - timedelta(days=1))
    app, _factory, _owner_id = _app((transaction,))
    valid_period = urlencode(
        {
            "start": "2026-08-01T00:00:00.000Z",
            "end": "2026-08-14T00:00:00.123456Z",
            "category_limit": "5",
            "transaction_limit": "8",
        }
    )
    valid_compare = urlencode(
        {
            "current_start": "2026-08-01T00:00:00Z",
            "current_end": "2026-08-14T00:00:00Z",
            "previous_start": "2026-07-01T00:00:00Z",
            "previous_end": "2026-07-14T00:00:00Z",
        }
    )

    async with _client(app) as client:
        period = await client.get(f"/api/v1/reports/period?{valid_period}")
        compare = await client.get(f"/api/v1/reports/compare?{valid_compare}")
        too_long = await client.get(
            "/api/v1/reports/period?start=2025-01-01T00%3A00%3A00Z&end=2026-08-14T00%3A00%3A00Z"
        )
        unequal = await client.get(
            "/api/v1/reports/compare?current_start=2026-08-01T00%3A00%3A00Z"
            "&current_end=2026-08-14T00%3A00%3A00Z"
            "&previous_start=2026-07-01T00%3A00%3A00Z"
            "&previous_end=2026-07-13T00%3A00%3A00Z"
        )
        overlap = await client.get(
            "/api/v1/reports/compare?current_start=2026-08-01T00%3A00%3A00Z"
            "&current_end=2026-08-14T00%3A00%3A00Z"
            "&previous_start=2026-07-25T00%3A00%3A00Z"
            "&previous_end=2026-08-02T00%3A00%3A00Z"
        )

    assert period.status_code == 200
    assert period.json()["transactions"][0]["amount_minor"] == str(transaction.amount_minor)
    assert compare.status_code == 200
    assert "comparable_period" in compare.json()
    assert too_long.status_code == 422
    assert unequal.status_code == 200
    assert overlap.status_code == 422
    for response in (period, compare, too_long, unequal, overlap):
        _security_headers(response)


@pytest.mark.asyncio
async def test_timeseries_rejects_noncanonical_or_unbounded_query_before_database() -> None:
    app, factory, _owner_id = _app(())
    cases = (
        "/api/v1/reports/timeseries?start=2026-08-01T00%3A00%3A00Z&end=2026-08-02T00%3A00%3A00Z",
        "/api/v1/reports/timeseries"
        "?start=2026-08-01T00%3A00%3A00Z&end=2026-08-02T00%3A00%3A00Z&grain=hour",
        "/api/v1/reports/timeseries"
        "?start=2026-08-01T03%3A00%3A00%2B03%3A00"
        "&end=2026-08-02T03%3A00%3A00%2B03%3A00&grain=day",
        "/api/v1/reports/timeseries"
        "?start=2025-01-01T00%3A00%3A00Z&end=2026-08-02T00%3A00%3A00Z&grain=month",
        "/api/v1/reports/timeseries"
        "?start=2026-08-01T00%3A00%3A00Z&end=2026-08-02T00%3A00%3A00Z"
        "&grain=day&unknown=1",
    )

    async with _client(app) as client:
        responses = [await client.get(path) for path in cases]

    assert all(response.status_code == 422 for response in responses)
    assert all(response.json()["error"]["code"] == "validation_failed" for response in responses)
    assert factory.entries == 0
    for response in responses:
        _security_headers(response)


@pytest.mark.asyncio
async def test_timeseries_date_max_overflow_fails_closed_as_validation_error() -> None:
    app, factory, _owner_id = _app(())

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/reports/timeseries"
            "?start=9999-12-31T00%3A00%3A00Z"
            "&end=9999-12-31T12%3A00%3A00Z"
            "&grain=day"
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert factory.entries == 1
    _security_headers(response)


@pytest.mark.asyncio
async def test_keyset_pagination_is_active_only_and_detail_includes_soft_deleted() -> None:
    occurred_at = NOW - timedelta(minutes=1)
    ids = (
        UUID("018f0000-0000-7000-8000-000000000003"),
        UUID("018f0000-0000-7000-8000-000000000002"),
        UUID("018f0000-0000-7000-8000-000000000001"),
    )
    active = tuple(_transaction(transaction_id=value, occurred_at=occurred_at) for value in ids)
    deleted = _transaction(deleted_at=NOW - timedelta(seconds=1))
    app, _factory, _owner_id = _app((*active, deleted))

    async with _client(app) as client:
        first = await client.get("/api/v1/transactions?limit=2")
        cursor = first.json()["next_cursor"]
        second = await client.get("/api/v1/transactions", params={"limit": 2, "cursor": cursor})
        repeated = await client.get(
            "/api/v1/transactions",
            params={"limit": 2, "cursor": cursor},
        )
        detail = await client.get(f"/api/v1/transactions/{deleted.transaction_id}")
        missing = await client.get(f"/api/v1/transactions/{uuid7()}")

    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == [str(ids[0]), str(ids[1])]
    assert isinstance(cursor, str) and len(cursor) == 76 and "=" not in cursor
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [str(ids[2])]
    assert second.json()["next_cursor"] is None
    assert repeated.json() == second.json()
    assert detail.status_code == 200
    assert detail.json()["deleted_at"] is not None
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "not_found",
            "details": {},
            "message": "Объект не найден",
        }
    }
    assert str(deleted.transaction_id) not in {item["id"] for item in first.json()["items"]}


@pytest.mark.asyncio
async def test_query_ambiguity_invalid_cursor_and_uuid_have_fixed_errors() -> None:
    app, factory, _owner_id = _app(())
    cases = (
        "/api/v1/dashboard?unknown=1",
        "/api/v1/reports/today?unknown=1",
        "/api/v1/transactions?limit=2&limit=3",
        "/api/v1/transactions?li%6dit=2",
        "/api/v1/transactions?limit=%ZZ",
        "/api/v1/transactions/018F0000-0000-7000-8000-000000000001",
    )

    async with _client(app) as client:
        responses = [await client.get(path) for path in cases]
        invalid_cursor = await client.get("/api/v1/transactions?cursor=short")

    assert all(response.status_code == 422 for response in responses)
    assert all(response.json()["error"]["code"] == "validation_failed" for response in responses)
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["error"]["code"] == "invalid_cursor"
    assert factory.entries == 0
    for response in (*responses, invalid_cursor):
        _security_headers(response)


@pytest.mark.asyncio
async def test_invalid_session_preserves_ambient_cookies_before_finance_query() -> None:
    app, factory, _owner_id = _app((), authenticated=False)

    async with _client(app) as client:
        response = await client.get("/api/v1/dashboard")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_session_invalid"
    assert response.headers.get_list("set-cookie") == []
    assert factory.entries == 1
    _security_headers(response)


@pytest.mark.asyncio
async def test_finance_openapi_is_authenticated_bounded_and_uses_string_money() -> None:
    app, _factory, _owner_id = _app(())

    async with _client(app) as client:
        schema = (await client.get("/api/v1/openapi.json")).json()

    paths = schema["paths"]
    assert {
        "/api/v1/dashboard",
        "/api/v1/reports/today",
        "/api/v1/reports/period",
        "/api/v1/reports/compare",
        "/api/v1/reports/timeseries",
        "/api/v1/transactions",
        "/api/v1/transactions/{transaction_id}",
    }.issubset(paths)
    for path in (
        "/api/v1/dashboard",
        "/api/v1/reports/today",
        "/api/v1/reports/period",
        "/api/v1/reports/compare",
        "/api/v1/reports/timeseries",
        "/api/v1/transactions",
        "/api/v1/transactions/{transaction_id}",
    ):
        assert paths[path]["get"]["security"] == [{"SessionCookie": []}]
        assert any(
            parameter["name"] == "X-Session-Binding"
            and parameter["in"] == "header"
            and parameter["required"] is True
            for parameter in paths[path]["get"]["parameters"]
        )
    assert schema["components"]["securitySchemes"]["SessionCookie"]["name"] == (
        "__Host-numismat_session"
    )
    money = schema["components"]["schemas"]["TransactionResponse"]["properties"]["amount_minor"]
    assert money["type"] == "string"
    transaction_parameters = paths["/api/v1/transactions"]["get"]["parameters"]
    assert next(item for item in transaction_parameters if item["name"] == "limit")["schema"] == {
        "default": 30,
        "maximum": 100,
        "minimum": 1,
        "type": "integer",
    }
    detail_parameters = paths["/api/v1/transactions/{transaction_id}"]["get"]["parameters"]
    assert (
        len(
            [
                item
                for item in detail_parameters
                if item["name"] == "transaction_id" and item["in"] == "path"
            ]
        )
        == 1
    )
    transaction_schema = schema["components"]["schemas"]["TransactionResponse"]
    assert "deleted_at" in transaction_schema["required"]
    page_schema = schema["components"]["schemas"]["TransactionPageResponse"]
    assert "next_cursor" in page_schema["required"]
    assert page_schema["properties"]["items"]["maxItems"] == 100
    timeseries_parameters = paths["/api/v1/reports/timeseries"]["get"]["parameters"]
    assert {item["name"] for item in timeseries_parameters if item["required"]} == {
        "X-Session-Binding",
        "start",
        "end",
        "grain",
    }
    assert next(item for item in timeseries_parameters if item["name"] == "grain")["schema"] == {
        "enum": ["day", "week", "month"],
        "type": "string",
    }
    timeseries_schema = schema["components"]["schemas"]["TimeSeriesResponse"]
    assert timeseries_schema["properties"]["buckets"]["maxItems"] == 366
    bucket_schema = schema["components"]["schemas"]["TimeSeriesBucketResponse"]
    assert bucket_schema["properties"]["totals"]["maxItems"] == 32
    aggregate_schema = schema["components"]["schemas"]["TimeSeriesCurrencyTotalsResponse"]
    assert aggregate_schema["properties"]["income_minor"]["type"] == "string"
    assert aggregate_schema["properties"]["income_count"]["type"] == "integer"


@pytest.mark.asyncio
async def test_finance_logs_and_errors_exclude_payload_cursor_owner_and_cookie() -> None:
    marker = "private-description-marker"
    transaction = _transaction(description=marker)
    second_transaction = _transaction(
        occurred_at=transaction.occurred_at - timedelta(seconds=1),
        description=marker,
    )
    app, _factory, owner_id = _app((transaction, second_transaction))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    try:
        async with _client(app) as client:
            first = await client.get("/api/v1/transactions?limit=1")
            cursor = first.json()["next_cursor"]
            tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
            rejected = await client.get(f"/api/v1/transactions?cursor={tampered}")
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    rendered = stream.getvalue()
    assert first.status_code == 200
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_cursor"
    for sensitive in (
        marker,
        SESSION_TOKEN,
        str(owner_id),
        str(transaction.transaction_id),
        str(transaction.amount_minor),
        cursor,
        tampered,
    ):
        assert sensitive not in rendered
    entries = [json.loads(line) for line in rendered.splitlines() if line]
    assert all(
        set(entry)
        <= {
            "component",
            "correlation_id",
            "duration",
            "error_code",
            "event",
            "level",
            "result",
            "route_template",
            "status_code",
        }
        for entry in entries
    )
