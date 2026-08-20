from __future__ import annotations

from fastapi import Request

from finbot.adapters.http.auth.cookies import (
    SESSION_COOKIE,
    InvalidCookieHeaderError,
    parse_cookie_headers,
)
from finbot.adapters.http.auth.crypto import is_canonical_opaque_token
from finbot.adapters.http.auth.service import SessionCredentials
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode

CANONICAL_OPAQUE_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
SESSION_BINDING_HEADER = "X-Session-Binding"
SESSION_BINDING_OPENAPI_PARAMETER = {
    "description": "Opaque binding to the exact host-only session cookie for this page context.",
    "in": "header",
    "name": SESSION_BINDING_HEADER,
    "required": True,
    "schema": {
        "maxLength": 43,
        "minLength": 43,
        "pattern": CANONICAL_OPAQUE_TOKEN_PATTERN,
        "type": "string",
    },
}
SESSION_BINDING_OPENAPI_RESPONSE_HEADER = {
    "description": "Opaque binding for the exact session cookie represented by this response.",
    "schema": SESSION_BINDING_OPENAPI_PARAMETER["schema"],
}


def _invalid_session() -> HttpApiError:
    return HttpApiError(
        status_code=401,
        code=HttpErrorCode.AUTH_SESSION_INVALID,
    )


def session_credentials(request: Request) -> SessionCredentials:
    raw_cookies = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == b"cookie"
    ]
    try:
        session_token = parse_cookie_headers(raw_cookies).get(SESSION_COOKIE, "")
    except InvalidCookieHeaderError as exc:
        raise _invalid_session() from exc

    bindings = [
        bytes(value)
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"x-session-binding"
    ]
    if len(bindings) != 1:
        raise _invalid_session()
    try:
        session_binding = bindings[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise _invalid_session() from exc
    if not is_canonical_opaque_token(session_binding):
        raise _invalid_session()
    return SessionCredentials(
        session_token=session_token,
        session_binding=session_binding,
    )
