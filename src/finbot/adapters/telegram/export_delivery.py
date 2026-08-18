from datetime import datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import TelegramResponseOutbox, User
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
    lock_current_or_prior_telegram_draft_presentation_context,
)
from finbot.adapters.database.repositories.exports import SqlAlchemyCsvExportRepository
from finbot.adapters.database.services.outbox import (
    CSV_EXPORT_JOB_BODY,
    bind_telegram_draft_presentation,
    queue_csv_export,
    queue_send_message,
)
from finbot.adapters.telegram.executor import (
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.ui import resume_draft_keyboard
from finbot.application.dto import DraftRef
from finbot.application.export import (
    CsvExportReceiptSnapshot,
    CsvExportTooLargeError,
    GeneratedCsvExport,
)
from finbot.application.use_cases.export import CsvExportClock, GenerateCsvExport

_EMPTY_EXPORT_TEXT = "Экспортировать пока нечего."
_TOO_LARGE_EXPORT_TEXT = "Экспорт слишком велик для безопасной отправки."
_RESUME_NOTICE = "Незавершённый ввод сохранён и приостановлен."


class _SystemCsvExportClock:
    __slots__ = ("_timezone",)

    def __init__(self, timezone: str) -> None:
        self._timezone = timezone

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self._timezone))


class CsvExportGenerator(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        owner_id: UUID,
        timezone: str,
        /,
    ) -> GeneratedCsvExport | None: ...


async def _default_generator(
    session: AsyncSession,
    owner_id: UUID,
    timezone: str,
) -> GeneratedCsvExport | None:
    clock: CsvExportClock = _SystemCsvExportClock(timezone)
    return await GenerateCsvExport(SqlAlchemyCsvExportRepository(session), clock)(
        owner_id,
        timezone,
    )


async def _presentation_context(
    session: TelegramMutationSession,
    suspended: DraftRef,
) -> TelegramDraftPresentationContext:
    return await lock_current_or_prior_telegram_draft_presentation_context(
        session,
        suspended,
    )


async def enqueue_csv_export_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: CsvExportReceiptSnapshot,
) -> None:
    if request.update_id is None:
        raise ValueError("Tracked CSV export receipt requires an update id")
    queue_csv_export(
        session,
        update_id=request.update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
    )
    if receipt.suspended_draft is None:
        return
    context = await _presentation_context(session, receipt.suspended_draft)
    queue_send_message(
        session,
        update_id=request.update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=_RESUME_NOTICE,
        reply_markup=resume_draft_keyboard(
            receipt.suspended_draft.draft_id,
            receipt.suspended_draft.revision,
        ),
        draft_id=receipt.suspended_draft.draft_id,
        draft_revision=receipt.suspended_draft.revision,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
        sequence=1,
    )


class CsvExportDelivery:
    """Generate sensitive CSV only in memory at post-commit delivery time."""

    __slots__ = ("_generator", "_sessions")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        generator: CsvExportGenerator = _default_generator,
    ) -> None:
        self._sessions = sessions
        self._generator = generator

    async def _generate(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
    ) -> GeneratedCsvExport | None:
        async with self._sessions() as session:
            owner = (
                await session.execute(
                    select(User.id, User.timezone).where(
                        User.telegram_user_id == telegram_user_id,
                        User.telegram_chat_id == chat_id,
                    )
                )
            ).one_or_none()
            if owner is None:
                raise RuntimeError("CSV export owner context is unavailable")
            owner_id, timezone = owner._t
            return await self._generator(session, owner_id, timezone)

    async def deliver_job(
        self,
        bot: Bot,
        response: TelegramResponseOutbox,
    ) -> int | None:
        if (
            response.method != "send_csv_export"
            or response.body != CSV_EXPORT_JOB_BODY
            or response.message_id is not None
            or response.parse_mode is not None
            or response.reply_markup is not None
            or response.draft_id is not None
            or response.draft_revision is not None
            or response.history_page is not None
            or response.pending_history_page is not None
        ):
            raise RuntimeError("Invalid CSV export outbox job")
        try:
            generated = await self._generate(
                telegram_user_id=response.owner_telegram_user_id,
                chat_id=response.chat_id,
            )
        except CsvExportTooLargeError:
            sent = await bot.send_message(response.chat_id, _TOO_LARGE_EXPORT_TEXT)
            return sent.message_id
        if generated is None:
            sent = await bot.send_message(response.chat_id, _EMPTY_EXPORT_TEXT)
            return sent.message_id
        sent = await bot.send_document(
            response.chat_id,
            BufferedInputFile(generated.content, filename=generated.filename),
            caption=f"Готово: {generated.row_count} операций · UTF-8 · разделитель «;»",
        )
        return sent.message_id

    async def deliver(
        self,
        message: Message,
        receipt: CsvExportReceiptSnapshot,
    ) -> None:
        try:
            generated = await self._generate(
                telegram_user_id=message.from_user.id if message.from_user is not None else 0,
                chat_id=message.chat.id,
            )
        except CsvExportTooLargeError:
            await message.answer(_TOO_LARGE_EXPORT_TEXT)
            generated = None
            export_was_too_large = True
        else:
            export_was_too_large = False
        if generated is None:
            if not export_was_too_large:
                await message.answer(_EMPTY_EXPORT_TEXT)
        else:
            await message.answer_document(
                BufferedInputFile(generated.content, filename=generated.filename),
                caption=(f"Готово: {generated.row_count} операций · UTF-8 · разделитель «;»"),
            )
        if receipt.suspended_draft is None:
            return
        sent = await message.answer(
            _RESUME_NOTICE,
            reply_markup=resume_draft_keyboard(
                receipt.suspended_draft.draft_id,
                receipt.suspended_draft.revision,
            ),
        )
        async with self._sessions() as session, session.begin():
            context = await _presentation_context(session, receipt.suspended_draft)
            await bind_telegram_draft_presentation(
                session,
                draft_id=receipt.suspended_draft.draft_id,
                draft_revision=receipt.suspended_draft.revision,
                chat_id=message.chat.id,
                message_id=sent.message_id,
                history_page=context.history_page,
                pending_history_page=context.pending_history_page,
            )
