from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

_CHAT_NOT_FOUND = "chat not found"
_MENU_TEXT = "Открыть Numismat"


def _is_missing_private_chat(error: TelegramBadRequest) -> bool:
    return _CHAT_NOT_FOUND in str(error).casefold()


class MiniAppMenuConfigurator:
    """Keep the public bot menu inert and expose the Mini App only to its owner."""

    __slots__ = (
        "_logger",
        "_owner_configured",
        "_owner_telegram_user_id",
        "_public_url",
    )

    def __init__(
        self,
        *,
        owner_telegram_user_id: int,
        public_url: str | None,
    ) -> None:
        if type(owner_telegram_user_id) is not int or owner_telegram_user_id <= 0:
            raise ValueError("owner Telegram user id must be positive")
        if public_url is not None and type(public_url) is not str:
            raise TypeError("Mini App public URL must be a string")
        self._owner_telegram_user_id = owner_telegram_user_id
        self._public_url = public_url
        self._owner_configured = False
        self._logger = logging.getLogger("finbot.telegram.miniapp_menu")

    @property
    def is_enabled(self) -> bool:
        return self._public_url is not None

    async def configure_startup(self, bot: Bot) -> None:
        """Fail closed on a public Main Mini App and install the owner menu."""

        if self._public_url is None:
            return
        try:
            identity = await bot.get_me()
            if identity.has_main_web_app is True:
                raise RuntimeError("Telegram Main Mini App must be disabled")
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            try:
                await self._set_owner_menu(bot, self._owner_telegram_user_id)
            except TelegramBadRequest as exc:
                if not _is_missing_private_chat(exc):
                    raise
                self._logger.info(
                    "miniapp_menu_configuration_completed",
                    extra={"result": "ignored"},
                )
                return
        except Exception:
            self._logger.error(
                "miniapp_menu_configuration_completed",
                extra={"result": "error"},
            )
            raise

        self._owner_configured = True
        self._logger.info(
            "miniapp_menu_configuration_completed",
            extra={"result": "success"},
        )

    async def ensure_owner_menu(self, bot: Bot, *, chat_id: int) -> None:
        """Retry configuration after the authorized owner has opened the private chat."""

        if self._public_url is None or self._owner_configured:
            return
        if type(chat_id) is not int or chat_id != self._owner_telegram_user_id:
            raise ValueError("Mini App menu target must be the configured owner")
        try:
            await self._set_owner_menu(bot, chat_id)
        except Exception:
            self._logger.error(
                "miniapp_menu_configuration_completed",
                extra={"result": "error"},
            )
            raise
        self._owner_configured = True
        self._logger.info(
            "miniapp_menu_configuration_completed",
            extra={"result": "success"},
        )

    async def _set_owner_menu(self, bot: Bot, chat_id: int) -> None:
        public_url = self._public_url
        if public_url is None:  # pragma: no cover - guarded by public methods
            raise RuntimeError("Mini App menu is disabled")
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(
                text=_MENU_TEXT,
                web_app=WebAppInfo(url=public_url),
            ),
        )


__all__ = ["MiniAppMenuConfigurator"]
