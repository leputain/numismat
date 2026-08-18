"""Focused Telegram callback handlers for owner-scoped finance queries."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram.types import CallbackQuery, Message

from finbot.adapters.telegram.controllers.finance_queries import (
    DELETED_TRANSACTION_PREFIX,
    FinanceQueryController,
    InvalidFinanceQueryCallback,
    StaleFinanceQueryCallback,
    TelegramQueryContext,
    TelegramQueryReceipt,
    parse_history_page_callback,
    parse_transaction_callback,
    parse_trash_page_callback,
)
from finbot.application.errors import ApplicationError, EntityNotFoundError

type FinanceQueryContextFactory = Callable[[int | None, Message], TelegramQueryContext]
type FinanceQueryReceiptDelivery = Callable[[Message, TelegramQueryReceipt], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FinanceQueryCallbackHandlers:
    """Parse callbacks, invoke the shared controller, and acknowledge post-commit."""

    controller: FinanceQueryController = field(repr=False)
    context_factory: FinanceQueryContextFactory = field(repr=False)
    deliver_untracked: FinanceQueryReceiptDelivery = field(repr=False)

    @staticmethod
    def _message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

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
            await self.deliver_untracked(message, receipt)
        await callback.answer()

    async def history_page(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        if callback.data == "h:noop":
            await callback.answer()
            return
        try:
            page = parse_history_page_callback(callback.data)
            receipt = await self.controller.list_transactions(
                self.context_factory(finbot_update_id, message),
                page=page,
                deleted=False,
            )
        except InvalidFinanceQueryCallback:
            await callback.answer("Кнопка устарела")
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def view_transaction(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_transaction_callback(callback.data)
            receipt = await self.controller.get_transaction(
                self.context_factory(finbot_update_id, message),
                target,
                deleted=False,
            )
        except InvalidFinanceQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except EntityNotFoundError:
            await callback.answer("Операция не найдена", show_alert=True)
            return
        except StaleFinanceQueryCallback as error:
            await callback.answer(str(error), show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def confirm_delete_transaction(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_transaction_callback(
                callback.data,
                prefix="tx:del:ask:",
            )
            receipt = await self.controller.confirm_transaction_delete(
                self.context_factory(finbot_update_id, message),
                target,
            )
        except InvalidFinanceQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except EntityNotFoundError, StaleFinanceQueryCallback:
            await callback.answer(
                "Операция изменилась. Откройте её заново",
                show_alert=True,
            )
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def trash_page(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            page = parse_trash_page_callback(callback.data)
            receipt = await self.controller.list_transactions(
                self.context_factory(finbot_update_id, message),
                page=page,
                deleted=True,
            )
        except InvalidFinanceQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def trash_view(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_transaction_callback(
                callback.data,
                prefix=DELETED_TRANSACTION_PREFIX,
            )
            receipt = await self.controller.get_transaction(
                self.context_factory(finbot_update_id, message),
                target,
                deleted=True,
            )
        except InvalidFinanceQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except EntityNotFoundError, StaleFinanceQueryCallback:
            await callback.answer("Операция изменилась", show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)
