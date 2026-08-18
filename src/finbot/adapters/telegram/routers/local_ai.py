from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from finbot.adapters.telegram.controllers.draft_ingress import TelegramDraftIngressContext
from finbot.adapters.telegram.controllers.local_ai import (
    LocalAiDraftController,
    LocalAiDraftReceiptSnapshot,
)

type LocalAiDraftContextFactory = Callable[[int | None, Message], TelegramDraftIngressContext]
type LocalAiDraftDirectDelivery = Callable[[Message, LocalAiDraftReceiptSnapshot], Awaitable[None]]


class ProcessedUpdateReader(Protocol):
    async def __call__(self, update_id: int, /) -> bool: ...


@dataclass(frozen=True, slots=True)
class LocalAiRouter:
    """Expose local suggestions only through the explicit `/ai <text>` command."""

    controller: LocalAiDraftController = field(repr=False)
    context: LocalAiDraftContextFactory = field(repr=False)
    deliver_untracked: LocalAiDraftDirectDelivery = field(repr=False)
    is_processed: ProcessedUpdateReader = field(repr=False)

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.suggest, Command("ai"))

    async def suggest(
        self,
        message: Message,
        command: CommandObject,
        finbot_update_id: int | None = None,
    ) -> None:
        if finbot_update_id is not None and await self.is_processed(finbot_update_id):
            return
        receipt = await self.controller.begin(
            self.context(finbot_update_id, message),
            command.args or "",
        )
        if receipt is not None and finbot_update_id is None:
            await self.deliver_untracked(message, receipt)


__all__ = ["LocalAiRouter", "ProcessedUpdateReader"]
