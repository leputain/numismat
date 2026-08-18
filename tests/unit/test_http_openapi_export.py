from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fastapi.routing import APIRoute

import finbot.adapters.http.app as http_app
from finbot.adapters.http.bank_imports.service import HttpBankImportService
from finbot.adapters.http.openapi_contract import (
    OpenApiContractError,
    build_openapi_document,
    export_openapi,
    serialize_openapi_document,
    validate_local_references,
)
from finbot.adapters.http.routes.bank_imports import bank_import_router
from finbot.config import Settings


def _unexpected_runtime_access(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("offline OpenAPI export must not access runtime configuration")


def test_openapi_export_is_offline_deterministic_and_self_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Settings, "from_secret_or_env", _unexpected_runtime_access)
    monkeypatch.setattr(http_app, "session_factory", _unexpected_runtime_access)

    first = build_openapi_document()
    second = build_openapi_document()
    first_bytes = serialize_openapi_document(first)
    second_bytes = serialize_openapi_document(second)

    assert first == second
    assert first_bytes == second_bytes
    assert first_bytes.endswith(b"\n") and not first_bytes.endswith(b"\n\n")
    assert b"numismat.invalid" not in first_bytes
    validate_local_references(first)


def test_openapi_export_contains_finance_automation_surface_and_session_scheme() -> None:
    document = build_openapi_document()
    paths = document.get("paths")
    assert isinstance(paths, dict)
    assert len(paths) == 62
    expected_operations = {
        "/api/v1/accounts": {"get", "post"},
        "/api/v1/accounts/{account_id}": {"patch"},
        "/api/v1/accounts/{account_id}/archive": {"post"},
        "/api/v1/accounts/{account_id}/default": {"post"},
        "/api/v1/accounts/{account_id}/restore": {"post"},
        "/api/v1/categories": {"get", "post"},
        "/api/v1/categories/{category_id}": {"patch"},
        "/api/v1/categories/{category_id}/archive": {"post"},
        "/api/v1/categories/{category_id}/restore": {"post"},
        "/api/v1/budgets": {"get", "post"},
        "/api/v1/budgets/{budget_id}": {"get", "put"},
        "/api/v1/budgets/{budget_id}/delete": {"post"},
        "/api/v1/budgets/{budget_id}/restore": {"post"},
        "/api/v1/recurring-schedules": {"get", "post"},
        "/api/v1/recurring-schedules/{schedule_id}": {"get", "put"},
        "/api/v1/recurring-schedules/{schedule_id}/instances": {"get"},
        "/api/v1/recurring-schedules/{schedule_id}/pause": {"post"},
        "/api/v1/recurring-schedules/{schedule_id}/resume": {"post"},
        "/api/v1/recurring-schedules/{schedule_id}/delete": {"post"},
        "/api/v1/recurring-schedules/{schedule_id}/restore": {"post"},
        "/api/v1/recurring-instances/{instance_id}": {"get"},
        "/api/v1/recurring-instances/{instance_id}/skip": {"post"},
        "/api/v1/recurring-instances/{instance_id}/retry": {"post"},
        "/api/v1/bank-imports": {"get"},
        "/api/v1/bank-imports/upload": {"post"},
        "/api/v1/bank-imports/{batch_id}": {"get"},
        "/api/v1/bank-imports/{batch_id}/cancel": {"post"},
        "/api/v1/bank-imports/{batch_id}/rows": {"get"},
        "/api/v1/bank-imports/{batch_id}/rows/{row_id}": {"get"},
        "/api/v1/bank-imports/{batch_id}/rows/{row_id}/candidates": {"get"},
        "/api/v1/bank-imports/{batch_id}/rows/{row_id}/draft": {"post"},
        "/api/v1/bank-imports/{batch_id}/rows/{row_id}/link": {"post"},
        "/api/v1/bank-imports/{batch_id}/rows/{row_id}/skip": {"post"},
        "/api/v1/reports/today": {"get"},
    }
    for path, methods in expected_operations.items():
        path_item = paths.get(path)
        assert isinstance(path_item, dict)
        assert methods <= path_item.keys()

    components = document.get("components")
    assert isinstance(components, dict)
    security_schemes = components.get("securitySchemes")
    assert isinstance(security_schemes, dict)
    assert security_schemes.get("SessionCookie") == {
        "in": "cookie",
        "name": "__Host-numismat_session",
        "type": "apiKey",
    }

    bank_paths = [
        route.path
        for route in bank_import_router(
            cast(HttpBankImportService, object()),
            expected_origin="https://numismat.invalid",
        ).routes
        if isinstance(route, APIRoute)
    ]
    assert bank_paths.index("/api/v1/bank-imports/upload") < bank_paths.index(
        "/api/v1/bank-imports/{batch_id}"
    )


def test_openapi_export_replaces_target_atomically_and_reproducibly(
    tmp_path: Path,
) -> None:
    output = tmp_path / "contract" / "openapi.json"
    output.parent.mkdir()
    output.write_bytes(b"stale")

    export_openapi(output)
    first = output.read_bytes()
    export_openapi(output)

    assert output.read_bytes() == first
    assert first == serialize_openapi_document(build_openapi_document())
    assert not tuple(output.parent.glob(f".{output.name}.*.tmp"))


def test_openapi_export_preserves_target_and_cleans_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "openapi.json"
    output.write_bytes(b"previous-contract")

    def fail_replace(_source: object, _target: object) -> NoReturn:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("finbot.adapters.http.openapi_contract.os.replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        export_openapi(output)

    assert output.read_bytes() == b"previous-contract"
    assert not tuple(output.parent.glob(f".{output.name}.*.tmp"))


def test_openapi_script_runs_without_pythonpath(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"

    result = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == serialize_openapi_document(build_openapi_document())


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema.json",
        "#/components/schemas/Missing",
        "#/components/schemas/Bad~2Escape",
    ],
)
def test_openapi_export_rejects_external_dangling_and_malformed_references(
    reference: str,
) -> None:
    document: dict[str, object] = {
        "components": {"schemas": {}},
        "openapi": "3.1.0",
        "paths": {"/test": {"get": {"responses": {"200": {"$ref": reference}}}}},
    }

    with pytest.raises(OpenApiContractError):
        validate_local_references(document)
