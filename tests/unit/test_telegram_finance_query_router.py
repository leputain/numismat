from datetime import UTC, datetime
from typing import cast

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from finbot.adapters.telegram.controllers.finance_queries import (
    FinanceQueryController,
    TelegramQueryContext,
    TelegramQueryReceipt,
    TransactionCallbackTarget,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.routers.finance_queries import FinanceQueryCallbackHandlers


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="opaque",
        from_user=User(id=900, is_bot=False, first_name="Owner"),
        chat_instance="private",
        message=Message(
            message_id=700,
            date=datetime(2026, 8, 13, tzinfo=UTC),
            chat=Chat(id=800, type="private"),
            text="safe",
        ),
        data=data,
    )


def _context(update_id: int | None, message: Message) -> TelegramQueryContext:
    return TelegramQueryContext(
        TelegramMutationRequest(update_id, 900, message.chat.id, "ru", "UTC", "RUB"),
        message_id=message.message_id,
    )


class _Controller:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result: TelegramQueryReceipt | None = TelegramQueryReceipt("safe receipt")

    async def list_transactions(
        self,
        context: TelegramQueryContext,
        *,
        page: int,
        deleted: bool,
    ) -> TelegramQueryReceipt | None:
        self.events.append(f"list:{page}:{deleted}:{context.request.update_id}")
        return self.result

    async def get_transaction(
        self,
        context: TelegramQueryContext,
        target: TransactionCallbackTarget,
        *,
        deleted: bool,
    ) -> TelegramQueryReceipt | None:
        self.events.append(
            f"get:{target.expected_version}:{target.page}:{deleted}:{context.request.update_id}"
        )
        return self.result

    async def confirm_transaction_delete(
        self,
        context: TelegramQueryContext,
        target: TransactionCallbackTarget,
    ) -> TelegramQueryReceipt | None:
        self.events.append(
            f"delete:{target.expected_version}:{target.page}:{context.request.update_id}"
        )
        return self.result


def _handlers(events: list[str], controller: _Controller) -> FinanceQueryCallbackHandlers:
    async def deliver(_message: Message, _receipt: TelegramQueryReceipt) -> None:
        events.append("deliver")

    return FinanceQueryCallbackHandlers(
        cast(FinanceQueryController, controller),
        _context,
        deliver,
    )


@pytest.mark.asyncio
async def test_history_callback_parses_then_delivers_only_after_controller_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _Controller(events)

    async def answer(
        _self: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(f"answer:{text}:{show_alert}")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)

    await _handlers(events, controller).history_page(_callback("h:3"), None)

    assert events == ["list:3:False:None", "deliver", "answer:None:None"]


@pytest.mark.asyncio
async def test_tracked_delete_confirmation_never_performs_direct_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _Controller(events)

    async def answer(
        _self: CallbackQuery,
        text: str | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(f"answer:{text}")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)
    transaction_id = "00000000-0000-7000-8000-000000000401"

    await _handlers(events, controller).confirm_delete_transaction(
        _callback(f"tx:del:ask:{transaction_id}:7:2"),
        91_000_001,
    )

    assert events == ["delete:7:2:91000001", "answer:None"]


@pytest.mark.asyncio
async def test_malformed_callback_is_rejected_before_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def answer(
        _self: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(f"answer:{text}:{show_alert}")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)

    await _handlers(events, _Controller(events)).trash_view(_callback("z:view:broken"), 1)

    assert events == ["answer:Кнопка повреждена:True"]


def test_router_bundle_hides_injected_dependencies_from_repr() -> None:
    events: list[str] = []
    rendered = repr(_handlers(events, _Controller(events)))

    assert "Controller" not in rendered
    assert "deliver" not in rendered
