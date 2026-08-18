from hmac import compare_digest

KEYED_DIGEST_BYTES = 32


class _KeyedDigest:
    """Validated database-bound keyed digest with a non-sensitive representation."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        if type(value) is not bytes or len(value) != KEYED_DIGEST_BYTES:
            raise ValueError("keyed digest must be exactly 32 bytes")
        self._value = value

    def database_value(self) -> bytes:
        return self._value

    def matches(self, value: bytes) -> bool:
        return type(value) is bytes and compare_digest(self._value, value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


class SessionTokenDigest(_KeyedDigest):
    pass


class CsrfTokenDigest(_KeyedDigest):
    pass


class IdempotencyKeyDigest(_KeyedDigest):
    pass


class RequestFingerprintDigest(_KeyedDigest):
    pass
