from __future__ import annotations

import json
import re
from typing import Any

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from finbot.adapters.http.auth.cookies import (
    CSRF_COOKIE,
    InvalidCookieHeaderError,
    parse_cookie_headers,
)
from finbot.adapters.http.auth.crypto import is_canonical_opaque_token
from finbot.adapters.http.auth.request import session_credentials
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.mutations.service import MutationCredentials

MAX_MUTATION_BODY_BYTES = 12 * 1024
MAX_HEADER_VALUE_BYTES = 4096
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 256
_CONTENT_TYPE = re.compile(
    rb"application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|\"utf-8\"))?",
    re.IGNORECASE,
)
_CONTENT_LENGTH = re.compile(rb"(?:0|[1-9][0-9]{0,5})\Z")


def _raw_header(
    request: Request,
    name: bytes,
    *,
    required: bool = False,
    status_code: int = 422,
    error_code: HttpErrorCode = HttpErrorCode.VALIDATION_FAILED,
) -> bytes | None:
    values = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == name
    ]
    if len(values) != 1:
        if required or values:
            raise HttpApiError(status_code=status_code, code=error_code)
        return None
    value = values[0]
    if not value or len(value) > MAX_HEADER_VALUE_BYTES:
        raise HttpApiError(status_code=status_code, code=error_code)
    return value


def _ascii_header(
    request: Request,
    name: bytes,
    *,
    status_code: int,
    error_code: HttpErrorCode,
) -> str:
    raw = _raw_header(
        request,
        name,
        required=True,
        status_code=status_code,
        error_code=error_code,
    )
    try:
        return raw.decode("ascii") if raw is not None else ""
    except UnicodeDecodeError as exc:
        raise HttpApiError(status_code=status_code, code=error_code) from exc


def _cookies(request: Request) -> dict[str, str]:
    raw_values = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == b"cookie"
    ]
    try:
        return parse_cookie_headers(raw_values)
    except InvalidCookieHeaderError as exc:
        raise HttpApiError(
            status_code=422,
            code=HttpErrorCode.VALIDATION_FAILED,
        ) from exc


def _validate_webview_origin_metadata(request: Request, *, expected_origin: str) -> None:
    raw = _raw_header(
        request,
        b"origin",
        required=False,
        status_code=403,
        error_code=HttpErrorCode.ORIGIN_FORBIDDEN,
    )
    if raw is None:
        return
    try:
        origin = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HttpApiError(status_code=403, code=HttpErrorCode.ORIGIN_FORBIDDEN) from exc
    if origin == expected_origin:
        return
    # Native Telegram WebViews do not expose one portable Origin contract.
    # The session-bound double-submit CSRF proof remains the mutation authority.
    return


def mutation_credentials(request: Request, *, expected_origin: str) -> MutationCredentials:
    _validate_webview_origin_metadata(request, expected_origin=expected_origin)

    session = session_credentials(request)
    cookies = _cookies(request)
    csrf_header = _ascii_header(
        request,
        b"x-csrf-token",
        status_code=403,
        error_code=HttpErrorCode.CSRF_FAILED,
    )
    if not is_canonical_opaque_token(csrf_header):
        raise HttpApiError(status_code=403, code=HttpErrorCode.CSRF_FAILED)
    idempotency_key = _ascii_header(
        request,
        b"idempotency-key",
        status_code=422,
        error_code=HttpErrorCode.VALIDATION_FAILED,
    )
    if not is_canonical_opaque_token(idempotency_key):
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return MutationCredentials(
        session_token=session.session_token,
        session_binding=session.session_binding,
        csrf_cookie=cookies.get(CSRF_COOKIE, ""),
        csrf_header=csrf_header,
        idempotency_key=idempotency_key,
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _validate_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON shape is too complex")
        if isinstance(current, dict):
            if any(type(key) is not str or len(key) > 64 for key in current):
                raise ValueError("JSON object key is invalid")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


async def bounded_json_body[ModelT](
    request: Request,
    adapter: TypeAdapter[ModelT],
) -> ModelT:
    if _raw_header(request, b"content-encoding") is not None:
        raise HttpApiError(
            status_code=415,
            code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE,
        )
    content_type = _raw_header(request, b"content-type", required=True)
    if content_type is None or _CONTENT_TYPE.fullmatch(content_type) is None:
        raise HttpApiError(
            status_code=415,
            code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = _raw_header(request, b"content-length")
    if content_length is not None:
        if _CONTENT_LENGTH.fullmatch(content_length) is None:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        if int(content_length) > MAX_MUTATION_BODY_BYTES:
            raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)

    payload = bytearray()
    async for chunk in request.stream():
        if chunk:
            if len(payload) + len(chunk) > MAX_MUTATION_BODY_BYTES:
                raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)
            payload.extend(chunk)
    try:
        parsed = json.loads(
            bytes(payload).decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
        _validate_shape(parsed)
        return adapter.validate_python(parsed)
    except (
        RecursionError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED) from exc
