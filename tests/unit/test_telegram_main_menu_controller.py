from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.main_menu import (
    MainMenuAction,
    MainMenuController,
    MainMenuReceiptSnapshot,
    TelegramMainMenuContext,
    render_main_menu,
)
from finbot.adapters.telegram.executor import (
    TelegramMutationExecutor,
    TelegramMutationRequest,
)
from finbot.adapters.telegram.ui import MAIN_MENU, MORE_MENU
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.ports import DraftRepository

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")


@dataclass(slots=True)
class _Owner:
    id: UUID


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Session:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _Sessions:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


class _Drafts:
    def __init__(
        self,
        events: list[str],
        active: DraftSnapshot | None,
    ) -> None:
        self.events = events
        self.active = active
        self.suspend_calls: list[DraftRef] = []

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def set_suspended(
        self,
        owner_id: UUID,
        expected: DraftRef,
        suspended: bool,
    ) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        assert suspended
        assert self.active is not None and self.active.ref == expected
        self.events.append("draft.suspend")
        self.suspend_calls.append(expected)
        if not self.active.suspended:
            self.active = replace(
                self.active,
                suspended=True,
                revision=self.active.revision + 1,
            )
        return self.active


def _request(update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _draft(*, suspended: bool = False) -> DraftSnapshot:
    return DraftSnapshot(
        DRAFT_ID,
        "quick_confirm",
        {"private": "must-not-leak"},
        revision=6,
        suspended=suspended,
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    drafts: _Drafts,
    receipts: list[MainMenuReceiptSnapshot],
    *,
    claimed: bool = True,
) -> MainMenuController:
    session = _Session(events)

    async def claim(actual: AsyncSession, update_id: int | None) -> bool:
        assert actual is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual: AsyncSession,
        **kwargs: object,
    ) -> _Owner:
        assert actual is cast(Any, session)
        assert kwargs == {
            "telegram_user_id": 92_000_002,
            "telegram_chat_id": 93_000_003,
            "locale": "ru",
            "timezone": "UTC",
            "currency": "RUB",
        }
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    executor = TelegramMutationExecutor(cast(async_sessionmaker[AsyncSession], _Sessions(session)))

    def repository_factory(actual: AsyncSession) -> DraftRepository:
        assert actual is cast(Any, session)
        events.append("draft.factory")
        return cast(DraftRepository, drafts)

    async def enqueue(
        actual: AsyncSession,
        request: TelegramMutationRequest,
        receipt: MainMenuReceiptSnapshot,
    ) -> None:
        assert actual is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return MainMenuController(executor, repository_factory, enqueue)


@pytest.mark.asyncio
async def test_navigation_suspends_exact_draft_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[MainMenuReceiptSnapshot] = []
    drafts = _Drafts(events, _draft())
    controller = _controller(monkeypatch, events, drafts, receipts)

    result = await controller.open(
        TelegramMainMenuContext(_request()),
        MainMenuAction.START,
    )

    assert result is receipts[0]
    assert result.suspended_draft == DraftRef(DRAFT_ID, 7)
    assert drafts.suspend_calls == [DraftRef(DRAFT_ID, 6)]
    assert events.index("draft.suspend") < events.index("outbox") < events.index("commit")
    assert "must-not-leak" not in repr(result)
    assert str(DRAFT_ID) not in repr(result)


@pytest.mark.asyncio
async def test_already_suspended_draft_keeps_its_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[MainMenuReceiptSnapshot] = []
    drafts = _Drafts(events, _draft(suspended=True))
    controller = _controller(monkeypatch, events, drafts, receipts)

    result = await controller.open(
        TelegramMainMenuContext(_request()),
        MainMenuAction.HELP,
    )

    assert result is not None
    assert result.suspended_draft == DraftRef(DRAFT_ID, 6)
    assert drafts.suspend_calls == [DraftRef(DRAFT_ID, 6)]


@pytest.mark.asyncio
async def test_quick_help_queues_receipt_without_reading_or_suspending_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[MainMenuReceiptSnapshot] = []
    controller = _controller(monkeypatch, events, _Drafts(events, _draft()), receipts)

    result = await controller.open(
        TelegramMainMenuContext(_request()),
        MainMenuAction.QUICK_HELP,
    )

    assert result is receipts[0]
    assert result.suspended_draft is None
    assert "draft.factory" not in events
    assert events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_duplicate_update_replays_neither_mutation_nor_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[MainMenuReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _Drafts(events, _draft()),
        receipts,
        claimed=False,
    )

    result = await controller.open(
        TelegramMainMenuContext(_request()),
        MainMenuAction.MENU,
    )

    assert result is None
    assert receipts == []
    assert "draft.factory" not in events
    assert "commit" not in events
    assert events.count("rollback") == 1


@pytest.mark.asyncio
async def test_untracked_navigation_commits_without_outbox_before_returning_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[MainMenuReceiptSnapshot] = []
    controller = _controller(monkeypatch, events, _Drafts(events, _draft()), receipts)

    result = await controller.open(
        TelegramMainMenuContext(_request(None)),
        MainMenuAction.MENU,
    )

    assert result is not None
    assert receipts == []
    assert events.index("draft.suspend") < events.index("commit") < events.index("session.exit")


@pytest.mark.parametrize(
    "action",
    [MainMenuAction.START, MainMenuAction.MENU, MainMenuAction.HELP],
)
def test_navigation_renderers_preserve_the_persistent_main_menu(
    action: MainMenuAction,
) -> None:
    assert render_main_menu(action).reply_markup is MAIN_MENU


def test_quick_help_preserves_its_markup_free_behavior() -> None:
    assert render_main_menu(MainMenuAction.QUICK_HELP).reply_markup is None


def test_primary_menu_is_bounded_to_four_frequent_actions() -> None:
    assert [[button.text for button in row] for row in MAIN_MENU.keyboard] == [
        ["➕ Добавить", "📅 Сегодня"],
        ["🧾 Операции", "••• Ещё"],
    ]
    assert MAIN_MENU.input_field_placeholder == "Например: 500"


def test_more_menu_keeps_secondary_actions_reachable() -> None:
    message = render_main_menu(MainMenuAction.MORE)

    assert message.reply_markup is MORE_MENU
    assert [button.text for button in MORE_MENU.keyboard[-1]] == ["🏠 Меню"]
