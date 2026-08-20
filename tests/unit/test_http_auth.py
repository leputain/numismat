from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
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
    AuthPersistence,
    SessionCheck,
    SessionCheckStatus,
)
from finbot.adapters.http.auth.service import (
    SessionAuthenticator,
    SessionCredentials,
    SessionInvalidError,
    TelegramAuthService,
)
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
SECOND_OWNER_ID = 424_243
FOREIGN_OWNER_ID = 424_244
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ORIGIN = "https://miniapp.example.test"
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SESSION_TOKEN = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
CSRF_TOKEN = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"


def _opaque_token(value: int) -> str:
    return base64.urlsafe_b64encode(bytes((value,)) * 32).rstrip(b"=").decode("ascii")


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


def test_telegram_init_data_accepts_allowlisted_users_and_rejects_foreign_user() -> None:
    allowed = frozenset((OWNER_ID, SECOND_OWNER_ID))

    for telegram_user_id in allowed:
        verified = verify_telegram_init_data(
            _signed_init_data(user={"id": telegram_user_id}),
            bot_token=BOT_TOKEN,
            allowed_telegram_user_ids=allowed,
            now=NOW,
        )
        assert verified.telegram_user_id == telegram_user_id

    with pytest.raises(TelegramAuthVerificationError) as caught:
        verify_telegram_init_data(
            _signed_init_data(user={"id": FOREIGN_OWNER_ID}),
            bot_token=BOT_TOKEN,
            allowed_telegram_user_ids=allowed,
            now=NOW,
        )

    assert caught.value.reason is TelegramAuthReason.FOREIGN_OWNER


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
        if self.owner.telegram_user_id == 0:
            self.owner = AuthOwner(
                self.owner.owner_id,
                self.owner.locale,
                self.owner.timezone,
                self.owner.base_currency,
                _telegram_user_id,
            )
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

    async def lock_session_for_login(self, _token: Any, **_kwargs: Any) -> SessionCheck:
        return await self.read_session(_token, **_kwargs)

    async def revoke_session_for_login(self, _token: Any, **_kwargs: Any) -> None:
        checked = await self.read_session(_token, **_kwargs)
        if checked.status is SessionCheckStatus.ACTIVE:
            self.revoked = True

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
    def __init__(self, persistence: Any) -> None:
        self.persistence = persistence

    @asynccontextmanager
    async def __call__(self) -> Any:
        yield self.persistence


@dataclass
class MultiOwnerAuthPersistence:
    owners: dict[int, AuthOwner]
    sessions: dict[bytes, AuthenticatedSession] = field(default_factory=dict)
    claim_status: IdempotencyClaimStatus = IdempotencyClaimStatus.NEW
    claim_count: int = 0
    completed_count: int = 0
    revoked_for_login: list[bytes] = field(default_factory=list)

    async def find_owner(self, telegram_user_id: int) -> AuthOwner | None:
        return self.owners.get(telegram_user_id)

    async def create_session(
        self, owner_id: Any, session_token: Any, _csrf_token: Any, **kwargs: Any
    ) -> None:
        owner = next(owner for owner in self.owners.values() if owner.owner_id == owner_id)
        self.sessions[session_token.database_value()] = AuthenticatedSession(
            owner=owner,
            expires_at=kwargs["expires_at"],
        )

    async def claim_auth_proof(
        self, _owner_id: Any, _key: Any, _fingerprint: Any, **_kwargs: Any
    ) -> IdempotencyClaim:
        self.claim_count += 1
        result = None
        if self.claim_status is IdempotencyClaimStatus.REPLAY:
            from finbot.adapters.database.repositories.http_idempotency import (
                IdempotencyResult,
                IdempotencyResultKind,
            )

            result = IdempotencyResult(http_status=200, kind=IdempotencyResultKind.NONE)
        return IdempotencyClaim(uuid7(), self.claim_status, result)

    async def complete_auth_proof(self, *_args: Any, **_kwargs: Any) -> None:
        self.completed_count += 1

    async def read_session(self, token: Any, **kwargs: Any) -> SessionCheck:
        authenticated = self.sessions.get(token.database_value())
        if authenticated is None or authenticated.expires_at <= kwargs["now"]:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        return SessionCheck(SessionCheckStatus.ACTIVE, authenticated)

    async def lock_session_for_login(self, token: Any, **kwargs: Any) -> SessionCheck:
        return await self.read_session(token, **kwargs)

    async def revoke_session_for_login(self, token: Any, **_kwargs: Any) -> None:
        digest = token.database_value()
        self.revoked_for_login.append(digest)
        self.sessions.pop(digest, None)

    async def lock_session_for_mutation(
        self, token: Any, _csrf: Any, **kwargs: Any
    ) -> SessionCheck:
        return await self.read_session(token, **kwargs)

    async def revoke_session(self, token: Any, _csrf: Any, **kwargs: Any) -> SessionCheck:
        checked = await self.read_session(token, **kwargs)
        self.sessions.pop(token.database_value(), None)
        return checked


def _multi_auth_app(
    persistence: MultiOwnerAuthPersistence,
    *,
    tokens: list[OpaqueAuthTokens],
) -> FastAPI:
    token_values = iter(tokens)
    service = TelegramAuthService(
        bot_token=BOT_TOKEN,
        allowed_telegram_user_ids=frozenset(persistence.owners),
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=FakeUow(persistence),
        clock=lambda: NOW,
        token_factory=lambda: next(token_values),
    )
    return create_app(
        readiness_probe=ReadinessStub(),
        auth_service=service,
        auth_origin=ORIGIN,
    )


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
        session_binding = login.headers["X-Session-Binding"]
        me = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": session_binding},
        )
        csrf = client.cookies.get("__Host-numismat_csrf")
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "X-Session-Binding": session_binding,
            },
        )
        after = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": session_binding},
        )

    assert login.status_code == 200
    assert login.json() == {
        "authenticated": True,
        "base_currency": "RUB",
        "expires_at": "2026-08-13T13:00:00.000Z",
        "locale": "ru_RU",
        "timezone": "Europe/Moscow",
    }
    assert session_binding == HttpSecurityDigester(SECURITY_KEY).session_binding(SESSION_TOKEN)
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
    assert logout.headers.get_list("set-cookie") == []
    assert after.status_code == 401
    assert after.headers.get_list("set-cookie") == []
    assert persistence.created_session_digest is not None
    assert SESSION_TOKEN.encode() not in persistence.created_session_digest
    assert CSRF_TOKEN.encode() not in persistence.created_csrf_digest
    for response in (login, me, logout, after):
        assert response.headers["cache-control"] == "no-store, no-cache"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_protected_read_requires_binding_to_the_exact_session_cookie() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    app = _auth_app(FakeAuthPersistence(owner))
    digester = HttpSecurityDigester(SECURITY_KEY)

    async with _client(app) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": _signed_init_data()},
        )
        binding = login.headers.get("X-Session-Binding")
        bound = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": binding or ""},
        )
        stale = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": digester.session_binding(_opaque_token(99))},
        )
        unbound = await client.get("/api/v1/auth/me")

    assert login.status_code == 200
    assert binding == digester.session_binding(SESSION_TOKEN)
    assert unbound.status_code == 401
    assert unbound.json()["error"]["code"] == "auth_session_invalid"
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "auth_session_invalid"
    assert stale.headers.get_list("set-cookie") == []
    assert bound.status_code == 200


@pytest.mark.asyncio
async def test_stale_logout_binding_is_rejected_before_csrf_and_preserves_cookies() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)
    stale_binding = HttpSecurityDigester(SECURITY_KEY).session_binding(_opaque_token(99))

    async with _client(app) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": _signed_init_data()},
        )
        response = await client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": _opaque_token(98),
                "X-Session-Binding": stale_binding,
            },
        )

    assert login.status_code == 200
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_session_invalid"
    assert response.headers.get_list("set-cookie") == []
    assert not persistence.revoked


@pytest.mark.parametrize("origin", [None, "https://web.telegram.org"])
@pytest.mark.asyncio
async def test_logout_accepts_native_webview_origin_metadata_with_valid_csrf(
    origin: str | None,
) -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)

    async with _client(_auth_app(persistence)) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        csrf = client.cookies.get("__Host-numismat_csrf")
        headers = {
            "X-CSRF-Token": csrf,
            "X-Session-Binding": login.headers["X-Session-Binding"],
        }
        if origin is not None:
            headers["Origin"] = origin
        logout = await client.post("/api/v1/auth/logout", headers=headers)

    assert login.status_code == 200
    assert logout.status_code == 204
    assert persistence.revoked


@pytest.mark.asyncio
async def test_auth_session_expiry_is_portable_rfc3339_milliseconds() -> None:
    precise_now = NOW.replace(microsecond=123_456)
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)

    async with _client(_auth_app(persistence, now=precise_now)) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data(auth_date=int(precise_now.timestamp()))},
        )

    assert response.status_code == 200
    assert response.json()["expires_at"] == "2026-08-13T13:00:00.123Z"


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
async def test_signed_login_switches_an_active_session_between_allowlisted_users() -> None:
    owner_a = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB", OWNER_ID)
    owner_b = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB", SECOND_OWNER_ID)
    persistence = MultiOwnerAuthPersistence({OWNER_ID: owner_a, SECOND_OWNER_ID: owner_b})
    first_tokens = OpaqueAuthTokens(_opaque_token(11), _opaque_token(12))
    second_tokens = OpaqueAuthTokens(_opaque_token(13), _opaque_token(14))

    async with _client(
        _multi_auth_app(persistence, tokens=[first_tokens, second_tokens])
    ) as client:
        first = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": _signed_init_data(user={"id": OWNER_ID})},
        )
        second = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": _signed_init_data(user={"id": SECOND_OWNER_ID})},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["X-Session-Binding"] != second.headers["X-Session-Binding"]
    assert len(second.headers.get_list("set-cookie")) == 2
    assert persistence.claim_count == 2
    assert persistence.completed_count == 2
    assert len(persistence.revoked_for_login) == 1
    assert len(persistence.sessions) == 1
    assert next(iter(persistence.sessions.values())).owner.owner_id == owner_b.owner_id


@pytest.mark.asyncio
async def test_repeated_signed_proof_reuses_same_owner_session_without_cookie_rotation() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB", OWNER_ID)
    persistence = MultiOwnerAuthPersistence({OWNER_ID: owner})
    tokens = OpaqueAuthTokens(_opaque_token(21), _opaque_token(22))
    app = _multi_auth_app(persistence, tokens=[tokens])
    proof = _signed_init_data(user={"id": OWNER_ID})

    async with _client(app) as client:
        first = await client.post("/api/v1/auth/telegram", json={"initData": proof})
        repeated = await client.post("/api/v1/auth/telegram", json={"initData": proof})

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.headers["X-Session-Binding"] == first.headers["X-Session-Binding"]
    assert repeated.headers.get_list("set-cookie") == []
    assert persistence.claim_count == 1
    assert persistence.completed_count == 1
    assert persistence.revoked_for_login == []


@pytest.mark.asyncio
async def test_foreign_signed_login_preserves_ambient_auth_cookies() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB", OWNER_ID)
    persistence = MultiOwnerAuthPersistence({OWNER_ID: owner})
    app = _multi_auth_app(
        persistence,
        tokens=[OpaqueAuthTokens(_opaque_token(31), _opaque_token(32))],
    )

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            headers={
                "Cookie": (
                    f"__Host-numismat_session={_opaque_token(33)}; "
                    f"__Host-numismat_csrf={_opaque_token(34)}"
                )
            },
            json={"initData": _signed_init_data(user={"id": FOREIGN_OWNER_ID})},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "telegram_owner_forbidden"
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.asyncio
async def test_removed_user_session_is_rejected_for_reads_and_mutations() -> None:
    removed_owner = AuthOwner(
        uuid7(),
        "ru_RU",
        "Europe/Moscow",
        "RUB",
        SECOND_OWNER_ID,
    )
    persistence = FakeAuthPersistence(
        removed_owner,
        active=AuthenticatedSession(removed_owner, NOW + timedelta(hours=1)),
    )
    authenticator = SessionAuthenticator(
        HttpSecurityDigester(SECURITY_KEY),
        frozenset((OWNER_ID,)),
    )
    credentials = SessionCredentials(
        SESSION_TOKEN,
        HttpSecurityDigester(SECURITY_KEY).session_binding(SESSION_TOKEN),
    )
    auth_persistence = cast(AuthPersistence, persistence)

    with pytest.raises(SessionInvalidError):
        await authenticator.authenticate_read(auth_persistence, credentials, now=NOW)
    with pytest.raises(SessionInvalidError):
        await authenticator.authenticate_mutation(
            auth_persistence,
            SESSION_TOKEN,
            credentials.session_binding,
            CSRF_TOKEN,
            CSRF_TOKEN,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_csrf_failure_does_not_clear_or_revoke_session() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)
    app = _auth_app(persistence)

    async with _client(app) as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        response = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": SESSION_TOKEN,
                "X-Session-Binding": login.headers["X-Session-Binding"],
            },
        )
        me = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": login.headers["X-Session-Binding"]},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert response.headers.get_list("set-cookie") == []
    assert not persistence.revoked
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_auth_boundary_rejects_content_type_duplicate_json_and_large_body() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    app = _auth_app(FakeAuthPersistence(owner))
    body = json.dumps({"initData": _signed_init_data()})

    async with _client(app) as client:
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

    assert wrong_type.status_code == 415
    assert duplicate.status_code == 422
    assert deeply_nested.status_code == 422
    assert too_large.status_code == 413
    for response in (wrong_type, duplicate, deeply_nested, too_large):
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
async def test_signed_telegram_login_does_not_trust_webview_origin_as_authority() -> None:
    owner = AuthOwner(uuid7(), "ru_RU", "Europe/Moscow", "RUB")
    persistence = FakeAuthPersistence(owner)

    async with _client(_auth_app(persistence)) as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            headers={
                "Origin": "https://web.telegram.org",
                "Content-Type": "application/json",
            },
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
        empty_origin = await client.post(
            "/api/v1/auth/telegram",
            headers=[("Origin", ""), ("Content-Type", "application/json")],
            content=body,
        )
        oversized_origin = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": "x" * 4097, "Content-Type": "application/json"},
            content=body,
        )
        non_ascii_origin = await client.post(
            "/api/v1/auth/telegram",
            headers=[(b"Origin", b"\xff"), (b"Content-Type", b"application/json")],
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
        login = await client.post(
            "/api/v1/auth/telegram",
            headers={"Origin": ORIGIN},
            json={"initData": _signed_init_data()},
        )
        missing_csrf = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": ORIGIN,
                "X-Session-Binding": login.headers["X-Session-Binding"],
            },
        )
        duplicate_csrf = await client.post(
            "/api/v1/auth/logout",
            headers=[
                ("Origin", ORIGIN),
                ("X-Session-Binding", login.headers["X-Session-Binding"]),
                ("X-CSRF-Token", CSRF_TOKEN),
                ("X-CSRF-Token", CSRF_TOKEN),
            ],
        )
        split_cookies = await client.get(
            "/api/v1/auth/me",
            headers=[
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_csrf={CSRF_TOKEN}"),
                ("X-Session-Binding", login.headers["X-Session-Binding"]),
            ],
        )
        duplicate_cookie = await client.get(
            "/api/v1/auth/me",
            headers=[
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("Cookie", f"__Host-numismat_session={SESSION_TOKEN}"),
                ("X-Session-Binding", login.headers["X-Session-Binding"]),
            ],
        )
        duplicate_binding = await client.get(
            "/api/v1/auth/me",
            headers=[
                ("X-Session-Binding", login.headers["X-Session-Binding"]),
                ("X-Session-Binding", login.headers["X-Session-Binding"]),
            ],
        )

    assert duplicate_origin.status_code == 403
    assert duplicate_origin.json()["error"]["code"] == "origin_forbidden"
    for response in (empty_origin, oversized_origin, non_ascii_origin):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "origin_forbidden"
    assert duplicate_type.status_code == 422
    assert encoded.status_code == 415
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_failed"
    assert duplicate_csrf.status_code == 403
    assert duplicate_csrf.json()["error"]["code"] == "csrf_failed"
    assert duplicate_csrf.headers.get_list("set-cookie") == []
    assert duplicate_cookie.status_code == 401
    assert split_cookies.status_code == 200
    assert duplicate_binding.status_code == 401
    assert duplicate_binding.headers.get_list("set-cookie") == []


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
        expired = await client.get(
            "/api/v1/auth/me",
            headers={"X-Session-Binding": login.headers["X-Session-Binding"]},
        )

    assert login.status_code == 200
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "auth_session_invalid"
    assert expired.headers.get_list("set-cookie") == []


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
                headers={
                    "Origin": "https://web.telegram.org",
                    "X-Unsafe-Marker": sensitive,
                },
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
    session_schema = schema["components"]["schemas"]["AuthSessionResponse"]
    assert session_schema["properties"]["expires_at"]["format"] == "date-time"
    assert schema["components"]["securitySchemes"]["SessionCookie"] == {
        "in": "cookie",
        "name": "__Host-numismat_session",
        "type": "apiKey",
    }
    me_operation = schema["paths"]["/api/v1/auth/me"]["get"]
    logout_operation = schema["paths"]["/api/v1/auth/logout"]["post"]
    binding_parameter = {
        "description": (
            "Opaque binding to the exact host-only session cookie for this page context."
        ),
        "in": "header",
        "name": "X-Session-Binding",
        "required": True,
        "schema": {
            "maxLength": 43,
            "minLength": 43,
            "pattern": "^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$",
            "type": "string",
        },
    }
    assert me_operation["security"] == [{"SessionCookie": []}]
    assert me_operation["parameters"] == [binding_parameter]
    assert logout_operation["security"] == [{"SessionCookie": []}]
    assert logout_operation["parameters"] == [
        binding_parameter,
        {
            "in": "header",
            "name": "X-CSRF-Token",
            "required": True,
            "schema": {"maxLength": 43, "minLength": 43, "type": "string"},
        },
    ]
    telegram_success = schema["paths"]["/api/v1/auth/telegram"]["post"]["responses"]["200"]
    assert telegram_success["headers"]["X-Session-Binding"]["schema"] == binding_parameter["schema"]
    for path, method, statuses in (
        ("/api/v1/auth/telegram", "post", (401, 403, 409, 413, 415, 422)),
        ("/api/v1/auth/me", "get", (401, 422)),
        ("/api/v1/auth/logout", "post", (401, 403, 422)),
    ):
        responses = schema["paths"][path][method]["responses"]
        for status in statuses:
            response_schema = responses[str(status)]["content"]["application/json"]["schema"]
            assert response_schema == {"$ref": "#/components/schemas/ApiErrorResponse"}
