from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from aiogram.types import ReplyKeyboardMarkup

from finbot import VERSION_LABEL
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.presenters import HELP_TEXT
from finbot.adapters.telegram.ui import MAIN_MENU
from finbot.application.dto import DraftRef
from finbot.application.ports import DraftRepository
from finbot.application.use_cases.drafts import DraftUseCases


class MainMenuAction(StrEnum):
    START = "start"
    MENU = "menu"
    HELP = "help"
    QUICK_HELP = "quick_help"


_SUSPENDING_ACTIONS = frozenset({MainMenuAction.START, MainMenuAction.MENU, MainMenuAction.HELP})


@dataclass(frozen=True, slots=True)
class TelegramMainMenuContext:
    request: TelegramMutationRequest = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram main-menu request is invalid")


@dataclass(frozen=True, slots=True)
class MainMenuMessage:
    text: str = field(repr=False)
    parse_mode: str | None = "HTML"
    reply_markup: ReplyKeyboardMarkup | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > 4096:
            raise ValueError("Main-menu text must contain between 1 and 4096 characters")
        if self.parse_mode not in {None, "HTML"}:
            raise ValueError("Main-menu parse mode is unsupported")


@dataclass(frozen=True, slots=True)
class MainMenuReceiptSnapshot:
    action: MainMenuAction
    suspended_draft: DraftRef | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, MainMenuAction):
            raise TypeError("Main-menu action is invalid")
        if self.suspended_draft is not None and not isinstance(self.suspended_draft, DraftRef):
            raise TypeError("Suspended main-menu draft must be an exact reference")
        if self.action not in _SUSPENDING_ACTIONS and self.suspended_draft is not None:
            raise ValueError("This main-menu action cannot suspend a draft")


class MainMenuDraftRepositoryFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftRepository: ...


type MainMenuReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, MainMenuReceiptSnapshot],
    Awaitable[None],
]


def render_main_menu(action: MainMenuAction) -> MainMenuMessage:
    if action is MainMenuAction.START:
        return MainMenuMessage(
            f"<b>Numismat {VERSION_LABEL} готов</b> 👋\n\n"
            "Отправьте <code>1450 ресторан</code> или нажмите «➕ Добавить операцию».\n"
            "Все данные доступны только вам в этом личном чате.",
            reply_markup=MAIN_MENU,
        )
    if action is MainMenuAction.MENU:
        return MainMenuMessage(
            "<b>Главное меню</b>\nВыберите действие или просто напишите покупку сообщением.",
            reply_markup=MAIN_MENU,
        )
    if action is MainMenuAction.HELP:
        return MainMenuMessage(HELP_TEXT, reply_markup=MAIN_MENU)
    if action is MainMenuAction.QUICK_HELP:
        return MainMenuMessage(
            "<b>Быстрый ввод</b>\n\nНапишите сумму и назначение одним сообщением:\n"
            "<code>1450 ресторан</code>\n<code>+250000 зарплата</code>"
        )
    raise ValueError("Unsupported main-menu action")


class MainMenuController:
    """Suspend an exact draft and queue the complete navigation receipt atomically."""

    __slots__ = ("_draft_repositories", "_enqueue_receipt", "_executor")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        draft_repository_factory: MainMenuDraftRepositoryFactory,
        enqueue_receipt: MainMenuReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._draft_repositories = draft_repository_factory
        self._enqueue_receipt = enqueue_receipt

    async def open(
        self,
        context: TelegramMainMenuContext,
        action: MainMenuAction,
    ) -> MainMenuReceiptSnapshot | None:
        if not isinstance(action, MainMenuAction):
            raise TypeError("Main-menu action is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftRef | None:
            if action not in _SUSPENDING_ACTIONS:
                return None
            drafts = DraftUseCases(self._draft_repositories(session))
            active = await drafts.get_active(owner_id)
            if active is None:
                return None
            return (await drafts.suspend(owner_id, active.ref)).ref

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=lambda suspended: MainMenuReceiptSnapshot(action, suspended),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = [
    "MainMenuAction",
    "MainMenuController",
    "MainMenuMessage",
    "MainMenuReceiptSnapshot",
    "TelegramMainMenuContext",
    "render_main_menu",
]
