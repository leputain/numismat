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
from finbot.application.dto import DraftRef
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
    TransactionDraftNavigationCommand,
    TransactionDraftNavigationResult,
)
from finbot.application.use_cases.transaction_draft_navigation import (
    TransactionDraftNavigationUseCases,
)


@dataclass(frozen=True, slots=True)
class TelegramTransactionDraftNavigationContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class TransactionDraftNavigationReceiptSnapshot:
    """Pure, resulting-revision-bound input for outbox and post-commit rendering."""

    expected: DraftRef = field(repr=False)
    result: TransactionDraftNavigationResult = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.history_page, bool) or not isinstance(self.history_page, int):
            raise TypeError("Transaction history page must be an integer")
        if self.history_page < 0:
            raise ValueError("Transaction history page must not be negative")


@dataclass(frozen=True, slots=True)
class TransactionDraftNavigationSessionUseCases:
    navigation: TransactionDraftNavigationUseCases = field(repr=False)


class TransactionDraftNavigationUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> TransactionDraftNavigationSessionUseCases: ...


class TransactionDraftNavigationPresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type TransactionDraftNavigationReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TransactionDraftNavigationReceiptSnapshot,
    ],
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


class TransactionDraftNavigationController:
    """Execute one exact edit-draft transition and durable receipt atomically."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TransactionDraftNavigationUseCaseFactory,
        presentation_guard: TransactionDraftNavigationPresentationGuard,
        enqueue_receipt: TransactionDraftNavigationReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def execute(
        self,
        context: TelegramTransactionDraftNavigationContext,
        expected: DraftRef,
        action: TransactionDraftNavigationAction,
        history_page: int = 0,
    ) -> TransactionDraftNavigationReceiptSnapshot | None:
        _validate_expected(expected)
        if not isinstance(action, TransactionDraftNavigationAction):
            raise TypeError("Transaction draft navigation action is invalid")
        if isinstance(history_page, bool) or not isinstance(history_page, int):
            raise TypeError("Transaction history page must be an integer")
        if not 0 <= history_page <= MAX_PAGE:
            raise ValueError(f"Transaction history page must be between 0 and {MAX_PAGE}")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> TransactionDraftNavigationResult:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_cases(session)
            return await use_cases.navigation.execute(
                TransactionDraftNavigationCommand(owner_id, expected, action)
            )

        def build_receipt(
            result: TransactionDraftNavigationResult,
        ) -> TransactionDraftNavigationReceiptSnapshot:
            return TransactionDraftNavigationReceiptSnapshot(
                expected,
                result,
                context.message_id,
                history_page,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=build_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
