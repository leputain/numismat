from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import time
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from finbot.adapters.http.auth.service import CsrfRejectedError, SessionInvalidError
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
    MutationOperation,
    MutationReceipt,
)
from finbot.adapters.http.recurring.cursor import (
    RECURRING_CURSOR_LENGTH,
    InvalidRecurringCursorError,
)
from finbot.adapters.http.recurring.service import (
    DEFAULT_RECURRING_LIMIT,
    HttpRecurringInstancePage,
    HttpRecurringSchedulePage,
    HttpRecurringService,
    RecurringDefinitionFields,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.adapters.http.schemas.recurring import (
    CREATE_RECURRING_SCHEDULE_ADAPTER,
    REPLACE_RECURRING_SCHEDULE_ADAPTER,
    VERSIONED_RECURRING_ADAPTER,
    CreateRecurringScheduleRequest,
    RecurringInstanceMutationResponse,
    RecurringInstancePageResponse,
    RecurringInstanceResponse,
    RecurringScheduleMutationResponse,
    RecurringSchedulePageResponse,
    RecurringScheduleResponse,
    ReplaceRecurringScheduleRequest,
    instance_mutation_response,
    instance_page_response,
    instance_response,
    schedule_mutation_response,
    schedule_page_response,
    schedule_response,
)
from finbot.application.recurring import RecurringInstanceSnapshot, RecurringScheduleSnapshot
from finbot.domain.recurrence import RecurrenceCadence
from finbot.domain.transactions import TransactionType

_SESSION_SECURITY: dict[str, Any] = {"security": [{"SessionCookie": []}]}
_CANONICAL_DIGEST_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
_CURSOR = re.compile(rf"[A-Za-z0-9_-]{{{RECURRING_CURSOR_LENGTH}}}\Z")
_LIMIT = re.compile(r"(?:[1-9]|[1-4][0-9]|50)\Z")
_SCHEDULE_QUERY = frozenset({"deleted", "limit", "cursor"})
_INSTANCE_QUERY = frozenset({"limit", "cursor"})
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


def _deleted(value: str | None) -> bool:
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)


def _limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_RECURRING_LIMIT
    if _LIMIT.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return int(value)


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if _CURSOR.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR)
    return value


def _fields(
    body: CreateRecurringScheduleRequest | ReplaceRecurringScheduleRequest,
) -> RecurringDefinitionFields:
    return RecurringDefinitionFields(
        name=body.name,
        kind=TransactionType(body.kind),
        amount_minor=int(body.amount_minor),
        currency=body.currency,
        account_id=body.account_id,
        category_id=body.category_id,
        cadence=RecurrenceCadence(body.cadence),
        interval=body.interval,
        anchor_date=body.anchor_date,
        local_time=time.fromisoformat(body.local_time),
        ends_on=body.ends_on,
        description=body.description,
    )


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
    except InvalidRecurringCursorError as exc:
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


def _schedule_mutation_response(receipt: MutationReceipt) -> JSONResponse:
    return JSONResponse(
        status_code=receipt.http_status,
        content=schedule_mutation_response(receipt).model_dump(mode="json"),
    )


def _instance_mutation_response(receipt: MutationReceipt) -> JSONResponse:
    return JSONResponse(
        status_code=receipt.http_status,
        content=instance_mutation_response(receipt).model_dump(mode="json"),
    )


def recurring_router(service: HttpRecurringService, *, expected_origin: str) -> APIRouter:
    router = APIRouter(tags=["recurring"])

    list_parameters = [
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
                "default": DEFAULT_RECURRING_LIMIT,
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
                "maxLength": RECURRING_CURSOR_LENGTH,
                "minLength": RECURRING_CURSOR_LENGTH,
                "pattern": rf"^[A-Za-z0-9_-]{{{RECURRING_CURSOR_LENGTH}}}$",
                "type": "string",
            },
        },
    ]

    @router.get(
        "/api/v1/recurring-schedules",
        response_model=RecurringSchedulePageResponse,
        responses=_error_responses(401, 422, 500),
        openapi_extra={**_SESSION_SECURITY, "parameters": list_parameters},
    )
    async def list_recurring_schedules(request: Request) -> RecurringSchedulePageResponse:
        query = strict_query(request, allowed=_SCHEDULE_QUERY)

        async def execute(raw_session: str) -> HttpRecurringSchedulePage:
            return await service.list_schedules(
                raw_session,
                deleted=_deleted(query.get("deleted")),
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor")),
            )

        return schedule_page_response(await _safe_read(request, execute))

    @router.post(
        "/api/v1/recurring-schedules",
        status_code=201,
        response_model=RecurringScheduleMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(CREATE_RECURRING_SCHEDULE_ADAPTER),
    )
    async def create_recurring_schedule(request: Request) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, CREATE_RECURRING_SCHEDULE_ADAPTER)
        receipt = await _safe_mutation(lambda: service.create(credentials, _fields(body)))
        return _schedule_mutation_response(receipt)

    @router.get(
        "/api/v1/recurring-schedules/{schedule_id}/instances",
        response_model=RecurringInstancePageResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": list_parameters[1:],
        },
    )
    async def list_recurring_instances(
        request: Request,
        schedule_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> RecurringInstancePageResponse:
        query = strict_query(request, allowed=_INSTANCE_QUERY)
        parsed_id = canonical_uuid(schedule_id)

        async def execute(raw_session: str) -> HttpRecurringInstancePage:
            return await service.list_instances(
                raw_session,
                parsed_id,
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor")),
            )

        return instance_page_response(await _safe_read(request, execute))

    @router.get(
        "/api/v1/recurring-schedules/{schedule_id}",
        response_model=RecurringScheduleResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_recurring_schedule(
        request: Request,
        schedule_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> RecurringScheduleResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(schedule_id)

        async def execute(raw_session: str) -> RecurringScheduleSnapshot:
            return await service.get_schedule(raw_session, parsed_id)

        return schedule_response(await _safe_read(request, execute))

    @router.put(
        "/api/v1/recurring-schedules/{schedule_id}",
        response_model=RecurringScheduleMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(REPLACE_RECURRING_SCHEDULE_ADAPTER),
    )
    async def replace_recurring_schedule(
        request: Request,
        schedule_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, REPLACE_RECURRING_SCHEDULE_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.replace(
                credentials,
                canonical_uuid(schedule_id),
                body.version,
                _fields(body),
            )
        )
        return _schedule_mutation_response(receipt)

    async def schedule_lifecycle(
        request: Request,
        raw_id: str,
        operation: MutationOperation,
    ) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, VERSIONED_RECURRING_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.schedule_lifecycle(
                credentials,
                canonical_uuid(raw_id),
                body.version,
                operation,
            )
        )
        return _schedule_mutation_response(receipt)

    schedule_lifecycle_openapi = _mutation_openapi(VERSIONED_RECURRING_ADAPTER)

    def register_schedule_lifecycle(path: str, operation: MutationOperation) -> None:
        async def endpoint(
            request: Request,
            schedule_id: Annotated[
                str,
                Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
            ],
        ) -> JSONResponse:
            return await schedule_lifecycle(request, schedule_id, operation)

        endpoint.__name__ = f"{operation.value.replace('.', '_')}_endpoint"
        router.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            response_model=RecurringScheduleMutationResponse,
            responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
            openapi_extra=schedule_lifecycle_openapi,
        )

    for action, operation in (
        ("pause", MutationOperation.RECURRING_SCHEDULE_PAUSE),
        ("resume", MutationOperation.RECURRING_SCHEDULE_RESUME),
        ("delete", MutationOperation.RECURRING_SCHEDULE_DELETE),
        ("restore", MutationOperation.RECURRING_SCHEDULE_RESTORE),
    ):
        register_schedule_lifecycle(
            f"/api/v1/recurring-schedules/{{schedule_id}}/{action}",
            operation,
        )

    @router.get(
        "/api/v1/recurring-instances/{instance_id}",
        response_model=RecurringInstanceResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_recurring_instance(
        request: Request,
        instance_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> RecurringInstanceResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(instance_id)

        async def execute(raw_session: str) -> RecurringInstanceSnapshot:
            return await service.get_instance(raw_session, parsed_id)

        return instance_response(await _safe_read(request, execute))

    async def instance_lifecycle(
        request: Request,
        raw_id: str,
        operation: MutationOperation,
    ) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, VERSIONED_RECURRING_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.instance_lifecycle(
                credentials,
                canonical_uuid(raw_id),
                body.version,
                operation,
            )
        )
        return _instance_mutation_response(receipt)

    instance_lifecycle_openapi = _mutation_openapi(VERSIONED_RECURRING_ADAPTER)

    def register_instance_lifecycle(path: str, operation: MutationOperation) -> None:
        async def endpoint(
            request: Request,
            instance_id: Annotated[
                str,
                Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
            ],
        ) -> JSONResponse:
            return await instance_lifecycle(request, instance_id, operation)

        endpoint.__name__ = f"{operation.value.replace('.', '_')}_endpoint"
        router.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            response_model=RecurringInstanceMutationResponse,
            responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
            openapi_extra=instance_lifecycle_openapi,
        )

    register_instance_lifecycle(
        "/api/v1/recurring-instances/{instance_id}/skip",
        MutationOperation.RECURRING_INSTANCE_SKIP,
    )
    register_instance_lifecycle(
        "/api/v1/recurring-instances/{instance_id}/retry",
        MutationOperation.RECURRING_INSTANCE_RETRY,
    )

    return router


__all__ = ["recurring_router"]
