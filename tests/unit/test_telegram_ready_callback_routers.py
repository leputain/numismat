import ast
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.handler import HandlerObject
from aiogram.types import CallbackQuery, Chat, Message, User

from finbot.adapters.telegram.routers import (
    FallbackCallbackHandlers,
    ReadyCallbackHandlers,
    register_late_callback_fallbacks,
    register_ready_callbacks,
    reject_legacy_draft_callback,
    reject_stale_callback,
)

type TrackedHandler = Callable[[CallbackQuery, int | None], Awaitable[None]]
type FallbackHandler = Callable[[CallbackQuery], Awaitable[None]]


def _callback(data: str) -> CallbackQuery:
    message = Message(
        message_id=700,
        date=datetime(2026, 8, 13, tzinfo=UTC),
        chat=Chat(id=800, type="private"),
        text="safe",
    )
    return CallbackQuery(
        id="callback",
        from_user=User(id=900, is_bot=False, first_name="Owner"),
        chat_instance="private",
        message=message,
        data=data,
    )


def _tracked(name: str, calls: list[str]) -> TrackedHandler:
    async def handler(callback: CallbackQuery, finbot_update_id: int | None = None) -> None:
        del callback, finbot_update_id
        calls.append(name)

    return handler


def _fallback(name: str, calls: list[str]) -> FallbackHandler:
    async def handler(callback: CallbackQuery) -> None:
        del callback
        calls.append(name)

    return handler


def _ready_handlers(calls: list[str]) -> tuple[ReadyCallbackHandlers, tuple[TrackedHandler, ...]]:
    ordered = tuple(
        _tracked(name, calls)
        for name in (
            "history_page",
            "view_transaction",
            "confirm_delete_transaction",
            "trash_page",
            "trash_view",
            "set_default_account",
            "archive_account",
            "restore_account",
            "archive_category",
            "restore_category",
            "versioned_draft",
        )
    )
    return ReadyCallbackHandlers(*ordered), ordered


async def _first_matching(
    handlers: list[HandlerObject],
    callback: CallbackQuery,
) -> HandlerObject | None:
    for handler in handlers:
        matched, _ = await handler.check(callback)
        if matched:
            return handler
    return None


def test_ready_callbacks_are_registered_in_legacy_relative_order() -> None:
    dispatcher = Dispatcher()
    handlers, ordered = _ready_handlers([])

    register_ready_callbacks(dispatcher, handlers)

    assert tuple(item.callback for item in dispatcher.callback_query.handlers) == ordered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "position"),
    [
        ("h:0", 0),
        ("tx:view:00000000-0000-7000-8000-000000000401:1:0", 1),
        ("tx:del:ask:00000000-0000-7000-8000-000000000401:1:0", 2),
        ("z:list:0", 3),
        ("z:view:00000000-0000-7000-8000-000000000401:1:0", 4),
        ("sa:default:00000000-0000-7000-8000-000000000201:1", 5),
        ("sa:archive:do:00000000-0000-7000-8000-000000000201:1", 6),
        ("sa:restore:00000000-0000-7000-8000-000000000201:1", 7),
        ("sc:archive:do:00000000-0000-7000-8000-000000000301:1", 8),
        ("sc:restore:00000000-0000-7000-8000-000000000301:1", 9),
        ("d0.AAAAAAAAAAAAAAAAAAAAAA.1", 10),
    ],
)
async def test_ready_filters_delegate_only_to_the_expected_handler(
    data: str,
    position: int,
) -> None:
    dispatcher = Dispatcher()
    handlers, ordered = _ready_handlers([])
    register_ready_callbacks(dispatcher, handlers)

    matched = await _first_matching(dispatcher.callback_query.handlers, _callback(data))

    assert matched is not None
    assert matched.callback is ordered[position]


@pytest.mark.asyncio
async def test_legacy_d_prefix_is_not_swallowed_by_versioned_draft_filter() -> None:
    dispatcher = Dispatcher()
    calls: list[str] = []
    ready, _ = _ready_handlers(calls)
    legacy = _fallback("legacy", calls)
    stale = _fallback("stale", calls)
    register_ready_callbacks(dispatcher, ready)
    register_late_callback_fallbacks(
        dispatcher,
        FallbackCallbackHandlers(legacy, stale),
    )

    legacy_match = await _first_matching(
        dispatcher.callback_query.handlers,
        _callback("d:resume"),
    )
    versioned_match = await _first_matching(
        dispatcher.callback_query.handlers,
        _callback("d0.AAAAAAAAAAAAAAAAAAAAAA.1"),
    )

    assert legacy_match is not None and legacy_match.callback is legacy
    assert versioned_match is not None and versioned_match.callback is ready.versioned_draft
    assert dispatcher.callback_query.handlers[-1].callback is stale


@pytest.mark.asyncio
async def test_late_fallback_rejects_only_retired_namespaces_then_catches_stale() -> None:
    dispatcher = Dispatcher()
    calls: list[str] = []
    legacy = _fallback("legacy", calls)
    stale = _fallback("stale", calls)
    register_late_callback_fallbacks(
        dispatcher,
        FallbackCallbackHandlers(legacy, stale),
    )

    for data in ("w:confirm", "d:keep", "e:back"):
        match = await _first_matching(dispatcher.callback_query.handlers, _callback(data))
        assert match is not None and match.callback is legacy
    match = await _first_matching(dispatcher.callback_query.handlers, _callback("unknown"))
    assert match is not None and match.callback is stale


@pytest.mark.asyncio
async def test_registration_performs_no_network_or_handler_work() -> None:
    dispatcher = Dispatcher()
    calls: list[str] = []
    ready, _ = _ready_handlers(calls)
    legacy = _fallback("legacy", calls)
    stale = _fallback("stale", calls)

    register_ready_callbacks(dispatcher, ready)
    register_late_callback_fallbacks(
        dispatcher,
        FallbackCallbackHandlers(legacy, stale),
    )

    assert calls == []
    selected = await _first_matching(dispatcher.callback_query.handlers, _callback("h:0"))
    assert selected is not None
    await cast(TrackedHandler, selected.callback)(_callback("h:0"), None)
    assert calls == ["history_page"]


def test_handler_bundles_hide_injected_callbacks_from_repr() -> None:
    calls: list[str] = []
    ready, _ = _ready_handlers(calls)
    fallbacks = FallbackCallbackHandlers(
        _fallback("private legacy closure", calls),
        _fallback("private stale closure", calls),
    )

    assert "handler" not in repr(ready)
    assert "private" not in repr(fallbacks)


@pytest.mark.asyncio
async def test_default_fallbacks_return_fixed_safe_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str | None, bool]] = []

    async def answer(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        answers.append((text, show_alert))

    monkeypatch.setattr(CallbackQuery, "answer", answer)
    legacy = _callback("d:resume")
    stale = _callback("unknown")

    await reject_legacy_draft_callback(legacy)
    await reject_stale_callback(stale)

    assert answers == [
        (
            "Эта кнопка относится к старой версии формы. Откройте актуальный черновик",
            True,
        ),
        ("Эта кнопка устарела. Откройте меню или историю", True),
    ]


def test_telegram_routers_do_not_import_database_or_composition_root() -> None:
    root = Path(__file__).parents[2] / "src/finbot/adapters/telegram/routers"
    forbidden = ("sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [(node.lineno, node.module)]
            else:
                continue
            violations.extend(
                (path.name, line, module)
                for line, module in imports
                if module.startswith(forbidden)
            )

    assert violations == []
