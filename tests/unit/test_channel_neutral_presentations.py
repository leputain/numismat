from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from inspect import getsource
from types import TracebackType
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid7

import pytest
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import (
    TelegramDraftPresentation,
    TelegramResponseOutbox,
)
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    queue_send_message,
)
from finbot.adapters.telegram import outbox as telegram_outbox
from finbot.application.interactions import MAX_PAGE


class _CaptureSession:
    def __init__(self) -> None:
        self.added: object | None = None

    def add(self, value: object) -> None:
        self.added = value


def test_projection_model_and_outbox_snapshot_are_adapter_specific() -> None:
    projection = TelegramDraftPresentation.__table__
    constraints = {constraint.name for constraint in projection.constraints}

    assert set(projection.c.keys()) == {
        "draft_id",
        "chat_id",
        "message_id",
        "rendered_revision",
        "history_page",
        "pending_history_page",
        "created_at",
        "updated_at",
    }
    assert "uq_telegram_draft_presentations_chat_message" in constraints
    assert TelegramResponseOutbox.__table__.c.draft_revision.nullable
    assert TelegramResponseOutbox.__table__.c.history_page.nullable
    assert TelegramResponseOutbox.__table__.c.pending_history_page.nullable


def test_queue_captures_revision_without_requiring_it_during_rolling_deploy() -> None:
    capture = _CaptureSession()
    draft_id = uuid7()

    response = queue_send_message(
        cast(AsyncSession, capture),
        update_id=1,
        owner_telegram_user_id=2,
        chat_id=3,
        text="Safe receipt",
        draft_id=draft_id,
        draft_revision=4,
        history_page=7,
        pending_history_page=11,
    )

    assert capture.added is response
    assert response.draft_id == draft_id
    assert response.draft_revision == 4
    assert response.history_page == 7
    assert response.pending_history_page == 11

    legacy = queue_send_message(
        cast(AsyncSession, capture),
        update_id=2,
        owner_telegram_user_id=2,
        chat_id=3,
        text="Rolling-compatible receipt",
        draft_id=draft_id,
    )
    assert legacy.draft_revision is None
    assert legacy.history_page is None
    assert legacy.pending_history_page is None


@pytest.mark.parametrize("draft_revision", [0, -1])
def test_queue_rejects_invalid_draft_snapshots(draft_revision: int) -> None:
    with pytest.raises(ValueError, match="Draft revision"):
        queue_send_message(
            cast(AsyncSession, _CaptureSession()),
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="Safe receipt",
            draft_id=uuid7(),
            draft_revision=draft_revision,
        )


@pytest.mark.parametrize("field", ["history_page", "pending_history_page"])
@pytest.mark.parametrize("value", [-1, MAX_PAGE + 1])
def test_queue_rejects_out_of_range_presentation_context(field: str, value: int) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match="must be between"):
        queue_send_message(
            cast(AsyncSession, _CaptureSession()),
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="Safe receipt",
            draft_id=uuid7(),
            draft_revision=1,
            **kwargs,
        )


@pytest.mark.parametrize("field", ["history_page", "pending_history_page"])
def test_queue_rejects_non_integer_presentation_context(field: str) -> None:
    kwargs = {field: True}
    with pytest.raises(TypeError, match="must be an integer"):
        queue_send_message(
            cast(AsyncSession, _CaptureSession()),
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="Safe receipt",
            draft_id=uuid7(),
            draft_revision=1,
            **kwargs,
        )


def test_queue_requires_exact_draft_for_presentation_context() -> None:
    with pytest.raises(ValueError, match="requires an exact draft snapshot"):
        queue_send_message(
            cast(AsyncSession, _CaptureSession()),
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="Safe receipt",
            history_page=1,
        )

    with pytest.raises(ValueError, match="requires a draft id"):
        queue_send_message(
            cast(AsyncSession, _CaptureSession()),
            update_id=1,
            owner_telegram_user_id=2,
            chat_id=3,
            text="Safe receipt",
            draft_revision=1,
        )


@pytest.mark.asyncio
async def test_binding_is_conditional_and_uses_a_monotonic_upsert() -> None:
    draft_id = uuid7()
    session = AsyncMock(spec=AsyncSession)
    session.scalar.side_effect = [None]

    assert not await bind_telegram_draft_presentation(
        session,
        draft_id=draft_id,
        draft_revision=2,
        chat_id=3,
        message_id=4,
    )
    assert session.scalar.await_count == 1

    source = getsource(bind_telegram_draft_presentation)
    assert ".with_for_update()" in source
    assert "rendered_revision" in source
    assert "<= statement.excluded.rendered_revision" in source


class _TransactionContext(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _SessionContext(AbstractAsyncContextManager[AsyncSession]):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _SessionWithBegin:
    def begin(self) -> _TransactionContext:
        return _TransactionContext()


@pytest.mark.asyncio
async def test_delivery_binds_snapshot_without_mutating_draft_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = uuid7()
    response = TelegramResponseOutbox(
        update_id=1,
        sequence=0,
        owner_telegram_user_id=2,
        chat_id=3,
        method="send_message",
        message_id=None,
        body="Safe receipt",
        parse_mode=None,
        reply_markup=None,
        draft_id=draft_id,
        draft_revision=5,
        history_page=8,
        pending_history_page=13,
    )
    session = cast(AsyncSession, _SessionWithBegin())
    lock = AsyncMock(return_value=[response])
    deliver = AsyncMock(return_value=44)
    bind = AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_outbox, "lock_pending_responses", lock)
    monkeypatch.setattr(telegram_outbox, "deliver_response", deliver)
    monkeypatch.setattr(telegram_outbox, "bind_telegram_draft_presentation", bind)

    def sessions() -> _SessionContext:
        return _SessionContext(session)

    delivered = await telegram_outbox.deliver_pending_responses(
        cast(async_sessionmaker[AsyncSession], sessions),
        cast(Bot, object()),
        update_id=1,
        owner_telegram_user_id=2,
        chat_id=3,
    )

    assert delivered == 1
    bind.assert_awaited_once_with(
        session,
        draft_id=draft_id,
        draft_revision=5,
        chat_id=3,
        message_id=44,
        history_page=8,
        pending_history_page=13,
    )
    assert response.sent_at is not None
    assert response.sent_at <= datetime.now(UTC)
    delivery_source = getsource(telegram_outbox.deliver_pending_responses)
    assert "payload" not in delivery_source
    assert "presentation_ref" not in delivery_source
