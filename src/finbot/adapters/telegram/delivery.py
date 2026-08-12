from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import Response, TelegramType

if TYPE_CHECKING:
    from aiogram import Bot


class ReliableDeliveryMiddleware(BaseRequestMiddleware):
    """Retry transient outgoing Telegram requests until delivery or cancellation.

    Business handlers may already have committed an idempotent command before rendering its
    response. Keeping transient delivery failures inside the same handler invocation prevents
    the polling retry from observing the processed-update claim without sending the response.
    Long-poll requests remain owned by the polling loop's separate retry policy.
    """

    def __init__(self, *, maximum_delay: float = 30.0) -> None:
        if maximum_delay <= 0:
            raise ValueError("maximum_delay must be positive")
        self.maximum_delay = maximum_delay

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if isinstance(method, GetUpdates):
            return await make_request(bot, cast(TelegramMethod[TelegramType], method))
        delay = 1.0
        while True:
            try:
                return await make_request(bot, method)
            except asyncio.CancelledError:
                raise
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(max(float(exc.retry_after), 0.0), self.maximum_delay))
            except TelegramNetworkError, TelegramServerError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.maximum_delay)
