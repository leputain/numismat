from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl

MAX_INIT_DATA_BYTES = 8192
MAX_INIT_DATA_FIELDS = 32
MAX_TELEGRAM_USER_ID = 2**52 - 1
AUTH_TTL_SECONDS = 300
AUTH_FUTURE_SKEW_SECONDS = 30

_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,10})\Z")
_PERCENT_ESCAPE = frozenset("0123456789abcdefABCDEF")


class TelegramAuthReason(StrEnum):
    MALFORMED = "malformed"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    FUTURE = "future"
    FOREIGN_OWNER = "foreign_owner"


class TelegramAuthVerificationError(Exception):
    def __init__(self, reason: TelegramAuthReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedTelegramAuth:
    telegram_user_id: int = field(repr=False)
    signed_proof_hash: bytes = field(repr=False)
    verified_at: datetime
    proof_expires_at: datetime


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _raw_ascii(value: str) -> bytes:
    if type(value) is not str:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED) from exc
    if not raw or len(raw) > MAX_INIT_DATA_BYTES:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    if any(byte < 0x20 or byte == 0x7F for byte in raw):
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in _PERCENT_ESCAPE
            or value[index + 2] not in _PERCENT_ESCAPE
        ):
            raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    return raw


def _parse_fields(value: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(
            value,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            separator="&",
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
    except (UnicodeError, ValueError) as exc:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED) from exc
    if not pairs or len(pairs) > MAX_INIT_DATA_FIELDS:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    fields: dict[str, str] = {}
    for key, field_value in pairs:
        if _KEY_PATTERN.fullmatch(key) is None or key in fields:
            raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
        if any(character in field_value for character in ("\0", "\r", "\n")):
            raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
        fields[key] = field_value
    provided_hash = fields.get("hash")
    if provided_hash is None or _HASH_PATTERN.fullmatch(provided_hash) is None:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    return fields


def _verify_signature(fields: dict[str, str], bot_token: str) -> None:
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, fields["hash"]):
        raise TelegramAuthVerificationError(TelegramAuthReason.INVALID_SIGNATURE)


def _parse_auth_date(value: str) -> int:
    if _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    timestamp = int(value)
    if timestamp < 1:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    return timestamp


def _parse_user_id(value: str) -> int:
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED) from exc
    if type(payload) is not dict:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    user_id = payload.get("id")
    if type(user_id) is not int or not 1 <= user_id <= MAX_TELEGRAM_USER_ID:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    return user_id


def verify_telegram_init_data(
    init_data: str,
    *,
    bot_token: str,
    owner_telegram_user_id: int,
    now: datetime | None = None,
) -> VerifiedTelegramAuth:
    """Verify Telegram's raw query payload before reading signed identity fields."""

    _raw_ascii(init_data)
    fields = _parse_fields(init_data)
    _verify_signature(fields, bot_token)

    auth_date_value = fields.get("auth_date")
    user_value = fields.get("user")
    if auth_date_value is None or user_value is None:
        raise TelegramAuthVerificationError(TelegramAuthReason.MALFORMED)
    auth_date = _parse_auth_date(auth_date_value)
    telegram_user_id = _parse_user_id(user_value)

    verified_at = now or datetime.now(UTC)
    if verified_at.tzinfo is None or verified_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current_timestamp = int(verified_at.timestamp())
    if auth_date > current_timestamp + AUTH_FUTURE_SKEW_SECONDS:
        raise TelegramAuthVerificationError(TelegramAuthReason.FUTURE)
    verification_expires_at = datetime.fromtimestamp(auth_date + AUTH_TTL_SECONDS, tz=UTC)
    if verified_at >= verification_expires_at:
        raise TelegramAuthVerificationError(TelegramAuthReason.EXPIRED)
    if telegram_user_id != owner_telegram_user_id:
        raise TelegramAuthVerificationError(TelegramAuthReason.FOREIGN_OWNER)
    return VerifiedTelegramAuth(
        telegram_user_id=telegram_user_id,
        signed_proof_hash=bytes.fromhex(fields["hash"]),
        verified_at=verified_at,
        proof_expires_at=datetime.fromtimestamp(
            auth_date + AUTH_TTL_SECONDS + AUTH_FUTURE_SKEW_SECONDS,
            tz=UTC,
        ),
    )
