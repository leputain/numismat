from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from finbot.adapters.http.auth.service import CsrfRejectedError, SessionInvalidError
from finbot.adapters.http.catalogs.service import AccountCatalog, HttpCatalogService
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
from finbot.adapters.http.schemas.catalogs import (
    CREATE_ACCOUNT_ADAPTER,
    CREATE_CATEGORY_ADAPTER,
    RENAME_CATALOG_ADAPTER,
    VERSIONED_CATALOG_ADAPTER,
    AccountMutationResponse,
    AccountsResponse,
    CategoriesResponse,
    CategoryMutationResponse,
    accounts_response,
    catalog_mutation_response,
    categories_response,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.application.dto import CategorySnapshot
from finbot.domain.transactions import TransactionType

_SESSION_SECURITY: dict[str, Any] = {"security": [{"SessionCookie": []}]}
_CANONICAL_DIGEST_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
_MUTATION_PARAMETERS: list[dict[str, Any]] = [
    {
        "description": (
            "Optional bounded transport metadata supplied by the user agent; "
            "the session cookie and double-submit CSRF proof are authoritative."
        ),
        "in": "header",
        "name": "Origin",
        "required": False,
        "schema": {"maxLength": 4096, "minLength": 1, "type": "string"},
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
_ACCOUNT_QUERY = frozenset({"archived"})
_CATEGORY_QUERY = frozenset({"archived", "kind"})


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


def _read_openapi(*parameters: dict[str, Any]) -> dict[str, Any]:
    return {**_SESSION_SECURITY, "parameters": list(parameters)}


def _archived(value: str | None) -> bool:
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)


def _kind(value: str | None) -> TransactionType | None:
    if value is None:
        return None
    try:
        return TransactionType(value)
    except ValueError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED) from exc


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


def _response(receipt: MutationReceipt) -> JSONResponse:
    body = catalog_mutation_response(receipt).model_dump(mode="json")
    return JSONResponse(status_code=receipt.http_status, content=body)


def catalog_router(service: HttpCatalogService, *, expected_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["catalogs"])

    @router.get(
        "/accounts",
        response_model=AccountsResponse,
        responses=_error_responses(401, 409, 422, 500),
        openapi_extra=_read_openapi(
            {
                "in": "query",
                "name": "archived",
                "required": False,
                "schema": {"default": False, "type": "boolean"},
            }
        ),
    )
    async def accounts(request: Request) -> AccountsResponse:
        query = strict_query(request, allowed=_ACCOUNT_QUERY)

        async def execute(raw_session: str) -> AccountCatalog:
            return await service.accounts(
                raw_session,
                archived=_archived(query.get("archived")),
            )

        result = await _safe_read(request, execute)
        return accounts_response(result)

    @router.get(
        "/categories",
        response_model=CategoriesResponse,
        responses=_error_responses(401, 409, 422, 500),
        openapi_extra=_read_openapi(
            {
                "in": "query",
                "name": "kind",
                "required": False,
                "schema": {"enum": ["expense", "income"], "type": "string"},
            },
            {
                "in": "query",
                "name": "archived",
                "required": False,
                "schema": {"default": False, "type": "boolean"},
            },
        ),
    )
    async def categories(request: Request) -> CategoriesResponse:
        query = strict_query(request, allowed=_CATEGORY_QUERY)

        async def execute(raw_session: str) -> tuple[CategorySnapshot, ...]:
            return await service.categories(
                raw_session,
                kind=_kind(query.get("kind")),
                archived=_archived(query.get("archived")),
            )

        result = await _safe_read(request, execute)
        return categories_response(result)

    async def credentials_and_id(request: Request, raw_id: str) -> tuple[MutationCredentials, UUID]:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(raw_id)
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        return credentials, parsed_id

    @router.post(
        "/accounts",
        status_code=201,
        response_model=AccountMutationResponse,
        responses=_error_responses(401, 403, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(CREATE_ACCOUNT_ADAPTER),
    )
    async def create_account(request: Request) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, CREATE_ACCOUNT_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.create_account(
                credentials,
                name=body.name,
                currency=body.currency,
            )
        )
        return _response(receipt)

    @router.patch(
        "/accounts/{account_id}",
        response_model=AccountMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(RENAME_CATALOG_ADAPTER),
    )
    async def update_account(
        request: Request,
        account_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, account_id)
        body = await bounded_json_body(request, RENAME_CATALOG_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.update_account(
                credentials,
                parsed_id,
                name=body.name,
                version=body.version,
            )
        )
        return _response(receipt)

    async def account_action(
        request: Request,
        account_id: str,
        operation: Callable[[MutationCredentials, UUID, int], Awaitable[MutationReceipt]],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, account_id)
        body = await bounded_json_body(request, VERSIONED_CATALOG_ADAPTER)
        return _response(
            await _safe_mutation(lambda: operation(credentials, parsed_id, body.version))
        )

    version_openapi = _mutation_openapi(VERSIONED_CATALOG_ADAPTER)

    @router.post(
        "/accounts/{account_id}/archive",
        response_model=AccountMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def archive_account(
        request: Request,
        account_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await account_action(request, account_id, service.archive_account)

    @router.post(
        "/accounts/{account_id}/restore",
        response_model=AccountMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def restore_account(
        request: Request,
        account_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await account_action(request, account_id, service.restore_account)

    @router.post(
        "/accounts/{account_id}/default",
        response_model=AccountMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def set_default_account(
        request: Request,
        account_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await account_action(request, account_id, service.set_default_account)

    @router.post(
        "/categories",
        status_code=201,
        response_model=CategoryMutationResponse,
        responses=_error_responses(401, 403, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(CREATE_CATEGORY_ADAPTER),
    )
    async def create_category(request: Request) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, CREATE_CATEGORY_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.create_category(
                credentials,
                name=body.name,
                kind=TransactionType(body.kind),
            )
        )
        return _response(receipt)

    @router.patch(
        "/categories/{category_id}",
        response_model=CategoryMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(RENAME_CATALOG_ADAPTER),
    )
    async def update_category(
        request: Request,
        category_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, category_id)
        body = await bounded_json_body(request, RENAME_CATALOG_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.update_category(
                credentials,
                parsed_id,
                name=body.name,
                version=body.version,
            )
        )
        return _response(receipt)

    async def category_action(
        request: Request,
        category_id: str,
        operation: Callable[[MutationCredentials, UUID, int], Awaitable[MutationReceipt]],
    ) -> JSONResponse:
        credentials, parsed_id = await credentials_and_id(request, category_id)
        body = await bounded_json_body(request, VERSIONED_CATALOG_ADAPTER)
        return _response(
            await _safe_mutation(lambda: operation(credentials, parsed_id, body.version))
        )

    @router.post(
        "/categories/{category_id}/archive",
        response_model=CategoryMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def archive_category(
        request: Request,
        category_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await category_action(request, category_id, service.archive_category)

    @router.post(
        "/categories/{category_id}/restore",
        response_model=CategoryMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=version_openapi,
    )
    async def restore_category(
        request: Request,
        category_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> JSONResponse:
        return await category_action(request, category_id, service.restore_category)

    return router
