import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.bootstrap as bootstrap
from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.finance_queries import (
    FinanceQueryController,
    FinanceReportPeriod,
    TelegramQueryContext,
    TelegramQueryReceipt,
)
from finbot.adapters.telegram.routers.finance_messages import FinanceMessageRouter
from finbot.application.dto import DraftRef
from finbot.application.errors import ApplicationValidationError

DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")


def _message(text: str) -> Message:
    return Message(
        message_id=700,
        date=datetime(2026, 8, 13, tzinfo=UTC),
        chat=Chat(id=800, type="private"),
        text=text,
    )


class _DraftIngress:
    def __init__(self, events: list[object], receipt: object | None) -> None:
        self.events = events
        self.receipt = receipt
        self.error: ApplicationValidationError | None = None

    async def begin_wizard(self, context: object) -> object | None:
        self.events.append(("wizard", context))
        if self.error is not None:
            raise self.error
        return self.receipt


class _FinanceQueries:
    def __init__(self, events: list[object], receipt: object | None) -> None:
        self.events = events
        self.receipt = receipt

    async def open_report(
        self,
        context: object,
        period: FinanceReportPeriod,
    ) -> object | None:
        self.events.append(("report", context, period))
        return self.receipt

    async def open_history(self, context: object) -> object | None:
        self.events.append(("history", context))
        return self.receipt


def _router(
    events: list[object],
    *,
    draft_receipt: object | None = None,
    finance_receipt: object | None = None,
) -> tuple[FinanceMessageRouter, _DraftIngress, _FinanceQueries]:
    draft = _DraftIngress(events, draft_receipt)
    finance = _FinanceQueries(events, finance_receipt)
    draft_context = cast(TelegramDraftIngressContext, object())
    finance_context = cast(TelegramQueryContext, object())

    def make_draft_context(update_id: int | None, message: Message) -> TelegramDraftIngressContext:
        events.append(("draft_context", update_id, message.message_id))
        return draft_context

    def make_finance_context(update_id: int | None, message: Message) -> TelegramQueryContext:
        events.append(("finance_context", update_id, message.message_id))
        return finance_context

    async def deliver_draft(
        message: Message,
        receipt: DraftIngressReceiptSnapshot,
    ) -> None:
        events.append(("deliver_draft", message.message_id, receipt))

    async def deliver_finance(message: Message, receipt: TelegramQueryReceipt) -> None:
        events.append(("deliver_finance", message.message_id, receipt))

    return (
        FinanceMessageRouter(
            cast(DraftIngressController, draft),
            cast(FinanceQueryController, finance),
            make_draft_context,
            make_finance_context,
            deliver_draft,
            deliver_finance,
        ),
        draft,
        finance,
    )


@pytest.mark.asyncio
async def test_untracked_wizard_delivers_only_after_controller_returns_receipt() -> None:
    events: list[object] = []
    receipt = cast(DraftIngressReceiptSnapshot, object())
    router, _draft, _finance = _router(events, draft_receipt=receipt)

    await router.start_wizard(_message("/wizard"), None)

    assert events[0][0] == "draft_context"
    assert events[1][0] == "wizard"
    assert events[2] == ("deliver_draft", 700, receipt)


@pytest.mark.asyncio
async def test_compact_add_button_starts_the_existing_review_first_wizard() -> None:
    events: list[object] = []
    receipt = cast(DraftIngressReceiptSnapshot, object())
    router, _draft, _finance = _router(events, draft_receipt=receipt)

    await router.start_wizard(_message("➕ Добавить"), None)

    assert events[1][0] == "wizard"
    assert events[2] == ("deliver_draft", 700, receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize(("update_id", "receipt"), [(91, object()), (None, None)])
async def test_wizard_tracked_or_duplicate_result_skips_direct_delivery(
    update_id: int | None,
    receipt: object | None,
) -> None:
    events: list[object] = []
    router, _draft, _finance = _router(events, draft_receipt=receipt)

    await router.start_wizard(_message("🧙 Мастер"), update_id)

    assert all(event[0] != "deliver_draft" for event in events)


@pytest.mark.asyncio
async def test_wizard_application_error_is_rendered_without_direct_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    router, draft, _finance = _router(events, draft_receipt=object())
    draft.error = ApplicationValidationError("Безопасная ошибка")
    answers: list[str] = []

    async def answer(_message: Message, text: str, **kwargs: object) -> Message:
        del kwargs
        answers.append(text)
        return _message

    monkeypatch.setattr(Message, "answer", answer)

    await router.start_wizard(_message("/wizard"), None)

    assert answers == ["Безопасная ошибка"]
    assert all(event[0] != "deliver_draft" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("/today", FinanceReportPeriod.TODAY),
        ("📅 Сегодня", FinanceReportPeriod.TODAY),
        ("/month", FinanceReportPeriod.MONTH),
        ("/month@numismat_bot", FinanceReportPeriod.MONTH),
        ("📊 Месяц", FinanceReportPeriod.MONTH),
    ],
)
async def test_report_selects_period_and_delivers_only_untracked(
    text: str,
    period: FinanceReportPeriod,
) -> None:
    events: list[object] = []
    receipt = TelegramQueryReceipt("report")
    router, _draft, _finance = _router(events, finance_receipt=receipt)

    await router.report(_message(text), None)

    assert events[0][0] == "finance_context"
    assert events[1][0] == "report"
    assert events[1][2] is period
    assert events[2] == ("deliver_finance", 700, receipt)


@pytest.mark.asyncio
async def test_tracked_report_and_duplicate_history_do_not_deliver_directly() -> None:
    events: list[object] = []
    router, _draft, finance = _router(
        events,
        finance_receipt=TelegramQueryReceipt("tracked"),
    )

    await router.report(_message("/today"), 91)
    finance.receipt = None
    await router.history(_message("/last"), None)

    assert [event[0] for event in events].count("report") == 1
    assert [event[0] for event in events].count("history") == 1
    assert all(event[0] != "deliver_finance" for event in events)


@pytest.mark.asyncio
async def test_untracked_history_calls_shared_delivery_with_controller_receipt() -> None:
    events: list[object] = []
    receipt = TelegramQueryReceipt("history")
    router, _draft, _finance = _router(events, finance_receipt=receipt)

    await router.history(_message("🧾 История"), None)

    assert events == [
        ("finance_context", None, 700),
        ("history", cast(Any, events[1][1])),
        ("deliver_finance", 700, receipt),
    ]


@pytest.mark.asyncio
async def test_compact_operations_button_opens_history() -> None:
    events: list[object] = []
    receipt = TelegramQueryReceipt("history")
    router, _draft, _finance = _router(events, finance_receipt=receipt)

    await router.history(_message("🧾 Операции"), None)

    assert events[1][0] == "history"


def test_registration_is_ordered_and_performs_no_handler_work() -> None:
    events: list[object] = []
    router, _draft, _finance = _router(events)
    dispatcher = Dispatcher()

    router.register(dispatcher)

    assert tuple(handler.callback for handler in dispatcher.message.handlers) == (
        router.start_wizard,
        router.start_wizard,
        router.report,
        router.report,
        router.history,
        router.history,
    )
    assert events == []


def test_router_hides_injected_dependencies_from_repr() -> None:
    router, _draft, _finance = _router([])
    rendered = repr(router)
    assert "DraftIngress" not in rendered
    assert "FinanceQueries" not in rendered
    assert "deliver" not in rendered


def test_bootstrap_only_constructs_and_registers_finance_message_router() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "finance_message_router = FinanceMessageRouter(" in source
    assert "finance_message_router.register(dp)" in source
    assert {
        "start_wizard",
        "report",
        "history",
    }.isdisjoint(function_names)


def test_finance_message_router_has_no_database_or_bootstrap_imports() -> None:
    path = Path(__file__).parents[2] / ("src/finbot/adapters/telegram/routers/finance_messages.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = ("sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert not [module for module in modules if module.startswith(forbidden)]


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


class _BindingSessions:
    def __init__(self, session: _BindingSession) -> None:
        self.session = session

    def __call__(self) -> _BindingSession:
        return self.session


@pytest.mark.asyncio
async def test_bootstrap_finance_direct_delivery_sends_then_binds_exact_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message("/last")
    draft = DraftRef(DRAFT_ID, 8)
    receipt = TelegramQueryReceipt("history", suspended_draft=draft)
    events: list[object] = []

    async def answer(
        _message: Message,
        text: str,
        **kwargs: object,
    ) -> Message:
        events.append(("answer", text, kwargs))
        return Message(
            message_id=701,
            date=_message.date,
            chat=_message.chat,
            text="rendered",
        )

    async def bind(
        session: AsyncSession,
        expected: DraftRef,
        *,
        chat_id: int,
        message_id: int,
    ) -> None:
        events.append(("bind", session, expected, chat_id, message_id))

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(
        bootstrap,
        "_bind_draft_presentation_preserving_context",
        bind,
    )
    sessions = cast(
        async_sessionmaker[AsyncSession],
        _BindingSessions(_BindingSession()),
    )

    await bootstrap._deliver_untracked_finance_message(sessions, message, receipt)

    assert events[0][0] == "answer"
    assert events[1][0] == "bind"
    assert events[1][2:] == (draft, 800, 701)


@pytest.mark.asyncio
async def test_bootstrap_wizard_direct_delivery_renders_sends_then_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message("/wizard")
    receipt = cast(DraftIngressReceiptSnapshot, object())
    draft = DraftRef(DRAFT_ID, 1)
    events: list[object] = []

    async def render(
        sessions: async_sessionmaker[AsyncSession],
        actual: DraftIngressReceiptSnapshot,
    ) -> tuple[object, DraftRef]:
        del sessions
        assert actual is receipt
        events.append("render")
        return bootstrap._CallbackMutationReceipt("wizard", None), draft

    async def answer(
        _message: Message,
        text: str,
        **kwargs: object,
    ) -> Message:
        events.append(("answer", text, kwargs))
        return Message(
            message_id=702,
            date=_message.date,
            chat=_message.chat,
            text="rendered",
        )

    async def bind(
        sessions: async_sessionmaker[AsyncSession],
        actual: DraftIngressReceiptSnapshot,
        expected: DraftRef,
        *,
        chat_id: int,
        message_id: int,
    ) -> None:
        del sessions
        events.append(("bind", actual, expected, chat_id, message_id))

    monkeypatch.setattr(bootstrap, "_render_untracked_draft_ingress_receipt", render)
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(
        bootstrap,
        "_bind_untracked_draft_ingress_presentation",
        bind,
    )
    sessions = cast(async_sessionmaker[AsyncSession], object())

    await bootstrap._deliver_untracked_wizard_message(sessions, message, receipt)

    assert events[0] == "render"
    assert events[1][0] == "answer"
    assert events[2] == ("bind", receipt, draft, 800, 702)
