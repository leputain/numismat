from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.handler import HandlerObject
from aiogram.types import CallbackQuery, Chat, Message, User

from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.transaction_edit_ingress import (
    TelegramTransactionEditContext,
    TransactionEditIngressController,
)
from finbot.adapters.telegram.controllers.transaction_lifecycle import (
    TelegramTransactionLifecycleContext,
    TransactionLifecycleController,
)
from finbot.adapters.telegram.controllers.undo import TelegramUndoContext, UndoController
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.routers.transaction_lifecycle import (
    TransactionLifecycleContexts,
    TransactionLifecycleControllers,
    TransactionLifecycleDeliveries,
    TransactionLifecycleRouter,
    parse_transaction_lifecycle_callback,
)
from finbot.application.interactions import MAX_OBJECT_VERSION, MAX_PAGE

TRANSACTION_ID = UUID("00000000-0000-7000-8000-0000000004af")

type TrackedHandler = Callable[[CallbackQuery, int | None], Awaitable[None]]


def _message(text: str = "safe") -> Message:
    return Message(
        message_id=700,
        date=datetime(2026, 8, 13, tzinfo=UTC),
        chat=Chat(id=800, type="private"),
        text=text,
    )


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="opaque",
        from_user=User(id=900, is_bot=False, first_name="Owner"),
        chat_instance="private",
        message=_message(),
        data=data,
    )


def _request(update_id: int | None, message: Message) -> TelegramMutationRequest:
    return TelegramMutationRequest(update_id, 900, message.chat.id, "ru", "UTC", "RUB")


class _Transactions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result: object | None = object()

    async def delete(
        self,
        context: TelegramTransactionLifecycleContext,
        transaction_id: UUID,
        version: int,
    ) -> Any:
        self.events.append(
            f"delete:{context.request.update_id}:{transaction_id}:{version}:{context.history_page}"
        )
        return self.result

    async def restore(
        self,
        context: TelegramTransactionLifecycleContext,
        transaction_id: UUID,
        version: int,
    ) -> Any:
        self.events.append(
            f"restore:{context.request.update_id}:{transaction_id}:{version}:{context.history_page}"
        )
        return self.result


class _Repeats:
    async def begin_repeat(self, *_args: object) -> Any:
        raise AssertionError("repeat controller must not be called")


class _Edits:
    async def begin(self, *_args: object) -> Any:
        raise AssertionError("edit controller must not be called")


class _Undo:
    async def execute(self, _context: TelegramUndoContext) -> Any:
        return None


def _router(events: list[str]) -> tuple[TransactionLifecycleRouter, _Transactions]:
    transactions = _Transactions(events)

    def lifecycle_context(
        update_id: int | None,
        message: Message,
        page: int | None,
    ) -> TelegramTransactionLifecycleContext:
        events.append(f"context:{page}")
        return TelegramTransactionLifecycleContext(_request(update_id, message), 700, page)

    def repeat_context(
        update_id: int | None,
        message: Message,
        page: int,
    ) -> TelegramDraftIngressContext:
        return TelegramDraftIngressContext(_request(update_id, message), 700, page)

    def edit_context(
        update_id: int | None,
        message: Message,
        page: int,
    ) -> TelegramTransactionEditContext:
        return TelegramTransactionEditContext(_request(update_id, message), 700, page)

    async def deliver_transaction(_message: Message, _receipt: object) -> None:
        events.append("deliver_transaction")

    async def deliver_unreachable(_message: Message, _receipt: object) -> None:
        raise AssertionError("unexpected direct delivery")

    async def confirm_legacy(callback: CallbackQuery, update_id: int | None) -> None:
        events.append(f"legacy:{callback.data}:{update_id}")

    router = TransactionLifecycleRouter(
        TransactionLifecycleControllers(
            cast(TransactionLifecycleController, transactions),
            cast(DraftIngressController, _Repeats()),
            cast(TransactionEditIngressController, _Edits()),
            cast(UndoController, _Undo()),
        ),
        TransactionLifecycleContexts(
            lifecycle_context,
            repeat_context,
            edit_context,
            lambda update_id, message: TelegramUndoContext(_request(update_id, message)),
        ),
        TransactionLifecycleDeliveries(
            cast(Any, deliver_transaction),
            cast(Any, deliver_unreachable),
            cast(Any, deliver_unreachable),
            cast(Any, deliver_unreachable),
        ),
        confirm_legacy,
    )
    return router, transactions


async def _first_matching(
    handlers: list[HandlerObject],
    callback: CallbackQuery,
) -> HandlerObject | None:
    for handler in handlers:
        matched, _ = await handler.check(callback)
        if matched:
            return handler
    return None


def test_lifecycle_routes_preserve_exact_legacy_registration_order() -> None:
    dispatcher = Dispatcher()
    router, _transactions = _router([])

    router.register(dispatcher)

    callbacks = tuple(item.callback for item in dispatcher.callback_query.handlers)
    assert callbacks == (
        router.delete_transaction,
        router.legacy_delete_requires_confirmation,
        router.restore_deleted,
        router.repeat_transaction,
        router.trash_restore,
        router.trash_noop,
        router.edit_menu,
    )
    assert tuple(item.callback for item in dispatcher.message.handlers) == (
        router.undo,
        router.undo,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (f"tx:del:do:{TRANSACTION_ID}:3:2", "delete_transaction"),
        (f"tx:del:{TRANSACTION_ID}:3:2", "legacy_delete_requires_confirmation"),
        (f"tx:restore:{TRANSACTION_ID}:3:2", "restore_deleted"),
        (f"tx:repeat:{TRANSACTION_ID}:3:2", "repeat_transaction"),
        (f"z:restore:{TRANSACTION_ID}:3:2", "trash_restore"),
        ("z:noop", "trash_noop"),
        (f"tx:edit:{TRANSACTION_ID}:3:2", "edit_menu"),
    ],
)
async def test_each_callback_filter_selects_only_its_lifecycle_handler(
    data: str,
    expected: str,
) -> None:
    dispatcher = Dispatcher()
    router, _transactions = _router([])
    router.register(dispatcher)

    matched = await _first_matching(dispatcher.callback_query.handlers, _callback(data))

    assert matched is not None
    assert matched.callback == getattr(router, expected)


@pytest.mark.asyncio
async def test_delete_confirmation_namespace_is_not_swallowed_by_legacy_filter() -> None:
    dispatcher = Dispatcher()
    router, _transactions = _router([])
    router.register(dispatcher)

    matched = await _first_matching(
        dispatcher.callback_query.handlers,
        _callback(f"tx:del:ask:{TRANSACTION_ID}:3:2"),
    )

    assert matched is None


@pytest.mark.asyncio
async def test_legacy_delete_translates_to_confirmation_without_mutation() -> None:
    events: list[str] = []
    router, _transactions = _router(events)

    await router.legacy_delete_requires_confirmation(
        _callback(f"tx:del:{TRANSACTION_ID}:3:2"),
        91,
    )

    assert events == [f"legacy:tx:del:ask:{TRANSACTION_ID}:3:2:91"]


@pytest.mark.asyncio
async def test_untracked_restore_preserves_history_page_then_delivers_before_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    router, _transactions = _router(events)

    async def answer(
        _self: CallbackQuery,
        text: str | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(f"answer:{text}")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)

    await router.restore_deleted(_callback(f"tx:restore:{TRANSACTION_ID}:3:2"))

    assert events == [
        "context:2",
        f"restore:None:{TRANSACTION_ID}:3:2",
        "deliver_transaction",
        "answer:Восстановлено",
    ]


@pytest.mark.asyncio
async def test_tracked_trash_restore_discards_page_and_never_delivers_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    router, _transactions = _router(events)

    async def answer(
        _self: CallbackQuery,
        text: str | None = None,
        **_kwargs: object,
    ) -> bool:
        events.append(f"answer:{text}")
        return True

    monkeypatch.setattr(CallbackQuery, "answer", answer)

    await router.trash_restore(_callback(f"z:restore:{TRANSACTION_ID}:3:2"), 91)

    assert events == [
        "context:None",
        f"restore:91:{TRANSACTION_ID}:3:None",
        "answer:Восстановлено",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "tx:restore:broken:3:2",
        f"tx:restore:{TRANSACTION_ID}:0:2",
        f"tx:restore:{TRANSACTION_ID}:{MAX_OBJECT_VERSION + 1}:2",
        f"tx:restore:{TRANSACTION_ID}:3:{MAX_PAGE + 1}",
    ],
)
async def test_malformed_callback_is_rejected_before_controller(
    monkeypatch: pytest.MonkeyPatch,
    data: str,
) -> None:
    events: list[str] = []
    router, _transactions = _router(events)

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

    await router.restore_deleted(_callback(data), 91)

    assert events == ["answer:Кнопка повреждена:True"]


def test_bounded_parser_accepts_canonical_callback_only() -> None:
    assert parse_transaction_lifecycle_callback(
        f"tx:edit:{TRANSACTION_ID}:3:2",
        "tx:edit:",
    ) == (TRANSACTION_ID, 3, 2)
    with pytest.raises(ValueError):
        parse_transaction_lifecycle_callback(
            f"tx:edit:{str(TRANSACTION_ID).upper()}:3:2",
            "tx:edit:",
        )
    with pytest.raises(ValueError):
        parse_transaction_lifecycle_callback(
            f"tx:edit:{TRANSACTION_ID}:03:2",
            "tx:edit:",
        )
    assert parse_transaction_lifecycle_callback(
        f"tx:repeat:{TRANSACTION_ID}:3:{MAX_PAGE}",
        "tx:repeat:",
        max_page=MAX_PAGE,
    ) == (TRANSACTION_ID, 3, MAX_PAGE)


def test_router_bundles_hide_injected_dependencies_from_repr() -> None:
    rendered = repr(_router([])[0])

    assert "Transactions" not in rendered
    assert "context" not in rendered
    assert "deliver" not in rendered
