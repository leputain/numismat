from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates, SendMessage

from finbot.adapters.telegram.delivery import ReliableDeliveryMiddleware


@pytest.mark.asyncio
async def test_transient_delivery_is_retried_in_same_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method = SendMessage(chat_id=1, text="synthetic")
    attempts = 0
    delays: list[float] = []

    async def request(_bot: Bot, _method: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TelegramNetworkError(method, "synthetic network failure")
        return "delivered"

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("finbot.adapters.telegram.delivery.asyncio.sleep", no_wait)
    middleware = ReliableDeliveryMiddleware(maximum_delay=5)

    result = await middleware(request, cast(Bot, object()), method)  # type: ignore[arg-type]

    assert result == "delivered"
    assert attempts == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_long_poll_failure_is_left_to_polling_policy() -> None:
    method = GetUpdates(timeout=30)
    attempts = 0

    async def request(_bot: Bot, _method: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise TelegramNetworkError(method, "synthetic network failure")

    middleware = ReliableDeliveryMiddleware()
    with pytest.raises(TelegramNetworkError):
        await middleware(request, cast(Bot, object()), method)  # type: ignore[arg-type]
    assert attempts == 1
