import os
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid7

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Message, Update
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    Transaction,
    User,
)
from finbot.adapters.database.services.catalogs import rename_account
from finbot.adapters.database.services.outbox import queue_edit_message_text
from finbot.adapters.database.services.transactions import soft_delete_transaction, undo_last_action
from finbot.adapters.database.services.updates import claim_update
from finbot.adapters.telegram.ui import timezone_token
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.bootstrap import build_dispatcher
from finbot.config import Settings

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
OWNER_ID = 900000006
BOT_ID = 123456
UPDATE_BASE = 981000000


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.last_message_id = 2000
        self.last_text = ""

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
            self.last_text = str(getattr(method, "text", ""))
            if method_name != "EditMessageText":
                self.last_message_id += 1
            message_id = (
                self.last_message_id
                if method_name != "EditMessageText"
                else int(cast(Any, method).message_id)
            )
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 1786093200,
                    "chat": {"id": OWNER_ID, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                    "text": self.last_text,
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


def _callback_update(update_id: int, message_id: int, data: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "new-features-test",
            "data": data,
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
        await session.execute(
            text("DELETE FROM telegram_response_outbox WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_BASE, "last": UPDATE_BASE + 1000},
        )
        await session.execute(
            text("DELETE FROM processed_updates WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_BASE, "last": UPDATE_BASE + 1000},
        )
        await session.commit()


@dataclass
class TelegramHarness:
    engine: AsyncEngine
    factory: async_sessionmaker[Any]
    telegram: FakeTelegramSession
    bot: Bot
    dispatcher: Dispatcher
    offset: int = 0

    def _next_update_id(self) -> int:
        self.offset += 1
        return UPDATE_BASE + self.offset

    async def message(self, value: str) -> None:
        update_id = self._next_update_id()
        await self.dispatcher.feed_update(
            self.bot,
            Update.model_validate(
                _message_update(update_id, update_id - UPDATE_BASE, value),
                context={"bot": self.bot},
            ),
        )

    async def callback(self, data: str, message_id: int | None = None) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update.model_validate(
                _callback_update(
                    self._next_update_id(),
                    message_id if message_id is not None else self.telegram.last_message_id,
                    data,
                ),
                context={"bot": self.bot},
            ),
        )

    async def draft(self) -> tuple[UUID, str, dict[str, object], int, bool]:
        async with self.factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.state, drafts.payload, drafts.revision, "
                        "drafts.suspended FROM drafts JOIN users ON users.id = drafts.user_id "
                        "WHERE users.telegram_user_id = :telegram_id"
                    ),
                    {"telegram_id": OWNER_ID},
                )
            ).one()
        return row.id, row.state, row.payload, row.revision, row.suspended

    async def draft_callback(
        self,
        action: DraftAction,
        *,
        page: int | None = None,
        object_id: UUID | None = None,
        object_version: int | None = None,
    ) -> str:
        draft_id, _, _, revision, _ = await self.draft()
        return cast(
            str,
            DraftInteraction(
                action=action,
                draft_id=draft_id,
                revision=revision,
                page=page,
                object_id=object_id,
                object_version=object_version,
            ).encode(),
        )


@pytest.fixture
async def telegram_harness() -> AsyncIterator[TelegramHarness]:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup_database(factory)
    telegram = FakeTelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        )
    )
    harness = TelegramHarness(engine, factory, telegram, bot, dispatcher)
    try:
        yield harness
    finally:
        await _cleanup_database(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_and_stale_draft_callbacks_cannot_mutate_and_resume_is_versioned(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("/wizard")
    initial = await harness.draft()
    wizard_message_id = int(str(initial[2]["ui_message_id"]))

    await harness.callback("w:type:expense", wizard_message_id)
    assert await harness.draft() == initial

    stale_revision_callback = DraftInteraction(
        action=DraftAction.SELECT_TYPE,
        draft_id=initial[0],
        revision=initial[3],
        page=0,
    ).encode()
    await harness.message("/today")
    suspended = await harness.draft()
    assert suspended[0] == initial[0]
    assert suspended[1] == initial[1]
    assert suspended[2] == initial[2]
    assert suspended[3] == initial[3] + 1
    assert suspended[4] is True

    await harness.callback(stale_revision_callback, wizard_message_id)
    assert await harness.draft() == suspended

    wrong_id_callback = DraftInteraction(
        action=DraftAction.SELECT_TYPE,
        draft_id=uuid7(),
        revision=suspended[3],
        page=0,
    ).encode()
    await harness.callback(wrong_id_callback, wizard_message_id)
    assert await harness.draft() == suspended

    resume_callback = DraftInteraction(
        action=DraftAction.RESUME,
        draft_id=suspended[0],
        revision=suspended[3],
    ).encode()
    await harness.callback(resume_callback)
    resumed = await harness.draft()
    assert resumed[0] == suspended[0]
    assert resumed[1] == "wizard_type"
    assert resumed[3] == suspended[3] + 1
    assert resumed[4] is False

    await harness.callback(
        await harness.draft_callback(DraftAction.SELECT_TYPE, page=0),
        int(str(resumed[2]["ui_message_id"])),
    )
    continued = await harness.draft()
    assert continued[0] == resumed[0]
    assert continued[1] == "wizard_amount"
    assert continued[3] == resumed[3] + 1

    await harness.message("/settings")
    settings_message_id = harness.telegram.last_message_id
    settings_draft = await harness.draft()
    assert settings_draft[4] is True
    await harness.callback("s:reset", settings_message_id)
    assert await harness.draft() == settings_draft

    await harness.callback(
        await harness.draft_callback(DraftAction.DISCARD),
        settings_message_id,
    )
    async with harness.factory() as session:
        remaining = await session.scalar(
            text(
                "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                "WHERE users.telegram_user_id = :telegram_id"
            ),
            {"telegram_id": OWNER_ID},
        )
    assert remaining == 0


async def _save_quick_transaction(harness: TelegramHarness, value: str) -> tuple[UUID, int, int]:
    await harness.message(value)
    _, state, payload, _, _ = await harness.draft()
    assert state == "quick_confirm"
    assert payload["flow"] == "quick"
    before = await _active_transaction_count(harness.factory)
    await harness.callback(
        await harness.draft_callback(DraftAction.CONFIRM),
        int(str(payload.get("ui_message_id", harness.telegram.last_message_id))),
    )
    async with harness.factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT transactions.id, transactions.version FROM transactions "
                    "JOIN users ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :telegram_id "
                    "AND transactions.deleted_at IS NULL "
                    "ORDER BY transactions.created_at DESC, transactions.id DESC LIMIT 1"
                ),
                {"telegram_id": OWNER_ID},
            )
        ).one()
    assert await _active_transaction_count(harness.factory) == before + 1
    return row.id, row.version, harness.telegram.last_message_id


async def _active_transaction_count(factory: async_sessionmaker[Any]) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM transactions JOIN users "
                    "ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :telegram_id "
                    "AND transactions.deleted_at IS NULL"
                ),
                {"telegram_id": OWNER_ID},
            )
            or 0
        )


@pytest.mark.asyncio
async def test_replayed_confirm_delivers_committed_outbox_without_duplicate_mutation(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("777 ресторан")
    _, state, payload, _, _ = await harness.draft()
    assert state == "quick_confirm"
    callback_data = await harness.draft_callback(DraftAction.CONFIRM)
    message_id = int(str(payload.get("ui_message_id", harness.telegram.last_message_id)))
    update_id = harness._next_update_id()

    # Model the exact crash point: the handler's claim, financial mutation and
    # receipt commit atomically, but the process stops before Telegram delivery.
    async with harness.factory() as session:
        assert await claim_update(session, update_id)
        user = await session.scalar(select(User).where(User.telegram_user_id == OWNER_ID))
        assert user is not None
        draft = await session.scalar(select(Draft).where(Draft.user_id == user.id))
        assert draft is not None
        transaction = Transaction(
            user_id=user.id,
            type=str(draft.payload["type"]),
            amount_minor=int(str(draft.payload["amount"])),
            currency=user.base_currency,
            account_id=UUID(str(draft.payload["account_id"])),
            category_id=UUID(str(draft.payload["category_id"])),
            occurred_at=datetime.fromisoformat(str(draft.payload["occurred_at"])),
            description=str(draft.payload.get("description", "")),
            source="manual",
            telegram_update_id=update_id,
        )
        session.add(transaction)
        await session.flush()
        await session.delete(draft)
        queue_edit_message_text(
            session,
            update_id=update_id,
            owner_telegram_user_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=message_id,
            text="<b>Committed receipt</b>",
            parse_mode="HTML",
        )
        await session.commit()

    sent_before = harness.telegram.last_text
    replay = Update.model_validate(
        _callback_update(update_id, message_id, callback_data),
        context={"bot": harness.bot},
    )
    await harness.dispatcher.feed_update(harness.bot, replay)

    assert harness.telegram.last_text == "<b>Committed receipt</b>"
    assert harness.telegram.last_text != sent_before
    assert await _active_transaction_count(harness.factory) == 1
    async with harness.factory() as session:
        processed = await session.scalar(
            text("SELECT count(*) FROM processed_updates WHERE update_id = :update_id"),
            {"update_id": update_id},
        )
        sent = await session.scalar(
            text(
                "SELECT count(*) FROM telegram_response_outbox "
                "WHERE update_id = :update_id AND sent_at IS NOT NULL"
            ),
            {"update_id": update_id},
        )
    assert processed == 1
    assert sent == 1

    # A second Telegram replay sees a sent receipt. Its handler reaches the
    # processed-update claim and returns without mutating business data.
    await harness.dispatcher.feed_update(harness.bot, replay)
    assert await _active_transaction_count(harness.factory) == 1


@pytest.mark.asyncio
async def test_replayed_delete_delivers_receipt_without_deleting_twice(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    transaction_id, version, message_id = await _save_quick_transaction(harness, "778 ресторан")
    update_id = harness._next_update_id()
    callback_data = f"tx:del:do:{transaction_id}:{version}:0"

    async with harness.factory() as session:
        assert await claim_update(session, update_id)
        user = await session.scalar(select(User).where(User.telegram_user_id == OWNER_ID))
        assert user is not None
        deleted = await soft_delete_transaction(session, user.id, transaction_id, version)
        queue_edit_message_text(
            session,
            update_id=update_id,
            owner_telegram_user_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=message_id,
            text="<b>Delete receipt</b>",
            parse_mode="HTML",
        )
        await session.commit()
        deleted_version = deleted.version

    replay = Update.model_validate(
        _callback_update(update_id, message_id, callback_data),
        context={"bot": harness.bot},
    )
    await harness.dispatcher.feed_update(harness.bot, replay)
    await harness.dispatcher.feed_update(harness.bot, replay)

    async with harness.factory() as session:
        row = (
            await session.execute(
                text("SELECT deleted_at, version FROM transactions WHERE id = :transaction_id"),
                {"transaction_id": transaction_id},
            )
        ).one()
        delete_audits = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE transaction_id = :transaction_id AND action = 'delete'"
            ),
            {"transaction_id": transaction_id},
        )
    assert harness.telegram.last_text == "<b>Delete receipt</b>"
    assert row.deleted_at is not None
    assert row.version == deleted_version
    assert delete_audits == 1


@pytest.mark.asyncio
async def test_replayed_catalog_rename_delivers_receipt_without_second_version_bump(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("/settings")
    message_id = harness.telegram.last_message_id
    update_id = harness._next_update_id()

    async with harness.factory() as session:
        assert await claim_update(session, update_id)
        user = await session.scalar(select(User).where(User.telegram_user_id == OWNER_ID))
        assert user is not None
        account = await session.scalar(
            select(Account)
            .where(Account.user_id == user.id, Account.archived_at.is_(None))
            .order_by(Account.id)
        )
        assert account is not None
        initial_version = account.version
        renamed = await rename_account(
            session,
            user.id,
            account.id,
            "Crash Replay Account",
            initial_version,
        )
        queue_edit_message_text(
            session,
            update_id=update_id,
            owner_telegram_user_id=OWNER_ID,
            chat_id=OWNER_ID,
            message_id=message_id,
            text="<b>Catalog receipt</b>",
            parse_mode="HTML",
        )
        await session.commit()
        account_id = renamed.id
        renamed_version = renamed.version

    replay = Update.model_validate(
        _callback_update(
            update_id,
            message_id,
            f"sa:rename:{account_id}:{initial_version}",
        ),
        context={"bot": harness.bot},
    )
    await harness.dispatcher.feed_update(harness.bot, replay)
    await harness.dispatcher.feed_update(harness.bot, replay)

    async with harness.factory() as session:
        row = (
            await session.execute(
                text("SELECT name, version FROM accounts WHERE id = :account_id"),
                {"account_id": account_id},
            )
        ).one()
    assert harness.telegram.last_text == "<b>Catalog receipt</b>"
    assert row.name == "Crash Replay Account"
    assert row.version == renamed_version == initial_version + 1


@pytest.mark.asyncio
async def test_transaction_edit_callbacks_reject_legacy_stale_wrong_message_and_object_version(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    transaction_id, transaction_version, card_message_id = await _save_quick_transaction(
        harness, "875 ресторан"
    )
    await harness.callback(
        f"tx:edit:{transaction_id}:{transaction_version}:0",
        card_message_id,
    )
    edit_draft = await harness.draft()
    assert edit_draft[1] == "edit_menu"
    edit_message_id = int(str(edit_draft[2]["ui_message_id"]))
    edit_category = await harness.draft_callback(DraftAction.TX_EDIT_CATEGORY)

    await harness.callback(
        f"e:category:{transaction_id}:{transaction_version}:0",
        edit_message_id,
    )
    assert await harness.draft() == edit_draft

    await harness.callback(edit_category, edit_message_id + 1)
    assert await harness.draft() == edit_draft

    await harness.callback(edit_category, edit_message_id)
    category_draft = await harness.draft()
    assert category_draft[1] == "edit_category"
    assert category_draft[3] == edit_draft[3] + 1

    await harness.callback(edit_category, edit_message_id)
    assert await harness.draft() == category_draft

    async with harness.factory() as session:
        alternative = (
            await session.execute(
                text(
                    "SELECT categories.id, categories.version FROM categories "
                    "JOIN users ON users.id = categories.user_id "
                    "JOIN transactions ON transactions.user_id = users.id "
                    "WHERE users.telegram_user_id = :telegram_id "
                    "AND transactions.id = :transaction_id "
                    "AND categories.kind = transactions.type "
                    "AND categories.id != transactions.category_id "
                    "AND categories.archived_at IS NULL "
                    "ORDER BY categories.id LIMIT 1"
                ),
                {"telegram_id": OWNER_ID, "transaction_id": transaction_id},
            )
        ).one()

    wrong_object_version = await harness.draft_callback(
        DraftAction.TX_SELECT_CATEGORY,
        object_id=alternative.id,
        object_version=alternative.version + 1,
    )
    await harness.callback(wrong_object_version, edit_message_id)
    assert await harness.draft() == category_draft

    await harness.callback(
        await harness.draft_callback(
            DraftAction.TX_SELECT_CATEGORY,
            object_id=alternative.id,
            object_version=alternative.version,
        ),
        edit_message_id,
    )
    async with harness.factory() as session:
        transaction = (
            await session.execute(
                text("SELECT category_id, version FROM transactions WHERE id = :transaction_id"),
                {"transaction_id": transaction_id},
            )
        ).one()
        remaining_drafts = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id"
                ),
                {"telegram_id": OWNER_ID},
            )
            or 0
        )
    assert transaction.category_id == alternative.id
    assert transaction.version == transaction_version + 1
    assert remaining_drafts == 0


@pytest.mark.asyncio
async def test_repeat_today_opens_review_and_persists_only_after_confirmation(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    transaction_id, version, card_message_id = await _save_quick_transaction(
        harness, "100 кофе на вынос"
    )

    await harness.callback(
        f"tx:repeat:{transaction_id}:{version}:0",
        card_message_id,
    )
    assert await _active_transaction_count(harness.factory) == 1
    _, state, payload, _, _ = await harness.draft()
    assert state == "quick_confirm"
    assert payload["flow"] == "repeat"
    occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
    assert occurred_at.date() == datetime.now(occurred_at.tzinfo).date()

    await harness.callback(
        await harness.draft_callback(DraftAction.CONFIRM),
        int(str(payload.get("ui_message_id", harness.telegram.last_message_id))),
    )
    assert await _active_transaction_count(harness.factory) == 2
    async with harness.factory() as session:
        descriptions = list(
            await session.scalars(
                text(
                    "SELECT transactions.description FROM transactions JOIN users "
                    "ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :telegram_id "
                    "ORDER BY transactions.created_at, transactions.id"
                ),
                {"telegram_id": OWNER_ID},
            )
        )
    assert descriptions == ["кофе на вынос", "кофе на вынос"]


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_trash_can_restore(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    transaction_id, version, card_message_id = await _save_quick_transaction(
        harness, "275 продукты"
    )

    await harness.callback(
        f"tx:del:ask:{transaction_id}:{version}:0",
        card_message_id,
    )
    async with harness.factory() as session:
        row = (
            await session.execute(
                text("SELECT deleted_at, version FROM transactions WHERE id = :id"),
                {"id": transaction_id},
            )
        ).one()
    assert row.deleted_at is None
    assert row.version == version

    await harness.callback(
        f"tx:del:do:{transaction_id}:{version}:0",
        card_message_id,
    )
    async with harness.factory() as session:
        deleted = (
            await session.execute(
                text("SELECT deleted_at, version FROM transactions WHERE id = :id"),
                {"id": transaction_id},
            )
        ).one()
    assert deleted.deleted_at is not None
    assert deleted.version == version + 1

    await harness.callback("z:list:0", card_message_id)
    await harness.callback(
        f"z:restore:{transaction_id}:{deleted.version}:0",
        card_message_id,
    )
    async with harness.factory() as session:
        restored = (
            await session.execute(
                text("SELECT deleted_at, version FROM transactions WHERE id = :id"),
                {"id": transaction_id},
            )
        ).one()
    assert restored.deleted_at is None
    assert restored.version == version + 2


@pytest.mark.asyncio
async def test_explicit_rule_is_staged_then_saved_atomically_and_overrides_builtin(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("321 кофе у маяка")
    _, state, initial_payload, _, _ = await harness.draft()
    assert state == "quick_confirm"

    async with harness.factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        groceries = (
            await session.execute(
                text(
                    "SELECT id, version FROM categories WHERE user_id = :user_id "
                    "AND kind = 'expense' AND slug = 'продукты'"
                ),
                {"user_id": user_id},
            )
        ).one()
        account = (
            await session.execute(
                text(
                    "SELECT accounts.id, accounts.name FROM accounts JOIN users "
                    "ON users.default_account_id = accounts.id WHERE users.id = :user_id"
                ),
                {"user_id": user_id},
            )
        ).one()

    await harness.callback(
        await harness.draft_callback(DraftAction.EDIT_CATEGORY),
        int(str(initial_payload.get("ui_message_id", harness.telegram.last_message_id))),
    )
    _, state, review_payload, _, _ = await harness.draft()
    assert state == "review_category"
    await harness.callback(
        await harness.draft_callback(
            DraftAction.SELECT_CATEGORY,
            object_id=groceries.id,
            object_version=groceries.version,
        ),
        int(str(review_payload["ui_message_id"])),
    )
    _, state, corrected_payload, _, _ = await harness.draft()
    assert state == "quick_confirm"
    assert corrected_payload["category_id"] == str(groceries.id)
    assert corrected_payload.get("rule_offer_pattern")

    await harness.callback(
        await harness.draft_callback(DraftAction.RULE_GLOBAL),
        int(str(corrected_payload["ui_message_id"])),
    )
    _, _, staged_payload, _, _ = await harness.draft()
    assert staged_payload["pending_rule"] == {
        "pattern": staged_payload["rule_offer_pattern"],
        "scope": "global",
        "account_id": staged_payload["account_id"],
        "category_id": staged_payload["category_id"],
    }
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM category_rules WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        await session.execute(
            text("UPDATE accounts SET archived_at = now() WHERE id = :account_id"),
            {"account_id": account.id},
        )
        await session.commit()

    confirm = await harness.draft_callback(DraftAction.CONFIRM)
    await harness.callback(confirm, int(str(staged_payload["ui_message_id"])))
    async with harness.factory() as session:
        # The rule upsert happens before transaction validation, so this proves
        # both writes still roll back together when saving the transaction fails.
        assert (
            await session.scalar(
                text("SELECT count(*) FROM category_rules WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        await session.execute(
            text("UPDATE accounts SET archived_at = NULL WHERE id = :account_id"),
            {"account_id": account.id},
        )
        await session.commit()

    await harness.callback(confirm, int(str(staged_payload["ui_message_id"])))
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM category_rules WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 1
        )

    await harness.message("322 кофе у маяка")
    _, learned_state, learned_payload, _, _ = await harness.draft()
    assert learned_state == "quick_confirm"
    assert learned_payload["category_id"] == str(groceries.id)
    assert learned_payload["category_id"] != initial_payload["category_id"]


@pytest.mark.asyncio
async def test_confirm_revalidates_staged_rule_against_final_description(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("410 кофе у маяка")
    _, _, initial_payload, _, _ = await harness.draft()
    async with harness.factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        groceries = (
            await session.execute(
                text(
                    "SELECT id, version FROM categories WHERE user_id = :user_id "
                    "AND kind = 'expense' AND slug = 'продукты'"
                ),
                {"user_id": user_id},
            )
        ).one()

    await harness.callback(
        await harness.draft_callback(DraftAction.EDIT_CATEGORY),
        int(str(initial_payload["ui_message_id"])),
    )
    _, _, category_payload, _, _ = await harness.draft()
    await harness.callback(
        await harness.draft_callback(
            DraftAction.SELECT_CATEGORY,
            object_id=groceries.id,
            object_version=groceries.version,
        ),
        int(str(category_payload["ui_message_id"])),
    )
    _, _, corrected_payload, _, _ = await harness.draft()
    await harness.callback(
        await harness.draft_callback(DraftAction.RULE_GLOBAL),
        int(str(corrected_payload["ui_message_id"])),
    )
    _, _, staged_payload, _, _ = await harness.draft()

    # Simulate stale metadata from an older client/path: confirm must validate the
    # rule against the final payload instead of trusting pending_rule blindly.
    async with harness.factory() as session:
        await session.execute(
            text(
                "UPDATE drafts SET payload = jsonb_set(payload, '{description}', "
                "to_jsonb(CAST(:description AS text))) WHERE user_id = :user_id"
            ),
            {"description": "обед в столовой", "user_id": user_id},
        )
        await session.commit()

    await harness.callback(
        await harness.draft_callback(DraftAction.CONFIRM),
        int(str(staged_payload["ui_message_id"])),
    )
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM category_rules WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM transactions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 1
        )


@pytest.mark.asyncio
async def test_rule_intent_is_cleared_when_back_changes_account(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("515 кофе у вокзала")
    _, _, initial_payload, _, _ = await harness.draft()
    async with harness.factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        groceries = (
            await session.execute(
                text(
                    "SELECT id, version FROM categories WHERE user_id = :user_id "
                    "AND kind = 'expense' AND slug = 'продукты'"
                ),
                {"user_id": user_id},
            )
        ).one()
        second_account = Account(
            user_id=user_id,
            name="Второй счёт",
            slug="второй-счёт",
            currency="RUB",
        )
        session.add(second_account)
        await session.commit()

    await harness.callback(
        await harness.draft_callback(DraftAction.EDIT_CATEGORY),
        int(str(initial_payload["ui_message_id"])),
    )
    _, _, category_payload, _, _ = await harness.draft()
    await harness.callback(
        await harness.draft_callback(
            DraftAction.SELECT_CATEGORY,
            object_id=groceries.id,
            object_version=groceries.version,
        ),
        int(str(category_payload["ui_message_id"])),
    )
    _, _, corrected_payload, _, _ = await harness.draft()
    await harness.callback(
        await harness.draft_callback(DraftAction.RULE_ACCOUNT),
        int(str(corrected_payload["ui_message_id"])),
    )
    _, _, staged_payload, _, _ = await harness.draft()
    assert staged_payload["pending_rule"] == {
        "pattern": staged_payload["rule_offer_pattern"],
        "scope": "account",
        "account_id": staged_payload["account_id"],
        "category_id": staged_payload["category_id"],
    }

    await harness.callback(
        await harness.draft_callback(DraftAction.BACK),
        int(str(staged_payload["ui_message_id"])),
    )
    _, back_state, back_payload, _, _ = await harness.draft()
    assert back_state == "quick_account"
    assert "account_id" not in back_payload
    assert "pending_rule" not in back_payload
    assert "rule_offer_pattern" not in back_payload

    await harness.callback(
        await harness.draft_callback(
            DraftAction.SELECT_ACCOUNT,
            object_id=second_account.id,
            object_version=second_account.version,
        ),
        int(str(back_payload["ui_message_id"])),
    )
    _, final_state, final_payload, _, _ = await harness.draft()
    assert final_state == "quick_confirm"
    assert final_payload["account_id"] == str(second_account.id)
    assert "pending_rule" not in final_payload

    await harness.callback(
        await harness.draft_callback(DraftAction.CONFIRM),
        int(str(final_payload["ui_message_id"])),
    )
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM category_rules WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text(
                    "SELECT account_id FROM transactions WHERE user_id = :user_id "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"user_id": user_id},
            )
            == second_account.id
        )


@pytest.mark.asyncio
async def test_catalog_callbacks_reject_stale_versions_and_typed_rename_is_guarded(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("/settings")
    async with harness.factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        original_default = await session.scalar(
            text("SELECT default_account_id FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        account = Account(
            user_id=user_id,
            name="Каталожный тест",
            slug="каталожный-тест",
            currency="RUB",
        )
        session.add(account)
        await session.commit()
        account_id = account.id
        initial_version = account.version

    await harness.callback(f"s:account:{account_id}")
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT default_account_id FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
            == original_default
        )
        await session.execute(
            text(
                "UPDATE accounts SET name = :name, slug = :slug, version = version + 1 "
                "WHERE id = :account_id"
            ),
            {
                "name": "Изменён извне",
                "slug": "изменён-извне",
                "account_id": account_id,
            },
        )
        await session.commit()

    await harness.callback(f"sa:default:{account_id}:{initial_version}")
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT default_account_id FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
            == original_default
        )
        current_version = int(
            await session.scalar(
                text("SELECT version FROM accounts WHERE id = :account_id"),
                {"account_id": account_id},
            )
        )

    await harness.callback(f"sa:default:{account_id}:{current_version}")
    async with harness.factory() as session:
        row = (
            await session.execute(
                text("SELECT name, version FROM accounts WHERE id = :account_id"),
                {"account_id": account_id},
            )
        ).one()
        assert (
            await session.scalar(
                text("SELECT default_account_id FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
            == account_id
        )
        await session.execute(
            text("UPDATE users SET timezone = :timezone WHERE id = :user_id"),
            {"timezone": "Europe/Samara", "user_id": user_id},
        )
        await session.commit()

    await harness.callback(f"s:timezone:0:{timezone_token('Europe/Moscow')}")
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT timezone FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
            == "Europe/Samara"
        )

    await harness.callback(f"sa:rename:{account_id}:{row.version}")
    rename_draft = await harness.draft()
    assert rename_draft[1] == "settings_account_rename"
    assert rename_draft[2]["object_version"] == row.version
    async with harness.factory() as session:
        await session.execute(
            text(
                "UPDATE accounts SET name = :name, slug = :slug, version = version + 1 "
                "WHERE id = :account_id"
            ),
            {
                "name": "Ещё новее",
                "slug": "ещё-новее",
                "account_id": account_id,
            },
        )
        await session.commit()

    await harness.message("Просроченное имя")
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text("SELECT name FROM accounts WHERE id = :account_id"),
                {"account_id": account_id},
            )
            == "Ещё новее"
        )
    guarded_draft = await harness.draft()
    assert guarded_draft[1] == "settings_account_rename"
    assert guarded_draft[2]["object_version"] == row.version


@pytest.mark.asyncio
async def test_review_and_saved_transaction_use_selected_account_currency(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("/settings")
    async with harness.factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :telegram_id"),
            {"telegram_id": OWNER_ID},
        )
        account = Account(
            user_id=user_id,
            name="Доллары",
            slug="доллары",
            currency="USD",
        )
        session.add(account)
        await session.commit()

    await harness.message("123 ресторан @Доллары")
    _, state, payload, _, _ = await harness.draft()
    assert state == "quick_confirm"
    assert payload["currency"] == "USD"
    assert "$" in harness.telegram.last_text

    await harness.callback(
        await harness.draft_callback(DraftAction.CONFIRM),
        int(str(payload["ui_message_id"])),
    )
    async with harness.factory() as session:
        saved = (
            await session.execute(
                text(
                    "SELECT id, version, currency FROM transactions WHERE user_id = :user_id "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"user_id": user_id},
            )
        ).one()
    assert saved.currency == "USD"

    await harness.callback(f"tx:repeat:{saved.id}:{saved.version}:0")
    _, repeated_state, repeated_payload, _, _ = await harness.draft()
    assert repeated_state == "quick_confirm"
    assert repeated_payload["currency"] == "USD"
    assert "$" in harness.telegram.last_text


@pytest.mark.asyncio
async def test_settings_input_navigation_cannot_replace_draft_and_discard_is_versioned(
    telegram_harness: TelegramHarness,
) -> None:
    harness = telegram_harness
    await harness.message("/settings")
    await harness.callback("s:accounts")
    await harness.callback("sa:new")
    initial = await harness.draft()
    assert initial[1] == "settings_account_create"

    async with harness.factory() as session:
        account = (
            await session.execute(
                text(
                    "SELECT accounts.id, accounts.version FROM accounts JOIN users "
                    "ON users.id = accounts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id LIMIT 1"
                ),
                {"telegram_id": OWNER_ID},
            )
        ).one()
        category = (
            await session.execute(
                text(
                    "SELECT categories.id, categories.version FROM categories JOIN users "
                    "ON users.id = categories.user_id "
                    "WHERE users.telegram_user_id = :telegram_id LIMIT 1"
                ),
                {"telegram_id": OWNER_ID},
            )
        ).one()

    blocked_callbacks = (
        "s:accounts",
        "sa:list",
        f"sa:view:{account.id}:{account.version}",
        "sc:root",
        "sc:list:expense",
        f"sc:view:{category.id}:{category.version}",
        "sa:new",
        "sc:new:expense",
        f"sa:rename:{account.id}:{account.version}",
        f"sc:rename:{category.id}:{category.version}",
    )
    for data in blocked_callbacks:
        await harness.callback(data)
        assert await harness.draft() == initial

    await harness.callback(await harness.draft_callback(DraftAction.DISCARD))
    async with harness.factory() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :telegram_id"
                ),
                {"telegram_id": OWNER_ID},
            )
            == 0
        )


@pytest.mark.asyncio
async def test_undo_without_audit_event_never_guesses_which_row_to_mutate() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(telegram_user_id=900000007)
        session.add(user)
        await session.flush()
        account = Account(user_id=user.id, name="Без аудита", slug="без-аудита")
        category = Category(
            user_id=user.id,
            kind="expense",
            name="Без аудита",
            slug="без-аудита",
        )
        session.add_all((account, category))
        await session.flush()
        transaction = Transaction(
            user_id=user.id,
            type="expense",
            amount_minor=12345,
            currency="RUB",
            account_id=account.id,
            category_id=category.id,
            occurred_at=datetime.now(UTC),
            description="legacy row",
            source="manual",
        )
        session.add(transaction)
        await session.flush()
        before = (
            transaction.amount_minor,
            transaction.deleted_at,
            transaction.version,
            transaction.description,
        )

        assert await undo_last_action(session, user.id) is None
        await session.flush()
        after = (
            transaction.amount_minor,
            transaction.deleted_at,
            transaction.version,
            transaction.description,
        )
        assert after == before
        await session.rollback()
    await engine.dispose()
