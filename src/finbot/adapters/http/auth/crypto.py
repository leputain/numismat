from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)

OPAQUE_TOKEN_BYTES = 32
OPAQUE_TOKEN_LENGTH = 43
_PREFIX = b"numismat/http-security/v1/"


@dataclass(frozen=True, slots=True, repr=False)
class OpaqueAuthTokens:
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


def generate_auth_tokens() -> OpaqueAuthTokens:
    return OpaqueAuthTokens(
        session_token=secrets.token_urlsafe(OPAQUE_TOKEN_BYTES),
        csrf_token=secrets.token_urlsafe(OPAQUE_TOKEN_BYTES),
    )


def is_canonical_opaque_token(value: object) -> bool:
    if type(value) is not str or len(value) != OPAQUE_TOKEN_LENGTH:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except ValueError, UnicodeEncodeError, binascii.Error:
        return False
    return (
        len(decoded) == OPAQUE_TOKEN_BYTES
        and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") == value
    )


class HttpSecurityDigester:
    __slots__ = ("_key",)

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key + "=")
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("invalid HTTP security key") from exc
        canonical = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
        if len(key) != OPAQUE_TOKEN_BYTES or canonical != encoded_key:
            raise ValueError("invalid HTTP security key")
        self._key = key

    def _digest(self, domain: bytes, value: bytes) -> bytes:
        return hmac.new(self._key, _PREFIX + domain + b"\0" + value, hashlib.sha256).digest()

    def session(self, raw_token: str) -> SessionTokenDigest:
        if not is_canonical_opaque_token(raw_token):
            raise ValueError("invalid session token")
        return SessionTokenDigest(self._digest(b"session", raw_token.encode("ascii")))

    def session_binding(self, raw_token: str) -> str:
        """Return a browser-visible binding for one exact raw session token."""
        if not is_canonical_opaque_token(raw_token):
            raise ValueError("invalid session token")
        digest = self._digest(b"session-binding", raw_token.encode("ascii"))
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def csrf(self, raw_token: str) -> CsrfTokenDigest:
        if not is_canonical_opaque_token(raw_token):
            raise ValueError("invalid CSRF token")
        return CsrfTokenDigest(self._digest(b"csrf", raw_token.encode("ascii")))

    def telegram_proof(self, signed_proof_hash: bytes) -> IdempotencyKeyDigest:
        if type(signed_proof_hash) is not bytes or len(signed_proof_hash) != 32:
            raise ValueError("invalid Telegram proof hash")
        return IdempotencyKeyDigest(self._digest(b"telegram-proof", signed_proof_hash))

    def telegram_fingerprint(self) -> RequestFingerprintDigest:
        return RequestFingerprintDigest(self._digest(b"request-fingerprint", b"auth.telegram:v1"))

    def idempotency_key(self, raw_key: str) -> IdempotencyKeyDigest:
        if not is_canonical_opaque_token(raw_key):
            raise ValueError("invalid idempotency key")
        return IdempotencyKeyDigest(self._digest(b"idempotency-key", raw_key.encode("ascii")))

    def request_fingerprint(self, canonical_request: bytes) -> RequestFingerprintDigest:
        if type(canonical_request) is not bytes or not canonical_request:
            raise ValueError("invalid request fingerprint input")
        return RequestFingerprintDigest(
            self._digest(b"mutation-request-fingerprint", canonical_request)
        )
