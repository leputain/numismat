from types import SimpleNamespace

import pytest

from finbot.adapters.telegram.middlewares.auth import OwnerOnlyMiddleware
from finbot.config import Settings


@pytest.mark.asyncio
async def test_owner_private_chat_is_allowed():
    middleware = OwnerOnlyMiddleware(
        Settings(telegram_bot_token="123456:synthetic_test_token", owner_telegram_user_id=42)
    )
    event = object()
    called = False

    async def handler(_event, _data):
        nonlocal called
        called = True
        return "ok"

    result = await middleware(
        handler,
        event,
        {"event_from_user": SimpleNamespace(id=42), "event_chat": SimpleNamespace(type="private")},
    )
    assert result == "ok"
    assert called


@pytest.mark.asyncio
async def test_foreign_or_group_update_is_rejected():
    middleware = OwnerOnlyMiddleware(
        Settings(telegram_bot_token="123456:synthetic_test_token", owner_telegram_user_id=42)
    )

    async def handler(_event, _data):
        raise AssertionError("handler must not run")

    assert (
        await middleware(
            handler,
            object(),
            {
                "event_from_user": SimpleNamespace(id=7),
                "event_chat": SimpleNamespace(type="private"),
            },
        )
        is None
    )
    assert (
        await middleware(
            handler,
            object(),
            {
                "event_from_user": SimpleNamespace(id=42),
                "event_chat": SimpleNamespace(type="group"),
            },
        )
        is None
    )
