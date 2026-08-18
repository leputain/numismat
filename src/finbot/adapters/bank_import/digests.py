from __future__ import annotations

import hashlib
import hmac
from datetime import UTC

from finbot.application.bank_imports import (
    BankImportRequestDigest,
    KeyedBankImportDigest,
    StageBankImportBatchCommand,
)
from finbot.domain.bank_imports import ParsedBankImportRow, normalize_bank_import_text

_FINGERPRINT_DOMAIN = b"numismat:bank-import:fingerprint:v1"
_REFERENCE_DOMAIN = b"numismat:bank-import:reference:v1"
_REQUEST_DOMAIN = b"numismat:bank-import:request:v1"


def _part(value: str) -> bytes:
    encoded = value.encode("utf-8", errors="strict")
    return len(encoded).to_bytes(4, "big") + encoded


def _update_part(digest: hmac.HMAC, value: bytes) -> None:
    digest.update(len(value).to_bytes(4, "big"))
    digest.update(value)


class HmacBankImportDigester:
    """Create domain-separated keyed digests without retaining source references."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("Bank import security key must contain exactly 32 bytes")
        self._key = key

    def fingerprint(self, row: ParsedBankImportRow) -> KeyedBankImportDigest:
        if not isinstance(row, ParsedBankImportRow):
            raise TypeError("Bank import fingerprint input is invalid")
        occurred_at = (
            row.occurred_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        payload = b"".join(
            (
                _part(occurred_at),
                _part(row.kind.value),
                _part(str(row.amount_minor)),
                _part(row.currency),
                _part(row.description),
                _part(row.source_account_reference),
                _part(row.source_reference or ""),
            )
        )
        return KeyedBankImportDigest(
            hmac.digest(self._key, _FINGERPRINT_DOMAIN + payload, hashlib.sha256)
        )

    def reference(self, value: str) -> KeyedBankImportDigest:
        normalized = normalize_bank_import_text(
            value,
            field_name="reference",
            allow_empty=False,
        )
        return KeyedBankImportDigest(
            hmac.digest(self._key, _REFERENCE_DOMAIN + _part(normalized), hashlib.sha256)
        )

    def request_digest(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportRequestDigest:
        """Digest prepared metadata and ordered row digests without financial wire data."""

        if not isinstance(command, StageBankImportBatchCommand):
            raise TypeError("Prepared bank import request is invalid")
        digest = hmac.new(self._key, _REQUEST_DOMAIN, hashlib.sha256)
        _update_part(digest, command.profile.value.encode("ascii"))
        _update_part(digest, command.encoding.value.encode("ascii"))
        _update_part(digest, command.account_id.bytes)
        _update_part(digest, command.expected_account_version.to_bytes(4, "big"))
        _update_part(digest, len(command.rows).to_bytes(4, "big"))
        for row in command.rows:
            _update_part(digest, row.fingerprint.value)
        return BankImportRequestDigest(digest.digest())


__all__ = ["HmacBankImportDigester"]
