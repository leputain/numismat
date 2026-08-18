from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.types import BufferedInputFile, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.export_delivery as delivery_module
import finbot.adapters.telegram.outbox as outbox_module
from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
)
from finbot.adapters.database.services.outbox import CSV_EXPORT_JOB_BODY
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.export_delivery import (
    CsvExportDelivery,
    enqueue_csv_export_receipt,
)
from finbot.adapters.telegram.outbox import (
    TelegramResponseOutboxMiddleware,
    deliver_response,
)
from finbot.application.dto import DraftRef
from finbot.application.export import (
    CsvExportReceiptSnapshot,
    CsvExportTooLargeError,
    GeneratedCsvExport,
)

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")


class _AddSession:
    def __init__(self) -> None:
        self.added: list[TelegramResponseOutbox] = []

    def add(self, value: object) -> None:
        self.added.append(cast(TelegramResponseOutbox, value))


class _OwnerRow:
    _t = (OWNER_ID, "UTC")


class _Result:
    def one_or_none(self) -> _OwnerRow:
        return _OwnerRow()


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def execute(self, statement: object) -> _Result:
        del statement
        return _Result()

    def begin(self) -> _Transaction:
        return _Transaction()


class _Sessions:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _Bot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, BufferedInputFile, dict[str, object]]] = []

    async def send_message(self, chat_id: int, text: str) -> _Sent:
        self.messages.append((chat_id, text))
        return _Sent(101)

    async def send_document(
        self,
        chat_id: int,
        document: BufferedInputFile,
        **kwargs: object,
    ) -> _Sent:
        self.documents.append((chat_id, document, kwargs))
        return _Sent(102)


class _Message:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"id": 93_000_003})()
        self.from_user = type("User", (), {"id": 92_000_002})()
        self.documents: list[tuple[BufferedInputFile, dict[str, object]]] = []
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer_document(
        self,
        document: BufferedInputFile,
        **kwargs: object,
    ) -> _Sent:
        self.documents.append((document, kwargs))
        return _Sent(101)

    async def answer(self, text: str, **kwargs: object) -> _Sent:
        self.answers.append((text, kwargs))
        return _Sent(102)


def _request() -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=91_000_001,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _job(**changes: object) -> TelegramResponseOutbox:
    values: dict[str, object] = {
        "update_id": 91_000_001,
        "sequence": 0,
        "owner_telegram_user_id": 92_000_002,
        "chat_id": 93_000_003,
        "method": "send_csv_export",
        "message_id": None,
        "body": CSV_EXPORT_JOB_BODY,
        "parse_mode": None,
        "reply_markup": None,
        "draft_id": None,
        "draft_revision": None,
        "history_page": None,
        "pending_history_page": None,
    }
    values.update(changes)
    return TelegramResponseOutbox(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tracked_export_queues_constant_job_and_exact_resume_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _AddSession()

    async def context_reader(
        actual: AsyncSession,
        expected: DraftRef,
    ) -> TelegramDraftPresentationContext:
        assert actual is cast(Any, session)
        assert expected == DraftRef(DRAFT_ID, 7)
        return TelegramDraftPresentationContext(history_page=8, pending_history_page=13)

    monkeypatch.setattr(delivery_module, "_presentation_context", context_reader)

    await enqueue_csv_export_receipt(
        cast(AsyncSession, session),
        _request(),
        CsvExportReceiptSnapshot(DraftRef(DRAFT_ID, 7)),
    )

    assert len(session.added) == 2
    job, resume = session.added
    assert (job.sequence, job.method, job.body) == (0, "send_csv_export", CSV_EXPORT_JOB_BODY)
    assert (
        job.message_id,
        job.parse_mode,
        job.reply_markup,
        job.draft_id,
        job.draft_revision,
        job.history_page,
        job.pending_history_page,
    ) == (None, None, None, None, None, None, None)
    assert (resume.sequence, resume.draft_id, resume.draft_revision) == (1, DRAFT_ID, 7)
    assert (resume.history_page, resume.pending_history_page) == (8, 13)
    assert "private" not in job.body
    assert str(DRAFT_ID) not in repr(job)


@pytest.mark.asyncio
async def test_job_generates_document_only_in_delivery_memory() -> None:
    generator_calls: list[tuple[UUID, str]] = []

    async def generator(
        session: AsyncSession,
        owner_id: UUID,
        timezone: str,
        /,
    ) -> GeneratedCsvExport:
        assert session is cast(Any, fake_session)
        generator_calls.append((owner_id, timezone))
        return GeneratedCsvExport(b"private-csv", "finbot-all-2026-08-13.csv", 1)

    fake_session = _Session()
    delivery = CsvExportDelivery(
        cast(async_sessionmaker[AsyncSession], _Sessions(fake_session)),
        generator,
    )
    bot = _Bot()
    job = _job()

    message_id = await delivery.deliver_job(cast(Bot, bot), job)

    assert message_id == 102
    assert generator_calls == [(OWNER_ID, "UTC")]
    assert bot.messages == []
    assert len(bot.documents) == 1
    _, document, kwargs = bot.documents[0]
    assert document.data == b"private-csv"
    assert document.filename == "finbot-all-2026-08-13.csv"
    assert "1 операций" in str(kwargs["caption"])
    assert job.body == CSV_EXPORT_JOB_BODY
    assert b"private-csv" != job.body.encode()


@pytest.mark.asyncio
async def test_oversized_job_delivers_safe_terminal_receipt_instead_of_retrying_forever() -> None:
    async def oversized(*args: object) -> GeneratedCsvExport:
        del args
        raise CsvExportTooLargeError

    delivery = CsvExportDelivery(
        cast(async_sessionmaker[AsyncSession], _Sessions(_Session())),
        oversized,
    )
    bot = _Bot()

    message_id = await delivery.deliver_job(cast(Bot, bot), _job())

    assert message_id == 101
    assert bot.documents == []
    assert bot.messages == [(93_000_003, "Экспорт слишком велик для безопасной отправки.")]


@pytest.mark.asyncio
async def test_invalid_export_job_fails_closed_before_query_or_network() -> None:
    async def forbidden(*args: object) -> GeneratedCsvExport:
        del args
        raise AssertionError("invalid job reached export query")

    delivery = CsvExportDelivery(
        cast(async_sessionmaker[AsyncSession], _Sessions(_Session())),
        forbidden,
    )
    bot = _Bot()

    with pytest.raises(RuntimeError, match="Invalid CSV export"):
        await delivery.deliver_job(cast(Bot, bot), _job(body="private-csv"))

    assert bot.messages == []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_outbox_dispatches_export_job_through_injected_delivery() -> None:
    job = _job()
    calls: list[TelegramResponseOutbox] = []

    async def special(bot: Bot, response: TelegramResponseOutbox) -> int:
        del bot
        calls.append(response)
        return 404

    result = await deliver_response(
        cast(Bot, _Bot()),
        job,
        special_delivery=special,
    )

    assert result == 404
    assert calls == [job]


@pytest.mark.asyncio
async def test_replay_delivers_pending_export_and_skips_business_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = CsvExportDelivery(
        cast(async_sessionmaker[AsyncSession], _Sessions(_Session())),
    )
    middleware = TelegramResponseOutboxMiddleware(
        cast(async_sessionmaker[AsyncSession], _Sessions(_Session())),
        delivery.deliver_job,
    )
    calls: list[dict[str, object]] = []

    async def pending(
        sessions: async_sessionmaker[AsyncSession],
        bot: Bot,
        **kwargs: object,
    ) -> int:
        del sessions, bot
        calls.append(kwargs)
        return 1

    async def forbidden_handler(
        event: TelegramObject,
        data: dict[str, Any],
    ) -> None:
        del event, data
        raise AssertionError("replay reached business handler")

    monkeypatch.setattr(outbox_module, "deliver_pending_responses", pending)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    try:
        result = await middleware(
            forbidden_handler,
            cast(TelegramObject, object()),
            {
                "finbot_update_id": 91_000_001,
                "event_from_user": type("User", (), {"id": 92_000_002})(),
                "event_chat": type("Chat", (), {"id": 93_000_003})(),
                "bot": bot,
            },
        )
    finally:
        await bot.session.close()

    assert result is None
    assert len(calls) == 1
    assert calls[0]["update_id"] == 91_000_001
    assert calls[0]["special_delivery"] == delivery.deliver_job


@pytest.mark.asyncio
async def test_untracked_delivery_restores_and_binds_exact_suspended_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _Session()
    calls: list[tuple[str, object]] = []

    async def generator(
        session: AsyncSession,
        owner_id: UUID,
        timezone: str,
        /,
    ) -> GeneratedCsvExport:
        assert session is cast(Any, fake_session)
        assert (owner_id, timezone) == (OWNER_ID, "UTC")
        return GeneratedCsvExport(b"safe", "finbot-all-2026-08-13.csv", 1)

    async def context_reader(
        actual: AsyncSession,
        expected: DraftRef,
    ) -> TelegramDraftPresentationContext:
        assert actual is cast(Any, fake_session)
        calls.append(("context", expected))
        return TelegramDraftPresentationContext(history_page=5, pending_history_page=8)

    async def bind(actual: AsyncSession, **kwargs: object) -> bool:
        assert actual is cast(Any, fake_session)
        calls.append(("bind", kwargs))
        return True

    monkeypatch.setattr(delivery_module, "_presentation_context", context_reader)
    monkeypatch.setattr(delivery_module, "bind_telegram_draft_presentation", bind)
    delivery = CsvExportDelivery(
        cast(async_sessionmaker[AsyncSession], _Sessions(fake_session)),
        generator,
    )
    message = _Message()

    await delivery.deliver(
        cast(Message, message),
        CsvExportReceiptSnapshot(DraftRef(DRAFT_ID, 7)),
    )

    assert len(message.documents) == 1
    assert len(message.answers) == 1
    assert message.answers[0][1]["reply_markup"].inline_keyboard
    assert calls == [
        ("context", DraftRef(DRAFT_ID, 7)),
        (
            "bind",
            {
                "draft_id": DRAFT_ID,
                "draft_revision": 7,
                "chat_id": 93_000_003,
                "message_id": 102,
                "history_page": 5,
                "pending_history_page": 8,
            },
        ),
    ]
