from __future__ import annotations

import re

from starlette.responses import Response

SESSION_COOKIE = "__Host-numismat_session"
CSRF_COOKIE = "__Host-numismat_csrf"
COOKIE_MAX_AGE_SECONDS = 3600
MAX_COOKIE_HEADER_BYTES = 4096
MAX_COOKIE_PAIRS = 32
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}\Z")


class InvalidCookieHeaderError(ValueError):
    pass


def parse_cookie_headers(raw_values: list[bytes]) -> dict[str, str]:
    if not raw_values:
        return {}
    if (
        any(not value for value in raw_values)
        or sum(len(value) for value in raw_values) > MAX_COOKIE_HEADER_BYTES
    ):
        raise InvalidCookieHeaderError
    try:
        values = [raw_value.decode("ascii") for raw_value in raw_values]
    except UnicodeDecodeError as exc:
        raise InvalidCookieHeaderError from exc
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F for value in values for character in value
    ):
        raise InvalidCookieHeaderError
    parts = [part for value in values for part in value.split(";")]
    if len(parts) > MAX_COOKIE_PAIRS:
        raise InvalidCookieHeaderError
    cookies: dict[str, str] = {}
    for part in parts:
        stripped = part.strip(" ")
        if not stripped or "=" not in stripped:
            raise InvalidCookieHeaderError
        name, cookie_value = stripped.split("=", 1)
        if _COOKIE_NAME.fullmatch(name) is None or name in cookies:
            raise InvalidCookieHeaderError
        if any(character in cookie_value for character in '"\\,;'):
            raise InvalidCookieHeaderError
        cookies[name] = cookie_value
    return cookies


def parse_cookie_header(raw_value: bytes | None) -> dict[str, str]:
    """Compatibility wrapper for a single HTTP/1 Cookie field."""

    return parse_cookie_headers([] if raw_value is None else [raw_value])


def set_auth_cookies(response: Response, *, session_token: str, csrf_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def clear_auth_cookies(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        expires=0,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        "",
        max_age=0,
        expires=0,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
