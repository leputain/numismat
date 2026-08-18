from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import HttpIdempotencyRecord, User, WebSession
from finbot.adapters.database.repositories.http_auth import (
    SqlAlchemyAuthUnitOfWorkFactory,
    SqlAlchemyHttpSessionAuthenticator,
)
from finbot.adapters.database.repositories.web_sessions import SqlAlchemyWebSessionRepository
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.crypto import HttpSecurityDigester, OpaqueAuthTokens
from finbot.adapters.http.auth.ports import SessionCheckStatus
from finbot.adapters.http.auth.service import TelegramAuthService

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
BOT_TOKEN = "123456:synthetic-integration-token"
ORIGIN = "https://miniapp.integration.test"
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class ReadinessStub:
    async def check(self) -> None:
        return None


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _token(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).rstrip(b"=").decode("ascii")


def _signed_init_data(owner_telegram_user_id: int, auth_date: int) -> str:
    fields = [
        ("auth_date", str(auth_date)),
        ("query_id", "integration-proof"),
        (
            "user",
            json.dumps(
                {"id": owner_telegram_user_id, "first_name": "Synthetic"},
                separators=(",", ":"),
            ),
        ),
    ]
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*fields, ("hash", signature)])


def _app(
    factory: async_sessionmaker,
    *,
    owner_telegram_user_id: int,
    clock: MutableClock,
    tokens: OpaqueAuthTokens | None = None,
) -> FastAPI:
    kwargs = {}
    if tokens is not None:
        kwargs["token_factory"] = lambda: tokens
    service = TelegramAuthService(
        bot_token=BOT_TOKEN,
        owner_telegram_user_id=owner_telegram_user_id,
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=SqlAlchemyAuthUnitOfWorkFactory(factory),
        clock=clock,
        **kwargs,
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


async def _create_owner(factory: async_sessionmaker, telegram_user_id: int) -> User:
    async with factory.begin() as session:
        owner = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
            locale="ru_RU",
            timezone="Europe/Moscow",
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        return owner


async def _delete_owner(factory: async_sessionmaker, owner: User) -> None:
    async with factory.begin() as session:
        await session.execute(delete(User).where(User.id == owner.id))


@pytest.mark.asyncio
async def test_http_auth_persists_only_keyed_state_and_enforces_session_lifecycle() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = 99_712_101
    owner = await _create_owner(factory, telegram_user_id)
    clock = MutableClock(NOW)
    session_token = _token(11)
    csrf_token = _token(12)
    app = _app(
        factory,
        owner_telegram_user_id=telegram_user_id,
        clock=clock,
        tokens=OpaqueAuthTokens(session_token, csrf_token),
    )
    proof = _signed_init_data(telegram_user_id, int(NOW.timestamp()))
    try:
        async with _client(app) as client:
            login = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": proof},
            )
            me = await client.get("/api/v1/auth/me")
            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
            )
            after_logout = await client.get("/api/v1/auth/me")

        assert login.status_code == 200
        assert me.status_code == 200
        assert logout.status_code == 204
        assert after_logout.status_code == 401
        assert len(after_logout.headers.get_list("set-cookie")) == 2

        async with factory() as session:
            stored_session = await session.scalar(
                select(WebSession).where(WebSession.user_id == owner.id)
            )
            stored_proof = await session.scalar(
                select(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id == owner.id)
            )
            assert stored_session is not None
            assert stored_session.revoked_at == NOW
            assert stored_proof is not None
            assert stored_proof.operation == "auth.telegram"
            assert stored_proof.status == "completed"
            assert stored_proof.result_kind == "none"
            forbidden = (session_token.encode(), csrf_token.encode(), proof.encode())
            persisted = (
                stored_session.session_token_hash,
                stored_session.csrf_token_hash,
                stored_proof.idempotency_key_hash,
                stored_proof.request_fingerprint,
            )
            assert all(raw not in persisted for raw in forbidden)
            assert all(len(value) == 32 for value in persisted)
    finally:
        await _delete_owner(factory, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_auth_equivalent_replay_and_future_skew_retention_are_atomic() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = 99_712_102
    owner = await _create_owner(factory, telegram_user_id)
    clock = MutableClock(NOW)
    app = _app(factory, owner_telegram_user_id=telegram_user_id, clock=clock)
    proof = _signed_init_data(telegram_user_id, int(NOW.timestamp()) + 30)
    reordered = "&".join(reversed(proof.split("&"))).replace("%7B", "%7b")
    try:
        async with _client(app) as first_client:
            first = await first_client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": proof},
            )
        clock.value = NOW + timedelta(seconds=300)
        async with _client(app) as replay_client:
            replay = await replay_client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": reordered},
            )

        assert first.status_code == 200
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "telegram_auth_replayed"
        async with factory() as session:
            records = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id == owner.id)
                )
            ).all()
            assert len(records) == 1
            assert records[0].expires_at == NOW + timedelta(seconds=360)
    finally:
        await _delete_owner(factory, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_auth_claim_rolls_back_when_session_creation_fails() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = 99_712_103
    owner = await _create_owner(factory, telegram_user_id)
    clock = MutableClock(NOW)
    duplicated_tokens = OpaqueAuthTokens(_token(21), _token(22))
    first_app = _app(
        factory,
        owner_telegram_user_id=telegram_user_id,
        clock=clock,
        tokens=duplicated_tokens,
    )
    first_proof = _signed_init_data(telegram_user_id, int(NOW.timestamp()))
    second_proof = _signed_init_data(telegram_user_id, int(NOW.timestamp()) + 1)
    try:
        async with _client(first_app) as client:
            first = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": first_proof},
            )
            failed = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": second_proof},
            )
        retry_app = _app(
            factory,
            owner_telegram_user_id=telegram_user_id,
            clock=clock,
            tokens=OpaqueAuthTokens(_token(23), _token(24)),
        )
        async with _client(retry_app) as client:
            retried = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": second_proof},
            )

        assert first.status_code == 200
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        assert retried.status_code == 200
        async with factory() as session:
            records = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id == owner.id)
                )
            ).all()
            assert len(records) == 2
            assert all(record.status == "completed" for record in records)
    finally:
        await _delete_owner(factory, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_http_auth_proof_has_one_session_and_one_replay() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = 99_712_104
    owner = await _create_owner(factory, telegram_user_id)
    app = _app(
        factory,
        owner_telegram_user_id=telegram_user_id,
        clock=MutableClock(NOW),
    )
    proof = _signed_init_data(telegram_user_id, int(NOW.timestamp()))

    async def login() -> int:
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/auth/telegram",
                headers={"Origin": ORIGIN},
                json={"initData": proof},
            )
            return response.status_code

    try:
        statuses = await asyncio.gather(login(), login())

        assert sorted(statuses) == [200, 409]
        async with factory() as session:
            sessions = (
                await session.scalars(select(WebSession).where(WebSession.user_id == owner.id))
            ).all()
            proofs = (
                await session.scalars(
                    select(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id == owner.id)
                )
            ).all()
            assert len(sessions) == 1
            assert len(proofs) == 1
            assert proofs[0].status == "completed"
    finally:
        await _delete_owner(factory, owner)
        await engine.dispose()


@pytest.mark.asyncio
async def test_mutation_session_lock_serializes_logout_and_logout_first_rejects_mutation() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    telegram_user_id = 99_712_105
    owner = await _create_owner(factory, telegram_user_id)
    digester = HttpSecurityDigester(SECURITY_KEY)
    session_token = _token(31)
    csrf_token = _token(32)
    session_digest = digester.session(session_token)
    csrf_digest = digester.csrf(csrf_token)
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            owner.id,
            session_digest,
            csrf_digest,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

    mutation_locked = asyncio.Event()
    release_mutation = asyncio.Event()
    logout_started = asyncio.Event()

    async def mutation_first() -> SessionCheckStatus:
        async with factory.begin() as session:
            checked = await SqlAlchemyHttpSessionAuthenticator(session).lock_for_mutation(
                session_digest,
                csrf_digest,
                now=NOW,
            )
            mutation_locked.set()
            await release_mutation.wait()
            return checked.status

    async def logout_second() -> SessionCheckStatus:
        await mutation_locked.wait()
        async with factory.begin() as session:
            logout_started.set()
            checked = await SqlAlchemyHttpSessionAuthenticator(session).revoke(
                session_digest,
                csrf_digest,
                now=NOW + timedelta(seconds=1),
            )
            return checked.status

    mutation_task = asyncio.create_task(mutation_first())
    logout_task = asyncio.create_task(logout_second())
    await logout_started.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(logout_task), timeout=0.2)
    finally:
        release_mutation.set()

    try:
        assert await mutation_task is SessionCheckStatus.ACTIVE
        assert await logout_task is SessionCheckStatus.ACTIVE
        async with factory.begin() as session:
            after_logout = await SqlAlchemyHttpSessionAuthenticator(session).lock_for_mutation(
                session_digest,
                csrf_digest,
                now=NOW + timedelta(seconds=2),
            )
            assert after_logout.status is SessionCheckStatus.INVALID
    finally:
        await _delete_owner(factory, owner)
        await engine.dispose()
