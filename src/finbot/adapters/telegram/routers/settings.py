"""Focused Telegram handlers for settings navigation and bounded mutations."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from finbot.adapters.telegram.controllers.settings_mutations import (
    InvalidSettingsMutationCallback,
    SettingsMutationController,
    TelegramSettingsMutationContext,
    parse_settings_timezone_callback,
)
from finbot.adapters.telegram.controllers.settings_queries import (
    InvalidSettingsQueryCallback,
    SettingsQueryController,
    SettingsQueryError,
    TelegramSettingsQueryContext,
    TelegramSettingsQueryReceipt,
    parse_settings_catalog_callback,
    parse_settings_category_kind,
)
from finbot.application.errors import (
    ActiveDraftConflictError,
    ApplicationError,
    EntityNotFoundError,
    ObjectVersionConflictError,
)

type SettingsQueryContextFactory = Callable[
    [int | None, Message, bool], TelegramSettingsQueryContext
]
type SettingsMutationContextFactory = Callable[
    [int | None, Message], TelegramSettingsMutationContext
]
type SettingsReceiptDelivery = Callable[[Message, TelegramSettingsQueryReceipt], Awaitable[None]]
type SettingsQueryOperation = Callable[
    [TelegramSettingsQueryContext], Awaitable[TelegramSettingsQueryReceipt | None]
]


@dataclass(frozen=True, slots=True)
class SettingsRouter:
    """Own parsing, controller calls, delivery, errors, and acknowledgements."""

    query_controller: SettingsQueryController = field(repr=False)
    mutation_controller: SettingsMutationController = field(repr=False)
    query_context: SettingsQueryContextFactory = field(repr=False)
    mutation_context: SettingsMutationContextFactory = field(repr=False)
    deliver_untracked: SettingsReceiptDelivery = field(repr=False)

    @staticmethod
    def _message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

    async def _finish(
        self,
        callback: CallbackQuery,
        message: Message,
        update_id: int | None,
        receipt: TelegramSettingsQueryReceipt | None,
        *,
        success_text: str | None = None,
    ) -> None:
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            await self.deliver_untracked(message, receipt)
        await callback.answer(success_text)

    async def _run_query(
        self,
        callback: CallbackQuery,
        update_id: int | None,
        operation: SettingsQueryOperation,
    ) -> None:
        message = self._message(callback)
        if message is None:
            await callback.answer()
            return
        try:
            receipt = await operation(self.query_context(update_id, message, True))
        except (ApplicationError, SettingsQueryError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, update_id, receipt)

    async def show_settings(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        try:
            receipt = await self.query_controller.settings_main(
                self.query_context(finbot_update_id, message, False),
                suspend_active_draft=True,
            )
        except (ApplicationError, SettingsQueryError) as error:
            await message.answer(str(error))
            return
        if receipt is not None and finbot_update_id is None:
            await self.deliver_untracked(message, receipt)

    async def settings_main(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.settings_main(
                context,
                suspend_active_draft=False,
            ),
        )

    async def settings_accounts(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(callback, finbot_update_id, self.query_controller.accounts)

    async def settings_account(self, callback: CallbackQuery) -> None:
        await callback.answer(
            "Эта кнопка устарела. Откройте список счетов заново",
            show_alert=True,
        )

    async def settings_account_view(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(callback.data, prefix="sa:view:")
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.account(context, target),
        )

    async def settings_account_archive_ask(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(
                callback.data,
                prefix="sa:archive:ask:",
            )
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.account_archive_confirmation(
                context,
                target,
            ),
        )

    async def settings_accounts_archived(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(
            callback,
            finbot_update_id,
            self.query_controller.archived_accounts,
        )

    async def settings_categories(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(callback, finbot_update_id, self.query_controller.categories)

    async def settings_category_list(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            kind = parse_settings_category_kind(callback.data, prefix="sc:list:")
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.category_list(context, kind),
        )

    async def settings_category_view(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(callback.data, prefix="sc:view:")
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.category(context, target),
        )

    async def settings_category_archive_ask(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(
                callback.data,
                prefix="sc:archive:ask:",
            )
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.category_archive_confirmation(
                context,
                target,
            ),
        )

    async def settings_categories_archived(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        if callback.data is None:
            await callback.answer()
            return
        try:
            kind = parse_settings_category_kind(callback.data, prefix="sc:archived:")
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        await self._run_query(
            callback,
            finbot_update_id,
            lambda context: self.query_controller.archived_categories(context, kind),
        )

    async def settings_timezones(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(callback, finbot_update_id, self.query_controller.timezones)

    async def settings_account_new(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None:
            await callback.answer()
            return
        try:
            receipt = await self.mutation_controller.account_create(
                self.mutation_context(finbot_update_id, message)
            )
        except ActiveDraftConflictError:
            await callback.answer(
                "Сначала отмените или завершите текущий ввод",
                show_alert=True,
            )
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def settings_account_rename(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(callback.data, prefix="sa:rename:")
            receipt = await self.mutation_controller.account_rename(
                self.mutation_context(finbot_update_id, message),
                target,
            )
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except ActiveDraftConflictError:
            await callback.answer(
                "Сначала отмените или завершите текущий ввод",
                show_alert=True,
            )
            return
        except EntityNotFoundError, ObjectVersionConflictError:
            await callback.answer("Счёт изменился. Обновите список", show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def settings_category_new(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            kind = parse_settings_category_kind(callback.data, prefix="sc:new:")
            receipt = await self.mutation_controller.category_create(
                self.mutation_context(finbot_update_id, message),
                kind,
            )
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except ActiveDraftConflictError:
            await callback.answer(
                "Сначала отмените или завершите текущий ввод",
                show_alert=True,
            )
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def settings_category_rename(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_catalog_callback(callback.data, prefix="sc:rename:")
            receipt = await self.mutation_controller.category_rename(
                self.mutation_context(finbot_update_id, message),
                target,
            )
        except InvalidSettingsQueryCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except ActiveDraftConflictError:
            await callback.answer(
                "Сначала отмените или завершите текущий ввод",
                show_alert=True,
            )
            return
        except EntityNotFoundError, ObjectVersionConflictError:
            await callback.answer("Категория изменилась. Обновите список", show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, finbot_update_id, receipt)

    async def settings_timezone(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            target = parse_settings_timezone_callback(callback.data)
            receipt = await self.mutation_controller.timezone(
                self.mutation_context(finbot_update_id, message),
                target,
            )
        except InvalidSettingsMutationCallback:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        except ObjectVersionConflictError:
            await callback.answer(
                "Часовой пояс уже изменился. Обновите настройки",
                show_alert=True,
            )
            return
        except (ApplicationError, SettingsQueryError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(
            callback,
            message,
            finbot_update_id,
            receipt,
            success_text="Часовой пояс изменён",
        )

    async def settings_help(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run_query(callback, finbot_update_id, self.query_controller.help)


def register_settings_routes(dispatcher: Dispatcher, router: SettingsRouter) -> None:
    message = dispatcher.message
    message.register(router.show_settings, Command("settings"))
    message.register(router.show_settings, F.text == "⚙️ Настройки")

    callback = dispatcher.callback_query
    callback.register(router.settings_main, F.data == "s:back")
    callback.register(router.settings_accounts, F.data.in_({"s:accounts", "sa:list"}))
    callback.register(router.settings_account, F.data.startswith("s:account:"))
    callback.register(router.settings_account_view, F.data.startswith("sa:view:"))
    callback.register(router.settings_account_new, F.data == "sa:new")
    callback.register(router.settings_account_rename, F.data.startswith("sa:rename:"))
    callback.register(
        router.settings_account_archive_ask,
        F.data.startswith("sa:archive:ask:"),
    )
    callback.register(router.settings_accounts_archived, F.data == "sa:archived")
    callback.register(router.settings_categories, F.data == "sc:root")
    callback.register(router.settings_category_list, F.data.startswith("sc:list:"))
    callback.register(router.settings_category_view, F.data.startswith("sc:view:"))
    callback.register(router.settings_category_new, F.data.startswith("sc:new:"))
    callback.register(router.settings_category_rename, F.data.startswith("sc:rename:"))
    callback.register(
        router.settings_category_archive_ask,
        F.data.startswith("sc:archive:ask:"),
    )
    callback.register(
        router.settings_categories_archived,
        F.data.startswith("sc:archived:"),
    )
    callback.register(router.settings_timezones, F.data == "s:timezones")
    callback.register(router.settings_timezone, F.data.startswith("s:timezone:"))
    callback.register(router.settings_help, F.data == "s:help")


__all__ = ["SettingsRouter", "register_settings_routes"]
