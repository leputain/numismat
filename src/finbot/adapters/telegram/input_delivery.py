from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
    lock_telegram_draft_presentation_context_by_revision,
)
from finbot.adapters.database.services.outbox import bind_telegram_draft_presentation
from finbot.adapters.database.services.updates import is_update_processed
from finbot.adapters.ocr.tesseract import (
    MAX_IMAGE_BYTES,
    SUPPORTED_IMAGE_MIME_TYPES,
    BoundedImageBuffer,
)
from finbot.adapters.telegram.controllers.draft_ingress import DraftIngressReceiptSnapshot
from finbot.adapters.telegram.controllers.finance_draft_text_input import (
    FinanceDraftTextInputReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.ocr_images import OcrImageReceiptSnapshot
from finbot.adapters.telegram.controllers.settings_text_input import (
    SettingsTextInputReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_edit_text_input import (
    TransactionEditTextInputReceiptSnapshot,
)
from finbot.application.dto import DraftRef, OcrImageIngressStatus
from finbot.application.ocr import OcrImportError

type TrackedReceiptDelivery = Callable[[Message, int], Awaitable[bool]]
type FinanceDirectDelivery = Callable[
    [Message, FinanceDraftTextInputReceiptSnapshot], Awaitable[int]
]
type FinancePresentationBinder = Callable[
    [Message, FinanceDraftTextInputReceiptSnapshot, int], Awaitable[None]
]
type TransactionDirectDelivery = Callable[
    [Message, TransactionEditTextInputReceiptSnapshot], Awaitable[tuple[int, DraftRef | None]]
]
type TransactionPresentationBinder = Callable[
    [Message, TransactionEditTextInputReceiptSnapshot, int, DraftRef], Awaitable[None]
]
type SettingsDirectDelivery = Callable[
    [Message, SettingsTextInputReceiptSnapshot], Awaitable[tuple[int, DraftRef | None]]
]
type SettingsPresentationBinder = Callable[
    [Message, SettingsTextInputReceiptSnapshot, int, DraftRef], Awaitable[None]
]
type QuickDirectDelivery = Callable[[Message, DraftIngressReceiptSnapshot], Awaitable[None]]
type OcrImageReceiptRenderer = Callable[
    [OcrImageReceiptSnapshot], tuple["RenderedTelegramReceipt", DraftRef | None]
]


class RenderedTelegramReceipt(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def reply_markup(self) -> InlineKeyboardMarkup | None: ...


async def _delete_input_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest, TelegramForbiddenError:
        pass


class TelegramTextInputDelivery:
    """Deliver text-input receipts after their application transaction commits."""

    __slots__ = (
        "_bind_finance",
        "_bind_settings",
        "_bind_transaction",
        "_finance_direct",
        "_quick_direct",
        "_settings_direct",
        "_tracked",
        "_transaction_direct",
    )

    def __init__(
        self,
        *,
        tracked: TrackedReceiptDelivery,
        finance_direct: FinanceDirectDelivery,
        bind_finance: FinancePresentationBinder,
        transaction_direct: TransactionDirectDelivery,
        bind_transaction: TransactionPresentationBinder,
        settings_direct: SettingsDirectDelivery,
        bind_settings: SettingsPresentationBinder,
        quick_direct: QuickDirectDelivery,
    ) -> None:
        self._tracked = tracked
        self._finance_direct = finance_direct
        self._bind_finance = bind_finance
        self._transaction_direct = transaction_direct
        self._bind_transaction = bind_transaction
        self._settings_direct = settings_direct
        self._bind_settings = bind_settings
        self._quick_direct = quick_direct

    async def finance(
        self,
        message: Message,
        update_id: int | None,
        receipt: FinanceDraftTextInputReceiptSnapshot,
    ) -> None:
        if update_id is not None:
            if not await self._tracked(message, update_id):
                return
        else:
            message_id = await self._finance_direct(message, receipt)
            await self._bind_finance(message, receipt, message_id)
        await _delete_input_message(message)

    async def transaction(
        self,
        message: Message,
        update_id: int | None,
        receipt: TransactionEditTextInputReceiptSnapshot,
    ) -> None:
        if update_id is not None:
            if not await self._tracked(message, update_id):
                return
        else:
            message_id, retry_draft = await self._transaction_direct(message, receipt)
            if retry_draft is not None:
                await self._bind_transaction(message, receipt, message_id, retry_draft)
        await _delete_input_message(message)

    async def settings(
        self,
        message: Message,
        update_id: int | None,
        receipt: SettingsTextInputReceiptSnapshot,
    ) -> None:
        if update_id is not None:
            if not await self._tracked(message, update_id):
                return
        else:
            message_id, retry_draft = await self._settings_direct(message, receipt)
            if retry_draft is not None:
                await self._bind_settings(message, receipt, message_id, retry_draft)
        await _delete_input_message(message)

    async def quick(
        self,
        message: Message,
        update_id: int | None,
        receipt: DraftIngressReceiptSnapshot,
    ) -> None:
        if update_id is None:
            await self._quick_direct(message, receipt)


class BoundedTelegramImageDownloader:
    """Download only bounded supported Telegram image media into memory."""

    __slots__ = ()

    async def __call__(self, bot: Bot, message: Message) -> tuple[bytes, str]:
        file_id = ""
        mime_type = ""
        file_size: int | None = None
        if message.photo:
            image = message.photo[-1]
            file_id = image.file_id
            file_size = image.file_size
            mime_type = "image/jpeg"
        elif message.document is not None:
            file_id = message.document.file_id
            file_size = message.document.file_size
            mime_type = (message.document.mime_type or "").lower()

        try:
            if not file_id or mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
                raise OcrImportError("unsupported image metadata")
            if file_size is not None and file_size > MAX_IMAGE_BYTES:
                raise OcrImportError("image exceeds the bounded limit")
            downloaded = await bot.download(
                file_id,
                destination=BoundedImageBuffer(),
                timeout=20,
            )
            if downloaded is None:
                raise OcrImportError("image download failed")
            if isinstance(downloaded, BytesIO):
                return downloaded.getvalue(), mime_type
            content = downloaded.read()
            if not isinstance(content, bytes):
                raise OcrImportError("image download returned invalid bytes")
            return content, mime_type
        except OcrImportError, TelegramBadRequest:
            # The empty sentinel is passed through the atomic controller so the
            # update is claimed and receives one durable generic rejection.
            return b"", mime_type


class ProcessedTelegramUpdateReader:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def __call__(self, update_id: int) -> bool:
        async with self._sessions() as session:
            return await is_update_processed(session, update_id)


class OcrImageDirectDelivery:
    """Deliver an untracked OCR receipt, then bind its exact draft revision."""

    __slots__ = ("_render", "_sessions")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        renderer: OcrImageReceiptRenderer,
    ) -> None:
        self._sessions = sessions
        self._render = renderer

    async def __call__(
        self,
        message: Message,
        receipt: OcrImageReceiptSnapshot,
    ) -> None:
        rendered, draft = self._render(receipt)
        sent = await message.answer(
            rendered.text,
            parse_mode="HTML",
            reply_markup=rendered.reply_markup,
        )
        if draft is None:
            return
        async with self._sessions() as session, session.begin():
            context = TelegramDraftPresentationContext()
            if receipt.result.status is OcrImageIngressStatus.ACTIVE_DRAFT:
                current = await lock_telegram_draft_presentation_context_by_revision(
                    session,
                    draft.draft_id,
                    draft.revision,
                )
                if current is not None:
                    context = current
            await bind_telegram_draft_presentation(
                session,
                draft_id=draft.draft_id,
                draft_revision=draft.revision,
                chat_id=message.chat.id,
                message_id=sent.message_id,
                history_page=context.history_page,
                pending_history_page=context.pending_history_page,
            )


__all__ = [
    "BoundedTelegramImageDownloader",
    "OcrImageDirectDelivery",
    "ProcessedTelegramUpdateReader",
    "TelegramTextInputDelivery",
]
