from __future__ import annotations

import io
import json
import logging

import httpx2
import pytest
from fastapi import FastAPI

from finbot.adapters.http.app import create_app
from finbot.application.errors import DraftRevisionConflictError
from finbot.observability.logging import JsonFormatter, correlation_id


class ReadinessStub:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    async def check(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://numismat.invalid",
    )


@pytest.mark.asyncio
async def test_http_skeleton_exposes_versioned_metadata_and_health() -> None:
    readiness = ReadinessStub()
    app = create_app(readiness_probe=readiness)

    async with _client(app) as client:
        api = await client.get("/api/v1")
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        schema = await client.get("/api/v1/openapi.json")

    assert api.status_code == 200
    assert api.json() == {"api_version": "v1", "service": "numismat"}
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}
    assert readiness.calls == 1
    assert schema.status_code == 200
    assert schema.json()["info"] == {
        "description": (
            "Versioned HTTP adapter for a bounded operator-managed allowlist of "
            "independent Numismat ledgers."
        ),
        "license": {
            "name": "Apache License 2.0",
            "identifier": "Apache-2.0",
        },
        "summary": "Private finance API for isolated allowlisted users",
        "title": "Numismat API",
        "version": "0.45.0",
    }
    assert set(schema.json()["paths"]) == {"/api/v1", "/health/live", "/health/ready"}


@pytest.mark.asyncio
async def test_readiness_failure_is_fixed_and_does_not_leak_exception() -> None:
    sensitive = "database-credential-fragment"
    app = create_app(readiness_probe=ReadinessStub(failure=RuntimeError(sensitive)))

    async with _client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "readiness_failed",
            "details": {},
            "message": "Сервис временно не готов",
        }
    }
    assert sensitive not in response.text


@pytest.mark.asyncio
async def test_http_errors_are_structured_and_application_details_are_allowlisted() -> None:
    app = create_app(readiness_probe=ReadinessStub())

    @app.get("/api/v1/conflict")
    async def conflict() -> None:
        raise DraftRevisionConflictError(current_revision=7)

    @app.get("/api/v1/failure")
    async def failure() -> None:
        raise RuntimeError("sensitive-exception-fragment")

    async with _client(app) as client:
        conflict_response = await client.get("/api/v1/conflict")
        failure_response = await client.get("/api/v1/failure")
        missing_response = await client.get("/api/v1/missing?secret=sensitive-query")
        method_response = await client.post("/api/v1")

    assert conflict_response.status_code == 409
    assert conflict_response.json() == {
        "error": {
            "code": "draft_revision_conflict",
            "details": {"current_revision": 7},
            "message": "Черновик был изменён",
        }
    }
    assert failure_response.status_code == 500
    assert failure_response.json()["error"]["code"] == "internal_error"
    assert "sensitive-exception-fragment" not in failure_response.text
    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "not_found"
    assert "sensitive-query" not in missing_response.text
    assert method_response.status_code == 405
    assert method_response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.asyncio
async def test_http_request_log_contains_only_route_status_duration_and_error_code() -> None:
    app = create_app(readiness_probe=ReadinessStub())
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    sensitive = "987654:sensitive-token-fragment"
    try:
        async with _client(app) as client:
            response = await client.get(
                f"/not-real/{sensitive}?auth={sensitive}",
                headers={"Authorization": f"Bearer {sensitive}"},
            )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert response.status_code == 404
    rendered = stream.getvalue()
    entries = [
        entry
        for line in rendered.splitlines()
        if line
        for entry in [json.loads(line)]
        if entry["event"] == "http_request_completed"
    ]
    assert entries == [
        {
            "component": "http",
            "correlation_id": entries[0]["correlation_id"],
            "duration": entries[0]["duration"],
            "error_code": "not_found",
            "event": "http_request_completed",
            "level": "INFO",
            "result": "rejected",
            "route_template": "/unmatched",
            "status_code": 404,
        }
    ]
    assert len(entries[0]["correlation_id"]) == 12
    assert sensitive not in rendered
    assert "Authorization" not in rendered
    assert "auth=" not in rendered
    assert correlation_id.get() == "system"
