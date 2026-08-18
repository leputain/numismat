from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from finbot.adapters.telegram.controllers.finance_queries import (
    TelegramQueryContext,
    TelegramQueryReceipt,
)
from finbot.adapters.telegram.controllers.recurring import TelegramRecurringController
from finbot.application.errors import ApplicationError, EntityNotFoundError

type RecurringContextFactory = Callable[[int | None, Message, bool], TelegramQueryContext]
type RecurringReceiptDelivery = Callable[[Message, TelegramQueryReceipt], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RecurringRouter:
    controller: TelegramRecurringController = field(repr=False)
    context_factory: RecurringContextFactory = field(repr=False)
    deliver_message: RecurringReceiptDelivery = field(repr=False)
    deliver_callback: RecurringReceiptDelivery = field(repr=False)

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.list_message, Command("recurring"))
        dispatcher.message.register(self.list_message, F.text.in_({"🔁 Регулярные"}))
        dispatcher.callback_query.register(
            self.list_callback,
            F.data == "rec:list",
        )
        dispatcher.callback_query.register(
            self.detail_callback,
            F.data.startswith("rec:s:"),
        )

    async def list_message(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        try:
            receipt = await self.controller.list(
                self.context_factory(finbot_update_id, message, False)
            )
        except ApplicationError as error:
            await message.answer(str(error))
            return
        if receipt is not None and finbot_update_id is None:
            await self.deliver_message(message, receipt)

    async def list_callback(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = callback.message if isinstance(callback.message, Message) else None
        if message is None:
            await callback.answer()
            return
        try:
            receipt = await self.controller.list(
                self.context_factory(finbot_update_id, message, True)
            )
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def detail_callback(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = callback.message if isinstance(callback.message, Message) else None
        raw = callback.data
        if message is None or raw is None:
            await callback.answer()
            return
        try:
            schedule_id = UUID(raw.removeprefix("rec:s:"))
            if raw != f"rec:s:{schedule_id}":
                raise ValueError
            receipt = await self.controller.detail(
                self.context_factory(finbot_update_id, message, True),
                schedule_id,
            )
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except EntityNotFoundError:
            await callback.answer("Расписание не найдено", show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def _finish(
        self,
        callback: CallbackQuery,
        message: Message,
        update_id: int | None,
        receipt: TelegramQueryReceipt | None,
    ) -> None:
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            await self.deliver_callback(message, receipt)
        await callback.answer()


__all__ = ["RecurringRouter"]
