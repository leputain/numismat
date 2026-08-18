from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.finance_draft_text_input import (
    FinanceDraftTextInputController,
    FinanceDraftTextInputReceiptSnapshot,
    TelegramFinanceDraftTextInputContext,
)
from finbot.adapters.telegram.controllers.ocr_images import (
    OcrImageController,
    OcrImageReceiptSnapshot,
    TelegramOcrImageContext,
)
from finbot.adapters.telegram.controllers.settings_text_input import (
    SettingsTextInputController,
    SettingsTextInputReceiptSnapshot,
    TelegramSettingsTextInputContext,
)
from finbot.adapters.telegram.controllers.transaction_edit_text_input import (
    TelegramTransactionEditTextInputContext,
    TransactionEditTextInputController,
    TransactionEditTextInputReceiptSnapshot,
)
from finbot.application.draft_ingress import QuickDraftIngressNotApplicableError
from finbot.application.errors import ApplicationError
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputNotApplicableError,
)
from finbot.application.settings_text_input import SettingsTextInputNotApplicableError
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputNotApplicableError,
)

type FinanceTextContextFactory = Callable[
    [int | None, Message], TelegramFinanceDraftTextInputContext
]
type TransactionTextContextFactory = Callable[
    [int | None, Message], TelegramTransactionEditTextInputContext
]
type SettingsTextContextFactory = Callable[[int | None, Message], TelegramSettingsTextInputContext]
type QuickTextContextFactory = Callable[[int | None, Message], TelegramDraftIngressContext]
type OcrImageContextFactory = Callable[[int | None, Message, bytes, str], TelegramOcrImageContext]


@dataclass(frozen=True, slots=True)
class TextInputContextFactories:
    finance: FinanceTextContextFactory = field(repr=False)
    transaction: TransactionTextContextFactory = field(repr=False)
    settings: SettingsTextContextFactory = field(repr=False)
    quick: QuickTextContextFactory = field(repr=False)


class TextInputDelivery(Protocol):
    async def finance(
        self,
        message: Message,
        update_id: int | None,
        receipt: FinanceDraftTextInputReceiptSnapshot,
    ) -> None: ...

    async def transaction(
        self,
        message: Message,
        update_id: int | None,
        receipt: TransactionEditTextInputReceiptSnapshot,
    ) -> None: ...

    async def settings(
        self,
        message: Message,
        update_id: int | None,
        receipt: SettingsTextInputReceiptSnapshot,
    ) -> None: ...

    async def quick(
        self,
        message: Message,
        update_id: int | None,
        receipt: DraftIngressReceiptSnapshot,
    ) -> None: ...


class OcrImageDownloader(Protocol):
    async def __call__(self, bot: Bot, message: Message, /) -> tuple[bytes, str]: ...


class ProcessedUpdateReader(Protocol):
    async def __call__(self, update_id: int, /) -> bool: ...


class OcrImageDirectDelivery(Protocol):
    async def __call__(
        self,
        message: Message,
        receipt: OcrImageReceiptSnapshot,
        /,
    ) -> None: ...


class _Disposition(Enum):
    HANDLED = "handled"
    NOT_APPLICABLE = "not_applicable"


class TextInputRouter:
    """Route supported text states through their exact application controllers."""

    __slots__ = (
        "_contexts",
        "_delivery",
        "_draft_ingress",
        "_finance",
        "_settings",
        "_transaction",
    )

    def __init__(
        self,
        finance: FinanceDraftTextInputController,
        transaction: TransactionEditTextInputController,
        settings: SettingsTextInputController,
        draft_ingress: DraftIngressController,
        contexts: TextInputContextFactories,
        delivery: TextInputDelivery,
    ) -> None:
        self._finance = finance
        self._transaction = transaction
        self._settings = settings
        self._draft_ingress = draft_ingress
        self._contexts = contexts
        self._delivery = delivery

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.text_input)

    async def _finance_input(
        self,
        message: Message,
        update_id: int | None,
        text: str,
    ) -> _Disposition:
        try:
            receipt = await self._finance.submit(
                self._contexts.finance(update_id, message),
                text,
            )
        except FinanceDraftTextInputNotApplicableError:
            return _Disposition.NOT_APPLICABLE
        except ApplicationError as exc:
            await message.answer(str(exc))
            return _Disposition.HANDLED
        if receipt is not None:
            await self._delivery.finance(message, update_id, receipt)
        return _Disposition.HANDLED

    async def _transaction_input(
        self,
        message: Message,
        update_id: int | None,
        text: str,
    ) -> _Disposition:
        try:
            receipt = await self._transaction.submit(
                self._contexts.transaction(update_id, message),
                text,
            )
        except TransactionEditTextInputNotApplicableError:
            return _Disposition.NOT_APPLICABLE
        except (ApplicationError, ValueError) as exc:
            await message.answer(str(exc))
            return _Disposition.HANDLED
        if receipt is not None:
            await self._delivery.transaction(message, update_id, receipt)
        return _Disposition.HANDLED

    async def _settings_input(
        self,
        message: Message,
        update_id: int | None,
        text: str,
    ) -> _Disposition:
        try:
            receipt = await self._settings.submit(
                self._contexts.settings(update_id, message),
                text,
            )
        except SettingsTextInputNotApplicableError:
            return _Disposition.NOT_APPLICABLE
        except (ApplicationError, ValueError) as exc:
            await message.answer(str(exc))
            return _Disposition.HANDLED
        if receipt is not None:
            await self._delivery.settings(message, update_id, receipt)
        return _Disposition.HANDLED

    async def _quick_input(
        self,
        message: Message,
        update_id: int | None,
        text: str,
    ) -> _Disposition:
        try:
            receipt = await self._draft_ingress.begin_quick(
                self._contexts.quick(update_id, message),
                text,
            )
        except QuickDraftIngressNotApplicableError:
            return _Disposition.NOT_APPLICABLE
        except (ApplicationError, ValueError) as exc:
            await message.answer(str(exc))
            return _Disposition.HANDLED
        if receipt is not None:
            await self._delivery.quick(message, update_id, receipt)
        return _Disposition.HANDLED

    async def text_input(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        text = message.text
        if not text:
            return
        handlers = (
            self._finance_input,
            self._transaction_input,
            self._settings_input,
            self._quick_input,
        )
        for handler in handlers:
            disposition = await handler(message, finbot_update_id, text)
            if disposition is _Disposition.HANDLED:
                return
        await message.answer(
            "Этот экран не принимает текстовый ввод. "
            "Используйте кнопки текущей формы или сбросьте черновик."
        )


class OcrImageRouter:
    """Download bounded Telegram media and invoke the shared OCR ingress UoW."""

    __slots__ = (
        "_context",
        "_controller",
        "_deliver_untracked",
        "_download",
        "_is_processed",
    )

    def __init__(
        self,
        controller: OcrImageController,
        context_factory: OcrImageContextFactory,
        downloader: OcrImageDownloader,
        is_processed: ProcessedUpdateReader,
        deliver_untracked: OcrImageDirectDelivery,
    ) -> None:
        self._controller = controller
        self._context = context_factory
        self._download = downloader
        self._is_processed = is_processed
        self._deliver_untracked = deliver_untracked

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.image_input, F.photo | F.document)

    async def image_input(
        self,
        message: Message,
        bot: Bot,
        finbot_update_id: int | None = None,
    ) -> None:
        if finbot_update_id is not None and await self._is_processed(finbot_update_id):
            return
        content, mime_type = await self._download(bot, message)
        receipt = await self._controller.process(
            self._context(finbot_update_id, message, content, mime_type)
        )
        if receipt is not None and finbot_update_id is None:
            await self._deliver_untracked(message, receipt)


__all__ = [
    "OcrImageContextFactory",
    "OcrImageDirectDelivery",
    "OcrImageDownloader",
    "OcrImageRouter",
    "ProcessedUpdateReader",
    "TextInputContextFactories",
    "TextInputDelivery",
    "TextInputRouter",
]
