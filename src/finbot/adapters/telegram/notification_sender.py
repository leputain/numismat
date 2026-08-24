from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from finbot.application.notifications import NotificationSendFailure


class AiogramNotificationSender:
    """Send only caller-selected static text and classify Telegram failures safely."""

    __slots__ = ("_bot",)

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, *, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError) as exc:
            raise NotificationSendFailure(retryable=True) from exc
        except TelegramAPIError as exc:
            raise NotificationSendFailure(retryable=False) from exc
