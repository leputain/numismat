from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import UTC, datetime
from uuid import UUID

from finbot.application.dto import DeletedTransactionCursor, TransactionCursor

_CURSOR_VERSION = 1
_CURSOR_PAYLOAD = struct.Struct(">Bq16s")
_CURSOR_MAC_BYTES = 32
_CURSOR_BYTES = _CURSOR_PAYLOAD.size + _CURSOR_MAC_BYTES
_CURSOR_TEXT_BYTES = 76
_MICROSECONDS_PER_SECOND = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ACTIVE_DOMAIN = b"numismat/http-cursor/v1/transactions/active\0"
_DELETED_DOMAIN = b"numismat/http-cursor/v1/transactions/deleted\0"


class InvalidTransactionCursorError(ValueError):
    pass


def _microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cursor timestamp must contain a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _datetime(value: int) -> datetime:
    try:
        seconds, microseconds = divmod(value, _MICROSECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidTransactionCursorError from exc


class TransactionCursorCodec:
    """Integrity-protected API cursor; signed, owner-bound, and intentionally not encrypted."""

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

    def _mac(self, owner_id: UUID, payload: bytes, *, domain: bytes) -> bytes:
        return hmac.new(
            self._key,
            domain + owner_id.bytes + payload,
            hashlib.sha256,
        ).digest()

    def _encode(
        self, owner_id: UUID, timestamp: datetime, transaction_id: UUID, *, domain: bytes
    ) -> str:
        payload = _CURSOR_PAYLOAD.pack(
            _CURSOR_VERSION,
            _microseconds(timestamp),
            transaction_id.bytes,
        )
        encoded = base64.urlsafe_b64encode(payload + self._mac(owner_id, payload, domain=domain))
        return encoded.rstrip(b"=").decode("ascii")

    def _decode(self, owner_id: UUID, value: str, *, domain: bytes) -> tuple[datetime, UUID]:
        if type(value) is not str or len(value) != _CURSOR_TEXT_BYTES or "=" in value:
            raise InvalidTransactionCursorError
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise InvalidTransactionCursorError from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != _CURSOR_BYTES or canonical != value:
            raise InvalidTransactionCursorError
        payload = raw[: _CURSOR_PAYLOAD.size]
        if not hmac.compare_digest(
            self._mac(owner_id, payload, domain=domain),
            raw[_CURSOR_PAYLOAD.size :],
        ):
            raise InvalidTransactionCursorError
        version, timestamp_us, transaction_bytes = _CURSOR_PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidTransactionCursorError
        return _datetime(timestamp_us), UUID(bytes=transaction_bytes)

    def encode(self, owner_id: UUID, cursor: TransactionCursor) -> str:
        return self._encode(
            owner_id,
            cursor.occurred_at,
            cursor.transaction_id,
            domain=_ACTIVE_DOMAIN,
        )

    def decode(self, owner_id: UUID, value: str) -> TransactionCursor:
        occurred_at, transaction_id = self._decode(
            owner_id,
            value,
            domain=_ACTIVE_DOMAIN,
        )
        return TransactionCursor(
            occurred_at=occurred_at,
            transaction_id=transaction_id,
        )

    def encode_deleted(self, owner_id: UUID, cursor: DeletedTransactionCursor) -> str:
        return self._encode(
            owner_id,
            cursor.deleted_at,
            cursor.transaction_id,
            domain=_DELETED_DOMAIN,
        )

    def decode_deleted(self, owner_id: UUID, value: str) -> DeletedTransactionCursor:
        deleted_at, transaction_id = self._decode(
            owner_id,
            value,
            domain=_DELETED_DOMAIN,
        )
        return DeletedTransactionCursor(
            deleted_at=deleted_at,
            transaction_id=transaction_id,
        )
