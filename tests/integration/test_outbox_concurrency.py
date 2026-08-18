import asyncio
import os
from uuid import uuid7

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import (
    Draft,
    ProcessedUpdate,
    TelegramDraftPresentation,
    TelegramResponseOutbox,
    User,
)
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
    lock_telegram_draft_presentation_context,
)
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    lock_pending_responses,
    queue_send_message,
)
from finbot.application.dto import DraftRef

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
UPDATE_ID = 987_000_001
OWNER_ID = 8_700_000_001


@pytest.mark.asyncio
async def test_presentation_binding_rejects_stale_draft_and_never_regresses_revision() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=OWNER_ID + 1, telegram_chat_id=OWNER_ID + 1)
        session.add(user)
        await session.flush()
        draft = Draft(user_id=user.id, state="review", payload={}, revision=1)
        session.add(draft)
        await session.flush()

        assert await bind_telegram_draft_presentation(
            session,
            draft_id=draft.id,
            draft_revision=1,
            chat_id=OWNER_ID + 1,
            message_id=101,
            history_page=4,
            pending_history_page=9,
        )
        draft.revision = 2
        await session.flush()

        assert not await bind_telegram_draft_presentation(
            session,
            draft_id=draft.id,
            draft_revision=1,
            chat_id=OWNER_ID + 1,
            message_id=102,
            history_page=40,
            pending_history_page=90,
        )
        assert await bind_telegram_draft_presentation(
            session,
            draft_id=draft.id,
            draft_revision=2,
            chat_id=OWNER_ID + 1,
            message_id=103,
            history_page=6,
            pending_history_page=10,
        )
        presentation = await session.get(TelegramDraftPresentation, draft.id)
        assert presentation is not None
        await session.refresh(presentation)
        assert presentation.message_id == 103
        assert presentation.rendered_revision == 2
        assert presentation.history_page == 6
        assert presentation.pending_history_page == 10

        context = await lock_telegram_draft_presentation_context(
            session,
            user.id,
            DraftRef(draft.id, 2),
            OWNER_ID + 1,
            103,
        )
        assert context == TelegramDraftPresentationContext(6, 10)

        # Even inconsistent legacy state cannot make the projection move backward.
        presentation.rendered_revision = 3
        await session.flush()
        assert not await bind_telegram_draft_presentation(
            session,
            draft_id=draft.id,
            draft_revision=2,
            chat_id=OWNER_ID + 1,
            message_id=104,
            history_page=60,
            pending_history_page=100,
        )
        await session.refresh(presentation)
        assert presentation.message_id == 103
        assert presentation.rendered_revision == 3
        assert presentation.history_page == 6
        assert presentation.pending_history_page == 10
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_historical_outbox_context_does_not_block_later_draft_deletion() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    update_id = UPDATE_ID + 1
    telegram_id = OWNER_ID + 2
    async with factory() as session:
        user = User(telegram_user_id=telegram_id, telegram_chat_id=telegram_id)
        session.add_all((ProcessedUpdate(update_id=update_id), user))
        await session.flush()
        draft = Draft(user_id=user.id, state="edit_menu", payload={}, revision=1)
        session.add(draft)
        await session.flush()
        response = queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=telegram_id,
            chat_id=telegram_id,
            text="Synthetic receipt",
            draft_id=draft.id,
            draft_revision=1,
            history_page=4,
            pending_history_page=9,
        )
        await session.commit()

        await session.delete(draft)
        await session.commit()
        await session.refresh(response)
        assert response.draft_id is None
        assert response.draft_revision == 1
        assert response.history_page == 4
        assert response.pending_history_page == 9

        await session.delete(response)
        processed = await session.get(ProcessedUpdate, update_id)
        assert processed is not None
        await session.delete(processed)
        await session.delete(user)
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_second_delivery_waits_then_recovers_pending_after_first_rolls_back() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    row_id = uuid7()
    async with factory() as setup:
        setup.add(ProcessedUpdate(update_id=UPDATE_ID))
        setup.add(
            TelegramResponseOutbox(
                id=row_id,
                update_id=UPDATE_ID,
                sequence=0,
                owner_telegram_user_id=OWNER_ID,
                chat_id=OWNER_ID,
                method="send_message",
                body="Synthetic receipt",
                parse_mode=None,
                reply_markup=None,
            )
        )
        await setup.commit()

    first_locked = asyncio.Event()
    allow_rollback = asyncio.Event()
    second_started = asyncio.Event()
    second_finished = asyncio.Event()

    async def failing_delivery() -> None:
        async with factory() as session:
            rows = await lock_pending_responses(
                session,
                update_id=UPDATE_ID,
                owner_telegram_user_id=OWNER_ID,
                chat_id=OWNER_ID,
            )
            assert [row.id for row in rows] == [row_id]
            first_locked.set()
            await allow_rollback.wait()
            await session.rollback()

    async def recovering_delivery() -> None:
        await first_locked.wait()
        async with factory() as session:
            second_started.set()
            rows = await lock_pending_responses(
                session,
                update_id=UPDATE_ID,
                owner_telegram_user_id=OWNER_ID,
                chat_id=OWNER_ID,
            )
            assert [row.id for row in rows] == [row_id]
            second_finished.set()
            await session.rollback()

    first = asyncio.create_task(failing_delivery())
    second = asyncio.create_task(recovering_delivery())
    try:
        await asyncio.wait_for(second_started.wait(), timeout=2)
        await asyncio.sleep(0.1)
        assert not second_finished.is_set()
        allow_rollback.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=4)
        assert second_finished.is_set()
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        async with factory() as cleanup:
            await cleanup.execute(
                delete(TelegramResponseOutbox).where(TelegramResponseOutbox.update_id == UPDATE_ID)
            )
            await cleanup.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.update_id == UPDATE_ID)
            )
            await cleanup.commit()
        await engine.dispose()
