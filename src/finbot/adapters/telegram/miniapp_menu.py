from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

_CHAT_NOT_FOUND = "chat not found"
_MENU_TEXT = "Открыть Numismat"
_MAX_TELEGRAM_USER_ID = 2**52 - 1


def _is_missing_private_chat(error: TelegramBadRequest) -> bool:
    return _CHAT_NOT_FOUND in str(error).casefold()


class MiniAppMenuConfigurator:
    """Keep the public bot menu inert and expose the Mini App per allowed private user."""

    __slots__ = (
        "_allowed_user_ids",
        "_configured_user_ids",
        "_logger",
        "_public_url",
    )

    def __init__(
        self,
        *,
        allowed_user_ids: frozenset[int],
        public_url: str | None,
    ) -> None:
        if (
            not isinstance(allowed_user_ids, frozenset)
            or not allowed_user_ids
            or any(
                type(user_id) is not int or not 1 <= user_id <= _MAX_TELEGRAM_USER_ID
                for user_id in allowed_user_ids
            )
        ):
            raise ValueError("allowed Telegram user ids must be a non-empty immutable set")
        if public_url is not None and type(public_url) is not str:
            raise TypeError("Mini App public URL must be a string")
        self._allowed_user_ids = allowed_user_ids
        self._public_url = public_url
        self._configured_user_ids: set[int] = set()
        self._logger = logging.getLogger("finbot.telegram.miniapp_menu")

    @property
    def is_enabled(self) -> bool:
        return self._public_url is not None

    async def configure_startup(self, bot: Bot) -> None:
        """Fail closed on a public Main Mini App and keep the default menu inert."""

        if self._public_url is None:
            return
        try:
            identity = await bot.get_me()
            if identity.has_main_web_app is True:
                raise RuntimeError("Telegram Main Mini App must be disabled")
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            self._logger.error(
                "miniapp_menu_configuration_completed",
                extra={"result": "error"},
            )
            raise

        self._logger.info(
            "miniapp_menu_configuration_completed",
            extra={"result": "success"},
        )

    async def ensure_user_menu(self, bot: Bot, *, chat_id: int) -> None:
        """Install one menu only after that allowed user's onboarding transaction commits."""

        if self._public_url is None:
            return
        if type(chat_id) is not int or chat_id not in self._allowed_user_ids:
            raise ValueError("Mini App menu target must be an allowed private user")
        if chat_id in self._configured_user_ids:
            return
        try:
            await self._set_user_menu(bot, chat_id)
        except TelegramBadRequest as exc:
            if not _is_missing_private_chat(exc):
                self._logger.error(
                    "miniapp_menu_configuration_completed",
                    extra={"result": "error"},
                )
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
        self._configured_user_ids.add(chat_id)
        self._logger.info(
            "miniapp_menu_configuration_completed",
            extra={"result": "success"},
        )

    async def _set_user_menu(self, bot: Bot, chat_id: int) -> None:
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
