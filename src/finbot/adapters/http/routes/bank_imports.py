from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from finbot.adapters.http.auth.request import (
    SESSION_BINDING_OPENAPI_PARAMETER,
    session_credentials,
)
from finbot.adapters.http.auth.service import (
    CsrfRejectedError,
    SessionBindingMismatchError,
    SessionCredentials,
    SessionInvalidError,
)
from finbot.adapters.http.bank_imports.cursor import (
    BANK_IMPORT_BATCH_CURSOR_LENGTH,
    BANK_IMPORT_ROW_CURSOR_LENGTH,
    InvalidBankImportCursorError,
)
from finbot.adapters.http.bank_imports.request import bounded_csv_body
from finbot.adapters.http.bank_imports.service import (
    DEFAULT_BANK_IMPORT_LIMIT,
    HttpBankImportBatchPage,
    HttpBankImportRowPage,
    HttpBankImportService,
)
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.finance.request import (
    CANONICAL_UUID_PATTERN,
    canonical_uuid,
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
from finbot.adapters.http.schemas.bank_imports import (
    CANCEL_BANK_IMPORT_ADAPTER,
    LINK_BANK_IMPORT_ROW_ADAPTER,
    VERSIONED_BANK_IMPORT_ROW_ADAPTER,
    BankImportBatchMutationResponse,
    BankImportBatchPageResponse,
    BankImportBatchResponse,
    BankImportDraftMutationResponse,
    BankImportRowMutationResponse,
    BankImportRowPageResponse,
    BankImportRowResponse,
    ReconciliationCandidatesResponse,
    bank_import_batch_mutation_response,
    bank_import_batch_page_response,
    bank_import_batch_response,
    bank_import_draft_mutation_response,
    bank_import_row_mutation_response,
    bank_import_row_page_response,
    bank_import_row_response,
    reconciliation_candidates_response,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.application.bank_imports import (
    BankImportBatchSnapshot,
    BankImportBatchState,
    BankImportRowSnapshot,
    BankImportRowState,
    ReconciliationCandidate,
)

_SESSION_SECURITY: dict[str, Any] = {
    "security": [{"SessionCookie": []}],
    "parameters": [SESSION_BINDING_OPENAPI_PARAMETER],
}
_CANONICAL_DIGEST_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
_LIMIT = re.compile(r"(?:[1-9]|[1-4][0-9]|50)\Z")
_VERSION = re.compile(r"[1-9][0-9]{0,9}\Z")
_BATCH_CURSOR = re.compile(rf"[A-Za-z0-9_-]{{{BANK_IMPORT_BATCH_CURSOR_LENGTH}}}\Z")
_ROW_CURSOR = re.compile(rf"[A-Za-z0-9_-]{{{BANK_IMPORT_ROW_CURSOR_LENGTH}}}\Z")
_LIST_BATCH_QUERY = frozenset({"state", "limit", "cursor"})
_LIST_ROW_QUERY = frozenset({"state", "limit", "cursor"})
_UPLOAD_QUERY = frozenset({"account_id", "account_version", "profile"})
_MUTATION_PARAMETERS: list[dict[str, Any]] = [
    SESSION_BINDING_OPENAPI_PARAMETER,
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
CanonicalUuidPath = Annotated[
    str,
    Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
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


def _upload_openapi() -> dict[str, Any]:
    return {
        **_SESSION_SECURITY,
        "parameters": [
            *_MUTATION_PARAMETERS,
            {
                "in": "query",
                "name": "account_id",
                "required": True,
                "schema": {"format": "uuid", "type": "string"},
            },
            {
                "in": "query",
                "name": "account_version",
                "required": True,
                "schema": {"minimum": 1, "maximum": 2**31 - 1, "type": "integer"},
            },
            {
                "in": "query",
                "name": "profile",
                "required": True,
                "schema": {"const": "canonical_v1", "type": "string"},
            },
        ],
        "requestBody": {
            "required": True,
            "content": {
                "text/csv": {
                    "schema": {
                        "format": "binary",
                        "maxLength": 2 * 1024 * 1024,
                        "type": "string",
                    }
                }
            },
        },
    }


def _limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_BANK_IMPORT_LIMIT
    if _LIMIT.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return int(value)


def _version(value: str | None) -> int:
    if value is None or _VERSION.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    version = int(value)
    if version > 2**31 - 1:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return version


def _batch_state(value: str | None) -> BankImportBatchState | None:
    if value is None:
        return None
    try:
        return BankImportBatchState(value)
    except ValueError as exc:
        raise HttpApiError(
            status_code=422,
            code=HttpErrorCode.VALIDATION_FAILED,
        ) from exc


def _row_state(value: str | None) -> BankImportRowState | None:
    if value is None:
        return None
    try:
        return BankImportRowState(value)
    except ValueError as exc:
        raise HttpApiError(
            status_code=422,
            code=HttpErrorCode.VALIDATION_FAILED,
        ) from exc


def _cursor(value: str | None, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if pattern.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR)
    return value


async def _safe_read[ResultT](
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
    except InvalidBankImportCursorError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR) from exc


async def _safe_admission(operation: Callable[[], Awaitable[UUID]]) -> UUID:
    try:
        return await operation()
    except CsrfRejectedError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED) from exc
    except SessionInvalidError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
        ) from exc
    except SessionBindingMismatchError as exc:
        raise HttpApiError(
            status_code=401,
            code=HttpErrorCode.AUTH_SESSION_INVALID,
        ) from exc


async def _safe_mutation(operation: Callable[[], Awaitable[MutationReceipt]]) -> MutationReceipt:
    try:
        return await operation()
    except CsrfRejectedError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED) from exc
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


def _credentials(request: Request, expected_origin: str) -> MutationCredentials:
    return mutation_credentials(request, expected_origin=expected_origin)


def bank_import_router(service: HttpBankImportService, *, expected_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/bank-imports", tags=["bank-imports"])

    @router.post(
        "/upload",
        status_code=201,
        response_model=BankImportBatchMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_upload_openapi(),
    )
    async def upload_bank_import(request: Request) -> JSONResponse:
        query = strict_query(request, allowed=_UPLOAD_QUERY)
        if query.get("profile") != "canonical_v1":
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        account_id = canonical_uuid(query.get("account_id", ""))
        account_version = _version(query.get("account_version"))
        credentials = _credentials(request, expected_origin)
        owner_id = await _safe_admission(lambda: service.admit_upload(credentials))
        content, encoding = await bounded_csv_body(request)
        prepared = service.prepare_upload(
            owner_id,
            account_id,
            account_version,
            content,
            encoding=encoding,
        )
        del content
        receipt = await _safe_mutation(lambda: service.create(credentials, prepared))
        return JSONResponse(
            status_code=receipt.http_status,
            content=bank_import_batch_mutation_response(receipt).model_dump(mode="json"),
        )

    @router.get(
        "",
        response_model=BankImportBatchPageResponse,
        responses=_error_responses(401, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def list_bank_imports(request: Request) -> BankImportBatchPageResponse:
        query = strict_query(request, allowed=_LIST_BATCH_QUERY)
        state = _batch_state(query.get("state"))

        async def execute(credentials: SessionCredentials) -> HttpBankImportBatchPage:
            return await service.list_batches(
                credentials,
                state=state,
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor"), _BATCH_CURSOR),
            )

        return bank_import_batch_page_response(await _safe_read(request, execute))

    @router.get(
        "/{batch_id}",
        response_model=BankImportBatchResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_bank_import(
        request: Request,
        batch_id: CanonicalUuidPath,
    ) -> BankImportBatchResponse:
        strict_query(request, allowed=frozenset())
        parsed_batch = canonical_uuid(batch_id)

        async def execute(credentials: SessionCredentials) -> BankImportBatchSnapshot:
            return await service.get_batch(credentials, parsed_batch)

        return bank_import_batch_response(await _safe_read(request, execute))

    @router.get(
        "/{batch_id}/rows",
        response_model=BankImportRowPageResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def list_bank_import_rows(
        request: Request,
        batch_id: CanonicalUuidPath,
    ) -> BankImportRowPageResponse:
        query = strict_query(request, allowed=_LIST_ROW_QUERY)
        parsed_batch = canonical_uuid(batch_id)
        state = _row_state(query.get("state"))

        async def execute(credentials: SessionCredentials) -> HttpBankImportRowPage:
            return await service.list_rows(
                credentials,
                parsed_batch,
                state=state,
                limit=_limit(query.get("limit")),
                raw_cursor=_cursor(query.get("cursor"), _ROW_CURSOR),
            )

        return bank_import_row_page_response(await _safe_read(request, execute))

    def parsed_row_ids(request: Request, batch_id: str, row_id: str) -> tuple[UUID, UUID]:
        strict_query(request, allowed=frozenset())
        return canonical_uuid(batch_id), canonical_uuid(row_id)

    row_path = "/{batch_id}/rows/{row_id}"

    @router.get(
        row_path,
        response_model=BankImportRowResponse,
        responses=_error_responses(401, 404, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_bank_import_row(
        request: Request,
        batch_id: CanonicalUuidPath,
        row_id: CanonicalUuidPath,
    ) -> BankImportRowResponse:
        parsed_batch, parsed_row = parsed_row_ids(request, batch_id, row_id)

        async def execute(credentials: SessionCredentials) -> BankImportRowSnapshot:
            return await service.get_row(credentials, parsed_batch, parsed_row)

        return bank_import_row_response(await _safe_read(request, execute))

    @router.get(
        row_path + "/candidates",
        response_model=ReconciliationCandidatesResponse,
        responses=_error_responses(401, 404, 409, 422, 500),
        openapi_extra=_SESSION_SECURITY,
    )
    async def list_reconciliation_candidates(
        request: Request,
        batch_id: CanonicalUuidPath,
        row_id: CanonicalUuidPath,
    ) -> ReconciliationCandidatesResponse:
        parsed_batch, parsed_row = parsed_row_ids(request, batch_id, row_id)

        async def execute(
            credentials: SessionCredentials,
        ) -> tuple[ReconciliationCandidate, ...]:
            return await service.candidates(credentials, parsed_batch, parsed_row)

        return reconciliation_candidates_response(await _safe_read(request, execute))

    async def row_mutation_context(
        request: Request,
        batch_id: str,
        row_id: str,
    ) -> tuple[MutationCredentials, UUID, UUID]:
        parsed_batch, parsed_row = parsed_row_ids(request, batch_id, row_id)
        return _credentials(request, expected_origin), parsed_batch, parsed_row

    @router.post(
        row_path + "/draft",
        status_code=201,
        response_model=BankImportDraftMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(VERSIONED_BANK_IMPORT_ROW_ADAPTER),
    )
    async def stage_bank_import_draft(
        request: Request,
        batch_id: CanonicalUuidPath,
        row_id: CanonicalUuidPath,
    ) -> JSONResponse:
        credentials, parsed_batch, parsed_row = await row_mutation_context(
            request, batch_id, row_id
        )
        body = await bounded_json_body(request, VERSIONED_BANK_IMPORT_ROW_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.stage_draft(
                credentials,
                parsed_batch,
                body.batch_version,
                parsed_row,
                body.row_version,
            )
        )
        return JSONResponse(
            status_code=receipt.http_status,
            content=bank_import_draft_mutation_response(receipt).model_dump(mode="json"),
        )

    @router.post(
        row_path + "/link",
        response_model=BankImportRowMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(LINK_BANK_IMPORT_ROW_ADAPTER),
    )
    async def link_bank_import_row(
        request: Request,
        batch_id: CanonicalUuidPath,
        row_id: CanonicalUuidPath,
    ) -> JSONResponse:
        credentials, parsed_batch, parsed_row = await row_mutation_context(
            request, batch_id, row_id
        )
        body = await bounded_json_body(request, LINK_BANK_IMPORT_ROW_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.link(
                credentials,
                parsed_batch,
                body.batch_version,
                parsed_row,
                body.row_version,
                body.transaction_id,
                body.transaction_version,
            )
        )
        return JSONResponse(
            status_code=receipt.http_status,
            content=bank_import_row_mutation_response(receipt).model_dump(mode="json"),
        )

    @router.post(
        row_path + "/skip",
        response_model=BankImportRowMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(VERSIONED_BANK_IMPORT_ROW_ADAPTER),
    )
    async def skip_bank_import_row(
        request: Request,
        batch_id: CanonicalUuidPath,
        row_id: CanonicalUuidPath,
    ) -> JSONResponse:
        credentials, parsed_batch, parsed_row = await row_mutation_context(
            request, batch_id, row_id
        )
        body = await bounded_json_body(request, VERSIONED_BANK_IMPORT_ROW_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.skip(
                credentials,
                parsed_batch,
                body.batch_version,
                parsed_row,
                body.row_version,
            )
        )
        return JSONResponse(
            status_code=receipt.http_status,
            content=bank_import_row_mutation_response(receipt).model_dump(mode="json"),
        )

    @router.post(
        "/{batch_id}/cancel",
        response_model=BankImportBatchMutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422, 500),
        openapi_extra=_mutation_openapi(CANCEL_BANK_IMPORT_ADAPTER),
    )
    async def cancel_bank_import(
        request: Request,
        batch_id: CanonicalUuidPath,
    ) -> JSONResponse:
        strict_query(request, allowed=frozenset())
        credentials = _credentials(request, expected_origin)
        parsed_batch = canonical_uuid(batch_id)
        body = await bounded_json_body(request, CANCEL_BANK_IMPORT_ADAPTER)
        receipt = await _safe_mutation(
            lambda: service.cancel(credentials, parsed_batch, body.version)
        )
        return JSONResponse(
            status_code=receipt.http_status,
            content=bank_import_batch_mutation_response(receipt).model_dump(mode="json"),
        )

    return router


__all__ = ["bank_import_router"]
