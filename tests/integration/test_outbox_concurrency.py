import asyncio
import os
from uuid import uuid7

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import ProcessedUpdate, TelegramResponseOutbox
from finbot.adapters.database.services.outbox import lock_pending_responses

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
UPDATE_ID = 987_000_001
OWNER_ID = 8_700_000_001


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
