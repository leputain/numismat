from __future__ import annotations

import base64
import os
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Message, Update
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    HttpRevisionMutationService,
)
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.bootstrap import build_dispatcher
from finbot.config import Settings

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
OWNER_TELEGRAM_USER_ID = 9_000_000_020
BOT_ID = 123_456
UPDATE_BASE = 983_000_000
ORIGIN = "https://miniapp.cross-channel.integration.test"
HTTP_SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class _ReadinessStub:
    async def check(self) -> None:
        return None


class _TelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.last_message_id = 4_000

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        del timeout
        method_name = type(method).__name__
        if method_name == "AnswerCallbackQuery":
            return True
        if method_name in {"SendMessage", "EditMessageText", "SendDocument"}:
            typed_method = cast(Any, method)
            if method_name != "EditMessageText":
                self.last_message_id += 1
            message_id = (
                self.last_message_id
                if method_name != "EditMessageText"
                else int(typed_method.message_id)
            )
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 1_786_093_200,
                    "chat": {"id": OWNER_TELEGRAM_USER_ID, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                    "text": str(getattr(typed_method, "text", "")),
                },
                context={"bot": bot},
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65_536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        del url, headers, timeout, chunk_size, raise_for_status
        if False:
            yield b""


def _message_update(bot: Bot, update_id: int, message_id: int, value: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": 1_786_093_200,
                "chat": {"id": OWNER_TELEGRAM_USER_ID, "type": "private"},
                "from": {
                    "id": OWNER_TELEGRAM_USER_ID,
                    "is_bot": False,
                    "first_name": "Owner",
                },
                "text": value,
            },
        },
        context={"bot": bot},
    )


def _callback_update(bot: Bot, update_id: int, message_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {
                    "id": OWNER_TELEGRAM_USER_ID,
                    "is_bot": False,
                    "first_name": "Owner",
                },
                "chat_instance": "miniapp-cross-channel",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": 1_786_093_200,
                    "chat": {"id": OWNER_TELEGRAM_USER_ID, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                    "text": "Finbot screen",
                },
            },
        },
        context={"bot": bot},
    )


def _opaque_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


async def _cleanup(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        owner_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_user_id"),
            {"telegram_user_id": OWNER_TELEGRAM_USER_ID},
        )
        if owner_id is not None:
            await session.execute(
                text("DELETE FROM http_idempotency WHERE user_id = :owner_id"),
                {"owner_id": owner_id},
            )
            await session.execute(
                text("DELETE FROM web_sessions WHERE user_id = :owner_id"),
                {"owner_id": owner_id},
            )
            await session.execute(
                text("UPDATE users SET default_account_id = NULL WHERE id = :owner_id"),
                {"owner_id": owner_id},
            )
            for table_name in (
                "audit_events",
                "category_rules",
                "drafts",
                "transactions",
                "categories",
                "accounts",
            ):
                await session.execute(
                    text(f"DELETE FROM {table_name} WHERE user_id = :owner_id"),
                    {"owner_id": owner_id},
                )
            await session.execute(
                text("DELETE FROM users WHERE id = :owner_id"),
                {"owner_id": owner_id},
            )
        await session.execute(
            text(
                "DELETE FROM telegram_response_outbox "
                "WHERE update_id BETWEEN :first_update AND :last_update"
            ),
            {"first_update": UPDATE_BASE, "last_update": UPDATE_BASE + 99},
        )
        await session.execute(
            text(
                "DELETE FROM processed_updates "
                "WHERE update_id BETWEEN :first_update AND :last_update"
            ),
            {"first_update": UPDATE_BASE, "last_update": UPDATE_BASE + 99},
        )


async def _draft(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str, int, bool]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT drafts.id, drafts.state, drafts.revision, drafts.suspended "
                    "FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_user_id"
                ),
                {"telegram_user_id": OWNER_TELEGRAM_USER_ID},
            )
        ).one()
    return row.id, row.state, row.revision, row.suspended


async def _presentation_message_id(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        message_id = await session.scalar(
            text(
                "SELECT presentation.message_id "
                "FROM telegram_draft_presentations AS presentation "
                "JOIN drafts ON drafts.id = presentation.draft_id "
                "JOIN users ON users.id = drafts.user_id "
                "WHERE users.telegram_user_id = :telegram_user_id "
                "AND presentation.rendered_revision = drafts.revision"
            ),
            {"telegram_user_id": OWNER_TELEGRAM_USER_ID},
        )
    assert message_id is not None
    return int(message_id)


@pytest.mark.asyncio
async def test_draft_handoff_is_cross_channel_and_mobile_retry_is_idempotent() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_TELEGRAM_USER_ID,
            database_url=DATABASE_URL,
        )
    )
    now = datetime.now(UTC)
    digester = HttpSecurityDigester(HTTP_SECURITY_KEY)
    session_token = _opaque_token()
    csrf_token = _opaque_token()

    try:
        await dispatcher.feed_update(
            bot,
            _message_update(bot, UPDATE_BASE, 1, "778 ресторан"),
        )
        telegram_draft_id, state, telegram_revision, suspended = await _draft(factory)
        assert (state, suspended) == ("review", False)

        async with factory.begin() as session:
            owner_id = await session.scalar(
                text("SELECT id FROM users WHERE telegram_user_id = :telegram_user_id"),
                {"telegram_user_id": OWNER_TELEGRAM_USER_ID},
            )
            assert owner_id is not None
            await SqlAlchemyWebSessionRepository(session).create(
                owner_id,
                digester.session(session_token),
                digester.csrf(csrf_token),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )

        executor = HttpMutationExecutor(
            digester=digester,
            uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(factory),
            clock=lambda: now,
        )
        app = create_app(
            readiness_probe=_ReadinessStub(),
            mutation_service=HttpRevisionMutationService(executor),
            mutation_origin=ORIGIN,
        )
        cookie = f"{SESSION_COOKIE}={session_token}; {CSRF_COOKIE}={csrf_token}"

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=ORIGIN,
        ) as client:
            active = await client.get(
                "/api/v1/drafts/active",
                headers={"Cookie": cookie},
            )
            assert active.status_code == 200
            assert active.json()["draft"]["id"] == str(telegram_draft_id)

            confirm_headers = {
                "Cookie": cookie,
                "Idempotency-Key": _opaque_token(),
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf_token,
            }
            confirm_path = f"/api/v1/drafts/{telegram_draft_id}/confirm"
            confirmed = await client.post(
                confirm_path,
                headers=confirm_headers,
                json={"revision": telegram_revision},
            )
            retried_confirm = await client.post(
                confirm_path,
                headers=confirm_headers,
                json={"revision": telegram_revision},
            )
            assert confirmed.status_code == retried_confirm.status_code == 201
            assert confirmed.json() == retried_confirm.json()

            create_headers = {
                "Cookie": cookie,
                "Idempotency-Key": _opaque_token(),
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf_token,
            }
            created = await client.post("/api/v1/drafts", headers=create_headers, json={})
            retried_create = await client.post(
                "/api/v1/drafts",
                headers=create_headers,
                json={},
            )
            assert created.status_code == retried_create.status_code == 201
            assert created.json() == retried_create.json()
            miniapp_draft_id = UUID(created.json()["result"]["draft_id"])

        await dispatcher.feed_update(
            bot,
            _message_update(bot, UPDATE_BASE + 1, 2, "/menu"),
        )
        draft_id, state, revision, suspended = await _draft(factory)
        assert draft_id == miniapp_draft_id
        assert (state, suspended) == ("wizard_type", True)

        resume = DraftInteraction(
            action=DraftAction.RESUME,
            draft_id=draft_id,
            revision=revision,
        ).encode()
        await dispatcher.feed_update(
            bot,
            _callback_update(
                bot,
                UPDATE_BASE + 2,
                await _presentation_message_id(factory),
                resume,
            ),
        )
        resumed_id, resumed_state, resumed_revision, resumed_suspended = await _draft(factory)
        assert resumed_id == miniapp_draft_id
        assert resumed_state == "wizard_type"
        assert resumed_revision == revision + 1
        assert resumed_suspended is False

        async with factory() as verification:
            transaction_count = await verification.scalar(
                text(
                    "SELECT count(*) FROM transactions JOIN users "
                    "ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :telegram_user_id"
                ),
                {"telegram_user_id": OWNER_TELEGRAM_USER_ID},
            )
        assert transaction_count == 1
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()
