from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid7

import httpx2
import pytest
from fastapi import FastAPI

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyClaimStatus,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.crypto import HttpSecurityDigester, OpaqueAuthTokens
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthOwner,
    SessionCheck,
    SessionCheckStatus,
)
from finbot.adapters.http.auth.service import TelegramAuthService
from finbot.adapters.http.auth.telegram import (
    AUTH_FUTURE_SKEW_SECONDS,
    AUTH_TTL_SECONDS,
    MAX_INIT_DATA_BYTES,
    TelegramAuthReason,
    TelegramAuthVerificationError,
    verify_telegram_init_data,
)
from finbot.observability.logging import JsonFormatter

BOT_TOKEN = "123456:synthetic-unit-token"
OWNER_ID = 424_242
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ORIGIN = "https://miniapp.example.test"
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SESSION_TOKEN = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
CSRF_TOKEN = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"


def _signed_init_data(
    *,
    auth_date: int | None = None,
    user: Any = None,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    user_value = {"id": OWNER_ID, "first_name": "Synthetic"} if user is None else user
    fields = [
        ("auth_date", str(auth_date if auth_date is not None else int(NOW.timestamp()))),
        ("query_id", "synthetic-query"),
        ("user", json.dumps(user_value, separators=(",", ":"))),
    ]
    fields.extend(extra or [])
    return _encode_signed(fields)


def _encode_signed(fields: list[tuple[str, str]]) -> str:
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*fields, ("hash", signature)])


def _verify(value: str, *, now: datetime = NOW) -> Any:
    return verify_telegram_init_data(
        value,
        bot_token=BOT_TOKEN,
        owner_telegram_user_id=OWNER_ID,
        now=now,
    )


def test_telegram_init_data_accepts_official_hmac_and_signature_field() -> None:
    value = _signed_init_data(extra=[("signature", "safe-synthetic-signature")])

    verified = _verify(value)

    assert verified.telegram_user_id == OWNER_ID
    assert len(verified.signed_proof_hash) == 32
    assert verified.proof_expires_at == NOW + timedelta(
        seconds=AUTH_TTL_SECONDS + AUTH_FUTURE_SKEW_SECONDS
    )
    assert BOT_TOKEN not in repr(verified)
    assert value not in repr(verified)


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda value: value.replace("synthetic-query", "tampered"), "invalid_signature"),
        (lambda value: value.replace("hash=", "hash=0"), "malformed"),
        (lambda value: value + "&hash=" + "0" * 64, "malformed"),
        (lambda value: value + "&user=%7B%22id%22%3A1%7D", "malformed"),
        (lambda _value: "auth_date=1&user=%7B%22id%22%3A1%7D", "malformed"),
        (lambda value: value + "&bad=%ZZ", "malformed"),
        (lambda value: value + "&bad", "malformed"),
        (lambda value: value + "&bad=%0A", "malformed"),
        (lambda value: value + "&bad=%00", "malformed"),
        (lambda value: value + "&Upper=value", "malformed"),
        (lambda value: value + "".join(f"&field_{index}=x" for index in range(33)), "malformed"),
        (lambda _value: "a=" + "x" * MAX_INIT_DATA_BYTES, "malformed"),
    ],
)
def test_telegram_init_data_rejects_malformed_or_tampered_input(mutator: Any, reason: str) -> None:
    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(mutator(_signed_init_data()))

    assert caught.value.reason.value == reason


@pytest.mark.parametrize(
    "fields",
    [
        [("auth_date", str(int(NOW.timestamp())))],
        [("user", json.dumps({"id": OWNER_ID}))],
    ],
)
def test_telegram_init_data_requires_signed_auth_date_and_user(
    fields: list[tuple[str, str]],
) -> None:
    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(_encode_signed(fields))

    assert caught.value.reason is TelegramAuthReason.MALFORMED


@pytest.mark.parametrize(
    ("auth_date", "reason"),
    [
        (int(NOW.timestamp()) - AUTH_TTL_SECONDS + 1, None),
        (int(NOW.timestamp()) - AUTH_TTL_SECONDS, TelegramAuthReason.EXPIRED),
        (int(NOW.timestamp()) + AUTH_FUTURE_SKEW_SECONDS, None),
        (int(NOW.timestamp()) + AUTH_FUTURE_SKEW_SECONDS + 1, TelegramAuthReason.FUTURE),
    ],
)
def test_telegram_init_data_enforces_inclusive_time_boundaries(
    auth_date: int, reason: TelegramAuthReason | None
) -> None:
    value = _signed_init_data(auth_date=auth_date)
    if reason is None:
        assert _verify(value).telegram_user_id == OWNER_ID
    else:
        with pytest.raises(TelegramAuthVerificationError) as caught:
            _verify(value)
        assert caught.value.reason is reason


@pytest.mark.parametrize("user_id", [True, str(OWNER_ID), 0, 2**52, OWNER_ID + 1])
def test_telegram_init_data_rejects_ambiguous_or_foreign_owner(user_id: Any) -> None:
    value = _signed_init_data(user={"id": user_id})

    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(value)

    expected = (
        TelegramAuthReason.FOREIGN_OWNER
        if user_id == OWNER_ID + 1
        else TelegramAuthReason.MALFORMED
    )
    assert caught.value.reason is expected


def test_telegram_init_data_rejects_duplicate_user_json_key() -> None:
    raw_user = f'{{"id":{OWNER_ID},"id":{OWNER_ID}}}'
    fields = [("auth_date", str(int(NOW.timestamp()))), ("user", raw_user)]
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()

    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(urlencode([*fields, ("hash", signature)]))

    assert caught.value.reason is TelegramAuthReason.MALFORMED


def test_telegram_init_data_rejects_excessively_nested_signed_user() -> None:
    raw_user = "[" * 1100 + "]" * 1100
    fields = [("auth_date", str(int(NOW.timestamp()))), ("user", raw_user)]
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()

    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(urlencode([*fields, ("hash", signature)]))

    assert caught.value.reason is TelegramAuthReason.MALFORMED


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_telegram_init_data_rejects_non_finite_signed_user_json(constant: str) -> None:
    fields = [
        ("auth_date", str(int(NOW.timestamp()))),
        ("user", f'{{"id":{OWNER_ID},"unsafe":{constant}}}'),
    ]

    with pytest.raises(TelegramAuthVerificationError) as caught:
        _verify(_encode_signed(fields))

    assert caught.value.reason is TelegramAuthReason.MALFORMED


def test_equivalent_query_encodings_have_one_signed_proof_hash() -> None:
    original = _signed_init_data(user={"id": OWNER_ID, "first_name": "A B"})
    pairs = original.split("&")
    reordered = "&".join(reversed(pairs))
    percent_case = original.replace("%7B", "%7b").replace("%7D", "%7d")
    space_encoding = original.replace("A+B", "A%20B")

    hashes = {
        _verify(value).signed_proof_hash
        for value in (original, reordered, percent_case, space_encoding)
    }

    assert len(hashes) == 1


class ReadinessStub:
    async def check(self) -> None:
        return None


@dataclass
class FakeAuthPersistence:
    owner: AuthOwner
    active: AuthenticatedSession | None = None
    claim_status: IdempotencyClaimStatus = IdempotencyClaimStatus.NEW
    created_session_digest: bytes | None = None
    created_csrf_digest: bytes | None = None
    completed: bool = False
    revoked: bool = False
    csrf_matches: bool = True

    async def find_owner(self, _telegram_user_id: int) -> AuthOwner | None:
        return self.owner

    async def claim_auth_proof(
        self, _owner_id: Any, _key: Any, _fingerprint: Any, **_kwargs: Any
    ) -> IdempotencyClaim:
        result = None
        if self.claim_status is IdempotencyClaimStatus.REPLAY:
            from finbot.adapters.database.repositories.http_idempotency import (
                IdempotencyResult,
                IdempotencyResultKind,
            )

            result = IdempotencyResult(http_status=200, kind=IdempotencyResultKind.NONE)
        return IdempotencyClaim(record_id=uuid7(), status=self.claim_status, result=result)

    async def create_session(
        self, _owner_id: Any, session_token: Any, csrf_token: Any, **kwargs: Any
    ) -> None:
        self.created_session_digest = session_token.database_value()
        self.created_csrf_digest = csrf_token.database_value()
        self.active = AuthenticatedSession(owner=self.owner, expires_at=kwargs["expires_at"])

    async def complete_auth_proof(
        self, _owner_id: Any, _claim: Any, _result: Any, **_kwargs: Any
    ) -> None:
        self.completed = True

    async def read_session(self, _token: Any, **_kwargs: Any) -> SessionCheck:
        if self.active is None or self.revoked or self.active.expires_at <= _kwargs["now"]:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        return SessionCheck(status=SessionCheckStatus.ACTIVE, authenticated=self.active)

    async def lock_session_for_mutation(
        self, _token: Any, _csrf: Any, **_kwargs: Any
    ) -> SessionCheck:
        if not self.csrf_matches:
            return SessionCheck(status=SessionCheckStatus.CSRF_FAILED)
        return await self.read_session(_token, **_kwargs)

    async def revoke_session(self, _token: Any, _csrf: Any, **_kwargs: Any) -> SessionCheck:
        if not self.csrf_matches:
            return SessionCheck(status=SessionCheckStatus.CSRF_FAILED)
        checked = await self.read_session(_token, **_kwargs)
        if checked.status is SessionCheckStatus.ACTIVE:
            self.revoked = True
        return checked


class FakeUow:
    def __init__(self, persistence: FakeAuthPersistence) -> None:
        self.persistence = persistence

    @asynccontextmanager
    async def __call__(self) -> Any:
        yield self.persistence


def _auth_app(
    persistence: FakeAuthPersistence,
    *,
    now: datetime = NOW,
    clock: Any | None = None,
) -> FastAPI:
    service = TelegramAuthService(
        bot_token=BOT_TOKEN,
        owner_telegram_user_id=OWNER_ID,
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=FakeUow(persistence),
        clock=clock or (lambda: now),
        token_factory=lambda: OpaqueAuthTokens(SESSION_TOKEN, CSRF_TOKEN),
    )
    return create_app(
        readiness_probe=ReadinessStub(),
        auth_service=service,
        auth_origin=ORIGIN,
    )


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ORIGIN,
    )


@pytest.mark.asyncio
async def test_auth_login_me_logout_lifecycle_and_cookie_flags() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)

    async with _client(app) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        me = await client.get("/api/v1/auth/me")
        csrf = client.cookies.get("__Host-numismat_csrf")
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        after = await client.get("/api/v1/auth/me")

    assert login.status_code == 200
    assert login.json() == {
        "authenticated": True,
        "base_currency": "RUB",
        "expires_at": "2026-08-13T13:00:00Z",
        "locale": "ru_RU",
        "timezone": "Europe/Moscow",
    }
    set_cookies = login.headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    assert any("__Host-numismat_session=" in value and "HttpOnly" in value for value in set_cookies)
    assert any(
        "__Host-numismat_csrf=" in value and "HttpOnly" not in value for value in set_cookies
    )
    assert all("Max-Age=3600" in value and "Path=/" in value for value in set_cookies)
    assert all(
        "SameSite=strict" in value and "Secure" in value and "Domain=" not in value
        for value in set_cookies
    )
    assert me.status_code == 200
    assert logout.status_code == 204
    logout_cookies = logout.headers.get_list("set-cookie")
    assert len(logout_cookies) == 2
    assert all("Max-Age=0" in value and "expires=" in value.lower() for value in logout_cookies)
    assert all(
        "Path=/" in value
        and "Secure" in value
        and "SameSite=strict" in value
        and "Domain=" not in value
        for value in logout_cookies
    )
    assert "HttpOnly" in next(
        value for value in logout_cookies if "__Host-numismat_session=" in value
    )
    assert "HttpOnly" not in next(
        value for value in logout_cookies if "__Host-numismat_csrf=" in value
    )
    assert after.status_code == 401
    stale_cookies = after.headers.get_list("set-cookie")
    assert len(stale_cookies) == 2
    assert all("Max-Age=0" in value and "expires=" in value.lower() for value in stale_cookies)
    assert all(
        "Path=/" in value
        and "Secure" in value
        and "SameSite=strict" in value
        and "Domain=" not in value
        for value in stale_cookies
    )
    assert "HttpOnly" in next(
        value for value in stale_cookies if "__Host-numismat_session=" in value
    )
    assert "HttpOnly" not in next(
        value for value in stale_cookies if "__Host-numismat_csrf=" in value
    )
    assert persistence.created_session_digest is not None
    assert SESSION_TOKEN.encode() not in persistence.created_session_digest
    assert CSRF_TOKEN.encode() not in persistence.created_csrf_digest
    for response in (login, me, logout, after):
        assert response.headers["cache-control"] == "no-store, no-cache"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_replay_is_rejected_without_new_cookies() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner, claim_status=IdempotencyClaimStatus.REPLAY)

    async with _client(_auth_app(persistence)) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "telegram_auth_replayed"
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.asyncio
async def test_csrf_failure_does_not_clear_or_revoke_session() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)

    async with _client(app) as client:
        await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        response = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": SESSION_TOKEN},
        )
        me = await client.get("/api/v1/auth/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert response.headers.get_list("set-cookie") == []
    assert not persistence.revoked
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_auth_boundary_rejects_origin_content_type_duplicate_json_and_large_body() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    app = _auth_app(FakeAuthPersistence(owner))
    body = json.dumps({"initData": _signed_init_data()})

    async with _client(app) as client:
        wrong_origin = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": "https://wrong.example"},
            content=body,
        )
        wrong_type = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
            content=body,
        )
        duplicate = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            content='{"initData":"x","initData":"y"}',
        )
        deeply_nested = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            content="[" * 1100 + "]" * 1100,
        )
        too_large = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            content=b"x" * (12 * 1024 + 1),
        )

    assert wrong_origin.status_code == 403
    assert wrong_type.status_code == 415
    assert duplicate.status_code == 422
    assert deeply_nested.status_code == 422
    assert too_large.status_code == 413
    for response in (wrong_origin, wrong_type, duplicate, deeply_nested, too_large):
        assert response.headers["cache-control"] == "no-store, no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_auth_streaming_body_limit_ignores_missing_or_lying_content_length() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)

    async def invoke(headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
        payload = b"x" * (12 * 1024 + 1)
        messages = iter(
            [
                {"type": "http.request", "body": payload[:6000], "more_body": True},
                {"type": "http.request", "body": payload[6000:], "more_body": False},
            ]
        )
        response_status = 0
        response_body = bytearray()

        async def receive() -> dict[str, Any]:
            return next(messages)

        async def send(message: dict[str, Any]) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        await app(
            {
                "asgi": {"spec_version": "2.4", "version": "3.0"},
                "client": ("127.0.0.1", 12345),
                "headers": headers,
                "http_version": "2",
                "method": "POST",
                "path": "/api/v1/auth/telegram",
                "query_string": b"",
                "raw_path": b"/api/v1/auth/telegram",
                "scheme": "https",
                "server": ("miniapp.example.test", 443),
                "type": "http",
            },
            receive,
            send,
        )
        return response_status, bytes(response_body)

    base_headers = [
        (b"content-type", b"application/json"),
        (b"origin", ORIGIN.encode("ascii")),
    ]
    missing_status, missing_body = await invoke(base_headers)
    lying_status, lying_body = await invoke([*base_headers, (b"content-length", b"1")])

    assert missing_status == 413
    assert lying_status == 413
    assert json.loads(missing_body)["error"]["code"] == "request_too_large"
    assert json.loads(lying_body)["error"]["code"] == "request_too_large"
    assert persistence.active is None
    assert persistence.created_session_digest is None


@pytest.mark.asyncio
async def test_signed_telegram_login_accepts_missing_origin_from_native_webview() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)

    async with _client(_auth_app(persistence)) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            headers={"Content-Type": "application/json"},
            content=json.dumps({"initData": _signed_init_data()}),
        )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert persistence.created_session_digest is not None


@pytest.mark.asyncio
async def test_auth_boundary_rejects_ambiguous_security_headers_with_fixed_codes() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)
    body = json.dumps({"initData": _signed_init_data()})

    async with _client(app) as client:
        duplicate_origin = await client.post(
            "/api/v1/auth/telegram",
            headers=[
                ("Origin", ORIGIN),
                ("Origin", ORIGIN),
                ("Content-Type", "application/json"),
            ],
            content=body,
        )
        duplicate_type = await client.post(
            "/api/v1/auth/telegram",
            headers=[
                ("Origin", ORIGIN),
                ("Content-Type", "application/json"),
                ("Content-Type", "application/json"),
            ],
            content=body,
        )
        encoded = await client.post(
            "/api/v1/auth/telegram",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            content=body,
        )
        await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        missing_csrf = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN},
        )
        duplicate_csrf = await client.post(
            "/api/v1/auth/logout",
            headers=[
                ("Origin", ORIGIN),
                ("X-CSRF-Token", CSRF_TOKEN),
                ("X-CSRF-Token", CSRF_TOKEN),
            ],
        )
        split_cookies = await client.get(
            "/api/v1/auth/me",
            headers=[
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_csrf={CSRF_TOKEN}"),
            ],
        )
        duplicate_cookie = await client.get(
            "/api/v1/auth/me",
            headers=[
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
            ],
        )

    assert duplicate_origin.status_code == 403
    assert duplicate_origin.json()["error"]["code"] == "origin_forbidden"
    assert duplicate_type.status_code == 422
    assert encoded.status_code == 415
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert duplicate_csrf.status_code == 403
    assert duplicate_csrf.json()["error"]["code"] == "csrf_failed"
    assert duplicate_csrf.headers.get_list("set-cookie") == []
    assert duplicate_cookie.status_code == 422
    assert split_cookies.status_code == 200


@pytest.mark.asyncio
async def test_expired_session_is_rejected_and_stale_cookies_are_cleared() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    current = [NOW]
    app = _auth_app(persistence, clock=lambda: current[0])

    async with _client(app) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        current[0] = NOW + timedelta(hours=1)
        expired = await client.get("/api/v1/auth/me")

    assert login.status_code == 200
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "auth_session_invalid"
    assert len(expired.headers.get_list("set-cookie")) == 2


@pytest.mark.asyncio
async def test_auth_logs_and_responses_do_not_leak_untrusted_auth_material() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    app = _auth_app(FakeAuthPersistence(owner))
    sensitive = "private-auth-marker"
    tampered = _signed_init_data().replace("synthetic-query", sensitive)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    try:
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN, "X-Unsafe-Marker": sensitive},
                json={"initData": tampered},
            )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    rendered = stream.getvalue()
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "telegram_auth_invalid"
    assert sensitive not in response.text
    assert sensitive not in rendered
    assert BOT_TOKEN not in rendered
    assert "initData" not in rendered
    assert "X-Unsafe-Marker" not in rendered
    entries = [json.loads(line) for line in rendered.splitlines() if line]
    auth_entries = [entry for entry in entries if entry["event"] == "http_auth_login_completed"]
    assert auth_entries == [
        {
            "auth_reason": "invalid_signature",
            "component": "http",
            "correlation_id": auth_entries[0]["correlation_id"],
            "event": "http_auth_login_completed",
            "level": "INFO",
            "result": "rejected",
        }
    ]


@pytest.mark.asyncio
async def test_auth_openapi_retains_manual_body_schema() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")

    async with _client(_auth_app(FakeAuthPersistence(owner))) as client:
        schema = (await client.get("/api/v1/openapi.json")).json()

    request_schema = schema["paths"]["/api/v1/auth/telegram"]["post"]["requestBody"]
    body_schema = request_schema["content"]["application/json"]["schema"]
    assert body_schema["title"] == "TelegramAuthRequest"
    assert set(body_schema["required"]) == {"initData"}
    assert body_schema["properties"]["initData"]["maxLength"] == 8192
    assert schema["components"]["securitySchemes"]["SessionCookie"] == {
        "in": "cookie",
        "name": "__Host-numismat_session",
        "type": "apiKey",
    }
    me_operation = schema["paths"]["/api/v1/auth/me"]["get"]
    logout_operation = schema["paths"]["/api/v1/auth/logout"]["post"]
    assert me_operation["security"] == [{"SessionCookie": []}]
    assert logout_operation["security"] == [{"SessionCookie": []}]
    assert logout_operation["parameters"] == [
        {
            "in": "header",
            "name": "X-CSRF-Token",
            "required": True,
            "schema": {"maxLength": 43, "minLength": 43, "type": "string"},
        }
    ]
    for path, method, statuses in (
        ("/api/v1/auth/telegram", "post", (401, 403, 409, 413, 415, 422)),
        ("/api/v1/auth/me", "get", (401, 422)),
        ("/api/v1/auth/logout", "post", (401, 403, 422)),
    ):
        responses = schema["paths"][path][method]["responses"]
        for status in statuses:
            response_schema = responses[str(status)]["content"]["application/json"]["schema"]
            assert response_schema == {"$ref": "#/components/schemas/ApiErrorResponse"}
