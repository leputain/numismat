from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.dto import OwnerSnapshot
from finbot.application.undo import UndoActionResult
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.undo import UndoLastAction


@dataclass(frozen=True, slots=True)
class TelegramUndoContext:
    request: TelegramMutationRequest = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram undo request is invalid")


@dataclass(frozen=True, slots=True)
class UndoReceiptSnapshot:
    """Pure renderer input captured before the mutation UoW commits."""

    owner: OwnerSnapshot = field(repr=False)
    result: UndoActionResult | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner, OwnerSnapshot):
            raise TypeError("Undo owner snapshot is invalid")
        if self.result is not None and not isinstance(self.result, UndoActionResult):
            raise TypeError("Undo result snapshot is invalid")


@dataclass(frozen=True, slots=True)
class UndoSessionUseCases:
    undo_last: UndoLastAction = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)


class UndoUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> UndoSessionUseCases: ...


type UndoReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, UndoReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(receipt: UndoReceiptSnapshot) -> UndoReceiptSnapshot:
    return receipt


class UndoController:
    """Reverse one audited action and persist its Telegram receipt atomically."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: UndoUseCaseFactory,
        enqueue_receipt: UndoReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def execute(self, context: TelegramUndoContext) -> UndoReceiptSnapshot | None:
        if not isinstance(context, TelegramUndoContext):
            raise TypeError("Telegram undo context is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> UndoReceiptSnapshot:
            use_cases = self._use_cases(session)
            result = await use_cases.undo_last.execute(owner_id)
            owner = await use_cases.get_owner_settings(owner_id)
            return UndoReceiptSnapshot(owner=owner, result=result)

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_same_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
