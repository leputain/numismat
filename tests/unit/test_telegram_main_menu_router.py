from dataclasses import dataclass
from inspect import getsource
from typing import cast

import pytest
from aiogram import Dispatcher
from aiogram.types import Message

from finbot.adapters.telegram.controllers.main_menu import (
    MainMenuAction,
    MainMenuController,
    MainMenuReceiptSnapshot,
    TelegramMainMenuContext,
)
from finbot.adapters.telegram.routers.main_menu import (
    MainMenuReceiptDelivery,
    MainMenuRequestDefaults,
    MainMenuRouter,
)
from finbot.bootstrap import build_dispatcher


@dataclass(slots=True)
class _Chat:
    id: int
    type: str = "private"


class _Message:
    def __init__(self) -> None:
        self.chat = _Chat(93_000_003)
        self.from_user = _Chat(93_000_003)
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        del kwargs
        self.answers.append(text)


class _Controller:
    def __init__(
        self,
        events: list[str],
        receipt: MainMenuReceiptSnapshot | None,
    ) -> None:
        self.events = events
        self.receipt = receipt
        self.calls: list[tuple[TelegramMainMenuContext, MainMenuAction]] = []

    async def open(
        self,
        context: TelegramMainMenuContext,
        action: MainMenuAction,
    ) -> MainMenuReceiptSnapshot | None:
        self.calls.append((context, action))
        self.events.append("controller.commit")
        return self.receipt


class _Delivery:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.receipts: list[MainMenuReceiptSnapshot] = []

    async def deliver(
        self,
        message: Message,
        receipt: MainMenuReceiptSnapshot,
    ) -> None:
        del message
        self.events.append("network")
        self.receipts.append(receipt)


def _router(
    events: list[str],
    receipt: MainMenuReceiptSnapshot | None,
) -> tuple[MainMenuRouter, _Controller, _Delivery]:
    controller = _Controller(events, receipt)
    delivery = _Delivery(events)
    router = MainMenuRouter(
        cast(MainMenuController, controller),
        cast(MainMenuReceiptDelivery, delivery),
        MainMenuRequestDefaults("ru", "UTC", "RUB"),
    )
    return router, controller, delivery


@pytest.mark.asyncio
async def test_untracked_handler_delivers_only_after_controller_commit() -> None:
    events: list[str] = []
    receipt = MainMenuReceiptSnapshot(MainMenuAction.START)
    router, controller, delivery = _router(events, receipt)
    message = cast(Message, _Message())

    await router.start(message)

    assert controller.calls[0][1] is MainMenuAction.START
    assert delivery.receipts == [receipt]
    assert events == ["controller.commit", "network"]


@pytest.mark.asyncio
async def test_tracked_handler_leaves_delivery_to_durable_outbox_middleware() -> None:
    events: list[str] = []
    receipt = MainMenuReceiptSnapshot(MainMenuAction.QUICK_HELP)
    router, controller, delivery = _router(events, receipt)
    message = cast(Message, _Message())

    await router.quick_help(message, finbot_update_id=91_000_001)

    request = controller.calls[0][0].request
    assert request.update_id == 91_000_001
    assert request.owner_telegram_user_id == 93_000_003
    assert request.chat_id == 93_000_003
    assert delivery.receipts == []
    assert events == ["controller.commit"]


@pytest.mark.asyncio
async def test_duplicate_handler_does_not_directly_deliver() -> None:
    events: list[str] = []
    router, _, delivery = _router(events, None)

    await router.menu(cast(Message, _Message()), finbot_update_id=91_000_001)

    assert delivery.receipts == []
    assert events == ["controller.commit"]


def test_main_menu_handlers_register_before_a_broad_message_fallback() -> None:
    events: list[str] = []
    router, _, _ = _router(events, MainMenuReceiptSnapshot(MainMenuAction.MENU))
    dispatcher = Dispatcher()
    router.register(dispatcher)

    async def broad_fallback(message: Message) -> None:
        del message

    dispatcher.message.register(broad_fallback)
    callback_names = [handler.callback.__name__ for handler in dispatcher.message.handlers]

    assert callback_names[:6] == [
        "start",
        "menu",
        "menu",
        "help_menu",
        "help_menu",
        "quick_help",
    ]
    assert callback_names[-1] == "broad_fallback"


def test_bootstrap_registers_focused_menu_router_before_the_text_catch_all() -> None:
    source = getsource(build_dispatcher)

    assert source.index("main_menu_router.register(dp)") < source.index(
        "text_input_router.register(dp)"
    )
    assert "async def start(" not in source
    assert "async def menu(" not in source
    assert "async def help_menu(" not in source
    assert "async def quick_help(" not in source


def test_request_defaults_and_context_hide_private_identifiers_from_repr() -> None:
    defaults = MainMenuRequestDefaults("private-locale", "UTC", "RUB")
    context = TelegramMainMenuContext(defaults.request(None, cast(Message, _Message())))

    rendered = f"{defaults!r} {context!r}"

    assert "92000002" not in rendered
    assert "93000003" not in rendered
    assert "private-locale" not in rendered
