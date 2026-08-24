import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.models import (
    Account,
    Category,
    ProcessedUpdate,
    TelegramResponseOutbox,
    Transaction,
    User,
)
from finbot.adapters.database.queries.transactions import export_transaction_details
from finbot.adapters.database.services.outbox import (
    CSV_EXPORT_JOB_BODY,
    queue_csv_export,
)

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.mark.asyncio
async def test_postgres_enforces_privacy_safe_csv_export_job_shape() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        telegram_user_id = 99_600_002
        session.add(
            User(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_user_id,
            )
        )
        session.add(ProcessedUpdate(update_id=99_600_001))
        await session.flush()
        job = queue_csv_export(
            session,
            update_id=99_600_001,
            owner_telegram_user_id=telegram_user_id,
            chat_id=telegram_user_id,
        )
        await session.flush()

        assert (job.method, job.body) == ("send_csv_export", CSV_EXPORT_JOB_BODY)
        assert (
            job.message_id,
            job.parse_mode,
            job.reply_markup,
            job.draft_id,
            job.draft_revision,
            job.history_page,
            job.pending_history_page,
        ) == (None, None, None, None, None, None, None)

        session.add(
            TelegramResponseOutbox(
                update_id=99_600_001,
                sequence=1,
                owner_telegram_user_id=telegram_user_id,
                chat_id=telegram_user_id,
                method="send_csv_export",
                message_id=None,
                body="private-csv-content",
                parse_mode=None,
                reply_markup=None,
                draft_id=None,
                draft_revision=None,
                history_page=None,
                pending_history_page=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_export_query_is_owner_scoped_and_sql_limited() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(telegram_user_id=99_600_011, telegram_chat_id=99_600_011)
        other = User(telegram_user_id=99_600_012, telegram_chat_id=99_600_012)
        session.add_all([owner, other])
        await session.flush()
        owner_account = Account(user_id=owner.id, name="Owner", slug="owner")
        owner_category = Category(
            user_id=owner.id,
            kind="expense",
            name="Owner",
            slug="owner",
        )
        other_account = Account(user_id=other.id, name="Other", slug="other")
        other_category = Category(
            user_id=other.id,
            kind="expense",
            name="Other",
            slug="other",
        )
        session.add_all([owner_account, owner_category, other_account, other_category])
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                Transaction(
                    user_id=owner.id,
                    type="expense",
                    amount_minor=100,
                    currency="RUB",
                    account_id=owner_account.id,
                    category_id=owner_category.id,
                    occurred_at=now,
                    description="owner-first",
                ),
                Transaction(
                    user_id=owner.id,
                    type="expense",
                    amount_minor=200,
                    currency="RUB",
                    account_id=owner_account.id,
                    category_id=owner_category.id,
                    occurred_at=now + timedelta(seconds=1),
                    description="owner-second",
                ),
                Transaction(
                    user_id=other.id,
                    type="expense",
                    amount_minor=300,
                    currency="RUB",
                    account_id=other_account.id,
                    category_id=other_category.id,
                    occurred_at=now - timedelta(seconds=1),
                    description="other-private",
                ),
            ]
        )
        await session.flush()

        rows = await export_transaction_details(session, owner.id, limit=1)

        assert len(rows) == 1
        assert rows[0].description == "owner-first"
        assert "other-private" not in repr(rows)
        with pytest.raises(ValueError, match="positive"):
            await export_transaction_details(session, owner.id, limit=0)
        await session.rollback()
    await engine.dispose()
