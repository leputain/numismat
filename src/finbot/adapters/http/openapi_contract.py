from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import cast

from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.service import TelegramAuthService
from finbot.adapters.http.bank_imports.service import HttpBankImportService
from finbot.adapters.http.budgets.service import HttpBudgetService
from finbot.adapters.http.catalogs.service import HttpCatalogService
from finbot.adapters.http.exchange_rates.service import HttpExchangeRateService
from finbot.adapters.http.finance.service import FinanceQueryService
from finbot.adapters.http.mutations.service import HttpRevisionMutationService
from finbot.adapters.http.ports import ReadinessProbe
from finbot.adapters.http.recurring.service import HttpRecurringService

DEFAULT_OPENAPI_OUTPUT = Path("openapi/numismat-v1.json")
_CONTRACT_ORIGIN = "https://numismat.invalid"
_INVALID_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")


class OpenApiContractError(ValueError):
    """The generated OpenAPI document is not a self-contained local contract."""


class _OfflineDependency:
    """Inert dependency used only while FastAPI inspects route declarations."""


def build_openapi_document() -> dict[str, object]:
    """Build the complete API contract without loading settings or touching a database."""

    dependency = _OfflineDependency()
    app = create_app(
        readiness_probe=cast(ReadinessProbe, dependency),
        auth_service=cast(TelegramAuthService, dependency),
        auth_origin=_CONTRACT_ORIGIN,
        finance_service=cast(FinanceQueryService, dependency),
        catalog_service=cast(HttpCatalogService, dependency),
        catalog_origin=_CONTRACT_ORIGIN,
        budget_service=cast(HttpBudgetService, dependency),
        budget_origin=_CONTRACT_ORIGIN,
        recurring_service=cast(HttpRecurringService, dependency),
        recurring_origin=_CONTRACT_ORIGIN,
        exchange_rate_service=cast(HttpExchangeRateService, dependency),
        exchange_rate_origin=_CONTRACT_ORIGIN,
        bank_import_service=cast(HttpBankImportService, dependency),
        bank_import_origin=_CONTRACT_ORIGIN,
        mutation_service=cast(HttpRevisionMutationService, dependency),
        mutation_origin=_CONTRACT_ORIGIN,
    )
    document = cast(dict[str, object], app.openapi())
    validate_local_references(document)
    return document


def _pointer_token(value: str) -> str:
    if _INVALID_POINTER_ESCAPE.search(value) is not None:
        raise OpenApiContractError("OpenAPI reference contains an invalid JSON Pointer escape")
    return value.replace("~1", "/").replace("~0", "~")


def _resolve_local_reference(document: object, reference: str) -> None:
    if not reference.startswith("#/"):
        raise OpenApiContractError("OpenAPI references must be local JSON Pointers")
    current = document
    for raw_token in reference[2:].split("/"):
        token = _pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                raise OpenApiContractError("OpenAPI reference target does not exist")
            current = current[token]
            continue
        if isinstance(current, list) and token.isascii() and token.isdecimal():
            index = int(token)
            if index < len(current):
                current = current[index]
                continue
        raise OpenApiContractError("OpenAPI reference target does not exist")


def validate_local_references(document: object) -> None:
    """Reject external, malformed, and dangling ``$ref`` values."""

    stack = [document]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            reference = current.get("$ref")
            if reference is not None:
                if not isinstance(reference, str):
                    raise OpenApiContractError("OpenAPI reference must be a string")
                _resolve_local_reference(document, reference)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def serialize_openapi_document(document: dict[str, object]) -> bytes:
    validate_local_references(document)
    try:
        rendered = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise OpenApiContractError("OpenAPI document is not canonical JSON") from exc
    return rendered.encode("utf-8") + b"\n"


def export_openapi(output: Path = DEFAULT_OPENAPI_OUTPUT) -> None:
    """Atomically export a deterministic OpenAPI document to ``output``."""

    target = Path(output)
    if not target.name or target.name in {".", ".."}:
        raise ValueError("OpenAPI output must name a file")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_openapi_document(build_openapi_document())
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
