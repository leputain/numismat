import os
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, TelegramMethod
from aiogram.types import File, Message, Update
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import finbot.bootstrap as bootstrap_module
from finbot.adapters.database.services.onboarding import ensure_owner_user
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.bootstrap import build_dispatcher
from finbot.config import Settings

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
OWNER_ID = 9_100_000_002
BOT_ID = 123_456
UPDATE_ID = 989_000_001


class _TelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.last_message_id = 2000
        self.last_text = ""
        self.fail_next_edit = False

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
        if method_name == "GetFile":
            return File(
                file_id="ocr-file",
                file_unique_id="ocr-unique",
                file_size=15,
                file_path="images/ocr.png",
            )
        if method_name in {"SendMessage", "EditMessageText"}:
            if method_name == "EditMessageText" and self.fail_next_edit:
                self.fail_next_edit = False
                raise TelegramBadRequest(method, "message to edit not found")
            self.last_text = str(getattr(method, "text", ""))
            if method_name == "SendMessage":
                self.last_message_id += 1
                message_id = self.last_message_id
            else:
                edit_message_id = cast(EditMessageText, method).message_id
                assert edit_message_id is not None
                message_id = int(edit_message_id)
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
        chunk_size: int = 65_536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        del url, headers, timeout, chunk_size, raise_for_status
        yield b"synthetic-image"


class _OcrExtractor:
    def __init__(
        self,
        text: str = "ООО Ромашка\nИТОГО 1 450,00 ₽\n12.08.2026 10:30",
    ) -> None:
        self.calls = 0
        self.text = text

    async def extract_text(self, content: bytes, mime_type: str) -> str:
        self.calls += 1
        assert content == b"synthetic-image"
        assert mime_type == "image/jpeg"
        return self.text


def _photo_update(*, update_id: int = UPDATE_ID) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1786093200,
            "chat": {"id": OWNER_ID, "type": "private"},
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "photo": [
                {
                    "file_id": "ocr-file",
                    "file_unique_id": "ocr-unique",
                    "width": 1280,
                    "height": 720,
                    "file_size": 15,
                }
            ],
        },
    }


def _callback_update(update_id: int, message_id: int, data: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "ocr-test",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1786093200,
                "chat": {"id": OWNER_ID, "type": "private"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Finbot"},
                "text": "OCR review",
            },
        },
    }


def _text_update(update_id: int, text_value: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5000 + update_id - UPDATE_ID,
            "date": 1786093200,
            "chat": {"id": OWNER_ID, "type": "private"},
            "from": {"id": OWNER_ID, "is_bot": False, "first_name": "Owner"},
            "text": text_value,
        },
    }


async def _cleanup(factory: async_sessionmaker[Any]) -> None:
    database = make_url(DATABASE_URL).database or ""
    if not database.endswith("_test"):
        raise RuntimeError("OCR integration cleanup requires a _test database")
    async with factory() as session:
        user_id = await session.scalar(
            text("SELECT id FROM users WHERE telegram_user_id = :owner"),
            {"owner": OWNER_ID},
        )
        await session.execute(
            text("DELETE FROM telegram_response_outbox WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_ID, "last": UPDATE_ID + 60},
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
                text("DELETE FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
        await session.execute(
            text("DELETE FROM processed_updates WHERE update_id BETWEEN :first AND :last"),
            {"first": UPDATE_ID, "last": UPDATE_ID + 60},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_photo_creates_review_draft_without_transaction() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    extractor = _OcrExtractor()
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=extractor,
    )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        async with factory() as session:
            state, payload = (
                await session.execute(
                    text(
                        "SELECT drafts.state, drafts.payload FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            transaction_count = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "JOIN users ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
        assert extractor.calls == 1
        assert state == "review"
        assert payload["flow"] == "ocr"
        assert payload["amount_minor"] == 145_000
        assert "amount" not in payload
        assert payload["description"] == "ООО Ромашка"
        assert transaction_count == 0

        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        async with factory() as session:
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts "
                    "JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
        assert extractor.calls == 1
        assert draft_count == 1
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_validates_and_renders_actual_default_account_currency() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    async with factory() as session:
        user = await ensure_owner_user(
            session,
            telegram_user_id=OWNER_ID,
            telegram_chat_id=OWNER_ID,
            locale="ru_RU",
            timezone="Europe/Moscow",
            currency="RUB",
        )
        await session.execute(
            text("UPDATE accounts SET currency = 'USD' WHERE id = :account_id"),
            {"account_id": user.default_account_id},
        )
        await session.commit()

    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    extractor = _OcrExtractor("Coffee Shop\nTOTAL 14.50 USD\n12.08.2026 10:30")
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=extractor,
    )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        async with factory() as session:
            payload = await session.scalar(
                text(
                    "SELECT drafts.payload FROM drafts "
                    "JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
        assert payload is not None
        assert payload["amount_minor"] == 1450
        assert "amount" not in payload
        assert payload["currency"] == "USD"
        assert extractor.calls == 1
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_batch_reviews_and_saves_every_operation_sequentially() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    extractor = _OcrExtractor(
        "12.08.2026\n10:15 Кофейня -250,00 ₽\n"
        "11:20 Перевод от Анны +2 000,00 ₽\n12:45 Метро -250,00 ₽"
    )
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=extractor,
    )

    async def current_draft() -> tuple[Any, str, dict[str, object], int, int]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.state, drafts.payload, drafts.revision, "
                        "presentation.message_id AS presentation_message_id FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            return row.id, row.state, row.payload, row.revision, row.presentation_message_id

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        first = await current_draft()
        assert first[1] == "review"
        assert first[2]["amount_minor"] == 25_000
        assert "amount" not in first[2]
        first_batch = cast(dict[str, Any], first[2]["ocr_batch"])
        assert first_batch["version"] == 1
        assert first_batch["index"] == 1
        assert first_batch["total"] == 3
        assert first_batch["saved"] == 0
        assert first_batch["skipped"] == 0
        assert len(first_batch["remaining"]) == 2

        for step in range(3):
            draft_id, _, payload, revision, presentation_ref = await current_draft()
            callback = DraftInteraction(
                action=DraftAction.CONFIRM,
                draft_id=draft_id,
                revision=revision,
            ).encode()
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    _callback_update(UPDATE_ID + step + 1, int(presentation_ref), callback),
                    context={"bot": bot},
                ),
            )
            if step < 2:
                advanced = await current_draft()
                assert advanced[1] == "review"
                assert advanced[2]["amount_minor"] == [200_000, 25_000][step]
                assert "amount" not in advanced[2]

        async with factory() as session:
            amounts = list(
                (
                    await session.scalars(
                        text(
                            "SELECT transactions.amount_minor FROM transactions "
                            "JOIN users ON users.id = transactions.user_id "
                            "WHERE users.telegram_user_id = :owner "
                            "ORDER BY transactions.occurred_at, transactions.created_at"
                        ),
                        {"owner": OWNER_ID},
                    )
                ).all()
            )
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
        assert amounts == [25_000, 200_000, 25_000]
        assert draft_count == 0
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_batch_replay_stale_and_skip_keep_exactly_confirmed_items() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    extractor = _OcrExtractor(
        "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Аптека -500,00 ₽\n12:45 Метро -100,00 ₽"
    )
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=extractor,
    )

    async def current_draft() -> tuple[Any, dict[str, object], int, int]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.payload, drafts.revision, "
                        "presentation.message_id AS presentation_message_id FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            return row.id, row.payload, row.revision, row.presentation_message_id

    async def transaction_amounts() -> list[int]:
        async with factory() as session:
            return list(
                (
                    await session.scalars(
                        text(
                            "SELECT transactions.amount_minor FROM transactions "
                            "JOIN users ON users.id = transactions.user_id "
                            "WHERE users.telegram_user_id = :owner "
                            "ORDER BY transactions.created_at"
                        ),
                        {"owner": OWNER_ID},
                    )
                ).all()
            )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        first_id, first_payload, first_revision, first_message_ref = await current_draft()
        assert first_payload["amount_minor"] == 25_000
        assert "amount" not in first_payload
        first_confirm = DraftInteraction(
            action=DraftAction.CONFIRM,
            draft_id=first_id,
            revision=first_revision,
        ).encode()
        confirm_update = _callback_update(
            UPDATE_ID + 10,
            int(first_message_ref),
            first_confirm,
        )

        await dispatcher.feed_update(
            bot,
            Update.model_validate(confirm_update, context={"bot": bot}),
        )
        second = await current_draft()
        assert second[1]["amount_minor"] == 50_000
        assert "amount" not in second[1]
        assert await transaction_amounts() == [25_000]

        await dispatcher.feed_update(
            bot,
            Update.model_validate(confirm_update, context={"bot": bot}),
        )
        assert await current_draft() == second
        assert await transaction_amounts() == [25_000]

        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 11, int(first_message_ref), first_confirm),
                context={"bot": bot},
            ),
        )
        assert await current_draft() == second
        assert await transaction_amounts() == [25_000]

        second_id, _, second_revision, second_message_ref = second
        skip_second = DraftInteraction(
            action=DraftAction.SKIP_OCR_ITEM,
            draft_id=second_id,
            revision=second_revision,
        ).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 12, int(second_message_ref), skip_second),
                context={"bot": bot},
            ),
        )
        third = await current_draft()
        assert third[1]["amount_minor"] == 10_000
        assert "amount" not in third[1]
        third_batch = cast(dict[str, Any], third[1]["ocr_batch"])
        assert third_batch["saved"] == 1
        assert third_batch["skipped"] == 1
        assert third_batch["remaining"] == []

        third_id, _, third_revision, third_message_ref = third
        skip_third = DraftInteraction(
            action=DraftAction.SKIP_OCR_ITEM,
            draft_id=third_id,
            revision=third_revision,
        ).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 13, int(third_message_ref), skip_third),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
            final_receipt = (
                await session.execute(
                    text(
                        "SELECT text, sent_at FROM telegram_response_outbox "
                        "WHERE update_id = :update_id"
                    ),
                    {"update_id": UPDATE_ID + 13},
                )
            ).one()
        assert draft_count == 0
        assert await transaction_amounts() == [25_000]
        assert final_receipt.sent_at is not None
        assert "Сохранено: <b>1</b>" in final_receipt.text
        assert "Пропущено: <b>2</b>" in final_receipt.text
        assert "Сохранено: <b>1</b>" in telegram.last_text
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_advanced_edit_fallback_binds_the_actual_message() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=_OcrExtractor(
            "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -100,00 ₽"
        ),
    )

    async def current_draft() -> tuple[Any, dict[str, object], int, int, str | None]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.payload, drafts.revision, "
                        "presentation.message_id AS presentation_message_id, "
                        "drafts.presentation_ref FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            return (
                row.id,
                row.payload,
                row.revision,
                row.presentation_message_id,
                row.presentation_ref,
            )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        first = await current_draft()
        async with factory() as session:
            await session.execute(
                text("UPDATE drafts SET presentation_ref = :message_id WHERE id = :draft_id"),
                {"draft_id": first[0], "message_id": str(first[3])},
            )
            await session.commit()

        telegram.fail_next_edit = True
        confirm = DraftInteraction(
            DraftAction.CONFIRM,
            first[0],
            first[2],
        ).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 15, first[3], confirm),
                context={"bot": bot},
            ),
        )

        advanced = await current_draft()
        assert advanced[1]["amount_minor"] == 10_000
        assert advanced[3] != first[3]
        assert advanced[4] == str(first[3])
        assert advanced[3] == telegram.last_message_id

        skip = DraftInteraction(
            DraftAction.SKIP_OCR_ITEM,
            advanced[0],
            advanced[2],
        ).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 16, advanced[3], skip),
                context={"bot": bot},
            ),
        )
        async with factory() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
                == 0
            )
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_cancel_commits_one_terminal_outbox_without_draft_binding() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=_OcrExtractor(
            "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -100,00 ₽"
        ),
    )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        async with factory() as session:
            draft = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.revision, presentation.message_id "
                        "FROM drafts JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()

        cancel = DraftInteraction(DraftAction.CANCEL, draft.id, draft.revision).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + 17, draft.message_id, cancel),
                context={"bot": bot},
            ),
        )

        async with factory() as session:
            draft_count = await session.scalar(
                text(
                    "SELECT count(*) FROM drafts JOIN users ON users.id = drafts.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
            transaction_count = await session.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "JOIN users ON users.id = transactions.user_id "
                    "WHERE users.telegram_user_id = :owner"
                ),
                {"owner": OWNER_ID},
            )
            outbox = (
                await session.execute(
                    text(
                        "SELECT text, draft_id, draft_revision, sent_at "
                        "FROM telegram_response_outbox WHERE update_id = :update_id"
                    ),
                    {"update_id": UPDATE_ID + 17},
                )
            ).one()
        assert draft_count == 0
        assert transaction_count == 0
        assert outbox.draft_id is None
        assert outbox.draft_revision is None
        assert outbox.sent_at is not None
        assert "Текущая и оставшиеся операции отменены" in outbox.text
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ocr_enqueue_failure_rolls_back_claim_finance_audit_and_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_enqueue(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(bootstrap_module, "_enqueue_ocr_queue_receipt", fail_enqueue)
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=_OcrExtractor(
            "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -100,00 ₽"
        ),
    )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        async with factory() as session:
            before = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.revision, drafts.payload, "
                        "presentation.message_id "
                        "FROM drafts JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()

        confirm = DraftInteraction(DraftAction.CONFIRM, before.id, before.revision).encode()
        with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    _callback_update(UPDATE_ID + 18, before.message_id, confirm),
                    context={"bot": bot},
                ),
            )

        async with factory() as session:
            after = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.revision, drafts.payload FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            counts = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM transactions "
                        " JOIN users ON users.id = transactions.user_id "
                        " WHERE users.telegram_user_id = :owner) AS transactions, "
                        "(SELECT count(*) FROM audit_events "
                        " JOIN users ON users.id = audit_events.user_id "
                        " WHERE users.telegram_user_id = :owner) AS audit_events, "
                        "(SELECT count(*) FROM category_rules "
                        " JOIN users ON users.id = category_rules.user_id "
                        " WHERE users.telegram_user_id = :owner) AS category_rules, "
                        "(SELECT count(*) FROM processed_updates WHERE update_id = :update_id) "
                        " AS processed_updates, "
                        "(SELECT count(*) FROM telegram_response_outbox "
                        " WHERE update_id = :update_id) "
                        " AS outbox"
                    ),
                    {"owner": OWNER_ID, "update_id": UPDATE_ID + 18},
                )
            ).one()
        assert (after.id, after.revision, after.payload) == (
            before.id,
            before.revision,
            before.payload,
        )
        assert tuple(counts) == (0, 0, 0, 0, 0)
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_advanced_canonical_review_supports_every_review_edit_path() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    telegram = _TelegramSession()
    bot = Bot("123456:synthetic_test_token", session=telegram)
    dispatcher = build_dispatcher(
        Settings(
            telegram_bot_token="123456:synthetic_test_token",
            owner_telegram_user_id=OWNER_ID,
            database_url=DATABASE_URL,
        ),
        image_text_extractor=_OcrExtractor(
            "12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -100,00 ₽"
        ),
    )

    async def current_draft() -> tuple[Any, str, dict[str, object], int, int]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT drafts.id, drafts.state, drafts.payload, drafts.revision, "
                        "presentation.message_id FROM drafts "
                        "JOIN users ON users.id = drafts.user_id "
                        "JOIN telegram_draft_presentations AS presentation "
                        "ON presentation.draft_id = drafts.id "
                        "WHERE users.telegram_user_id = :owner"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()
            return row.id, row.state, row.payload, row.revision, row.message_id

    async def click(update_offset: int, action: DraftAction, **kwargs: object) -> None:
        draft_id, _, _, revision, message_id = await current_draft()
        interaction = DraftInteraction(
            action,
            draft_id,
            revision,
            **kwargs,  # type: ignore[arg-type]
        ).encode()
        await dispatcher.feed_update(
            bot,
            Update.model_validate(
                _callback_update(UPDATE_ID + update_offset, message_id, interaction),
                context={"bot": bot},
            ),
        )

    try:
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_photo_update(), context={"bot": bot}),
        )
        await click(20, DraftAction.CONFIRM)
        advanced = await current_draft()
        assert advanced[1] == "review"
        assert advanced[2]["amount_minor"] == 10_000

        await click(21, DraftAction.EDIT_DESCRIPTION)
        assert (await current_draft())[1] == "wizard_description"
        await click(22, DraftAction.BACK)
        assert (await current_draft())[1] == "review"

        await click(23, DraftAction.RULE_REMOVE)
        assert (await current_draft())[1] == "review"

        async with factory() as session:
            catalog = (
                await session.execute(
                    text(
                        "SELECT categories.id AS category_id, "
                        "categories.version AS category_version, "
                        "accounts.id AS account_id, accounts.version AS account_version "
                        "FROM users JOIN categories ON categories.user_id = users.id "
                        "JOIN accounts ON accounts.user_id = users.id "
                        "WHERE users.telegram_user_id = :owner "
                        "AND categories.kind = 'expense' "
                        "AND categories.archived_at IS NULL "
                        "AND accounts.archived_at IS NULL "
                        "ORDER BY categories.name, accounts.name LIMIT 1"
                    ),
                    {"owner": OWNER_ID},
                )
            ).one()

        await click(24, DraftAction.EDIT_CATEGORY)
        assert (await current_draft())[1] == "review_category"
        await click(
            25,
            DraftAction.SELECT_CATEGORY,
            object_id=catalog.category_id,
            object_version=catalog.category_version,
        )
        assert (await current_draft())[1] == "review"

        await click(26, DraftAction.EDIT_ACCOUNT)
        assert (await current_draft())[1] == "review_account"
        await click(
            27,
            DraftAction.SELECT_ACCOUNT,
            object_id=catalog.account_id,
            object_version=catalog.account_version,
        )
        assert (await current_draft())[1] == "review"

        await click(28, DraftAction.EDIT_AMOUNT)
        assert (await current_draft())[1] == "review_amount"
        await dispatcher.feed_update(
            bot,
            Update.model_validate(_text_update(UPDATE_ID + 29, "333"), context={"bot": bot}),
        )
        edited = await current_draft()
        assert edited[1] == "review"
        assert edited[2]["amount_minor"] == 33_300
        assert "amount" not in edited[2]

        await click(30, DraftAction.CONFIRM)
        async with factory() as session:
            amounts = list(
                (
                    await session.scalars(
                        text(
                            "SELECT transactions.amount_minor FROM transactions "
                            "JOIN users ON users.id = transactions.user_id "
                            "WHERE users.telegram_user_id = :owner "
                            "ORDER BY transactions.created_at"
                        ),
                        {"owner": OWNER_ID},
                    )
                ).all()
            )
        assert amounts == [25_000, 33_300]
    finally:
        await _cleanup(factory)
        await bot.session.close()
        await engine.dispose()
