from __future__ import annotations

import base64
import io
import logging
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI

import finbot.adapters.http.app as http_app_module
from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.service import TelegramAuthService
from finbot.adapters.http.catalogs.service import AccountCatalog, HttpCatalogService
from finbot.adapters.http.mutations.service import (
    HttpRevisionMutationService,
    IdempotencyKeyReuseError,
    MutationCredentials,
    MutationReceipt,
)
from finbot.adapters.http.schemas.catalogs import (
    account_response,
    catalog_mutation_response,
    category_response,
)
from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.config import Settings
from finbot.domain.transactions import TransactionType
from finbot.observability.logging import JsonFormatter

ORIGIN = "https://miniapp.example.test"
NOW = datetime(2026, 8, 14, 7, 0, tzinfo=UTC)
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
IDEMPOTENCY_KEY = base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode("ascii")
SECURITY_KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
ACCOUNT_ID = UUID("018f0000-0000-7000-8000-000000000002")
CATEGORY_ID = UUID("018f0000-0000-7000-8000-000000000003")


class ReadinessStub:
    async def check(self) -> None:
        return None


def _account(*, archived: bool = False, name: str = "A" * 100) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=ACCOUNT_ID,
        name=name,
        account_type="card",
        currency="RUB",
        archived_at=NOW if archived else None,
        version=4,
    )


def _category(*, archived: bool = False, name: str = "C" * 100) -> CategorySnapshot:
    return CategorySnapshot(
        category_id=CATEGORY_ID,
        kind=TransactionType.EXPENSE,
        name=name,
        emoji="▫️",
        archived_at=NOW if archived else None,
        version=5,
    )


class FakeCatalogService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None

    def _raise_once(self) -> None:
        if self.error is not None:
            error = self.error
            self.error = None
            raise error

    async def accounts(self, raw_session: str, *, archived: bool) -> AccountCatalog:
        self.calls.append(("accounts", (raw_session, archived)))
        self._raise_once()
        return AccountCatalog(ACCOUNT_ID, (_account(archived=archived),))

    async def categories(
        self,
        raw_session: str,
        *,
        kind: TransactionType | None,
        archived: bool,
    ) -> tuple[CategorySnapshot, ...]:
        self.calls.append(("categories", (raw_session, kind, archived)))
        self._raise_once()
        return (_category(archived=archived),)

    async def create_account(
        self,
        credentials: MutationCredentials,
        *,
        name: str,
        currency: str,
    ) -> MutationReceipt:
        self.calls.append(("create_account", (credentials, name, currency)))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.ACCOUNT, ACCOUNT_ID, 1)

    async def update_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        *,
        name: str,
        version: int,
    ) -> MutationReceipt:
        self.calls.append(("update_account", (credentials, account_id, name, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.ACCOUNT, account_id, version + 1)

    async def archive_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return self._account_action("archive_account", credentials, account_id, version)

    async def restore_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return self._account_action("restore_account", credentials, account_id, version)

    async def set_default_account(
        self,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return self._account_action("default_account", credentials, account_id, version)

    def _account_action(
        self,
        name: str,
        credentials: MutationCredentials,
        account_id: UUID,
        version: int,
    ) -> MutationReceipt:
        self.calls.append((name, (credentials, account_id, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.ACCOUNT, account_id, version + 1)

    async def create_category(
        self,
        credentials: MutationCredentials,
        *,
        name: str,
        kind: TransactionType,
    ) -> MutationReceipt:
        self.calls.append(("create_category", (credentials, name, kind)))
        self._raise_once()
        return MutationReceipt(201, IdempotencyResultKind.CATEGORY, CATEGORY_ID, 1)

    async def update_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        *,
        name: str,
        version: int,
    ) -> MutationReceipt:
        self.calls.append(("update_category", (credentials, category_id, name, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.CATEGORY, category_id, version + 1)

    async def archive_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return self._category_action("archive_category", credentials, category_id, version)

    async def restore_category(
        self,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return self._category_action("restore_category", credentials, category_id, version)

    def _category_action(
        self,
        name: str,
        credentials: MutationCredentials,
        category_id: UUID,
        version: int,
    ) -> MutationReceipt:
        self.calls.append((name, (credentials, category_id, version)))
        self._raise_once()
        return MutationReceipt(200, IdempotencyResultKind.CATEGORY, category_id, version + 1)


def _app(service: FakeCatalogService) -> FastAPI:
    app: FastAPI = create_app(
        readiness_probe=ReadinessStub(),
        catalog_service=cast(HttpCatalogService, service),
        catalog_origin=ORIGIN,
    )
    return app


def _headers() -> dict[str, str]:
    return {
        "Cookie": (f"__Host-numismat_session={SESSION_TOKEN}; __Host-numismat_csrf={CSRF_TOKEN}"),
        "Idempotency-Key": IDEMPOTENCY_KEY,
        "Origin": ORIGIN,
        "X-CSRF-Token": CSRF_TOKEN,
    }


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ORIGIN,
    )


def _assert_local_openapi_refs_resolve(document: dict[str, Any]) -> None:
    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            target: object = document
            for encoded_segment in reference.removeprefix("#/").split("/"):
                segment = encoded_segment.replace("~1", "/").replace("~0", "~")
                assert isinstance(target, dict)
                assert segment in target, reference
                target = target[segment]
        for item in value.values():
            visit(item)

    visit(document)


@pytest.mark.asyncio
async def test_catalog_reads_are_owner_scoped_bounded_projections() -> None:
    service = FakeCatalogService()

    async with _client(_app(service)) as client:
        accounts = await client.get(
            "/api/v1/accounts?archived=true",
            headers={"Cookie": _headers()["Cookie"]},
        )
        categories = await client.get(
            "/api/v1/categories?kind=expense&archived=false",
            headers={"Cookie": _headers()["Cookie"]},
        )

    assert accounts.status_code == categories.status_code == 200
    assert accounts.json() == {
        "default_account_id": str(ACCOUNT_ID),
        "items": [
            {
                "archived": True,
                "currency": "RUB",
                "id": str(ACCOUNT_ID),
                "name": "A" * 100,
                "type": "card",
                "version": 4,
            }
        ],
    }
    assert categories.json()["items"][0] == {
        "archived": False,
        "emoji": "▫️",
        "id": str(CATEGORY_ID),
        "kind": "expense",
        "name": "C" * 100,
        "version": 5,
    }
    assert service.calls == [
        ("accounts", (SESSION_TOKEN, True)),
        ("categories", (SESSION_TOKEN, TransactionType.EXPENSE, False)),
    ]


@pytest.mark.parametrize(
    "query",
    [
        "archived=1",
        "archived=False",
        "archived=",
        "archived=true&archived=false",
        "unknown=true",
    ],
)
@pytest.mark.asyncio
async def test_account_query_rejects_noncanonical_duplicate_and_unknown_fields(query: str) -> None:
    service = FakeCatalogService()
    async with _client(_app(service)) as client:
        response = await client.get(
            f"/api/v1/accounts?{query}",
            headers={"Cookie": _headers()["Cookie"]},
        )
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("query", ["kind=other", "kind=", "kind=expense&kind=income"])
@pytest.mark.asyncio
async def test_category_query_rejects_invalid_or_duplicate_kind(query: str) -> None:
    service = FakeCatalogService()
    async with _client(_app(service)) as client:
        response = await client.get(
            f"/api/v1/categories?{query}",
            headers={"Cookie": _headers()["Cookie"]},
        )
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_all_catalog_mutation_routes_return_minimal_closed_receipts() -> None:
    service = FakeCatalogService()
    requests = (
        ("POST", "/api/v1/accounts", {"name": "Новый", "currency": "RUB"}, 201, "account"),
        (
            "PATCH",
            f"/api/v1/accounts/{ACCOUNT_ID}",
            {"name": "Новый", "version": 4},
            200,
            "account",
        ),
        ("POST", f"/api/v1/accounts/{ACCOUNT_ID}/archive", {"version": 4}, 200, "account"),
        ("POST", f"/api/v1/accounts/{ACCOUNT_ID}/restore", {"version": 4}, 200, "account"),
        ("POST", f"/api/v1/accounts/{ACCOUNT_ID}/default", {"version": 4}, 200, "account"),
        (
            "POST",
            "/api/v1/categories",
            {"name": "Новая", "kind": "expense"},
            201,
            "category",
        ),
        (
            "PATCH",
            f"/api/v1/categories/{CATEGORY_ID}",
            {"name": "Новая", "version": 5},
            200,
            "category",
        ),
        (
            "POST",
            f"/api/v1/categories/{CATEGORY_ID}/archive",
            {"version": 5},
            200,
            "category",
        ),
        (
            "POST",
            f"/api/v1/categories/{CATEGORY_ID}/restore",
            {"version": 5},
            200,
            "category",
        ),
    )

    async with _client(_app(service)) as client:
        responses = [
            await client.request(method, path, headers=_headers(), json=body)
            for method, path, body, _status, _kind in requests
        ]

    for response, (_method, _path, _body, status, kind) in zip(responses, requests, strict=True):
        assert response.status_code == status
        result = response.json()["result"]
        assert result["kind"] == kind
        assert set(result) == {"kind", f"{kind}_id", "version"}
        serialized = response.text
        assert "name" not in serialized
        assert "currency" not in serialized
        assert "state" not in serialized


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/accounts", {"name": "", "currency": "RUB"}),
        ("/api/v1/accounts", {"name": "x" * 61, "currency": "RUB"}),
        ("/api/v1/accounts", {"name": 7, "currency": "RUB"}),
        ("/api/v1/accounts", {"name": "A", "currency": "rub"}),
        ("/api/v1/accounts", {"name": "A", "currency": "RUBLE"}),
        ("/api/v1/accounts", {"name": "A", "currency": "RUB", "extra": True}),
        (f"/api/v1/accounts/{ACCOUNT_ID}/archive", {"version": True}),
        (f"/api/v1/accounts/{ACCOUNT_ID}/archive", {"version": "4"}),
        (f"/api/v1/accounts/{ACCOUNT_ID}/archive", {"version": 0}),
        (f"/api/v1/accounts/{ACCOUNT_ID}/archive", {"version": 2**31}),
        ("/api/v1/categories", {"name": "A", "kind": "other"}),
    ],
)
@pytest.mark.asyncio
async def test_catalog_mutation_bodies_are_strict(path: str, body: object) -> None:
    service = FakeCatalogService()
    async with _client(_app(service)) as client:
        response = await client.post(path, headers=_headers(), json=body)
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_catalog_mutations_reject_duplicate_json_and_noncanonical_uuid() -> None:
    service = FakeCatalogService()
    async with _client(_app(service)) as client:
        duplicate = await client.post(
            "/api/v1/accounts",
            headers={**_headers(), "Content-Type": "application/json"},
            content='{"name":"A","name":"B","currency":"RUB"}',
        )
        uppercase = await client.post(
            f"/api/v1/accounts/{str(ACCOUNT_ID).upper()}/archive",
            headers=_headers(),
            json={"version": 4},
        )
    assert duplicate.status_code == uppercase.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_catalog_mutation_reuses_fixed_idempotency_error_mapping() -> None:
    service = FakeCatalogService()
    service.error = IdempotencyKeyReuseError()
    async with _client(_app(service)) as client:
        response = await client.post(
            "/api/v1/accounts",
            headers=_headers(),
            json={"name": "Новый", "currency": "RUB"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_conflict"


def test_catalog_schema_accepts_legacy_names_and_only_catalog_receipts() -> None:
    assert account_response(_account()).name == "A" * 100
    assert category_response(_category()).name == "C" * 100
    with pytest.raises(ValueError):
        catalog_mutation_response(
            MutationReceipt(200, IdempotencyResultKind.TRANSACTION, ACCOUNT_ID, 1)
        )


@pytest.mark.asyncio
async def test_catalog_logs_exclude_names_body_tokens_ids_and_versions() -> None:
    marker = "private-catalog-name-marker"
    service = FakeCatalogService()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    try:
        async with _client(_app(service)) as client:
            response = await client.post(
                "/api/v1/accounts",
                headers=_headers(),
                json={"name": marker, "currency": "RUB"},
            )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert response.status_code == 201
    rendered = stream.getvalue()
    for sensitive in (
        marker,
        IDEMPOTENCY_KEY,
        SESSION_TOKEN,
        CSRF_TOKEN,
        str(ACCOUNT_ID),
        '"version":1',
    ):
        assert sensitive not in rendered


@pytest.mark.asyncio
async def test_catalog_openapi_is_closed_exact_and_resolvable() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        schema = _app(FakeCatalogService()).openapi()
    _assert_local_openapi_refs_resolve(schema)

    paths = schema["paths"]
    mutation_paths = {
        "/api/v1/accounts",
        "/api/v1/accounts/{account_id}",
        "/api/v1/accounts/{account_id}/archive",
        "/api/v1/accounts/{account_id}/restore",
        "/api/v1/accounts/{account_id}/default",
        "/api/v1/categories",
        "/api/v1/categories/{category_id}",
        "/api/v1/categories/{category_id}/archive",
        "/api/v1/categories/{category_id}/restore",
    }
    assert mutation_paths.issubset(paths)
    assert set(paths["/api/v1/accounts"]["post"]["responses"]) >= {"201"}
    assert "200" not in paths["/api/v1/accounts"]["post"]["responses"]
    assert set(paths["/api/v1/categories"]["post"]["responses"]) >= {"201"}
    assert "200" not in paths["/api/v1/categories"]["post"]["responses"]
    assert set(paths["/api/v1/accounts/{account_id}"]["patch"]["responses"]) >= {"200"}

    components = schema["components"]["schemas"]
    account_request = paths["/api/v1/accounts"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert account_request["additionalProperties"] is False
    assert account_request["properties"]["name"]["maxLength"] == 60
    assert components["AccountResponse"]["properties"]["name"]["maxLength"] == 100
    assert components["CategoryResponse"]["properties"]["name"]["maxLength"] == 100
    assert components["AccountsResponse"]["properties"]["items"]["maxItems"] == 200
    assert components["CategoriesResponse"]["properties"]["items"]["maxItems"] == 200
    account_result = components["AccountMutationResultResponse"]
    category_result = components["CategoryMutationResultResponse"]
    assert set(account_result["required"]) == {"kind", "account_id", "version"}
    assert set(category_result["required"]) == {"kind", "category_id", "version"}
    assert account_result["properties"]["kind"]["const"] == "account"
    assert category_result["properties"]["kind"]["const"] == "category"
    account_response_schema = paths["/api/v1/accounts"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]
    category_response_schema = paths["/api/v1/categories"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]
    assert account_response_schema["$ref"].endswith("/AccountMutationResponse")
    assert category_response_schema["$ref"].endswith("/CategoryMutationResponse")

    for path in mutation_paths:
        for method, operation in paths[path].items():
            if method not in {"post", "patch"}:
                continue
            assert operation["security"] == [{"SessionCookie": []}]
            header_parameters = [item for item in operation["parameters"] if item["in"] == "header"]
            assert {item["name"] for item in header_parameters} == {
                "Origin",
                "X-CSRF-Token",
                "Idempotency-Key",
            }
            for item in header_parameters:
                if item["name"] == "Origin":
                    assert item["required"] is False
                    assert item["schema"]["maxLength"] == 4096
                else:
                    assert item["required"] is True
                    assert item["schema"]["pattern"] == (r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


def test_catalog_routes_depend_only_on_typed_service() -> None:
    root = Path(__file__).parents[2]
    source = (root / "src/finbot/adapters/http/routes/catalogs.py").read_text(encoding="utf-8")
    assert "HttpCatalogService" in source
    assert "HttpMutationExecutor" not in source
    assert "SqlAlchemy" not in source
    assert "finbot.adapters.database" not in source


@pytest.mark.parametrize(
    ("service", "origin"),
    [
        (cast(HttpCatalogService, FakeCatalogService()), None),
        (None, ORIGIN),
    ],
)
def test_catalog_service_and_origin_must_be_configured_together(
    service: HttpCatalogService | None,
    origin: str | None,
) -> None:
    with pytest.raises(ValueError, match="catalog service and origin"):
        create_app(
            readiness_probe=ReadinessStub(),
            catalog_service=service,
            catalog_origin=origin,
        )


def test_catalog_origin_must_match_auth_and_mutation_origins() -> None:
    service = cast(HttpCatalogService, FakeCatalogService())
    with pytest.raises(ValueError, match="auth and catalog origins"):
        create_app(
            readiness_probe=ReadinessStub(),
            auth_service=cast(TelegramAuthService, object()),
            auth_origin=ORIGIN,
            catalog_service=service,
            catalog_origin="https://other.example.test",
        )
    with pytest.raises(ValueError, match="catalog and mutation origins"):
        create_app(
            readiness_probe=ReadinessStub(),
            catalog_service=service,
            catalog_origin=ORIGIN,
            mutation_service=cast(HttpRevisionMutationService, object()),
            mutation_origin="https://other.example.test",
        )


def test_runtime_composition_reuses_one_mutation_executor_for_both_facades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = object()
    captured: dict[str, object] = {}

    def mutation_executor(**_kwargs: object) -> object:
        return executor

    def catalog_service(*, mutation_executor: object, **_kwargs: object) -> object:
        captured["catalog_executor"] = mutation_executor
        return object()

    def revision_service(candidate: object) -> object:
        captured["revision_executor"] = candidate
        return object()

    def composed_app(**kwargs: object) -> FastAPI:
        captured["app_arguments"] = kwargs
        return FastAPI()

    monkeypatch.setattr(http_app_module, "session_factory", lambda _settings: object())
    monkeypatch.setattr(http_app_module, "_alembic_head", lambda: "synthetic-head")
    monkeypatch.setattr(http_app_module, "HttpMutationExecutor", mutation_executor)
    monkeypatch.setattr(http_app_module, "HttpCatalogService", catalog_service)
    monkeypatch.setattr(http_app_module, "HttpRevisionMutationService", revision_service)
    monkeypatch.setattr(http_app_module, "create_app", composed_app)
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=42,
        miniapp_public_url=ORIGIN,
        http_security_key=SECURITY_KEY,
        bank_import_security_key="AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA",
    )

    app = http_app_module.create_runtime_app(settings=settings)

    assert isinstance(app, FastAPI)
    assert captured["catalog_executor"] is executor
    assert captured["revision_executor"] is executor
