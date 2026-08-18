from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import UTC, datetime
from uuid import UUID

from finbot.application.exchange_rates import RateVersionCursor

EXCHANGE_RATE_CURSOR_LENGTH = 76
_CURSOR_VERSION = 1
_PAYLOAD = struct.Struct(">Bq16s")
_MAC_BYTES = 32
_RAW_BYTES = _PAYLOAD.size + _MAC_BYTES
_MICROSECONDS_PER_SECOND = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DOMAIN = b"numismat/http-cursor/v1/exchange-rate/versions\0"


class InvalidExchangeRateCursorError(ValueError):
    pass


def _microseconds(value: datetime) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("exchange-rate cursor timestamp must contain a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _datetime(value: int) -> datetime:
    try:
        seconds, microseconds = divmod(value, _MICROSECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidExchangeRateCursorError from exc


class ExchangeRateCursorCodec:
    """Signed owner/source-bound version cursor; deliberately not encrypted."""

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

    def _mac(self, owner_id: UUID, source_id: UUID, payload: bytes) -> bytes:
        return hmac.new(
            self._key,
            _DOMAIN + owner_id.bytes + source_id.bytes + payload,
            hashlib.sha256,
        ).digest()

    def encode(
        self,
        owner_id: UUID,
        source_id: UUID,
        cursor: RateVersionCursor,
    ) -> str:
        payload = _PAYLOAD.pack(
            _CURSOR_VERSION,
            _microseconds(cursor.created_at),
            cursor.rate_version_id.bytes,
        )
        encoded = (
            base64.urlsafe_b64encode(payload + self._mac(owner_id, source_id, payload))
            .rstrip(b"=")
            .decode("ascii")
        )
        if len(encoded) != EXCHANGE_RATE_CURSOR_LENGTH:  # pragma: no cover - wire invariant
            raise RuntimeError("exchange-rate cursor wire length changed")
        return encoded

    def decode(
        self,
        owner_id: UUID,
        source_id: UUID,
        value: str,
    ) -> RateVersionCursor:
        if type(value) is not str or len(value) != EXCHANGE_RATE_CURSOR_LENGTH or "=" in value:
            raise InvalidExchangeRateCursorError
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise InvalidExchangeRateCursorError from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != _RAW_BYTES or canonical != value:
            raise InvalidExchangeRateCursorError
        payload = raw[: _PAYLOAD.size]
        expected = self._mac(owner_id, source_id, payload)
        if not hmac.compare_digest(expected, raw[_PAYLOAD.size :]):
            raise InvalidExchangeRateCursorError
        version, timestamp_us, entity_bytes = _PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidExchangeRateCursorError
        return RateVersionCursor(
            created_at=_datetime(timestamp_us),
            rate_version_id=UUID(bytes=entity_bytes),
        )


__all__ = [
    "EXCHANGE_RATE_CURSOR_LENGTH",
    "ExchangeRateCursorCodec",
    "InvalidExchangeRateCursorError",
]
