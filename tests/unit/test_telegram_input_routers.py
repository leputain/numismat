from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Message

from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.finance_draft_text_input import (
    FinanceDraftTextInputController,
    TelegramFinanceDraftTextInputContext,
)
from finbot.adapters.telegram.controllers.ocr_images import (
    OcrImageController,
    OcrImageReceiptSnapshot,
    TelegramOcrImageContext,
)
from finbot.adapters.telegram.controllers.settings_text_input import (
    SettingsTextInputController,
    SettingsTextInputReceiptSnapshot,
    TelegramSettingsTextInputContext,
)
from finbot.adapters.telegram.controllers.transaction_edit_text_input import (
    TelegramTransactionEditTextInputContext,
    TransactionEditTextInputController,
    TransactionEditTextInputReceiptSnapshot,
)
from finbot.adapters.telegram.routers.inputs import (
    OcrImageRouter,
    TextInputContextFactories,
    TextInputRouter,
)
from finbot.application.draft_ingress import QuickDraftIngressNotApplicableError
from finbot.application.errors import InvalidStateError
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputNotApplicableError,
)
from finbot.application.settings_text_input import SettingsTextInputNotApplicableError
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputNotApplicableError,
)


class _Controller:
    def __init__(self, outcome: object | BaseException, calls: list[str], label: str) -> None:
        self._outcome = outcome
        self._calls = calls
        self._label = label

    async def submit(self, _context: object, _text: str) -> object:
        self._calls.append(self._label)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def begin_quick(self, _context: object, _text: str) -> object:
        self._calls.append(self._label)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _Delivery:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def finance(self, _message: Message, _update_id: int | None, _receipt: object) -> None:
        self._calls.append("deliver_finance")

    async def transaction(
        self, _message: Message, _update_id: int | None, _receipt: object
    ) -> None:
        self._calls.append("deliver_transaction")

    async def settings(self, _message: Message, _update_id: int | None, _receipt: object) -> None:
        self._calls.append("deliver_settings")

    async def quick(self, _message: Message, _update_id: int | None, _receipt: object) -> None:
        self._calls.append("deliver_quick")


def _message(text: str | None = "value") -> Message:
    return cast(
        Message,
        SimpleNamespace(
            text=text,
            answer=AsyncMock(),
        ),
    )


def _contexts() -> TextInputContextFactories:
    return TextInputContextFactories(
        finance=lambda _update_id, _message: cast(TelegramFinanceDraftTextInputContext, object()),
        transaction=lambda _update_id, _message: cast(
            TelegramTransactionEditTextInputContext, object()
        ),
        settings=lambda _update_id, _message: cast(TelegramSettingsTextInputContext, object()),
        quick=lambda _update_id, _message: cast(TelegramDraftIngressContext, object()),
    )


def _router(
    outcomes: tuple[object | BaseException, ...],
    calls: list[str],
) -> TextInputRouter:
    return TextInputRouter(
        cast(FinanceDraftTextInputController, _Controller(outcomes[0], calls, "finance")),
        cast(TransactionEditTextInputController, _Controller(outcomes[1], calls, "transaction")),
        cast(SettingsTextInputController, _Controller(outcomes[2], calls, "settings")),
        cast(DraftIngressController, _Controller(outcomes[3], calls, "quick")),
        _contexts(),
        cast(Any, _Delivery(calls)),
    )


@pytest.mark.asyncio
async def test_text_router_falls_through_only_not_applicable_in_exact_order() -> None:
    calls: list[str] = []
    receipt = cast(TransactionEditTextInputReceiptSnapshot, object())
    router = _router(
        (
            FinanceDraftTextInputNotApplicableError("not finance"),
            receipt,
            SettingsTextInputNotApplicableError("unused"),
            QuickDraftIngressNotApplicableError("unused"),
        ),
        calls,
    )

    await router.text_input(_message(), 41)

    assert calls == ["finance", "transaction", "deliver_transaction"]


@pytest.mark.asyncio
async def test_text_router_application_error_is_fail_closed() -> None:
    calls: list[str] = []
    message = _message()
    router = _router(
        (
            InvalidStateError("safe error"),
            cast(TransactionEditTextInputReceiptSnapshot, object()),
            cast(SettingsTextInputReceiptSnapshot, object()),
            cast(DraftIngressReceiptSnapshot, object()),
        ),
        calls,
    )

    await router.text_input(message)

    assert calls == ["finance"]
    message.answer.assert_awaited_once_with("safe error")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_text_router_duplicate_does_not_fall_through_or_deliver() -> None:
    calls: list[str] = []
    router = _router(
        (
            None,
            cast(TransactionEditTextInputReceiptSnapshot, object()),
            cast(SettingsTextInputReceiptSnapshot, object()),
            cast(DraftIngressReceiptSnapshot, object()),
        ),
        calls,
    )

    await router.text_input(_message(), 42)

    assert calls == ["finance"]


@pytest.mark.asyncio
async def test_text_router_unknown_state_returns_fixed_fail_closed_message() -> None:
    calls: list[str] = []
    message = _message()
    router = _router(
        (
            FinanceDraftTextInputNotApplicableError("not finance"),
            TransactionEditTextInputNotApplicableError("not transaction"),
            SettingsTextInputNotApplicableError("not settings"),
            QuickDraftIngressNotApplicableError("not quick"),
        ),
        calls,
    )

    await router.text_input(message)

    assert calls == ["finance", "transaction", "settings", "quick"]
    assert "не принимает текстовый ввод" in message.answer.await_args.args[0]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ocr_router_suppresses_processed_replay_before_download() -> None:
    controller = SimpleNamespace(process=AsyncMock())
    downloader = AsyncMock()
    delivery = AsyncMock()
    router = OcrImageRouter(
        cast(OcrImageController, controller),
        lambda _update_id, _message, _content, _mime: cast(TelegramOcrImageContext, object()),
        downloader,
        AsyncMock(return_value=True),
        delivery,
    )

    await router.image_input(_message(None), cast(Bot, object()), 43)

    downloader.assert_not_awaited()
    controller.process.assert_not_awaited()
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_ocr_router_delivers_untracked_only_after_controller_returns() -> None:
    events: list[str] = []
    receipt = cast(OcrImageReceiptSnapshot, object())

    async def download(_bot: Bot, _message: Message) -> tuple[bytes, str]:
        events.append("download")
        return b"bounded", "image/jpeg"

    async def process(_context: object) -> object:
        events.append("process")
        return receipt

    async def deliver(_message: Message, _receipt: object) -> None:
        events.append("deliver")

    router = OcrImageRouter(
        cast(OcrImageController, SimpleNamespace(process=process)),
        lambda _update_id, _message, _content, _mime: cast(TelegramOcrImageContext, object()),
        download,
        AsyncMock(return_value=False),
        deliver,
    )

    await router.image_input(_message(None), cast(Bot, object()), None)

    assert events == ["download", "process", "deliver"]
