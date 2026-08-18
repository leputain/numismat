from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.main_menu_delivery as delivery_module
from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
)
from finbot.adapters.telegram.controllers.main_menu import (
    MainMenuAction,
    MainMenuReceiptSnapshot,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.main_menu_delivery import (
    MainMenuDirectDelivery,
    enqueue_main_menu_receipt,
)
from finbot.application.dto import DraftRef

DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")


class _AddSession:
    def __init__(self) -> None:
        self.added: list[TelegramResponseOutbox] = []

    def add(self, value: object) -> None:
        self.added.append(cast(TelegramResponseOutbox, value))


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _BindingSession:
    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionContext:
    def __init__(self, session: _BindingSession) -> None:
        self.session = session

    async def __aenter__(self) -> _BindingSession:
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Sessions:
    def __init__(self, session: _BindingSession) -> None:
        self.session = session

    def __call__(self) -> _SessionContext:
        return _SessionContext(self.session)


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _Message:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"id": 93_000_003})()
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> _Sent:
        self.answers.append((text, kwargs))
        return _Sent(100 + len(self.answers))


def _request() -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=91_000_001,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


@pytest.mark.asyncio
async def test_tracked_menu_queues_reply_keyboard_and_exact_resume_in_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _AddSession()

    async def context_reader(
        actual: AsyncSession,
        expected: DraftRef,
    ) -> TelegramDraftPresentationContext:
        assert actual is cast(Any, session)
        assert expected == DraftRef(DRAFT_ID, 7)
        return TelegramDraftPresentationContext(history_page=8, pending_history_page=13)

    monkeypatch.setattr(
        delivery_module,
        "lock_current_or_prior_telegram_draft_presentation_context",
        context_reader,
    )

    await enqueue_main_menu_receipt(
        cast(AsyncSession, session),
        _request(),
        MainMenuReceiptSnapshot(MainMenuAction.MENU, DraftRef(DRAFT_ID, 7)),
    )

    assert len(session.added) == 2
    primary, resume = session.added
    assert primary.sequence == 0
    assert primary.method == "send_message"
    assert primary.reply_markup is not None and "keyboard" in primary.reply_markup
    assert resume.sequence == 1
    assert resume.reply_markup is not None and "inline_keyboard" in resume.reply_markup
    assert (resume.draft_id, resume.draft_revision) == (DRAFT_ID, 7)
    assert (resume.history_page, resume.pending_history_page) == (8, 13)


@pytest.mark.asyncio
async def test_untracked_delivery_preserves_main_menu_and_binds_resume_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _BindingSession()
    message = _Message()
    calls: list[tuple[str, object]] = []

    async def context_reader(
        actual: AsyncSession,
        expected: DraftRef,
    ) -> TelegramDraftPresentationContext:
        assert actual is cast(Any, session)
        calls.append(("context", expected))
        return TelegramDraftPresentationContext(history_page=5)

    async def bind(actual: AsyncSession, **kwargs: object) -> bool:
        assert actual is cast(Any, session)
        calls.append(("bind", kwargs))
        return True

    monkeypatch.setattr(
        delivery_module,
        "lock_current_or_prior_telegram_draft_presentation_context",
        context_reader,
    )
    monkeypatch.setattr(delivery_module, "bind_telegram_draft_presentation", bind)
    delivery = MainMenuDirectDelivery(cast(async_sessionmaker[AsyncSession], _Sessions(session)))

    await delivery.deliver(
        cast(Any, message),
        MainMenuReceiptSnapshot(MainMenuAction.HELP, DraftRef(DRAFT_ID, 7)),
    )

    assert len(message.answers) == 2
    assert message.answers[0][1]["reply_markup"].keyboard
    assert message.answers[1][1]["reply_markup"].inline_keyboard
    assert calls[0] == ("context", DraftRef(DRAFT_ID, 7))
    assert calls[1] == (
        "bind",
        {
            "draft_id": DRAFT_ID,
            "draft_revision": 7,
            "chat_id": 93_000_003,
            "message_id": 102,
            "history_page": 5,
            "pending_history_page": None,
        },
    )
