from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import Request

from finbot.adapters.http.auth.cookies import (
    SESSION_COOKIE,
    InvalidCookieHeaderError,
    parse_cookie_headers,
)
from finbot.adapters.http.errors import HttpApiError, HttpErrorCode

MAX_QUERY_BYTES = 2048
MAX_QUERY_FIELDS = 8
MAX_REPORT_SPAN = timedelta(days=366)
DEFAULT_TRANSACTION_LIMIT = 30
DEFAULT_REPORT_LIMIT = 20
_KEY = re.compile(r"[a-z][a-z_]{0,31}\Z")
_PERCENT_ESCAPE = re.compile(rb"%[0-9A-Fa-f]{2}")
_LIMIT = re.compile(r"(?:[1-9]|[1-9][0-9]|100)\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z\Z"
)
CANONICAL_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CANONICAL_UUID = re.compile(CANONICAL_UUID_PATTERN)
_CURSOR = re.compile(r"[A-Za-z0-9_-]{76}\Z")


def _invalid_query() -> HttpApiError:
    return HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)


def _validate_percent_escapes(raw: bytes) -> None:
    index = 0
    while index < len(raw):
        if raw[index] == ord("%"):
            if _PERCENT_ESCAPE.match(raw, index) is None:
                raise _invalid_query()
            index += 3
        else:
            index += 1


def strict_query(request: Request, *, allowed: frozenset[str]) -> dict[str, str]:
    raw = bytes(request.scope.get("query_string", b""))
    if len(raw) > MAX_QUERY_BYTES:
        raise _invalid_query()
    if not raw:
        return {}
    if any(byte < 0x20 or byte == 0x7F for byte in raw):
        raise _invalid_query()
    _validate_percent_escapes(raw)
    fields = raw.split(b"&")
    if len(fields) > MAX_QUERY_FIELDS or any(field.count(b"=") != 1 for field in fields):
        raise _invalid_query()
    for field in fields:
        raw_key = field.split(b"=", 1)[0]
        try:
            key = raw_key.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _invalid_query() from exc
        if _KEY.fullmatch(key) is None:
            raise _invalid_query()
    try:
        pairs = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_QUERY_FIELDS,
            separator="&",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid_query() from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in allowed or key in values:
            raise _invalid_query()
        if not value or len(value) > MAX_QUERY_BYTES:
            raise _invalid_query()
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise _invalid_query()
        values[key] = value
    return values


def session_token(request: Request) -> str:
    raw_values = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == b"cookie"
    ]
    try:
        return parse_cookie_headers(raw_values).get(SESSION_COOKIE, "")
    except InvalidCookieHeaderError as exc:
        raise _invalid_query() from exc


def bounded_limit(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    if _LIMIT.fullmatch(value) is None:
        raise _invalid_query()
    return int(value)


def utc_timestamp(value: str) -> datetime:
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        raise _invalid_query()
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00").astimezone(UTC)
    except ValueError as exc:
        raise _invalid_query() from exc


def validate_period(start: datetime, end: datetime) -> None:
    if start >= end or end - start > MAX_REPORT_SPAN:
        raise _invalid_query()


def validate_comparison(
    current_start: datetime,
    current_end: datetime,
    previous_start: datetime,
    previous_end: datetime,
) -> None:
    validate_period(current_start, current_end)
    validate_period(previous_start, previous_end)
    if previous_end > current_start:
        raise _invalid_query()


def canonical_uuid(value: str) -> UUID:
    if _CANONICAL_UUID.fullmatch(value) is None:
        raise _invalid_query()
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise _invalid_query() from exc
    if str(parsed) != value:
        raise _invalid_query()
    return parsed


def canonical_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if _CURSOR.fullmatch(value) is None:
        raise HttpApiError(status_code=422, code=HttpErrorCode.INVALID_CURSOR)
    return value
