from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from finbot.adapters.http.auth.service import CsrfRejectedError, SessionInvalidError
from finbot.adapters.http.budgets.cursor import (
    BUDGET_CURSOR_LENGTH,
    InvalidBudgetCursorError,
)
from finbot.adapters.http.budgets.service import (
    DEFAULT_BUDGET_LIMIT,
    HttpBudgetPage,
    HttpBudgetService,
)
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.finance.request import (
    CANONICAL_UUID_PATTERN,
    canonical_uuid,
    session_token,
    strict_query,
)
from finbot.adapters.http.mutations.request import bounded_json_body, mutation_credentials
from finbot.adapters.http.mutations.service import (
    IdempotencyInProgressError,
    IdempotencyKeyReuseError,
    InvalidStoredMutationResultError,
    MutationCredentials,
    MutationReceipt,
)
from finbot.adapters.http.schemas.budgets import (
    CREATE_BUDGET_ADAPTER,
    REPLACE_BUDGET_ADAPTER,
    VERSIONED_BUDGET_ADAPTER,
    BudgetMutationResponse,
    BudgetPageResponse,
    BudgetResponse,
    budget_mutation_response,
    budget_page_response,
    budget_response,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.application.budgets import BudgetProgressSnapshot

_SESSION_SECURITY: dict[str, Any] = {"security": [{"SessionCookie": []}]}
_CANONICAL_DIGEST_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
_DATE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])\Z")
_CURSOR = re.compile(rf"[A-Za-z0-9_-]{{{BUDGET_CURSOR_LENGTH}}}\Z")
_LIMIT = re.compile(r"(?:[1-9]|[1-4][0-9]|50)\Z")
_QUERY = frozenset({"starts_on", "ends_on", "deleted", "limit", "cursor"})
_MUTATION_PARAMETERS: list[dict[str, Any]] = [
    {
        "in": "header",
        "name": "Origin",
        "required": True,
        "schema": {"format": "uri", "type": "string"},
    },
    {
        "in": "header",
        "name": "X-CSRF-Token",
        "required": True,
        "schema": {
            "maxLength": 43,
            "minLength": 43,
            "pattern": _CANONICAL_DIGEST_PATTERN,
            "type": "string",
        },
    },
    {
        "description": "Opaque owner-wide idempotency key retained for 24 hours.",
        "in": "header",
        "name": "Idempotency-Key",
        "required": True,
        "schema": {
            "maxLength": 43,
            "minLength": 43,
            "pattern": _CANONICAL_DIGEST_PATTERN,
            "type": "string",
        },
    },
]


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ApiErrorResponse} for status in statuses}


def _mutation_openapi[ModelT](adapter: TypeAdapter[ModelT]) -> dict[str, Any]:
    return {
        **_SESSION_SECURITY,
        "parameters": _MUTATION_PARAMETERS,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": adapter.json_schema()}},
        },
    }


def _local_date(value: str | None) -> date | None:
    if value is None:
        return None
    if _DATE.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED) from exc
    if parsed.isoformat() != value:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return parsed


def _deleted(value: str | None) -> bool:
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)


def _limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_BUDGET_LIMIT
    if _LIMIT.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return int(value)


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if _CURSOR.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR)
    return value


async def _safe_read[ResultT](
    request: Request,
    operation: Callable[[str], Awaitable[ResultT]],
) -> ResultT:
    try:
        return await operation(session_token(request))
    except SessionInvalidError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
            clear_auth_cookies=True,
        ) from exc
    except InvalidBudgetCursorError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR) from exc


async def _safe_mutation(operation: Callable[[], Awaitable[MutationReceipt]]) -> MutationReceipt:
    try:
        return await operation()
    except CsrfRejectedError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED) from exc
    except SessionInvalidError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
            clear_auth_cookies=True,
        ) from exc
    except IdempotencyKeyReuseError as exc:
        raise HttpApiError(
            status_code=409,
            code=HttpErrorCode.IDEMPOTENCY_KEY_CONFLICT,
        ) from exc
    except IdempotencyInProgressError as exc:
        raise HttpApiError(
            status_code=409,
            code=HttpErrorCode.IDEMPOTENCY_IN_PROGRESS,
        ) from exc
    except InvalidStoredMutationResultError as exc:
        raise HttpApiError(status_code=500, code=HttpErrorCode.INTERNAL_ERROR) from exc


def _mutation_response(receipt: MutationReceipt) -> JSONResponse:
    body = budget_mutation_response(receipt).model_dump(mode="json")
    return JSONResponse(status_code=receipt.http_status, content=body)


def budget_router(service: HttpBudgetService, *, expected_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/budgets", tags=["budgets"])

    @router.get(
        "",
        response_model=BudgetPageResponse,
        responses=_error_responses(401, 422, 500),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                {
                    "in": "query",
                    "name": "starts_on",
                    "required": False,
                    "schema": {"format": "date", "type": "string"},
                },
                {
                    "in": "query",
                    "name": "ends_on",
                    "required": False,
                    "schema": {"format": "date", "type": "string"},
                },
                {
                    "in": "query",
                    "name": "deleted",
                    "required": False,
                    "schema": {"default": False, "type": "boolean"},
                },
                {
                    "in": "query",
                    "name": "limit",
                    "required": False,
                    "schema": {
                        "default": DEFAULT_BUDGET_LIMIT,
                        "maximum": 50,
                        "minimum": 1,
                        "type": "integer",
                    },
                },
                {
                    "description": (
                        "Signed integrity-protected owner/filter-bound cursor. It is not "
                        "encrypted; clients must treat it as opaque."
                    ),
                    "in": "query",
                    "name": "cursor",
                    "required": False,
                    "schema": {
                        "maxLength": BUDGET_CURSOR_LENGTH,
                        "minLength": BUDGET_CURSOR_LENGTH,
                        "pattern": rf"^[A-Za-z0-9_-]{{{BUDGET_CURSOR_LENGTH}}}$",
                        "type": "string",
                    },
                },
            ],
        },
    )
    async def list_budgets(request: Request) -> BudgetPageResponse:
        query = strict_query(request, allowed=_QUERY)

        async def execute(raw_session: str) -> HttpBudgetPage:
            return await service.list(
                raw_session,
                window_start=_local_date(query.get("starts_on")),
                window_end=_local_date(query.get("ends_on")),
                deleted=_deleted(query.get("deleted")),
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor")),
            )

        return budget_page_response(await _safe_read(request, execute))

    @router.get(
        "/{budget_id}",
        response_model=BudgetResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_budget(
        request: Request,
        budget_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> BudgetResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(budget_id)

        async def execute(raw_session: str) -> BudgetProgressSnapshot:
            return await service.get(raw_session, parsed_id)

        return budget_response(await _safe_read(request, execute))

    @router.post(
        "",
        status_code=201,
        response_model=BudgetMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(CREATE_BUDGET_ADAPTER),
    )
    async def create_budget(request: Request) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, CREATE_BUDGET_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.create(
                credentials,
                name=body.name,
                limit_minor=int(body.limit_minor),
                currency=body.currency,
                category_id=body.category_id,
                starts_on=body.starts_on,
                ends_on=body.ends_on,
            )
        )
        return _mutation_response(receipt)

    async def credentials_and_id(
        request: Request,
        raw_id: str,
    ) -> tuple[MutationCredentials, UUID]:
        strict_query(request, allowed=frozenset())
        return (
            mutation_credentials(request, expected_origin=expected_origin),
            canonical_uuid(raw_id),
        )

    @router.put(
        "/{budget_id}",
        response_model=BudgetMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(REPLACE_BUDGET_ADAPTER),
    )
    async def replace_budget(
        request: Request,
        budget_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, budget_id)
        body = await bounded_json_body(request, REPLACE_BUDGET_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.replace(
                credentials,
                parsed_id,
                version=body.version,
                name=body.name,
                limit_minor=int(body.limit_minor),
                currency=body.currency,
                category_id=body.category_id,
                starts_on=body.starts_on,
                ends_on=body.ends_on,
            )
        )
        return _mutation_response(receipt)

    async def lifecycle(
        request: Request,
        raw_id: str,
        operation: Callable[[MutationCredentials, UUID, int], Awaitable[MutationReceipt]],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, raw_id)
        body = await bounded_json_body(request, VERSIONED_BUDGET_ADAPTER)
        return _mutation_response(
            await _safe_mutation(lambda: operation(credentials, parsed_id, body.version))
        )

    version_openapi = _mutation_openapi(VERSIONED_BUDGET_ADAPTER)

    @router.post(
        "/{budget_id}/delete",
        response_model=BudgetMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def delete_budget(
        request: Request,
        budget_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await lifecycle(request, budget_id, service.delete)

    @router.post(
        "/{budget_id}/restore",
        response_model=BudgetMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def restore_budget(
        request: Request,
        budget_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await lifecycle(request, budget_id, service.restore)

    return router


__all__ = ["budget_router"]
