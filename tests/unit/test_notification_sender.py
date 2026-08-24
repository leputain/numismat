from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from finbot.adapters.telegram.notification_sender import AiogramNotificationSender
from finbot.application.notifications import NotificationSendFailure


class _Bot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_sender_classifies_retryable_errors_without_copying_telegram_text() -> None:
    bot = _Bot(TelegramNetworkError(method=object(), message="untrusted details"))  # type: ignore[arg-type]

    with pytest.raises(NotificationSendFailure) as captured:
        await AiogramNotificationSender(bot).send(chat_id=42, text="static")  # type: ignore[arg-type]

    assert captured.value.retryable
    assert "untrusted" not in str(captured.value)


@pytest.mark.asyncio
async def test_sender_classifies_api_rejection_as_terminal() -> None:
    bot = _Bot(TelegramBadRequest(method=object(), message="untrusted details"))  # type: ignore[arg-type]

    with pytest.raises(NotificationSendFailure) as captured:
        await AiogramNotificationSender(bot).send(chat_id=42, text="static")  # type: ignore[arg-type]

    assert not captured.value.retryable
