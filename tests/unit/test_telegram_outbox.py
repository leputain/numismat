from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.database.services.outbox import (
    queue_edit_message_text,
    queue_send_message,
    serialize_reply_markup,
)
from finbot.adapters.telegram.outbox import deliver_response


def test_inline_keyboard_serialization_is_json_safe_and_schema_bound() -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open", callback_data="safe:1")]]
    )

    payload = serialize_reply_markup(keyboard)

    assert payload == {"inline_keyboard": [[{"text": "Open", "callback_data": "safe:1"}]]}


def test_queue_send_message_rejects_unsafe_method_fields_before_database() -> None:
    session = cast(AsyncSession, object())

    with pytest.raises(ValueError, match="between 1 and 4096"):
        queue_send_message(
            session,
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="",
        )
    with pytest.raises(ValueError, match="parse mode"):
        queue_send_message(
            session,
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="safe",
            parse_mode="MarkdownV2",
        )
    with pytest.raises(ValueError, match="message id"):
        queue_edit_message_text(
            session,
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            message_id=0,
            text="safe",
        )


class _EditBot:
    def __init__(self, error: str) -> None:
        self.error = error
        self.sent = 0

    async def edit_message_text(self, **kwargs: Any) -> None:
        raise TelegramBadRequest(EditMessageText(**kwargs), self.error)

    async def send_message(self, **kwargs: Any) -> Message:
        del kwargs
        self.sent += 1
        return Message(
            message_id=99,
            date=datetime.now(UTC),
            chat=Chat(id=3, type="private"),
        )


def _edit_response() -> TelegramResponseOutbox:
    return TelegramResponseOutbox(
        update_id=1,
        sequence=0,
        owner_telegram_user_id=2,
        chat_id=3,
        method="edit_message_text",
        message_id=4,
        body="Receipt",
        parse_mode="HTML",
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_already_applied_edit_is_considered_delivered() -> None:
    bot = _EditBot("Bad Request: message is not modified")

    await deliver_response(cast(Bot, bot), _edit_response())

    assert bot.sent == 0


@pytest.mark.asyncio
async def test_missing_edit_target_falls_back_to_fresh_receipt() -> None:
    bot = _EditBot("Bad Request: message to edit not found")

    await deliver_response(cast(Bot, bot), _edit_response())

    assert bot.sent == 1


def test_pending_select_waits_for_delivery_lock_instead_of_skipping() -> None:
    from finbot.adapters.database.services.outbox import lock_pending_responses

    source = __import__("inspect").getsource(lock_pending_responses)
    assert ".with_for_update()" in source
    assert "skip_locked" not in source
