from __future__ import annotations

import re

from fastapi import Request

from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.application.bank_imports import BankImportEncoding
from finbot.domain.bank_imports import MAX_BANK_IMPORT_BYTES

_CONTENT_TYPE = re.compile(
    rb"text/csv(?:\s*;\s*charset\s*=\s*(utf-8|windows-1251|\"utf-8\"|\"windows-1251\"))?",
    re.IGNORECASE,
)
_CONTENT_LENGTH = re.compile(rb"(?:0|[1-9][0-9]{0,7})\Z")
_MAX_HEADER_BYTES = 4096


def _raw_header(request: Request, name: bytes, *, required: bool = False) -> bytes | None:
    values = [
        bytes(value) for key, value in request.scope.get("headers", []) if key.lower() == name
    ]
    if len(values) != 1:
        if required or values:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        return None
    value = values[0]
    if not value or len(value) > _MAX_HEADER_BYTES:
        raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
    return value


def _encoding(request: Request) -> BankImportEncoding | None:
    content_type = _raw_header(request, b"content-type", required=True)
    if content_type is None:
        raise HttpApiError(status_code=415, code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE)
    match = _CONTENT_TYPE.fullmatch(content_type)
    if match is None:
        raise HttpApiError(status_code=415, code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE)
    raw = match.group(1)
    if raw is None:
        return None
    value = raw.strip(b'"').decode("ascii").lower()
    return (
        BankImportEncoding.UTF8
        if value == BankImportEncoding.UTF8.value
        else BankImportEncoding.WINDOWS_1251
    )


async def bounded_csv_body(request: Request) -> tuple[bytes, BankImportEncoding | None]:
    """Read at most 2 MiB into memory after route-level auth admission."""

    if _raw_header(request, b"content-encoding") is not None:
        raise HttpApiError(status_code=415, code=HttpErrorCode.UNSUPPORTED_MEDIA_TYPE)
    encoding = _encoding(request)
    content_length = _raw_header(request, b"content-length")
    if content_length is not None:
        if _CONTENT_LENGTH.fullmatch(content_length) is None:
            raise HttpApiError(status_code=422, code=HttpErrorCode.VALIDATION_FAILED)
        if int(content_length) > MAX_BANK_IMPORT_BYTES:
            raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)

    payload = bytearray()
    async for chunk in request.stream():
        if chunk:
            if len(payload) + len(chunk) > MAX_BANK_IMPORT_BYTES:
                raise HttpApiError(status_code=413, code=HttpErrorCode.REQUEST_TOO_LARGE)
            payload.extend(chunk)
    return bytes(payload), encoding


__all__ = ["bounded_csv_body"]
