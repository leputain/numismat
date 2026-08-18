from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from finbot.adapters.telegram.controllers.exports import CsvExportController
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.application.errors import ApplicationError
from finbot.application.export import CsvExportReceiptSnapshot


@dataclass(frozen=True, slots=True)
class CsvExportRequestDefaults:
    owner_telegram_user_id: int = field(repr=False)
    locale: str = field(repr=False)
    timezone: str = field(repr=False)
    currency: str = field(repr=False)

    def request(
        self,
        update_id: int | None,
        message: Message,
    ) -> TelegramMutationRequest:
        return TelegramMutationRequest(
            update_id=update_id,
            owner_telegram_user_id=self.owner_telegram_user_id,
            chat_id=message.chat.id,
            locale=self.locale,
            timezone=self.timezone,
            currency=self.currency,
        )


class CsvExportDirectDelivery(Protocol):
    async def deliver(
        self,
        message: Message,
        receipt: CsvExportReceiptSnapshot,
    ) -> None: ...


class CsvExportRouter:
    __slots__ = ("_controller", "_defaults", "_direct_delivery")

    def __init__(
        self,
        controller: CsvExportController,
        direct_delivery: CsvExportDirectDelivery,
        defaults: CsvExportRequestDefaults,
    ) -> None:
        self._controller = controller
        self._direct_delivery = direct_delivery
        self._defaults = defaults

    def register(self, dispatcher: Dispatcher) -> None:
        observer = dispatcher.message
        observer.register(self.export_csv, Command("export"))
        observer.register(self.export_csv, F.text.in_({"📤 CSV", "📤 Экспорт"}))

    async def export_csv(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        try:
            receipt = await self._controller.request(
                self._defaults.request(finbot_update_id, message)
            )
        except ApplicationError as exc:
            await message.answer(str(exc))
            return
        if receipt is not None and finbot_update_id is None:
            await self._direct_delivery.deliver(message, receipt)
