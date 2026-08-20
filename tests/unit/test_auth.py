from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from finbot.adapters.telegram.middlewares.auth import OwnerOnlyMiddleware
from finbot.adapters.telegram.principal import (
    TELEGRAM_PRINCIPAL_DATA_KEY,
    TelegramPrincipal,
    telegram_principal_for_message,
)
from finbot.bootstrap import _telegram_mutation_request
from finbot.config import Settings


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=42,
        telegram_allowed_user_ids=(42, 43),
    )


def _private_message(*, actor_id: int, chat_id: int) -> Message:
    return cast(
        Message,
        SimpleNamespace(
            from_user=SimpleNamespace(id=actor_id),
            chat=SimpleNamespace(id=chat_id, type="private"),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_id", [42, 43])
async def test_each_allowlisted_private_actor_gets_an_immutable_scoped_principal(
    actor_id: int,
) -> None:
    middleware = OwnerOnlyMiddleware(_settings())
    event = object()
    observed: TelegramPrincipal | None = None

    async def handler(_event: object, data: dict[str, Any]) -> str:
        nonlocal observed
        principal = data[TELEGRAM_PRINCIPAL_DATA_KEY]
        assert isinstance(principal, TelegramPrincipal)
        observed = principal
        assert (
            telegram_principal_for_message(_private_message(actor_id=actor_id, chat_id=actor_id))
            is principal
        )
        return "ok"

    result = await middleware(
        handler,
        event,
        {
            "event_from_user": SimpleNamespace(id=actor_id),
            "event_chat": SimpleNamespace(id=actor_id, type="private"),
        },
    )

    assert result == "ok"
    assert observed is not None
    assert str(actor_id) not in repr(observed)
    with pytest.raises(FrozenInstanceError):
        observed.chat_id = 99  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor_id", "chat_id", "chat_type"),
    [
        (7, 7, "private"),
        (42, 42, "group"),
        (42, 43, "private"),
    ],
)
async def test_unlisted_group_or_user_chat_mismatch_is_rejected(
    actor_id: int,
    chat_id: int,
    chat_type: str,
) -> None:
    middleware = OwnerOnlyMiddleware(_settings())

    async def handler(_event: object, _data: dict[str, Any]) -> None:
        raise AssertionError("handler must not run")

    assert (
        await middleware(
            handler,
            object(),
            {
                "event_from_user": SimpleNamespace(id=actor_id),
                "event_chat": SimpleNamespace(id=chat_id, type=chat_type),
            },
        )
        is None
    )


@pytest.mark.asyncio
async def test_secondary_callback_context_uses_verified_callback_actor_not_message_author() -> None:
    settings = _settings()
    middleware = OwnerOnlyMiddleware(settings)
    callback_message = _private_message(actor_id=999_999, chat_id=43)
    observed_request = None

    async def handler(_event: object, _data: dict[str, Any]) -> None:
        nonlocal observed_request
        observed_request = _telegram_mutation_request(settings, 101, callback_message)

    await middleware(
        handler,
        object(),
        {
            "event_from_user": SimpleNamespace(id=43),
            "event_chat": SimpleNamespace(id=43, type="private"),
        },
    )

    assert observed_request is not None
    assert observed_request.owner_telegram_user_id == 43
    assert observed_request.chat_id == 43
    with pytest.raises(RuntimeError, match="Verified private Telegram context"):
        telegram_principal_for_message(callback_message)
