"""Telegram routing for saved-transaction lifecycle actions."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.finance_queries import (
    InvalidFinanceQueryCallback,
    parse_transaction_callback,
)
from finbot.adapters.telegram.controllers.transaction_edit_ingress import (
    TelegramTransactionEditContext,
    TransactionEditIngressController,
    TransactionEditReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_lifecycle import (
    TelegramTransactionLifecycleContext,
    TransactionLifecycleController,
    TransactionLifecycleOperation,
    TransactionLifecycleReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.undo import (
    TelegramUndoContext,
    UndoController,
    UndoReceiptSnapshot,
)
from finbot.adapters.telegram.ui import MAX_TX_HISTORY_PAGE
from finbot.application.draft_ingress import DraftIngressStatus
from finbot.application.errors import ApplicationError, ObjectVersionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_edit_ingress import TransactionEditIngressStatus
from finbot.domain.errors import FinbotError

type TrackedCallbackHandler = Callable[[CallbackQuery, int | None], Awaitable[None]]
type TransactionLifecycleContextFactory = Callable[
    [int | None, Message, int | None],
    TelegramTransactionLifecycleContext,
]
type RepeatContextFactory = Callable[
    [int | None, Message, int],
    TelegramDraftIngressContext,
]
type TransactionEditContextFactory = Callable[
    [int | None, Message, int],
    TelegramTransactionEditContext,
]
type UndoContextFactory = Callable[[int | None, Message], TelegramUndoContext]
type TransactionLifecycleDirectDelivery = Callable[
    [Message, TransactionLifecycleReceiptSnapshot],
    Awaitable[None],
]
type RepeatDirectDelivery = Callable[
    [Message, DraftIngressReceiptSnapshot],
    Awaitable[None],
]
type TransactionEditDirectDelivery = Callable[
    [Message, TransactionEditReceiptSnapshot],
    Awaitable[None],
]
type UndoDirectDelivery = Callable[[Message, UndoReceiptSnapshot], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TransactionLifecycleControllers:
    transactions: TransactionLifecycleController = field(repr=False)
    repeats: DraftIngressController = field(repr=False)
    edits: TransactionEditIngressController = field(repr=False)
    undo: UndoController = field(repr=False)


@dataclass(frozen=True, slots=True)
class TransactionLifecycleContexts:
    transaction: TransactionLifecycleContextFactory = field(repr=False)
    repeat: RepeatContextFactory = field(repr=False)
    edit: TransactionEditContextFactory = field(repr=False)
    undo: UndoContextFactory = field(repr=False)


@dataclass(frozen=True, slots=True)
class TransactionLifecycleDeliveries:
    transaction: TransactionLifecycleDirectDelivery = field(repr=False)
    repeat: RepeatDirectDelivery = field(repr=False)
    edit: TransactionEditDirectDelivery = field(repr=False)
    undo: UndoDirectDelivery = field(repr=False)


def parse_transaction_lifecycle_callback(
    data: str,
    prefix: str,
    *,
    max_page: int = MAX_TX_HISTORY_PAGE,
) -> tuple[UUID, int, int]:
    """Decode the legacy UUID/version/page callback envelope."""

    try:
        target = parse_transaction_callback(data, prefix=prefix)
    except InvalidFinanceQueryCallback as error:
        raise ValueError("invalid transaction callback") from error
    if target.page > max_page:
        raise ValueError("invalid transaction history page")
    payload_parts = data.removeprefix(prefix).split(":")
    canonical = f"{target.transaction_id}:{target.expected_version}"
    if len(payload_parts) == 3:
        canonical = f"{canonical}:{target.page}"
    if data != f"{prefix}{canonical}":
        raise ValueError("non-canonical transaction callback")
    return target.transaction_id, target.expected_version, target.page


@dataclass(frozen=True, slots=True)
class TransactionLifecycleRouter:
    """Thin handlers over atomic controllers and post-commit direct deliveries."""

    controllers: TransactionLifecycleControllers = field(repr=False)
    contexts: TransactionLifecycleContexts = field(repr=False)
    deliveries: TransactionLifecycleDeliveries = field(repr=False)
    confirm_legacy_delete: TrackedCallbackHandler = field(repr=False)

    def register(self, dispatcher: Dispatcher) -> None:
        """Preserve the legacy filters before broad callback/message fallbacks."""

        callbacks = dispatcher.callback_query
        callbacks.register(self.delete_transaction, F.data.startswith("tx:del:do:"))
        callbacks.register(
            self.legacy_delete_requires_confirmation,
            F.data.regexp(r"^tx:del:[0-9a-fA-F-]{36}:\d+(?::\d+)?$"),
        )
        callbacks.register(self.restore_deleted, F.data.startswith("tx:restore:"))
        callbacks.register(self.repeat_transaction, F.data.startswith("tx:repeat:"))
        callbacks.register(self.trash_restore, F.data.startswith("z:restore:"))
        callbacks.register(self.trash_noop, F.data == "z:noop")
        callbacks.register(self.edit_menu, F.data.startswith("tx:edit:"))

        messages = dispatcher.message
        messages.register(self.undo, Command("undo"))
        messages.register(self.undo, F.text.in_({"↩️ Отменить действие", "↩️ Отменить"}))

    @staticmethod
    def _message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

    async def _mutate_transaction(
        self,
        callback: CallbackQuery,
        update_id: int | None,
        *,
        prefix: str,
        operation: TransactionLifecycleOperation,
        preserve_history_page: bool,
        success_text: str,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = parse_transaction_lifecycle_callback(
                callback.data,
                prefix,
                max_page=MAX_PAGE,
            )
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        try:
            context = self.contexts.transaction(
                update_id,
                message,
                history_page if preserve_history_page else None,
            )
            invoke = (
                self.controllers.transactions.delete
                if operation is TransactionLifecycleOperation.DELETE
                else self.controllers.transactions.restore
            )
            receipt = await invoke(context, transaction_id, version)
        except (ValueError, FinbotError, ApplicationError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            await self.deliveries.transaction(message, receipt)
        await callback.answer(success_text)

    async def delete_transaction(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._mutate_transaction(
            callback,
            finbot_update_id,
            prefix="tx:del:do:",
            operation=TransactionLifecycleOperation.DELETE,
            preserve_history_page=True,
            success_text="Удалено",
        )

    async def legacy_delete_requires_confirmation(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        translated = callback.model_copy(
            update={"data": "tx:del:ask:" + callback.data.removeprefix("tx:del:")}
        )
        await self.confirm_legacy_delete(translated, finbot_update_id)

    async def restore_deleted(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._mutate_transaction(
            callback,
            finbot_update_id,
            prefix="tx:restore:",
            operation=TransactionLifecycleOperation.RESTORE,
            preserve_history_page=True,
            success_text="Восстановлено",
        )

    async def repeat_transaction(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = parse_transaction_lifecycle_callback(
                callback.data,
                "tx:repeat:",
                max_page=MAX_PAGE,
            )
        except ValueError, TypeError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        try:
            receipt = await self.controllers.repeats.begin_repeat(
                self.contexts.repeat(finbot_update_id, message, history_page),
                transaction_id,
                version,
            )
        except ObjectVersionConflictError:
            await callback.answer(
                "Операция изменилась. Откройте её заново",
                show_alert=True,
            )
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if finbot_update_id is None:
            await self.deliveries.repeat(message, receipt)
        await callback.answer(
            "Проверьте копию"
            if receipt.result.status is DraftIngressStatus.STARTED
            else "Выберите действие"
        )

    async def trash_restore(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._mutate_transaction(
            callback,
            finbot_update_id,
            prefix="z:restore:",
            operation=TransactionLifecycleOperation.RESTORE,
            preserve_history_page=False,
            success_text="Восстановлено",
        )

    @staticmethod
    async def trash_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    async def edit_menu(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            transaction_id, version, history_page = parse_transaction_lifecycle_callback(
                callback.data,
                "tx:edit:",
            )
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        try:
            receipt = await self.controllers.edits.begin(
                self.contexts.edit(finbot_update_id, message, history_page),
                transaction_id,
                version,
            )
        except ObjectVersionConflictError:
            await callback.answer(
                "Карточка устарела. Откройте историю",
                show_alert=True,
            )
            return
        except (ApplicationError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if finbot_update_id is None:
            await self.deliveries.edit(message, receipt)
        await callback.answer(
            "Выберите поле"
            if receipt.result.status is TransactionEditIngressStatus.DRAFT_CREATED
            else "Выберите действие"
        )

    async def undo(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        receipt = await self.controllers.undo.execute(self.contexts.undo(finbot_update_id, message))
        if receipt is not None and finbot_update_id is None:
            await self.deliveries.undo(message, receipt)


__all__ = [
    "TransactionLifecycleContexts",
    "TransactionLifecycleControllers",
    "TransactionLifecycleDeliveries",
    "TransactionLifecycleRouter",
    "parse_transaction_lifecycle_callback",
]
