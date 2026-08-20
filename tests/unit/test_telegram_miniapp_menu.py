from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import MenuButtonCommands, MenuButtonWebApp

from finbot.adapters.telegram.miniapp_menu import MiniAppMenuConfigurator


def _bot(*, has_main_web_app: bool = False) -> tuple[Bot, AsyncMock]:
    set_menu = AsyncMock()
    value = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(has_main_web_app=has_main_web_app)),
        set_chat_menu_button=set_menu,
    )
    return cast(Bot, value), set_menu


@pytest.mark.asyncio
async def test_allowed_users_receive_independent_miniapp_menus_after_startup() -> None:
    bot, set_menu = _bot()
    configurator = MiniAppMenuConfigurator(
        allowed_user_ids=frozenset((42, 84)),
        public_url="https://miniapp.example.test",
    )

    await configurator.configure_startup(bot)
    await configurator.ensure_user_menu(bot, chat_id=42)
    await configurator.ensure_user_menu(bot, chat_id=84)
    await configurator.ensure_user_menu(bot, chat_id=84)

    calls = set_menu.await_args_list
    assert len(calls) == 3
    assert isinstance(calls[0].kwargs["menu_button"], MenuButtonCommands)
    assert [call.kwargs.get("chat_id") for call in calls[1:]] == [42, 84]
    assert all(isinstance(call.kwargs["menu_button"], MenuButtonWebApp) for call in calls[1:])


@pytest.mark.asyncio
async def test_public_or_unlisted_miniapp_menu_configuration_fails_closed() -> None:
    public_bot, _ = _bot(has_main_web_app=True)
    configurator = MiniAppMenuConfigurator(
        allowed_user_ids=frozenset((42,)),
        public_url="https://miniapp.example.test",
    )

    with pytest.raises(RuntimeError, match="Main Mini App must be disabled"):
        await configurator.configure_startup(public_bot)

    private_bot, set_menu = _bot()
    with pytest.raises(ValueError, match="allowed private user"):
        await configurator.ensure_user_menu(private_bot, chat_id=84)
    set_menu.assert_not_awaited()
