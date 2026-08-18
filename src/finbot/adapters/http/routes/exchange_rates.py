from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from finbot.adapters.http.auth.service import CsrfRejectedError, SessionInvalidError
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.exchange_rates.cursor import (
    EXCHANGE_RATE_CURSOR_LENGTH,
    InvalidExchangeRateCursorError,
)
from finbot.adapters.http.exchange_rates.service import (
    DEFAULT_EXCHANGE_RATE_VERSION_LIMIT,
    HttpExchangeRateService,
    HttpRateVersionPage,
    ManualRateInput,
    ManualRateVersionFields,
)
from finbot.adapters.http.finance.request import (
    CANONICAL_UUID_PATTERN,
    canonical_uuid,
    session_token,
    strict_query,
    utc_timestamp,
    validate_period,
)
from finbot.adapters.http.mutations.request import bounded_json_body, mutation_credentials
from finbot.adapters.http.mutations.service import (
    IdempotencyInProgressError,
    IdempotencyKeyReuseError,
    InvalidStoredMutationResultError,
    MutationReceipt,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.adapters.http.schemas.exchange_rates import (
    PUBLISH_MANUAL_RATE_VERSION_ADAPTER,
    ConvertedPeriodResponse,
    RateSourcesResponse,
    RateVersionMutationResponse,
    RateVersionPageResponse,
    RateVersionResponse,
    converted_period_response,
    sources_response,
    version_mutation_response,
    version_page_response,
    version_response,
)
from finbot.application.exchange_rates import (
    ConvertedPeriodValuation,
    RateSourceSnapshot,
    RateVersionSnapshot,
)

_SESSION_SECURITY: dict[str, Any] = {"security": [{"SessionCookie": []}]}
_CANONICAL_DIGEST_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
_CURSOR = re.compile(rf"[A-Za-z0-9_-]{{{EXCHANGE_RATE_CURSOR_LENGTH}}}\Z")
_LIMIT = re.compile(r"(?:[1-9]|[1-4][0-9]|50)\Z")
_VERSION_QUERY = frozenset({"limit", "cursor"})
_CONVERTED_QUERY = frozenset({"version_id", "start", "end"})
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


def _inline_request_schema(adapter: TypeAdapter[Any]) -> dict[str, Any]:
    schema = adapter.json_schema()
    raw_definitions = schema.pop("$defs", {})
    definitions = raw_definitions if isinstance(raw_definitions, dict) else {}

    def resolve(value: object) -> object:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError("request schema contains an unresolved definition")
            merged = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
            return resolve(merged)
        return {key: resolve(item) for key, item in value.items()}

    resolved = resolve(schema)
    if not isinstance(resolved, dict):  # pragma: no cover - Pydantic contract
        raise ValueError("request schema root must be an object")
    return resolved


def _mutation_openapi[ModelT](adapter: TypeAdapter[ModelT]) -> dict[str, Any]:
    return {
        **_SESSION_SECURITY,
        "parameters": _MUTATION_PARAMETERS,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inline_request_schema(adapter)}},
        },
    }


def _limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_EXCHANGE_RATE_VERSION_LIMIT
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
    except InvalidExchangeRateCursorError as exc:
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
    body = version_mutation_response(receipt).model_dump(mode="json")
    return JSONResponse(status_code=receipt.http_status, content=body)


def exchange_rate_router(
    service: HttpExchangeRateService,
    *,
    expected_origin: str,
) -> APIRouter:
    router = APIRouter(tags=["exchange-rates"])

    @router.get(
        "/api/v1/exchange-rate-sources",
        response_model=RateSourcesResponse,
        responses=_error_responses(401, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def list_exchange_rate_sources(request: Request) -> RateSourcesResponse:
        strict_query(request, allowed=frozenset())

        async def execute(raw_session: str) -> tuple[RateSourceSnapshot, ...]:
            return await service.list_sources(raw_session)

        return sources_response(await _safe_read(request, execute))

    @router.get(
        "/api/v1/exchange-rate-sources/{source_id}/versions",
        response_model=RateVersionPageResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                {
                    "in": "query",
                    "name": "limit",
                    "required": False,
                    "schema": {
                        "default": DEFAULT_EXCHANGE_RATE_VERSION_LIMIT,
                        "maximum": 50,
                        "minimum": 1,
                        "type": "integer",
                    },
                },
                {
                    "description": (
                        "Signed integrity-protected owner/source-bound cursor. It is not "
                        "encrypted; clients must treat it as opaque."
                    ),
                    "in": "query",
                    "name": "cursor",
                    "required": False,
                    "schema": {
                        "maxLength": EXCHANGE_RATE_CURSOR_LENGTH,
                        "minLength": EXCHANGE_RATE_CURSOR_LENGTH,
                        "pattern": (rf"^[A-Za-z0-9_-]{{{EXCHANGE_RATE_CURSOR_LENGTH}}}$"),
                        "type": "string",
                    },
                },
            ],
        },
    )
    async def list_exchange_rate_versions(
        request: Request,
        source_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> RateVersionPageResponse:
        query = strict_query(request, allowed=_VERSION_QUERY)
        parsed_id = canonical_uuid(source_id)

        async def execute(raw_session: str) -> HttpRateVersionPage:
            return await service.list_versions(
                raw_session,
                parsed_id,
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor")),
            )

        return version_page_response(await _safe_read(request, execute))

    @router.get(
        "/api/v1/exchange-rate-versions/{version_id}",
        response_model=RateVersionResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_exchange_rate_version(
        request: Request,
        version_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> RateVersionResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(version_id)

        async def execute(raw_session: str) -> RateVersionSnapshot:
            return await service.get_version(raw_session, parsed_id)

        return version_response(await _safe_read(request, execute))

    @router.post(
        "/api/v1/exchange-rate-sources/manual/versions",
        status_code=201,
        response_model=RateVersionMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(PUBLISH_MANUAL_RATE_VERSION_ADAPTER),
    )
    async def publish_manual_exchange_rate_version(request: Request) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, PUBLISH_MANUAL_RATE_VERSION_ADAPTER)
        fields = ManualRateVersionFields(
            target_currency=body.target_currency,
            expected_source_version=body.expected_source_version,
            effective_at=utc_timestamp(body.effective_at),
            entries=tuple(
                ManualRateInput(
                    source_currency=entry.source_currency,
                    rate=entry.rate,
                )
                for entry in body.entries
            ),
        )
        return _mutation_response(
            await _safe_mutation(lambda: service.publish(credentials, fields))
        )

    @router.get(
        "/api/v1/reports/period/converted",
        response_model=ConvertedPeriodResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                {
                    "in": "query",
                    "name": "version_id",
                    "required": True,
                    "schema": {
                        "format": "uuid",
                        "pattern": CANONICAL_UUID_PATTERN,
                        "type": "string",
                    },
                },
                *[
                    {
                        "in": "query",
                        "name": name,
                        "required": True,
                        "schema": {
                            "format": "date-time",
                            "type": "string",
                        },
                    }
                    for name in ("start", "end")
                ],
            ],
        },
    )
    async def converted_period(request: Request) -> ConvertedPeriodResponse:
        query = strict_query(request, allowed=_CONVERTED_QUERY)
        try:
            version_id = canonical_uuid(query["version_id"])
            start = utc_timestamp(query["start"])
            end = utc_timestamp(query["end"])
        except KeyError as exc:
            raise HttpApiError(
                status_code=422,
                code=HttpErrorCode.VALIDATION_FAILED,
            ) from exc
        validate_period(start, end)

        async def execute(raw_session: str) -> ConvertedPeriodValuation:
            return await service.converted_period(
                raw_session,
                version_id,
                start,
                end,
            )

        return converted_period_response(await _safe_read(request, execute))

    return router


__all__ = ["exchange_rate_router"]
