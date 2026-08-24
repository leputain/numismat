import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid7

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Message, Update
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.bootstrap import build_dispatcher
from finbot.config import Settings

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
OWNER_ID = 900000002
BOT_ID = 123456
UPDATE_BASE = 980000000


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.last_message_id = 1000

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        del timeout
        method_name = type(method).__name__
        if method_name in {"SendMessage", "EditMessageText", "SendDocument"}:
            if method_name != "EditMessageText":
                self.last_message_id += 1
            message_id = (
                self.last_message_id if method_name != "EditMessageText" else int(method.message_id)
            )
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 1786093200,
                    "chat": {"id": OWNER_ID, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                    "text": str(getattr(method, "text", "")),
                },
                context={"bot": bot},
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        del url, headers, timeout, chunk_size, raise_for_status
        if False:
            yield b""


def _message_update(update_id: int, message_id: int, value: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1786093200,
            "chat": {"id": OWNER_ID, "type": "private"},
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "text": value,
        },
    }


def _callback_update(update_id: int, message_id: int, callback_data: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "test-chat",
            "data": callback_data,
            "message": {
                "message_id": message_id,
                "date": 1786093200,
                "chat": {"id": OWNER_ID, "type": "private"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                "text": "Finbot screen",
            },
        },
    }


async def _cleanup_database(factory: async_sessionmaker[Any]) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM telegram_response_outbox WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_BASE, "last": UPDATE_BASE + 100},
        )
        await session.execute(
            text("DELETE FROM processed_updates WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_BASE, "last": UPDATE_BASE + 100},
        )
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        if user_id is not None:
            await session.execute(
                text("UPDATE users SET default_account_id = NULL WHERE id = :user_id"),
                {"user_id": user_id},
            )
            for table_name in (
                "audit_events",
                "category_rules",
                "drafts",
                "transactions",
                "categories",
                "accounts",
            ):
                await session.execute(
                    text(f"DELETE FROM {table_name} WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
            await session.execute(
                text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
            )
        await session.commit()


async def _draft_callback_data(
    factory: async_sessionmaker[Any],
    action: DraftAction,
    *,
    page: int | None = None,
    object_id: UUID | None = None,
    object_table: str | None = None,
) -> str:
    async with factory() as session:
        draft_id, revision = (
            await session.execute(
                text(
                    "SELECT drafts.id, drafts.revision FROM drafts "
                    "JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id"
                ),
                {"telegram_id": OWNER_ID},
            )
        ).one()
        object_version = None
        if object_id is not None:
            if object_table not in {"accounts", "categories"}:
                raise AssertionError("object_table must be an allowlisted catalog")
            object_version = await session.scalar(
                text(f"SELECT version FROM {object_table} WHERE id = :object_id"),
                {"object_id": object_id},
            )
    return DraftInteraction(
        action=action,
        draft_id=draft_id,
        revision=revision,
        page=page,
        object_id=object_id,
        object_version=object_version,
    ).encode()


@pytest.mark.asyncio
async def test_discard_rejects_replay_from_a_non_current_telegram_message() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup_database(factory)
    fake_session = FakeTelegramSession()
    bot = Bot("123456:synthetic_test_token", session=fake_session)
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=OWNER_ID,
        database_url=DATABASE_URL,
    )
    dispatcher = build_dispatcher(settings)

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE, 1, "/wizard"),
                context={"bot": bot},
            ),
        )
        wizard_message_id = fake_session.last_message_id
        discard = await _draft_callback_data(factory, DraftAction.DISCARD)

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 1,
                    wizard_message_id + 999,
                    discard,
                ),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts "
                    "JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id"
                ),
                {"telegram_id": OWNER_ID},
            )
        assert draft_count == 1

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_BASE + 2, wizard_message_id, discard),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts "
                    "JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id"
                ),
                {"telegram_id": OWNER_ID},
            )
        assert draft_count == 0
    finally:
        await _cleanup_database(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_wizard_back_returns_one_step_and_keeps_upstream_data() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup_database(factory)
    fake_session = FakeTelegramSession()
    bot = Bot("123456:synthetic_test_token", session=fake_session)
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=OWNER_ID,
        database_url=DATABASE_URL,
    )
    dispatcher = build_dispatcher(settings)

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_message_update(UPDATE_BASE, 1, "/wizard"), context={"bot": bot}),
        )
        wizard_message_id = fake_session.last_message_id
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 1,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.SELECT_TYPE, page=0),
                ),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 2, 2, "1450"), context={"bot": bot}
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 3,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.BACK),
                ),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            user_id = await session.scalar(
                text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
                {"telegram_id": OWNER_ID},
            )
            state, payload = (
                await session.execute(
                    text("SELECT state, payload FROM drafts WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
            ).one()
            assert state == "wizard_amount"
            assert payload["type"] == "expense"
            assert "amount" not in payload
            category_id = await session.scalar(
                text(
                    "SELECT id FROM categories "
                    "WHERE user_id = :user_id AND kind = 'expense' ORDER BY name LIMIT 1"
                ),
                {"user_id": user_id},
            )
            account_id = await session.scalar(
                text("SELECT id FROM accounts WHERE user_id = :user_id ORDER BY name LIMIT 1"),
                {"user_id": user_id},
            )

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 4, 3, "1700"), context={"bot": bot}
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 5,
                    wizard_message_id,
                    await _draft_callback_data(
                        factory,
                        DraftAction.SELECT_CATEGORY,
                        object_id=category_id,
                        object_table="categories",
                    ),
                ),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 6,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.BACK),
                ),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            state = await session.scalar(
                text("SELECT state FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert state == "wizard_category"

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 7,
                    wizard_message_id,
                    await _draft_callback_data(
                        factory,
                        DraftAction.SELECT_CATEGORY,
                        object_id=category_id,
                        object_table="categories",
                    ),
                ),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 8,
                    wizard_message_id,
                    await _draft_callback_data(
                        factory,
                        DraftAction.SELECT_ACCOUNT,
                        object_id=account_id,
                        object_table="accounts",
                    ),
                ),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 9,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.BACK),
                ),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            state, payload = (
                await session.execute(
                    text("SELECT state, payload FROM drafts WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
            ).one()
            assert state == "wizard_account"
            assert payload["amount_minor"] == 170000
            assert "amount" not in payload
            assert payload["category_id"] == str(category_id)
    finally:
        await _cleanup_database(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_settings_manage_accounts_and_categories_persistently() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup_database(factory)
    fake_session = FakeTelegramSession()
    bot = Bot("123456:synthetic_test_token", session=fake_session)
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=OWNER_ID,
        database_url=DATABASE_URL,
    )
    dispatcher = build_dispatcher(settings)

    async def callback(offset: int, data: str, message_id: int) -> None:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_BASE + offset, message_id, data),
                context={"bot": bot},
            ),
        )

    async def message(offset: int, value: str) -> None:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + offset, offset, value),
                context={"bot": bot},
            ),
        )

    try:
        await message(0, "/settings")
        settings_message_id = fake_session.last_message_id
        await callback(1, "s:accounts", settings_message_id)
        async with factory() as session:
            user_id = await session.scalar(
                text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
                {"telegram_id": OWNER_ID},
            )
            await session.execute(
                text(
                    "INSERT INTO drafts (id, user_id, state, payload) "
                    "VALUES (:id, :user_id, 'wizard_confirm', '{}'::jsonb)"
                ),
                {"id": uuid7(), "user_id": user_id},
            )
            await session.commit()
        await callback(50, "sa:new", settings_message_id)
        async with factory() as session:
            protected_state = await session.scalar(
                text("SELECT state FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert protected_state == "wizard_confirm"
            await session.execute(
                text("DELETE FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            await session.commit()

        await callback(2, "sa:new", settings_message_id)
        await message(3, "Наличные")

        async with factory() as session:
            account = (
                await session.execute(
                    text(
                        "SELECT id, version FROM accounts "
                        "WHERE user_id = :user_id AND name = 'Наличные'"
                    ),
                    {"user_id": user_id},
                )
            ).one()
            account_id = account.id
            draft_count = await session.scalar(
                text("SELECT count(*) FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert account_id is not None
            assert draft_count == 0

        await callback(4, f"sa:rename:{account_id}:{account.version}", settings_message_id)
        await message(5, "Кошелёк")
        async with factory() as session:
            renamed_account_version = int(
                await session.scalar(
                    text("SELECT version FROM accounts WHERE id = :account_id"),
                    {"account_id": account_id},
                )
            )
        await callback(
            6,
            f"sa:archive:ask:{account_id}:{renamed_account_version}",
            settings_message_id,
        )
        await callback(
            7,
            f"sa:archive:do:{account_id}:{renamed_account_version}",
            settings_message_id,
        )
        await callback(8, "sa:archived", settings_message_id)
        await callback(
            9,
            f"sa:restore:{account_id}:{renamed_account_version + 1}",
            settings_message_id,
        )

        async with factory() as session:
            account_row = (
                await session.execute(
                    text(
                        "SELECT name, archived_at FROM accounts "
                        "WHERE id = :account_id AND user_id = :user_id"
                    ),
                    {"account_id": account_id, "user_id": user_id},
                )
            ).one()
            assert account_row.name == "Кошелёк"
            assert account_row.archived_at is None

        await callback(10, "sc:root", settings_message_id)
        await callback(11, "sc:list:expense", settings_message_id)
        await callback(12, "sc:new:expense", settings_message_id)
        await message(13, "Питомцы")
        async with factory() as session:
            category = (
                await session.execute(
                    text(
                        "SELECT id, version FROM categories "
                        "WHERE user_id = :user_id AND kind = 'expense' AND name = 'Питомцы'"
                    ),
                    {"user_id": user_id},
                )
            ).one()
            category_id = category.id

        await callback(
            14,
            f"sc:rename:{category_id}:{category.version}",
            settings_message_id,
        )
        await message(15, "Домашние животные")
        async with factory() as session:
            renamed_category_version = int(
                await session.scalar(
                    text("SELECT version FROM categories WHERE id = :category_id"),
                    {"category_id": category_id},
                )
            )
        await callback(
            16,
            f"sc:archive:ask:{category_id}:{renamed_category_version}",
            settings_message_id,
        )
        await callback(
            17,
            f"sc:archive:do:{category_id}:{renamed_category_version}",
            settings_message_id,
        )
        await callback(18, "sc:archived:expense", settings_message_id)
        await callback(
            19,
            f"sc:restore:{category_id}:{renamed_category_version + 1}",
            settings_message_id,
        )

        async with factory() as session:
            category_row = (
                await session.execute(
                    text(
                        "SELECT name, archived_at FROM categories "
                        "WHERE id = :category_id AND user_id = :user_id"
                    ),
                    {"category_id": category_id, "user_id": user_id},
                )
            ).one()
            assert category_row.name == "Домашние животные"
            assert category_row.archived_at is None
    finally:
        await _cleanup_database(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_wizard_survives_dispatcher_and_persists() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup_database(factory)
    fake_session = FakeTelegramSession()
    bot = Bot("123456:synthetic_test_token", session=fake_session)
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=OWNER_ID,
        database_url=DATABASE_URL,
    )
    dispatcher = build_dispatcher(settings)

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_message_update(UPDATE_BASE, 1, "/wizard"), context={"bot": bot}),
        )
        wizard_message_id = fake_session.last_message_id
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 1,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.SELECT_TYPE, page=0),
                ),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 2, 2, "1450"), context={"bot": bot}
            ),
        )

        async with factory() as session:
            user_id = await session.scalar(
                text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
                {"telegram_id": OWNER_ID},
            )
            category_id = await session.scalar(
                text(
                    "SELECT id FROM categories "
                    "WHERE user_id = :user_id AND kind = 'expense' ORDER BY name LIMIT 1"
                ),
                {"user_id": user_id},
            )
            account_id = await session.scalar(
                text("SELECT id FROM accounts WHERE user_id = :user_id ORDER BY name LIMIT 1"),
                {"user_id": user_id},
            )
            state = await session.scalar(
                text("SELECT state FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert state == "wizard_category"

        draft_steps = (
            (DraftAction.SELECT_CATEGORY, category_id, "categories", None),
            (DraftAction.SELECT_ACCOUNT, account_id, "accounts", None),
            (DraftAction.SELECT_DATE, None, None, 0),
        )
        for offset, (action, object_id, object_table, page) in enumerate(draft_steps, start=3):
            callback_data = await _draft_callback_data(
                factory,
                action,
                page=page,
                object_id=object_id,
                object_table=object_table,
            )
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    _callback_update(UPDATE_BASE + offset, wizard_message_id, callback_data),
                    context={"bot": bot},
                ),
            )

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 6, 6, "ужин с друзьями"),
                context={"bot": bot},
            ),
        )
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 7,
                    wizard_message_id,
                    await _draft_callback_data(factory, DraftAction.CONFIRM),
                ),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            saved_count = await session.scalar(
                text("SELECT count(*) FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            draft_count = await session.scalar(
                text("SELECT count(*) FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert saved_count == 1
            assert draft_count == 0
            description = await session.scalar(
                text("SELECT description FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert description == "ужин с друзьями"

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 8, 8, "1450 ресторан"), context={"bot": bot}
            ),
        )
        async with factory() as session:
            quick_count = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE user_id = :user_id AND deleted_at IS NULL"
                ),
                {"user_id": user_id},
            )
            quick_state = await session.scalar(
                text("SELECT state FROM drafts WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert quick_count == 1
            assert quick_state == "review"

        quick_review_message_id = fake_session.last_message_id
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 9,
                    quick_review_message_id,
                    await _draft_callback_data(factory, DraftAction.CONFIRM),
                ),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            quick_count = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE user_id = :user_id AND deleted_at IS NULL"
                ),
                {"user_id": user_id},
            )
            assert quick_count == 2

        for offset, command in enumerate(
            ("/today", "/month", "/last", "/export", "/settings"), start=10
        ):
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    _message_update(UPDATE_BASE + offset, offset, command),
                    context={"bot": bot},
                ),
            )
        settings_message_id = fake_session.last_message_id
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_BASE + 15, settings_message_id, "s:back"),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            transaction_row = (
                await session.execute(
                    text(
                        "SELECT id, version FROM transactions "
                        "WHERE user_id = :user_id AND description = :description"
                    ),
                    {"user_id": user_id, "description": "ужин с друзьями"},
                )
            ).one()

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 16,
                    settings_message_id,
                    f"tx:del:ask:{transaction_row.id}:{transaction_row.version}",
                ),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            deleted_after_button = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE user_id = :user_id AND deleted_at IS NOT NULL"
                ),
                {"user_id": user_id},
            )
            assert deleted_after_button == 0

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(
                    UPDATE_BASE + 17,
                    settings_message_id,
                    f"tx:del:do:{transaction_row.id}:{transaction_row.version}",
                ),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            deleted_after_confirmation = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE user_id = :user_id AND deleted_at IS NOT NULL"
                ),
                {"user_id": user_id},
            )
            assert deleted_after_confirmation == 1

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _message_update(UPDATE_BASE + 18, 20, "/undo"), context={"bot": bot}
            ),
        )
        async with factory() as session:
            deleted_count = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE user_id = :user_id AND deleted_at IS NOT NULL"
                ),
                {"user_id": user_id},
            )
            assert deleted_count == 0
    finally:
        await _cleanup_database(factory)
        await bot.session.close()
        await engine.dispose()
