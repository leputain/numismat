from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import UTC, datetime
from uuid import UUID

from finbot.application.recurring import RecurringInstanceCursor, RecurringScheduleCursor

RECURRING_CURSOR_LENGTH = 76
_CURSOR_VERSION = 1
_PAYLOAD = struct.Struct(">Bq16s")
_MAC_BYTES = 32
_RAW_BYTES = _PAYLOAD.size + _MAC_BYTES
_MICROSECONDS_PER_SECOND = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SCHEDULE_DOMAIN = b"numismat/http-cursor/v1/recurring/schedules\0"
_INSTANCE_DOMAIN = b"numismat/http-cursor/v1/recurring/instances\0"


class InvalidRecurringCursorError(ValueError):
    pass


def _microseconds(value: datetime) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("recurring cursor timestamp must contain a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _datetime(value: int) -> datetime:
    try:
        seconds, microseconds = divmod(value, _MICROSECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidRecurringCursorError from exc


class RecurringCursorCodec:
    """Signed owner/filter-bound recurring cursors; deliberately not encrypted."""

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

    def _mac(self, owner_id: UUID, context: bytes, payload: bytes, *, domain: bytes) -> bytes:
        return hmac.new(
            self._key,
            domain + owner_id.bytes + context + payload,
            hashlib.sha256,
        ).digest()

    def _encode(
        self,
        owner_id: UUID,
        timestamp: datetime,
        entity_id: UUID,
        *,
        context: bytes,
        domain: bytes,
    ) -> str:
        payload = _PAYLOAD.pack(_CURSOR_VERSION, _microseconds(timestamp), entity_id.bytes)
        encoded = (
            base64.urlsafe_b64encode(payload + self._mac(owner_id, context, payload, domain=domain))
            .rstrip(b"=")
            .decode("ascii")
        )
        if len(encoded) != RECURRING_CURSOR_LENGTH:  # pragma: no cover - wire invariant
            raise RuntimeError("recurring cursor wire length changed")
        return encoded

    def _decode(
        self,
        owner_id: UUID,
        value: str,
        *,
        context: bytes,
        domain: bytes,
    ) -> tuple[datetime, UUID]:
        if type(value) is not str or len(value) != RECURRING_CURSOR_LENGTH or "=" in value:
            raise InvalidRecurringCursorError
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise InvalidRecurringCursorError from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != _RAW_BYTES or canonical != value:
            raise InvalidRecurringCursorError
        payload = raw[: _PAYLOAD.size]
        expected = self._mac(owner_id, context, payload, domain=domain)
        if not hmac.compare_digest(expected, raw[_PAYLOAD.size :]):
            raise InvalidRecurringCursorError
        version, timestamp_us, entity_bytes = _PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidRecurringCursorError
        return _datetime(timestamp_us), UUID(bytes=entity_bytes)

    def encode_schedule(
        self,
        owner_id: UUID,
        cursor: RecurringScheduleCursor,
        *,
        deleted: bool,
    ) -> str:
        return self._encode(
            owner_id,
            cursor.created_at,
            cursor.schedule_id,
            context=bytes((int(deleted),)),
            domain=_SCHEDULE_DOMAIN,
        )

    def decode_schedule(
        self,
        owner_id: UUID,
        value: str,
        *,
        deleted: bool,
    ) -> RecurringScheduleCursor:
        created_at, schedule_id = self._decode(
            owner_id,
            value,
            context=bytes((int(deleted),)),
            domain=_SCHEDULE_DOMAIN,
        )
        return RecurringScheduleCursor(created_at, schedule_id)

    def encode_instance(
        self,
        owner_id: UUID,
        schedule_id: UUID,
        cursor: RecurringInstanceCursor,
    ) -> str:
        return self._encode(
            owner_id,
            cursor.scheduled_for,
            cursor.instance_id,
            context=schedule_id.bytes,
            domain=_INSTANCE_DOMAIN,
        )

    def decode_instance(
        self,
        owner_id: UUID,
        schedule_id: UUID,
        value: str,
    ) -> RecurringInstanceCursor:
        scheduled_for, instance_id = self._decode(
            owner_id,
            value,
            context=schedule_id.bytes,
            domain=_INSTANCE_DOMAIN,
        )
        return RecurringInstanceCursor(scheduled_for, instance_id)


__all__ = [
    "RECURRING_CURSOR_LENGTH",
    "InvalidRecurringCursorError",
    "RecurringCursorCodec",
]
