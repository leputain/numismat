from dataclasses import dataclass
from inspect import getsource
from typing import cast

import pytest
from aiogram import Dispatcher
from aiogram.types import Message

from finbot.adapters.telegram.controllers.exports import CsvExportController
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.routers.exports import (
    CsvExportDirectDelivery,
    CsvExportRequestDefaults,
    CsvExportRouter,
)
from finbot.application.export import CsvExportReceiptSnapshot
from finbot.bootstrap import build_dispatcher


@dataclass(slots=True)
class _Chat:
    id: int


class _Message:
    def __init__(self) -> None:
        self.chat = _Chat(93_000_003)


class _Controller:
    def __init__(
        self,
        events: list[str],
        receipt: CsvExportReceiptSnapshot | None,
    ) -> None:
        self.events = events
        self.receipt = receipt
        self.calls: list[TelegramMutationRequest] = []

    async def request(
        self,
        request: TelegramMutationRequest,
    ) -> CsvExportReceiptSnapshot | None:
        self.calls.append(request)
        self.events.append("controller.commit")
        return self.receipt


class _Delivery:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.receipts: list[CsvExportReceiptSnapshot] = []

    async def deliver(
        self,
        message: Message,
        receipt: CsvExportReceiptSnapshot,
    ) -> None:
        del message
        self.events.append("network")
        self.receipts.append(receipt)


def _router(
    events: list[str],
    receipt: CsvExportReceiptSnapshot | None,
) -> tuple[CsvExportRouter, _Controller, _Delivery]:
    controller = _Controller(events, receipt)
    delivery = _Delivery(events)
    router = CsvExportRouter(
        cast(CsvExportController, controller),
        cast(CsvExportDirectDelivery, delivery),
        CsvExportRequestDefaults(92_000_002, "ru", "UTC", "RUB"),
    )
    return router, controller, delivery


@pytest.mark.asyncio
async def test_untracked_export_delivers_only_after_controller_commit() -> None:
    events: list[str] = []
    receipt = CsvExportReceiptSnapshot()
    router, controller, delivery = _router(events, receipt)

    await router.export_csv(cast(Message, _Message()))

    assert controller.calls[0].update_id is None
    assert delivery.receipts == [receipt]
    assert events == ["controller.commit", "network"]


@pytest.mark.asyncio
async def test_tracked_export_leaves_delivery_to_durable_outbox() -> None:
    events: list[str] = []
    receipt = CsvExportReceiptSnapshot()
    router, controller, delivery = _router(events, receipt)

    await router.export_csv(cast(Message, _Message()), finbot_update_id=91_000_001)

    assert controller.calls[0].update_id == 91_000_001
    assert delivery.receipts == []
    assert events == ["controller.commit"]


@pytest.mark.asyncio
async def test_duplicate_export_never_delivers_directly() -> None:
    events: list[str] = []
    router, _, delivery = _router(events, None)

    await router.export_csv(cast(Message, _Message()), finbot_update_id=91_000_001)

    assert delivery.receipts == []
    assert events == ["controller.commit"]


def test_export_router_registers_before_a_broad_message_fallback() -> None:
    events: list[str] = []
    router, _, _ = _router(events, CsvExportReceiptSnapshot())
    dispatcher = Dispatcher()
    router.register(dispatcher)

    async def broad_fallback(message: Message) -> None:
        del message

    dispatcher.message.register(broad_fallback)
    names = [handler.callback.__name__ for handler in dispatcher.message.handlers]

    assert names[:2] == ["export_csv", "export_csv"]
    assert names[-1] == "broad_fallback"


def test_bootstrap_registers_focused_export_router_before_text_catch_all() -> None:
    source = getsource(build_dispatcher)

    assert source.index("csv_export_router.register(dp)") < source.index(
        "text_input_router.register(dp)"
    )
    assert "async def export_csv(" not in source


def test_export_request_defaults_hide_private_identifiers_from_repr() -> None:
    defaults = CsvExportRequestDefaults(92_000_002, "private-locale", "UTC", "RUB")
    request = defaults.request(None, cast(Message, _Message()))

    rendered = f"{defaults!r} {request!r}"

    assert "92000002" not in rendered
    assert "93000003" not in rendered
    assert "private-locale" not in rendered
