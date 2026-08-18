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
from finbot.application.draft_navigation import (
    DraftNavigationAction,
    DraftNavigationChoice,
    DraftNavigationCommand,
    DraftNavigationResult,
)
from finbot.application.dto import DraftRef
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases


@dataclass(frozen=True, slots=True)
class TelegramDraftNavigationContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class DraftNavigationReceiptSnapshot:
    """Pure renderer input captured before the mutation transaction commits."""

    expected: DraftRef = field(repr=False)
    result: DraftNavigationResult = field(repr=False)
    message_id: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftNavigationSessionUseCases:
    navigation: DraftNavigationUseCases = field(repr=False)


class DraftNavigationUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftNavigationSessionUseCases: ...


class DraftNavigationPresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type DraftNavigationReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftNavigationReceiptSnapshot],
    Awaitable[None],
]


def _validate_expected(expected: DraftRef) -> None:
    if not isinstance(expected, DraftRef):
        raise TypeError("Expected draft reference must be a DraftRef")
    if not isinstance(expected.draft_id, UUID):
        raise TypeError("Draft id must be a UUID")
    if isinstance(expected.revision, bool) or not isinstance(expected.revision, int):
        raise TypeError("Draft revision must be an integer")
    if expected.revision < 1:
        raise ValueError("Draft revision must be positive")


class DraftNavigationController:
    """Execute one exact draft transition and its durable Telegram receipt atomically."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftNavigationUseCaseFactory,
        presentation_guard: DraftNavigationPresentationGuard,
        enqueue_receipt: DraftNavigationReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def execute(
        self,
        context: TelegramDraftNavigationContext,
        expected: DraftRef,
        action: DraftNavigationAction,
        choice: DraftNavigationChoice | None = None,
    ) -> DraftNavigationReceiptSnapshot | None:
        _validate_expected(expected)
        if not isinstance(action, DraftNavigationAction):
            raise TypeError("Draft navigation action is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftNavigationResult:
            is_current = await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            )
            if not is_current:
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_cases(session)
            return await use_cases.navigation.execute(
                DraftNavigationCommand(owner_id, expected, action, choice)
            )

        def build_receipt(result: DraftNavigationResult) -> DraftNavigationReceiptSnapshot:
            return DraftNavigationReceiptSnapshot(expected, result, context.message_id)

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=build_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
