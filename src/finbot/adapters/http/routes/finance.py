from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request

from finbot.adapters.http.auth.request import (
    SESSION_BINDING_OPENAPI_PARAMETER,
    session_credentials,
)
from finbot.adapters.http.auth.service import (
    SessionBindingMismatchError,
    SessionCredentials,
    SessionInvalidError,
)
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.finance.cursor import InvalidTransactionCursorError
from finbot.adapters.http.finance.request import (
    CANONICAL_UUID_PATTERN,
    DEFAULT_REPORT_LIMIT,
    DEFAULT_TRANSACTION_LIMIT,
    bounded_limit,
    canonical_cursor,
    canonical_uuid,
    strict_query,
    timeseries_grain,
    transaction_list_filters,
    utc_timestamp,
    validate_comparison,
    validate_period,
)
from finbot.adapters.http.finance.service import FinanceQueryService, TransactionCursorPage
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.adapters.http.schemas.finance import (
    DashboardResponse,
    PeriodComparisonResponse,
    PeriodReportResponse,
    TimeSeriesResponse,
    TransactionPageResponse,
    TransactionResponse,
    comparison_response,
    dashboard_response,
    period_report_response,
    timeseries_response,
    transaction_response,
)
from finbot.application.dto import (
    PeriodComparisonSnapshot,
    PeriodReportSnapshot,
    TimeSeriesSnapshot,
    TransactionSnapshot,
)

_SESSION_SECURITY: dict[str, Any] = {
    "security": [{"SessionCookie": []}],
    "parameters": [SESSION_BINDING_OPENAPI_PARAMETER],
}
_PERIOD_QUERY = frozenset({"start", "end", "category_limit", "transaction_limit"})
_COMPARE_QUERY = frozenset({"current_start", "current_end", "previous_start", "previous_end"})
_TIMESERIES_QUERY = frozenset({"start", "end", "grain"})
_ACTIVE_TRANSACTION_QUERY = frozenset(
    {
        "limit",
        "cursor",
        "start",
        "end",
        "type",
        "account_id",
        "category_id",
        "currency",
    }
)
_DELETED_TRANSACTION_QUERY = frozenset({"limit", "cursor"})
_UTC_DATE_SCHEMA = {
    "description": "UTC RFC 3339 timestamp with optional 1-6 fractional digits and `Z`.",
    "format": "date-time",
    "pattern": (
        r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
        r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
    ),
    "type": "string",
}


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ApiErrorResponse} for status in statuses}


def _date_parameter(name: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "in": "query",
        "name": name,
        "required": required,
        "schema": _UTC_DATE_SCHEMA,
    }


def _limit_parameter(name: str, default: int) -> dict[str, Any]:
    return {
        "in": "query",
        "name": name,
        "required": False,
        "schema": {"default": default, "maximum": 100, "minimum": 1, "type": "integer"},
    }


def _transaction_filter_parameters() -> list[dict[str, Any]]:
    start = _date_parameter("start", required=False)
    start["description"] = (
        "Inclusive UTC occurrence boundary. Must be provided together with `end`; "
        "the maximum span is 366 days."
    )
    end = _date_parameter("end", required=False)
    end["description"] = (
        "Exclusive UTC occurrence boundary. Must be provided together with `start`; "
        "the maximum span is 366 days."
    )
    return [
        start,
        end,
        {
            "in": "query",
            "name": "type",
            "required": False,
            "schema": {"enum": ["expense", "income"], "type": "string"},
        },
        *[
            {
                "in": "query",
                "name": name,
                "required": False,
                "schema": {
                    "format": "uuid",
                    "maxLength": 36,
                    "minLength": 36,
                    "pattern": CANONICAL_UUID_PATTERN,
                    "type": "string",
                },
            }
            for name in ("account_id", "category_id")
        ],
        {
            "in": "query",
            "name": "currency",
            "required": False,
            "schema": {
                "maxLength": 3,
                "minLength": 3,
                "pattern": r"^[A-Z]{3}$",
                "type": "string",
            },
        },
    ]


async def _safe_session_call[ResultT](
    request: Request,
    operation: Callable[[SessionCredentials], Awaitable[ResultT]],
) -> ResultT:
    try:
        return await operation(session_credentials(request))
    except SessionBindingMismatchError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
        ) from exc
    except SessionInvalidError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
        ) from exc
    except InvalidTransactionCursorError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR) from exc


def finance_router(service: FinanceQueryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["finance"])

    @router.get(
        "/dashboard",
        response_model=DashboardResponse,
        responses=_error_responses(401, 422),
        openapi_extra=_SESSION_SECURITY,
    )
    async def dashboard(request: Request) -> DashboardResponse:
        strict_query(request, allowed=frozenset())
        result = await _safe_session_call(request, service.dashboard)
        return dashboard_response(result)

    @router.get(
        "/reports/today",
        response_model=PeriodReportResponse,
        responses=_error_responses(401, 422),
        openapi_extra=_SESSION_SECURITY,
    )
    async def today_report(request: Request) -> PeriodReportResponse:
        strict_query(request, allowed=frozenset())
        result = await _safe_session_call(request, service.today_report)
        return period_report_response(result)

    @router.get(
        "/reports/period",
        response_model=PeriodReportResponse,
        responses=_error_responses(401, 422),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                SESSION_BINDING_OPENAPI_PARAMETER,
                _date_parameter("start"),
                _date_parameter("end"),
                _limit_parameter("category_limit", DEFAULT_REPORT_LIMIT),
                _limit_parameter("transaction_limit", DEFAULT_REPORT_LIMIT),
            ],
        },
    )
    async def period_report(request: Request) -> PeriodReportResponse:
        query = strict_query(request, allowed=_PERIOD_QUERY)
        if "start" not in query or "end" not in query:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        start = utc_timestamp(query["start"])
        end = utc_timestamp(query["end"])
        validate_period(start, end)
        category_limit = bounded_limit(
            query.get("category_limit"),
            default=DEFAULT_REPORT_LIMIT,
        )
        transaction_limit = bounded_limit(
            query.get("transaction_limit"),
            default=DEFAULT_REPORT_LIMIT,
        )

        async def execute(credentials: SessionCredentials) -> PeriodReportSnapshot:
            return await service.period_report(
                credentials,
                start,
                end,
                category_limit=category_limit,
                transaction_limit=transaction_limit,
            )

        result = await _safe_session_call(request, execute)
        return period_report_response(result)

    @router.get(
        "/reports/compare",
        response_model=PeriodComparisonResponse,
        responses=_error_responses(401, 422),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                SESSION_BINDING_OPENAPI_PARAMETER,
                _date_parameter("current_start"),
                _date_parameter("current_end"),
                _date_parameter("previous_start"),
                _date_parameter("previous_end"),
            ],
        },
    )
    async def compare_periods(request: Request) -> PeriodComparisonResponse:
        query = strict_query(request, allowed=_COMPARE_QUERY)
        if set(query) != _COMPARE_QUERY:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        current_start = utc_timestamp(query["current_start"])
        current_end = utc_timestamp(query["current_end"])
        previous_start = utc_timestamp(query["previous_start"])
        previous_end = utc_timestamp(query["previous_end"])
        validate_comparison(current_start, current_end, previous_start, previous_end)

        async def execute(credentials: SessionCredentials) -> PeriodComparisonSnapshot:
            return await service.compare_periods(
                credentials,
                current_start,
                current_end,
                previous_start,
                previous_end,
            )

        result = await _safe_session_call(request, execute)
        return comparison_response(result)

    @router.get(
        "/reports/timeseries",
        response_model=TimeSeriesResponse,
        responses=_error_responses(401, 422),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                SESSION_BINDING_OPENAPI_PARAMETER,
                _date_parameter("start"),
                _date_parameter("end"),
                {
                    "in": "query",
                    "name": "grain",
                    "required": True,
                    "schema": {
                        "enum": ["day", "week", "month"],
                        "type": "string",
                    },
                },
            ],
        },
    )
    async def timeseries(request: Request) -> TimeSeriesResponse:
        query = strict_query(request, allowed=_TIMESERIES_QUERY)
        if set(query) != _TIMESERIES_QUERY:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        start = utc_timestamp(query["start"])
        end = utc_timestamp(query["end"])
        validate_period(start, end)
        grain = timeseries_grain(query["grain"])

        async def execute(credentials: SessionCredentials) -> TimeSeriesSnapshot:
            return await service.timeseries(credentials, start, end, grain=grain)

        result = await _safe_session_call(request, execute)
        return timeseries_response(result)

    @router.get(
        "/transactions",
        response_model=TransactionPageResponse,
        responses=_error_responses(401, 422),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                SESSION_BINDING_OPENAPI_PARAMETER,
                _limit_parameter("limit", DEFAULT_TRANSACTION_LIMIT),
                *_transaction_filter_parameters(),
                {
                    "in": "query",
                    "name": "cursor",
                    "required": False,
                    "schema": {
                        "description": (
                            "Signed integrity-protected owner- and filter-bound cursor. It is not "
                            "encrypted; clients must treat it as opaque and must not parse it."
                        ),
                        "maxLength": 76,
                        "minLength": 76,
                        "pattern": r"^[A-Za-z0-9_-]{76}$",
                        "type": "string",
                    },
                },
            ],
        },
    )
    async def transactions(request: Request) -> TransactionPageResponse:
        query = strict_query(request, allowed=_ACTIVE_TRANSACTION_QUERY)
        limit = bounded_limit(query.get("limit"), default=DEFAULT_TRANSACTION_LIMIT)
        cursor = canonical_cursor(query.get("cursor"))
        filters = transaction_list_filters(query)

        async def execute(credentials: SessionCredentials) -> TransactionCursorPage:
            return await service.transactions(
                credentials,
                limit=limit,
                raw_cursor=cursor,
                filters=filters,
            )

        result = await _safe_session_call(request, execute)
        return TransactionPageResponse(
            items=tuple(transaction_response(item) for item in result.items),
            next_cursor=result.next_cursor,
        )

    @router.get(
        "/transactions/trash",
        response_model=TransactionPageResponse,
        responses=_error_responses(401, 422),
        openapi_extra={
            **_SESSION_SECURITY,
            "parameters": [
                SESSION_BINDING_OPENAPI_PARAMETER,
                _limit_parameter("limit", DEFAULT_TRANSACTION_LIMIT),
                {
                    "in": "query",
                    "name": "cursor",
                    "required": False,
                    "schema": {
                        "description": (
                            "Signed integrity-protected owner-bound deleted-transaction cursor. "
                            "It is not encrypted; clients must treat it as opaque."
                        ),
                        "maxLength": 76,
                        "minLength": 76,
                        "pattern": r"^[A-Za-z0-9_-]{76}$",
                        "type": "string",
                    },
                },
            ],
        },
    )
    async def deleted_transactions(request: Request) -> TransactionPageResponse:
        query = strict_query(request, allowed=_DELETED_TRANSACTION_QUERY)
        limit = bounded_limit(query.get("limit"), default=DEFAULT_TRANSACTION_LIMIT)
        cursor = canonical_cursor(query.get("cursor"))

        async def execute(credentials: SessionCredentials) -> TransactionCursorPage:
            return await service.deleted_transactions(
                credentials,
                limit=limit,
                raw_cursor=cursor,
            )

        result = await _safe_session_call(request, execute)
        return TransactionPageResponse(
            items=tuple(transaction_response(item) for item in result.items),
            next_cursor=result.next_cursor,
        )

    @router.get(
        "/transactions/{transaction_id}",
        response_model=TransactionResponse,
        responses=_error_responses(401, 404, 422),
        openapi_extra=_SESSION_SECURITY,
    )
    async def transaction_detail(
        request: Request,
        transaction_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> TransactionResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(transaction_id)

        async def execute(credentials: SessionCredentials) -> TransactionSnapshot:
            return await service.transaction(credentials, parsed_id)

        result = await _safe_session_call(request, execute)
        return transaction_response(result)

    return router
