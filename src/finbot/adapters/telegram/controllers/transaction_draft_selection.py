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
from finbot.application.draft_navigation import DraftCatalogRef, DraftDateChoice
from finbot.application.dto import DraftRef
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
    TransactionDraftSelectionResult,
)
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)

type TransactionDraftSelectionChoice = DraftCatalogRef | DraftDateChoice


@dataclass(frozen=True, slots=True)
class TelegramTransactionDraftSelectionContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class TransactionDraftSelectionReceiptSnapshot:
    """Pure renderer input captured before the mutation transaction commits."""

    expected: DraftRef = field(repr=False)
    result: TransactionDraftSelectionResult = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.history_page, bool) or not isinstance(self.history_page, int):
            raise TypeError("Transaction history page must be an integer")
        if self.history_page < 0:
            raise ValueError("Transaction history page must not be negative")
        if self.result.draft is not None and (
            self.result.draft.draft_id != self.expected.draft_id
            or self.result.draft.revision != self.expected.revision + 1
        ):
            raise ValueError("Transaction draft receipt contains an unexpected draft revision")


@dataclass(frozen=True, slots=True)
class TransactionDraftSelectionSessionUseCases:
    selections: TransactionDraftSelectionUseCases = field(repr=False)


class TransactionDraftSelectionUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> TransactionDraftSelectionSessionUseCases: ...


class TransactionDraftSelectionPresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type TransactionDraftSelectionReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TransactionDraftSelectionReceiptSnapshot,
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


class TransactionDraftSelectionController:
    """Apply one TX_SELECT_* choice and enqueue its receipt in one UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TransactionDraftSelectionUseCaseFactory,
        presentation_guard: TransactionDraftSelectionPresentationGuard,
        enqueue_receipt: TransactionDraftSelectionReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def execute(
        self,
        context: TelegramTransactionDraftSelectionContext,
        expected: DraftRef,
        action: TransactionDraftSelectionAction,
        choice: TransactionDraftSelectionChoice,
        history_page: int = 0,
    ) -> TransactionDraftSelectionReceiptSnapshot | None:
        _validate_expected(expected)
        if not isinstance(action, TransactionDraftSelectionAction):
            raise TypeError("Transaction draft selection action is invalid")
        if isinstance(history_page, bool) or not isinstance(history_page, int):
            raise TypeError("Transaction history page must be an integer")
        if not 0 <= history_page <= MAX_PAGE:
            raise ValueError(f"Transaction history page must be between 0 and {MAX_PAGE}")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> TransactionDraftSelectionResult:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_cases(session)
            return await use_cases.selections.execute(
                TransactionDraftSelectionCommand(
                    owner_id,
                    expected,
                    action,
                    choice,
                )
            )

        def build_receipt(
            result: TransactionDraftSelectionResult,
        ) -> TransactionDraftSelectionReceiptSnapshot:
            return TransactionDraftSelectionReceiptSnapshot(
                expected=expected,
                result=result,
                message_id=context.message_id,
                history_page=history_page,
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
