import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram.types import Chat, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.routers.settings as settings_router_module
import finbot.bootstrap as bootstrap
from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
)
from finbot.adapters.telegram.controllers.settings_queries import (
    TelegramSettingsQueryReceipt,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.ui import settings_help_keyboard
from finbot.application.dto import DraftRef

DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")


class _CaptureSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


class _Begin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class _BindingSession:
    async def __aenter__(self) -> _BindingSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> _Begin:
        return _Begin()


class _Sessions:
    def __init__(self, session: _BindingSession) -> None:
        self.session = session

    def __call__(self) -> _BindingSession:
        return self.session


def _request() -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=91_000_001,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _receipt(*, message_id: int | None) -> TelegramSettingsQueryReceipt:
    return TelegramSettingsQueryReceipt(
        text="<b>Настройки</b>",
        reply_markup=settings_help_keyboard(),
        message_id=message_id,
        draft_ref=DraftRef(DRAFT_ID, 8),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "method"),
    [(None, "send_message"), (77, "edit_message_text")],
)
async def test_tracked_settings_receipt_preserves_exact_draft_context_in_outbox(
    monkeypatch: pytest.MonkeyPatch,
    message_id: int | None,
    method: str,
) -> None:
    session = _CaptureSession()
    context = TelegramDraftPresentationContext(history_page=4, pending_history_page=7)

    async def presentation_context(
        actual_session: AsyncSession,
        draft: DraftRef,
    ) -> TelegramDraftPresentationContext:
        assert actual_session is cast(Any, session)
        assert draft == DraftRef(DRAFT_ID, 8)
        return context

    monkeypatch.setattr(
        bootstrap,
        "_current_or_prior_draft_presentation_context",
        presentation_context,
    )

    await bootstrap._enqueue_settings_query_receipt(
        cast(AsyncSession, session),
        _request(),
        _receipt(message_id=message_id),
    )

    assert len(session.added) == 1
    outbox = cast(TelegramResponseOutbox, session.added[0])
    assert outbox.method == method
    assert outbox.message_id == message_id
    assert (outbox.draft_id, outbox.draft_revision) == (DRAFT_ID, 8)
    assert (outbox.history_page, outbox.pending_history_page) == (4, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current", [False, True])
async def test_settings_context_prefers_exact_projection_then_prior_revision(
    monkeypatch: pytest.MonkeyPatch,
    has_current: bool,
) -> None:
    calls: list[int] = []
    expected = TelegramDraftPresentationContext(history_page=3, pending_history_page=6)

    async def lock_context(
        _session: AsyncSession,
        draft_id: UUID,
        revision: int,
    ) -> TelegramDraftPresentationContext | None:
        assert draft_id == DRAFT_ID
        calls.append(revision)
        if revision == 8 and has_current:
            return expected
        if revision == 7:
            return expected
        return None

    monkeypatch.setattr(
        bootstrap,
        "lock_telegram_draft_presentation_context_by_revision",
        lock_context,
    )

    result = await bootstrap._current_or_prior_draft_presentation_context(
        cast(AsyncSession, object()),
        DraftRef(DRAFT_ID, 8),
    )

    assert result == expected
    assert calls == ([8] if has_current else [8, 7])


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, 77])
async def test_untracked_settings_delivery_binds_delivered_message_with_context(
    monkeypatch: pytest.MonkeyPatch,
    message_id: int | None,
) -> None:
    source_message = Message(
        message_id=77,
        date=datetime(2026, 8, 13, tzinfo=UTC),
        chat=Chat(id=93_000_003, type="private"),
        text="settings",
    )
    delivered_message_id = 88
    context = TelegramDraftPresentationContext(history_page=2, pending_history_page=5)
    calls: list[tuple[object, ...]] = []

    async def answer(
        _message: Message,
        text: str,
        **kwargs: object,
    ) -> Message:
        calls.append(("answer", text, kwargs))
        return Message(
            message_id=delivered_message_id,
            date=datetime(2026, 8, 13, tzinfo=UTC),
            chat=source_message.chat,
            text="rendered",
        )

    async def replace(
        message: Message,
        text: str,
        reply_markup: object,
    ) -> int:
        calls.append(("replace", message.message_id, text, reply_markup))
        return delivered_message_id

    async def presentation_context(
        _session: AsyncSession,
        draft: DraftRef,
    ) -> TelegramDraftPresentationContext:
        calls.append(("context", draft))
        return context

    async def bind(
        _session: AsyncSession,
        draft: DraftRef,
        *,
        chat_id: int,
        message_id: int,
        history_page: int | None,
        pending_history_page: int | None,
        override_context: bool,
    ) -> None:
        calls.append(
            (
                "bind",
                draft,
                chat_id,
                message_id,
                history_page,
                pending_history_page,
                override_context,
            )
        )

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(bootstrap, "_replace_message", replace)
    monkeypatch.setattr(
        bootstrap,
        "_current_or_prior_draft_presentation_context",
        presentation_context,
    )
    monkeypatch.setattr(
        bootstrap,
        "_bind_draft_presentation_preserving_context",
        bind,
    )
    sessions = cast(
        async_sessionmaker[AsyncSession],
        _Sessions(_BindingSession()),
    )

    await bootstrap._deliver_untracked_settings_query_receipt(
        sessions,
        source_message,
        _receipt(message_id=message_id),
    )

    assert calls[-1] == (
        "bind",
        DraftRef(DRAFT_ID, 8),
        93_000_003,
        delivered_message_id,
        2,
        5,
        True,
    )
    assert calls[0][0] == ("answer" if message_id is None else "replace")


def test_settings_handlers_live_in_focused_router_without_direct_database_access() -> None:
    source = Path(settings_router_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    migrated = {
        "show_settings": "settings_main",
        "settings_main": "settings_main",
        "settings_accounts": "accounts",
        "settings_account_view": "account",
        "settings_account_archive_ask": "account_archive_confirmation",
        "settings_accounts_archived": "archived_accounts",
        "settings_categories": "categories",
        "settings_category_list": "category_list",
        "settings_category_view": "category",
        "settings_category_archive_ask": "category_archive_confirmation",
        "settings_categories_archived": "archived_categories",
        "settings_timezones": "timezones",
        "settings_help": "help",
    }

    for handler_name, operation in migrated.items():
        rendered = ast.unparse(handlers[handler_name])
        assert f"query_controller.{operation}" in rendered
        assert "claim_update(" not in rendered
        assert "ensure_user(" not in rendered
        assert "session.scalar(" not in rendered
        assert "session.execute(" not in rendered

    mutation_handlers = {
        "settings_account_new": "account_create",
        "settings_account_rename": "account_rename",
        "settings_category_new": "category_create",
        "settings_category_rename": "category_rename",
        "settings_timezone": "timezone",
    }
    for handler_name, operation in mutation_handlers.items():
        rendered = ast.unparse(handlers[handler_name])
        assert f"mutation_controller.{operation}" in rendered
        assert "claim_update(" not in rendered
        assert "ensure_user(" not in rendered
        assert "session.scalar(" not in rendered
        assert "session.execute(" not in rendered


def test_bootstrap_only_wires_settings_router_and_has_no_duplicate_handler_bodies() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "register_settings_routes(dp, settings_router)" in source
    assert "SettingsMutationController(" in source
    assert "_settings_mutation_session_use_cases" in source
    assert {
        "show_settings",
        "settings_main",
        "settings_accounts",
        "settings_account_view",
        "settings_account_new",
        "settings_account_rename",
        "settings_categories",
        "settings_category_new",
        "settings_category_rename",
        "settings_timezones",
        "settings_timezone",
    }.isdisjoint(function_names)
