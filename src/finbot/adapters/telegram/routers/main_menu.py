from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from finbot.adapters.telegram.controllers.main_menu import (
    MainMenuAction,
    MainMenuController,
    MainMenuReceiptSnapshot,
    TelegramMainMenuContext,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.principal import telegram_principal_for_message
from finbot.application.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class MainMenuRequestDefaults:
    locale: str = field(repr=False)
    timezone: str = field(repr=False)
    currency: str = field(repr=False)

    def request(
        self,
        update_id: int | None,
        message: Message,
    ) -> TelegramMutationRequest:
        principal = telegram_principal_for_message(message)
        return TelegramMutationRequest(
            update_id=update_id,
            owner_telegram_user_id=principal.telegram_user_id,
            chat_id=principal.chat_id,
            locale=self.locale,
            timezone=self.timezone,
            currency=self.currency,
        )


class MainMenuReceiptDelivery(Protocol):
    async def deliver(
        self,
        message: Message,
        receipt: MainMenuReceiptSnapshot,
    ) -> None: ...


class MiniAppUserMenu(Protocol):
    async def ensure_user_menu(self, bot: Bot, *, chat_id: int) -> None: ...


class MainMenuRouter:
    """Concrete root-router handlers for menu navigation and quick-input help."""

    __slots__ = ("_controller", "_defaults", "_direct_delivery", "_miniapp_menu")

    def __init__(
        self,
        controller: MainMenuController,
        direct_delivery: MainMenuReceiptDelivery,
        defaults: MainMenuRequestDefaults,
        miniapp_menu: MiniAppUserMenu | None = None,
    ) -> None:
        self._controller = controller
        self._direct_delivery = direct_delivery
        self._defaults = defaults
        self._miniapp_menu = miniapp_menu

    def register(self, dispatcher: Dispatcher) -> None:
        """Register on the root observer before broad legacy message handlers."""

        observer = dispatcher.message
        observer.register(self.start, CommandStart())
        observer.register(self.menu, Command("menu"))
        observer.register(self.menu, F.text.in_({"🏠 Меню"}))
        observer.register(self.help_menu, Command("help"))
        observer.register(
            self.help_menu,
            F.text.in_({"❓ Помощь", "❓ Как пользоваться", "❓ Справка"}),
        )
        observer.register(self.quick_help, F.text.in_({"⚡ Быстрый ввод"}))
        observer.register(self.more_menu, F.text.in_({"••• Ещё", "⋯ Ещё"}))

    async def _open(
        self,
        message: Message,
        update_id: int | None,
        action: MainMenuAction,
    ) -> bool:
        try:
            receipt = await self._controller.open(
                TelegramMainMenuContext(self._defaults.request(update_id, message)),
                action,
            )
        except ApplicationError as exc:
            await message.answer(str(exc))
            return False
        if receipt is not None and update_id is None:
            await self._direct_delivery.deliver(message, receipt)
        return True

    async def start(self, message: Message, finbot_update_id: int | None = None) -> None:
        if await self._open(message, finbot_update_id, MainMenuAction.START):
            await self._ensure_user_menu(message)

    async def menu(self, message: Message, finbot_update_id: int | None = None) -> None:
        if await self._open(message, finbot_update_id, MainMenuAction.MENU):
            await self._ensure_user_menu(message)

    async def _ensure_user_menu(self, message: Message) -> None:
        if self._miniapp_menu is None:
            return
        bot = message.bot
        if bot is None:  # pragma: no cover - aiogram dispatch always binds the bot
            raise RuntimeError("Telegram bot is not bound to the message")
        principal = telegram_principal_for_message(message)
        await self._miniapp_menu.ensure_user_menu(bot, chat_id=principal.chat_id)

    async def help_menu(self, message: Message, finbot_update_id: int | None = None) -> None:
        await self._open(message, finbot_update_id, MainMenuAction.HELP)

    async def quick_help(self, message: Message, finbot_update_id: int | None = None) -> None:
        await self._open(message, finbot_update_id, MainMenuAction.QUICK_HELP)

    async def more_menu(self, message: Message, finbot_update_id: int | None = None) -> None:
        await self._open(message, finbot_update_id, MainMenuAction.MORE)


__all__ = [
    "MainMenuReceiptDelivery",
    "MainMenuRequestDefaults",
    "MainMenuRouter",
]
