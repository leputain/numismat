from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, Response
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
    HttpRevisionMutationService,
    IdempotencyInProgressError,
    IdempotencyKeyReuseError,
    InvalidStoredMutationResultError,
    MutationCredentials,
    MutationReceipt,
)
from finbot.adapters.http.schemas.common import ApiErrorResponse
from finbot.adapters.http.schemas.mutations import (
    DRAFT_PATCH_ADAPTER,
    EMPTY_MUTATION_ADAPTER,
    REVISION_MUTATION_ADAPTER,
    VERSION_MUTATION_ADAPTER,
    ActiveDraftResponse,
    DraftPatchRequest,
    DraftResponse,
    ExistingSelectionRequest,
    InputTextPatchRequest,
    MutationResponse,
    NavigationPatchRequest,
    SelectCatalogPatchRequest,
    SelectDatePatchRequest,
    SelectEditTypePatchRequest,
    SelectTypePatchRequest,
    SetRulePatchRequest,
    draft_response,
    mutation_response,
)
from finbot.application.draft_navigation import DraftCatalogChoice, DraftDateChoice
from finbot.application.draft_rules import DraftRuleAction
from finbot.application.draft_views import project_public_draft
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.revision_mutations import (
    DraftExistingSelection,
    DraftPatchAction,
    DraftPatchSpec,
)
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


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ApiErrorResponse} for status in statuses}


def _body_schema[ModelT](adapter: TypeAdapter[ModelT]) -> dict[str, Any]:
    schema = _inline_local_definitions(adapter.json_schema())
    return {
        "required": True,
        "content": {"application/json": {"schema": schema}},
    }


def _inline_local_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise TypeError("request schema definitions are invalid")

    def resolve(value: object, trail: frozenset[str] = frozenset()) -> object:
        if isinstance(value, list):
            return [resolve(item, trail) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict) or name in trail:
                raise TypeError("request schema reference is invalid")
            merged = {
                **target,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
            return resolve(merged, trail | {name})
        result: dict[str, object] = {}
        for key, item in value.items():
            if key == "$defs":
                continue
            if key == "discriminator" and isinstance(item, dict):
                property_name = item.get("propertyName")
                if isinstance(property_name, str):
                    result[key] = {"propertyName": property_name}
                continue
            result[key] = resolve(item, trail)
        return result

    resolved = resolve(schema)
    if not isinstance(resolved, dict):  # pragma: no cover - root invariant
        raise TypeError("request schema root is invalid")
    return resolved


def _mutation_openapi[ModelT](adapter: TypeAdapter[ModelT]) -> dict[str, Any]:
    return {
        **_SESSION_SECURITY,
        "parameters": _MUTATION_PARAMETERS,
        "requestBody": _body_schema(adapter),
    }


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


async def _safe_mutation(
    operation: Callable[[], Awaitable[MutationReceipt]],
) -> MutationReceipt:
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


def _response(receipt: MutationReceipt) -> Response:
    if receipt.http_status == 204:
        return Response(status_code=204)
    body = mutation_response(receipt).model_dump(mode="json")
    return JSONResponse(status_code=receipt.http_status, content=body)


def _expected(draft_id: UUID, revision: int) -> DraftRef:
    return DraftRef(draft_id, revision)


def _patch_spec(draft_id: UUID, body: DraftPatchRequest) -> DraftPatchSpec:
    expected = _expected(draft_id, body.revision)
    action = DraftPatchAction(body.action)
    if isinstance(body, InputTextPatchRequest):
        return DraftPatchSpec(expected, action, text=body.text)
    if isinstance(body, NavigationPatchRequest):
        return DraftPatchSpec(expected, action)
    if isinstance(body, SelectTypePatchRequest):
        return DraftPatchSpec(expected, action, value=TransactionType(body.value))
    if isinstance(body, SelectEditTypePatchRequest):
        return DraftPatchSpec(
            expected,
            action,
            value=TransactionType(body.value),
            selection=DraftExistingSelection(
                canonical_uuid(body.category.id),
                body.category.version,
            ),
        )
    if isinstance(body, SelectDatePatchRequest):
        return DraftPatchSpec(expected, action, value=DraftDateChoice(body.value))
    if isinstance(body, SetRulePatchRequest):
        return DraftPatchSpec(expected, action, value=DraftRuleAction(body.value))
    if isinstance(body, SelectCatalogPatchRequest):
        selection = (
            DraftExistingSelection(
                canonical_uuid(body.selection.id),
                body.selection.version,
            )
            if isinstance(body.selection, ExistingSelectionRequest)
            else DraftCatalogChoice.CUSTOM
        )
        return DraftPatchSpec(expected, action, selection=selection)
    raise TypeError("unsupported draft patch request")


def mutation_router(
    service: HttpRevisionMutationService,
    *,
    expected_origin: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["mutations"])

    @router.get(
        "/drafts/active",
        response_model=ActiveDraftResponse,
        responses=_error_responses(401, 422),
        openapi_extra=_SESSION_SECURITY,
    )
    async def active_draft(request: Request) -> ActiveDraftResponse:
        strict_query(request, allowed=frozenset())
        draft = await _safe_read(request, service.active_draft)
        return ActiveDraftResponse(
            draft=draft_response(project_public_draft(draft)) if draft is not None else None
        )

    @router.get(
        "/drafts/{draft_id}",
        response_model=DraftResponse,
        responses=_error_responses(401, 404, 422),
        openapi_extra=_SESSION_SECURITY,
    )
    async def get_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> DraftResponse:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(draft_id)

        async def execute(raw_session: str) -> DraftSnapshot:
            return await service.draft(raw_session, parsed_id)

        draft = await _safe_read(request, execute)
        return draft_response(project_public_draft(draft))

    @router.post(
        "/drafts",
        response_model=MutationResponse,
        responses={
            201: {"model": MutationResponse},
            **_error_responses(401, 403, 409, 413, 415, 422),
        },
        openapi_extra=_mutation_openapi(EMPTY_MUTATION_ADAPTER),
    )
    async def create_draft(request: Request) -> Response:
        strict_query(request, allowed=frozenset())
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        await bounded_json_body(request, EMPTY_MUTATION_ADAPTER)
        return _response(await _safe_mutation(lambda: service.create_draft(credentials)))

    @router.patch(
        "/drafts/{draft_id}",
        response_model=MutationResponse,
        responses={
            204: {"description": "The back action closed the draft."},
            **_error_responses(401, 403, 404, 409, 413, 415, 422),
        },
        openapi_extra=_mutation_openapi(DRAFT_PATCH_ADAPTER),
    )
    async def patch_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(draft_id)
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, DRAFT_PATCH_ADAPTER)
        spec = _patch_spec(parsed_id, body)
        return _response(await _safe_mutation(lambda: service.patch_draft(credentials, spec)))

    async def draft_revision_mutation(
        request: Request,
        draft_id: str,
        operation: Callable[[MutationCredentials, DraftRef], Awaitable[MutationReceipt]],
    ) -> Response:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(draft_id)
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, REVISION_MUTATION_ADAPTER)
        expected = _expected(parsed_id, body.revision)
        return _response(await _safe_mutation(lambda: operation(credentials, expected)))

    revision_openapi = _mutation_openapi(REVISION_MUTATION_ADAPTER)

    @router.post(
        "/drafts/{draft_id}/confirm",
        status_code=201,
        response_model=MutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=revision_openapi,
    )
    async def confirm_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await draft_revision_mutation(request, draft_id, service.confirm_draft)

    @router.post(
        "/drafts/{draft_id}/cancel",
        status_code=204,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=revision_openapi,
    )
    async def cancel_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await draft_revision_mutation(request, draft_id, service.cancel_draft)

    @router.post(
        "/drafts/{draft_id}/resume",
        response_model=MutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=revision_openapi,
    )
    async def resume_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await draft_revision_mutation(request, draft_id, service.resume_draft)

    @router.post(
        "/drafts/{draft_id}/replace",
        response_model=MutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=revision_openapi,
    )
    async def replace_draft(
        request: Request,
        draft_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await draft_revision_mutation(request, draft_id, service.replace_draft)

    async def transaction_mutation(
        request: Request,
        transaction_id: str,
        operation: Callable[[MutationCredentials, UUID, int], Awaitable[MutationReceipt]],
    ) -> Response:
        strict_query(request, allowed=frozenset())
        parsed_id = canonical_uuid(transaction_id)
        credentials = mutation_credentials(request, expected_origin=expected_origin)
        body = await bounded_json_body(request, VERSION_MUTATION_ADAPTER)
        return _response(
            await _safe_mutation(lambda: operation(credentials, parsed_id, body.version))
        )

    version_openapi = _mutation_openapi(VERSION_MUTATION_ADAPTER)

    @router.post(
        "/transactions/{transaction_id}/repeat",
        response_model=MutationResponse,
        responses={
            201: {"model": MutationResponse},
            **_error_responses(401, 403, 404, 409, 413, 415, 422),
        },
        openapi_extra=version_openapi,
    )
    async def repeat_transaction(
        request: Request,
        transaction_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await transaction_mutation(
            request,
            transaction_id,
            service.repeat_transaction,
        )

    @router.post(
        "/transactions/{transaction_id}/edit-draft",
        response_model=MutationResponse,
        responses={
            201: {"model": MutationResponse},
            **_error_responses(401, 403, 404, 409, 413, 415, 422),
        },
        openapi_extra=version_openapi,
    )
    async def edit_transaction_draft(
        request: Request,
        transaction_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await transaction_mutation(
            request,
            transaction_id,
            service.begin_transaction_edit,
        )

    @router.post(
        "/transactions/{transaction_id}/delete",
        response_model=MutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=version_openapi,
    )
    async def delete_transaction(
        request: Request,
        transaction_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await transaction_mutation(
            request,
            transaction_id,
            service.delete_transaction,
        )

    @router.post(
        "/transactions/{transaction_id}/restore",
        response_model=MutationResponse,
        responses=_error_responses(401, 403, 404, 409, 413, 415, 422),
        openapi_extra=version_openapi,
    )
    async def restore_transaction(
        request: Request,
        transaction_id: Annotated[
            str,
            Path(min_length=36, max_length=36, pattern=CANONICAL_UUID_PATTERN),
        ],
    ) -> Response:
        return await transaction_mutation(
            request,
            transaction_id,
            service.restore_transaction,
        )

    return router
