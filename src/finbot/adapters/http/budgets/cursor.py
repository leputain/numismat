from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import date, datetime
from uuid import UUID

from finbot.application.budgets import BudgetCursor

BUDGET_CURSOR_LENGTH = 71
_CURSOR_VERSION = 1
_PAYLOAD = struct.Struct(">BI16s")
_CONTEXT = struct.Struct(">II?")
_MAC_BYTES = 32
_RAW_BYTES = _PAYLOAD.size + _MAC_BYTES
_DOMAIN = b"numismat/http-cursor/v1/budgets\0"


class InvalidBudgetCursorError(ValueError):
    pass


def _date_ordinal(value: date) -> int:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError("budget cursor context date is invalid")
    return value.toordinal()


class BudgetCursorCodec:
    """Signed owner/filter-bound budget cursor; deliberately not encrypted."""

    __slots__ = ("_key",)

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("invalid HTTP cursor key") from exc
        canonical = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
        if len(key) != 32 or canonical != encoded_key:
            raise ValueError("invalid HTTP cursor key")
        self._key = key

    def _mac(
        self,
        owner_id: UUID,
        payload: bytes,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
    ) -> bytes:
        if not isinstance(owner_id, UUID):
            raise ValueError("budget cursor owner is invalid")
        if type(deleted) is not bool:
            raise ValueError("budget cursor deletion selector is invalid")
        context = _CONTEXT.pack(
            _date_ordinal(window_start),
            _date_ordinal(window_end),
            deleted,
        )
        return hmac.new(
            self._key,
            _DOMAIN + owner_id.bytes + context + payload,
            hashlib.sha256,
        ).digest()

    def encode(
        self,
        owner_id: UUID,
        cursor: BudgetCursor,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
    ) -> str:
        payload = _PAYLOAD.pack(
            _CURSOR_VERSION,
            cursor.starts_on.toordinal(),
            cursor.budget_id.bytes,
        )
        mac = self._mac(
            owner_id,
            payload,
            window_start=window_start,
            window_end=window_end,
            deleted=deleted,
        )
        encoded = base64.urlsafe_b64encode(payload + mac).rstrip(b"=").decode("ascii")
        if len(encoded) != BUDGET_CURSOR_LENGTH:  # pragma: no cover - structural invariant
            raise RuntimeError("budget cursor wire length changed")
        return encoded

    def decode(
        self,
        owner_id: UUID,
        value: str,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
    ) -> BudgetCursor:
        if type(value) is not str or len(value) != BUDGET_CURSOR_LENGTH or "=" in value:
            raise InvalidBudgetCursorError
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise InvalidBudgetCursorError from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != _RAW_BYTES or canonical != value:
            raise InvalidBudgetCursorError
        payload = raw[: _PAYLOAD.size]
        expected = self._mac(
            owner_id,
            payload,
            window_start=window_start,
            window_end=window_end,
            deleted=deleted,
        )
        if not hmac.compare_digest(expected, raw[_PAYLOAD.size :]):
            raise InvalidBudgetCursorError
        version, ordinal, budget_bytes = _PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidBudgetCursorError
        try:
            starts_on = date.fromordinal(ordinal)
            return BudgetCursor(starts_on=starts_on, budget_id=UUID(bytes=budget_bytes))
        except (TypeError, ValueError) as exc:
            raise InvalidBudgetCursorError from exc


__all__ = ["BUDGET_CURSOR_LENGTH", "BudgetCursorCodec", "InvalidBudgetCursorError"]
