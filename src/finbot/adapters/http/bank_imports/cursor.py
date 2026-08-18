from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import UTC, datetime
from uuid import UUID

from finbot.application.bank_imports import (
    BankImportBatchCursor,
    BankImportBatchState,
    BankImportRowCursor,
    BankImportRowState,
)

BANK_IMPORT_BATCH_CURSOR_LENGTH = 76
BANK_IMPORT_ROW_CURSOR_LENGTH = 68
_CURSOR_VERSION = 1
_BATCH_PAYLOAD = struct.Struct(">Bq16s")
_ROW_PAYLOAD = struct.Struct(">BH16s")
_MAC_BYTES = 32
_MICROSECONDS_PER_SECOND = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_BATCH_DOMAIN = b"numismat/http-cursor/v1/bank-import/batches\0"
_ROW_DOMAIN = b"numismat/http-cursor/v1/bank-import/rows\0"


class InvalidBankImportCursorError(ValueError):
    pass


def _microseconds(value: datetime) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("bank import cursor timestamp must contain a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _datetime(value: int) -> datetime:
    try:
        seconds, microseconds = divmod(value, _MICROSECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidBankImportCursorError from exc


def _batch_context(state: BankImportBatchState | None) -> bytes:
    if state is None:
        return b"\0"
    if not isinstance(state, BankImportBatchState):
        raise ValueError("bank import batch cursor state is invalid")
    return bytes((list(BankImportBatchState).index(state) + 1,))


def _row_context(batch_id: UUID, state: BankImportRowState | None) -> bytes:
    if not isinstance(batch_id, UUID):
        raise ValueError("bank import row cursor batch is invalid")
    if state is not None and not isinstance(state, BankImportRowState):
        raise ValueError("bank import row cursor state is invalid")
    state_code = 0 if state is None else list(BankImportRowState).index(state) + 1
    return batch_id.bytes + bytes((state_code,))


class BankImportCursorCodec:
    """Signed owner/domain/filter-bound cursors; deliberately not encrypted."""

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
        context: bytes,
        payload: bytes,
        *,
        domain: bytes,
    ) -> bytes:
        if not isinstance(owner_id, UUID):
            raise ValueError("bank import cursor owner is invalid")
        return hmac.new(
            self._key,
            domain + owner_id.bytes + context + payload,
            hashlib.sha256,
        ).digest()

    def _decode(
        self,
        owner_id: UUID,
        value: str,
        *,
        length: int,
        payload_size: int,
        context: bytes,
        domain: bytes,
    ) -> bytes:
        if type(value) is not str or len(value) != length or "=" in value:
            raise InvalidBankImportCursorError
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise InvalidBankImportCursorError from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != payload_size + _MAC_BYTES or canonical != value:
            raise InvalidBankImportCursorError
        payload = raw[:payload_size]
        expected = self._mac(owner_id, context, payload, domain=domain)
        if not hmac.compare_digest(expected, raw[payload_size:]):
            raise InvalidBankImportCursorError
        return payload

    def encode_batch(
        self,
        owner_id: UUID,
        cursor: BankImportBatchCursor,
        *,
        state: BankImportBatchState | None,
    ) -> str:
        payload = _BATCH_PAYLOAD.pack(
            _CURSOR_VERSION,
            _microseconds(cursor.created_at),
            cursor.batch_id.bytes,
        )
        encoded = (
            base64.urlsafe_b64encode(
                payload
                + self._mac(
                    owner_id,
                    _batch_context(state),
                    payload,
                    domain=_BATCH_DOMAIN,
                )
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        if len(encoded) != BANK_IMPORT_BATCH_CURSOR_LENGTH:  # pragma: no cover
            raise RuntimeError("bank import batch cursor wire length changed")
        return encoded

    def decode_batch(
        self,
        owner_id: UUID,
        value: str,
        *,
        state: BankImportBatchState | None,
    ) -> BankImportBatchCursor:
        payload = self._decode(
            owner_id,
            value,
            length=BANK_IMPORT_BATCH_CURSOR_LENGTH,
            payload_size=_BATCH_PAYLOAD.size,
            context=_batch_context(state),
            domain=_BATCH_DOMAIN,
        )
        version, created_us, batch_bytes = _BATCH_PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidBankImportCursorError
        return BankImportBatchCursor(_datetime(created_us), UUID(bytes=batch_bytes))

    def encode_row(
        self,
        owner_id: UUID,
        batch_id: UUID,
        cursor: BankImportRowCursor,
        *,
        state: BankImportRowState | None,
    ) -> str:
        payload = _ROW_PAYLOAD.pack(
            _CURSOR_VERSION,
            cursor.position,
            cursor.row_id.bytes,
        )
        encoded = (
            base64.urlsafe_b64encode(
                payload
                + self._mac(
                    owner_id,
                    _row_context(batch_id, state),
                    payload,
                    domain=_ROW_DOMAIN,
                )
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        if len(encoded) != BANK_IMPORT_ROW_CURSOR_LENGTH:  # pragma: no cover
            raise RuntimeError("bank import row cursor wire length changed")
        return encoded

    def decode_row(
        self,
        owner_id: UUID,
        batch_id: UUID,
        value: str,
        *,
        state: BankImportRowState | None,
    ) -> BankImportRowCursor:
        payload = self._decode(
            owner_id,
            value,
            length=BANK_IMPORT_ROW_CURSOR_LENGTH,
            payload_size=_ROW_PAYLOAD.size,
            context=_row_context(batch_id, state),
            domain=_ROW_DOMAIN,
        )
        version, position, row_bytes = _ROW_PAYLOAD.unpack(payload)
        if version != _CURSOR_VERSION:
            raise InvalidBankImportCursorError
        return BankImportRowCursor(position, UUID(bytes=row_bytes))


__all__ = [
    "BANK_IMPORT_BATCH_CURSOR_LENGTH",
    "BANK_IMPORT_ROW_CURSOR_LENGTH",
    "BankImportCursorCodec",
    "InvalidBankImportCursorError",
]
