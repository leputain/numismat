from collections.abc import Sequence
from typing import cast
from uuid import UUID

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Draft,
    TelegramDraftPresentation,
    TelegramResponseOutbox,
)
from finbot.application.interactions import MAX_PAGE

MAX_TELEGRAM_TEXT_LENGTH = 4096
CSV_EXPORT_JOB_BODY = "csv_export:v1"
_ALLOWED_PARSE_MODES = {None, "HTML"}


def _validate_response(text: str, parse_mode: str | None) -> None:
    if not 1 <= len(text) <= MAX_TELEGRAM_TEXT_LENGTH:
        raise ValueError("Telegram response text must contain between 1 and 4096 characters")
    if parse_mode not in _ALLOWED_PARSE_MODES:
        raise ValueError("Unsupported Telegram response parse mode")


def _validate_optional_history_page(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= MAX_PAGE:
        raise ValueError(f"{name} must be between 0 and {MAX_PAGE}")


def _validate_draft_snapshot(
    draft_id: UUID | None,
    draft_revision: int | None,
    history_page: int | None = None,
    pending_history_page: int | None = None,
) -> None:
    if draft_revision is not None and draft_id is None:
        raise ValueError("Draft revision requires a draft id")
    if draft_revision is not None and (
        isinstance(draft_revision, bool)
        or not isinstance(draft_revision, int)
        or draft_revision < 1
    ):
        raise ValueError("Draft revision must be positive")
    _validate_optional_history_page("History page", history_page)
    _validate_optional_history_page("Pending history page", pending_history_page)
    if (history_page is not None or pending_history_page is not None) and (
        draft_id is None or draft_revision is None
    ):
        raise ValueError("Presentation context requires an exact draft snapshot")


def serialize_reply_markup(
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
) -> dict[str, object] | None:
    """Serialize one validated Telegram keyboard schema into JSON-safe data."""
    if reply_markup is None:
        return None
    if not isinstance(reply_markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        raise TypeError("Unsupported Telegram reply markup")
    payload = reply_markup.model_dump(mode="json", exclude_none=True)
    # ``model_dump(mode='json')`` recursively produces JSON-compatible values.
    return cast(dict[str, object], payload)


def queue_send_message(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
    draft_revision: int | None = None,
    history_page: int | None = None,
    pending_history_page: int | None = None,
    sequence: int = 0,
) -> TelegramResponseOutbox:
    _validate_response(text, parse_mode)
    _validate_draft_snapshot(draft_id, draft_revision, history_page, pending_history_page)
    if sequence < 0:
        raise ValueError("Outbox response sequence must not be negative")
    response = TelegramResponseOutbox(
        update_id=update_id,
        sequence=sequence,
        owner_telegram_user_id=owner_telegram_user_id,
        chat_id=chat_id,
        method="send_message",
        message_id=None,
        body=text,
        parse_mode=parse_mode,
        reply_markup=serialize_reply_markup(reply_markup),
        draft_id=draft_id,
        draft_revision=draft_revision,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )
    session.add(response)
    return response


def queue_edit_message_text(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
    draft_revision: int | None = None,
    history_page: int | None = None,
    pending_history_page: int | None = None,
    sequence: int = 0,
) -> TelegramResponseOutbox:
    _validate_response(text, parse_mode)
    _validate_draft_snapshot(draft_id, draft_revision, history_page, pending_history_page)
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        raise ValueError("Reply keyboards cannot be attached to edited messages")
    if message_id <= 0:
        raise ValueError("Telegram message id must be positive")
    if sequence < 0:
        raise ValueError("Outbox response sequence must not be negative")
    response = TelegramResponseOutbox(
        update_id=update_id,
        sequence=sequence,
        owner_telegram_user_id=owner_telegram_user_id,
        chat_id=chat_id,
        method="edit_message_text",
        message_id=message_id,
        body=text,
        parse_mode=parse_mode,
        reply_markup=serialize_reply_markup(reply_markup),
        draft_id=draft_id,
        draft_revision=draft_revision,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )
    session.add(response)
    return response


def queue_csv_export(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
    sequence: int = 0,
) -> TelegramResponseOutbox:
    if sequence < 0:
        raise ValueError("Outbox response sequence must not be negative")
    response = TelegramResponseOutbox(
        update_id=update_id,
        sequence=sequence,
        owner_telegram_user_id=owner_telegram_user_id,
        chat_id=chat_id,
        method="send_csv_export",
        message_id=None,
        body=CSV_EXPORT_JOB_BODY,
        parse_mode=None,
        reply_markup=None,
        draft_id=None,
        draft_revision=None,
        history_page=None,
        pending_history_page=None,
    )
    session.add(response)
    return response


async def bind_telegram_draft_presentation(
    session: AsyncSession,
    *,
    draft_id: UUID,
    draft_revision: int,
    chat_id: int,
    message_id: int,
    history_page: int | None = None,
    pending_history_page: int | None = None,
) -> bool:
    """Bind a delivered Telegram message to an unchanged draft revision.

    Locking the draft before the upsert makes the revision check and projection
    update one transaction-level operation: a competing draft mutation either
    commits first and makes this snapshot stale, or waits until this binding is
    durable. The conflict predicate prevents an older delivery from decreasing the
    last rendered revision.
    """
    _validate_draft_snapshot(draft_id, draft_revision, history_page, pending_history_page)
    if chat_id == 0:
        raise ValueError("Telegram chat id must not be zero")
    if message_id <= 0:
        raise ValueError("Telegram message id must be positive")

    current_draft_id = await session.scalar(
        select(Draft.id)
        .where(Draft.id == draft_id, Draft.revision == draft_revision)
        .with_for_update()
    )
    if current_draft_id is None:
        return False

    statement = insert(TelegramDraftPresentation).values(
        draft_id=draft_id,
        chat_id=chat_id,
        message_id=message_id,
        rendered_revision=draft_revision,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[TelegramDraftPresentation.draft_id],
        set_={
            "chat_id": statement.excluded.chat_id,
            "message_id": statement.excluded.message_id,
            "rendered_revision": statement.excluded.rendered_revision,
            "history_page": statement.excluded.history_page,
            "pending_history_page": statement.excluded.pending_history_page,
            "updated_at": func.now(),
        },
        where=(TelegramDraftPresentation.rendered_revision <= statement.excluded.rendered_revision),
    )
    bound_draft_id = await session.scalar(statement.returning(TelegramDraftPresentation.draft_id))
    return bound_draft_id is not None


async def lock_pending_responses(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
) -> Sequence[TelegramResponseOutbox]:
    """Lock pending rows so parallel replays cannot both start delivery."""
    result = await session.scalars(
        select(TelegramResponseOutbox)
        .where(
            TelegramResponseOutbox.update_id == update_id,
            TelegramResponseOutbox.owner_telegram_user_id == owner_telegram_user_id,
            TelegramResponseOutbox.chat_id == chat_id,
            TelegramResponseOutbox.sent_at.is_(None),
        )
        .order_by(TelegramResponseOutbox.sequence)
        .with_for_update()
    )
    return result.all()
