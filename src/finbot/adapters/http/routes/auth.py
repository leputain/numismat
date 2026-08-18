from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from finbot.adapters.http.auth.cookies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    InvalidCookieHeaderError,
    clear_auth_cookies,
    parse_cookie_headers,
    set_auth_cookies,
)
from finbot.adapters.http.auth.service import (
    AuthOwnerUnavailableError,
    CsrfRejectedError,
    SessionInvalidError,
    TelegramAuthReplayError,
    TelegramAuthService,
)
from finbot.adapters.http.auth.telegram import (
    TelegramAuthReason,
    TelegramAuthVerificationError,
)
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.schemas.auth import AuthSessionResponse, TelegramAuthRequest
from finbot.adapters.http.schemas.common import ApiErrorResponse

MAX_AUTH_BODY_BYTES = 12 * 1024
MAX_HEADER_VALUE_BYTES = 4096
_CONTENT_TYPE = re.compile(
    rb"application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|\"utf-8\"))?",
    re.IGNORECASE,
)
_CONTENT_LENGTH = re.compile(rb"(?:0|[1-9][0-9]{0,5})\Z")
_logger = logging.getLogger("finbot.http.auth")


def _error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ApiErrorResponse} for status in statuses}


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _raw_header(
    request: Request,
    name: bytes,
    *,
    required: bool = False,
    error_code: HttpErrorCode = HttpErrorCode.VALIDATION_FAILED,
    status_code: int = 422,
) -> bytes | None:
    values = [value for key, value in request.scope.get("headers", []) if key.lower() == name]
    if len(values) != 1:
        if required or values:
            raise HttpApiError(status_code=status_code, code=error_code)
        return None
    value = bytes(values[0])
    if not value or len(value) > MAX_HEADER_VALUE_BYTES:
        raise HttpApiError(status_code=status_code, code=error_code)
    return value


def _require_exact_origin(request: Request, expected_origin: str) -> None:
    raw = _raw_header(
        request,
        b"origin",
        required=True,
        error_code=HttpErrorCode.ORIGIN_FORBIDDEN,
        status_code=403,
    )
    try:
        value = raw.decode("ascii") if raw is not None else ""
    except UnicodeDecodeError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.ORIGIN_FORBIDDEN) from exc
    if value != expected_origin:
        raise HttpApiError(status_code=403, code=HttpErrorCode.ORIGIN_FORBIDDEN)


def _cookies(request: Request) -> dict[str, str]:
    raw_values = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == b"cookie"
    ]
    try:
        return parse_cookie_headers(raw_values)
    except InvalidCookieHeaderError as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED) from exc


def _csrf_header(request: Request) -> str:
    raw = _raw_header(
        request,
        b"x-csrf-token",
        required=True,
        error_code=HttpErrorCode.CSRF_FAILED,
        status_code=403,
    )
    try:
        return raw.decode("ascii") if raw is not None else ""
    except UnicodeDecodeError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED) from exc


async def _bounded_auth_body(request: Request) -> TelegramAuthRequest:
    if _raw_header(request, b"content-encoding") is not None:
        raise HttpApiError(status_code=415, code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE)
    content_type = _raw_header(request, b"content-type", required=True)
    if content_type is None or _CONTENT_TYPE.fullmatch(content_type) is None:
        raise HttpApiError(status_code=415, code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE)
    content_length = _raw_header(request, b"content-length")
    if content_length is not None:
        if _CONTENT_LENGTH.fullmatch(content_length) is None:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        declared = int(content_length)
        if declared < 0 or declared > MAX_AUTH_BODY_BYTES:
            raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)

    payload = bytearray()
    async for chunk in request.stream():
        if chunk:
            if len(payload) + len(chunk) > MAX_AUTH_BODY_BYTES:
                raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)
            payload.extend(chunk)
    try:
        parsed = json.loads(
            bytes(payload).decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
        return TelegramAuthRequest.model_validate(parsed)
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED) from exc


def _session_response(authenticated: Any) -> AuthSessionResponse:
    return AuthSessionResponse(
        locale=authenticated.owner.locale,
        timezone=authenticated.owner.timezone,
        base_currency=authenticated.owner.base_currency,
        expires_at=authenticated.expires_at,
    )


def _safe_log(event: str, *, result: str, reason: str) -> None:
    _logger.info(event, extra={"result": result, "auth_reason": reason})


def _translate_verification(error: TelegramAuthVerificationError) -> HttpApiError:
    if error.reason is TelegramAuthReason.EXPIRED:
        code = HttpErrorCode.TELEGRAM_AUTH_EXPIRED
        status = 401
    elif error.reason is TelegramAuthReason.FOREIGN_OWNER:
        code = HttpErrorCode.TELEGRAM_OWNER_FORBIDDEN
        status = 403
    else:
        code = HttpErrorCode.TELEGRAM_AUTH_INVALID
        status = 401
    return HttpApiError(status_code=status, code=code)


def auth_router(service: TelegramAuthService, *, expected_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.post(
        "/telegram",
        response_model=AuthSessionResponse,
        responses=_error_responses(401, 403, 409, 413, 415, 422),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "additionalProperties": False,
                            "properties": {
                                "initData": {
                                    "maxLength": 8192,
                                    "minLength": 1,
                                    "title": "Initdata",
                                    "type": "string",
                                }
                            },
                            "required": ["initData"],
                            "title": "TelegramAuthRequest",
                            "type": "object",
                        }
                    }
                },
            }
        },
    )
    async def telegram_auth(request: Request) -> Response:
        try:
            _require_exact_origin(request, expected_origin)
            body = await _bounded_auth_body(request)
            result = await service.login(body.initData)
        except TelegramAuthVerificationError as exc:
            _safe_log("http_auth_login_completed", result="rejected", reason=exc.reason.value)
            raise _translate_verification(exc) from exc
        except TelegramAuthReplayError as exc:
            _safe_log("http_auth_login_completed", result="rejected", reason="replayed")
            raise HttpApiError(
                status_code=409,
                code=HttpErrorCode.TELEGRAM_AUTH_REPLAYED,
            ) from exc
        except AuthOwnerUnavailableError as exc:
            _safe_log("http_auth_login_completed", result="rejected", reason="owner_unavailable")
            raise HttpApiError(status_code=401, code=HttpErrorCode.TELEGRAM_AUTH_INVALID) from exc

        response = JSONResponse(_session_response(result.authenticated).model_dump(mode="json"))
        set_auth_cookies(
            response,
            session_token=result.tokens.session_token,
            csrf_token=result.tokens.csrf_token,
        )
        _safe_log("http_auth_login_completed", result="success", reason="success")
        return response

    @router.get(
        "/me",
        response_model=AuthSessionResponse,
        responses=_error_responses(401, 422),
        openapi_extra={"security": [{"SessionCookie": []}]},
    )
    async def auth_me(request: Request) -> Response:
        try:
            session_token = _cookies(request).get(SESSION_COOKIE, "")
            authenticated = await service.read(session_token)
        except SessionInvalidError as exc:
            _safe_log("http_auth_session_checked", result="rejected", reason="invalid_session")
            raise HttpApiError(
                status_code=401,
                code=HttpErrorCode.AUTH_SESSION_INVALID,
                clear_auth_cookies=True,
            ) from exc
        _safe_log("http_auth_session_checked", result="success", reason="success")
        return JSONResponse(_session_response(authenticated).model_dump(mode="json"))

    @router.post(
        "/logout",
        status_code=204,
        responses=_error_responses(401, 403, 422),
        openapi_extra={
            "security": [{"SessionCookie": []}],
            "parameters": [
                {
                    "in": "header",
                    "name": "X-CSRF-Token",
                    "required": True,
                    "schema": {"maxLength": 43, "minLength": 43, "type": "string"},
                }
            ],
        },
    )
    async def auth_logout(request: Request) -> Response:
        _require_exact_origin(request, expected_origin)
        cookies = _cookies(request)
        try:
            await service.logout(
                cookies.get(SESSION_COOKIE, ""),
                cookies.get(CSRF_COOKIE, ""),
                _csrf_header(request),
            )
        except CsrfRejectedError as exc:
            _safe_log("http_auth_logout_completed", result="rejected", reason="csrf_failed")
            raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED) from exc
        except SessionInvalidError as exc:
            _safe_log("http_auth_logout_completed", result="rejected", reason="invalid_session")
            raise HttpApiError(
                status_code=401,
                code=HttpErrorCode.AUTH_SESSION_INVALID,
                clear_auth_cookies=True,
            ) from exc
        response = Response(status_code=204)
        clear_auth_cookies(response)
        _safe_log("http_auth_logout_completed", result="success", reason="success")
        return response

    return router
