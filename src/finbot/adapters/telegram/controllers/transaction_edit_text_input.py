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
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputCommand,
    TransactionEditTextInputResult,
)
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)


def _validate_message_id(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {label} message id must be an integer")
    if value <= 0:
        raise ValueError(f"Telegram {label} message id must be positive")


def _validate_history_page(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Transaction history page must be an integer")
    if not 0 <= value <= MAX_PAGE:
        raise ValueError(f"Transaction history page must be between 0 and {MAX_PAGE}")


@dataclass(frozen=True, slots=True)
class TelegramTransactionEditTextInputContext:
    request: TelegramMutationRequest = field(repr=False)
    input_message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")


@dataclass(frozen=True, slots=True)
class TransactionEditTextInputReceiptSnapshot:
    """Renderer input; Telegram navigation state never enters application DTOs."""

    result: TransactionEditTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int = field(repr=False)
    history_page: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")
        _validate_message_id(self.target_message_id, label="target")
        _validate_history_page(self.history_page)

    @property
    def draft_ref(self) -> DraftRef | None:
        return self.result.draft.ref if self.result.draft is not None else None


@dataclass(frozen=True, slots=True)
class TransactionEditTextInputSessionUseCases:
    text_input: TransactionEditTextInputUseCase = field(repr=False)


class TransactionEditTextInputUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> TransactionEditTextInputSessionUseCases: ...


class TransactionEditTextTargetRepository(Protocol):
    async def lock_active(self, owner_id: UUID) -> DraftSnapshot: ...

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None: ...


class TransactionEditTextTargetRepositoryFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> TransactionEditTextTargetRepository: ...


class TransactionEditTextPresentationContext(Protocol):
    @property
    def history_page(self) -> int | None: ...

    @property
    def pending_history_page(self) -> int | None: ...


class TransactionEditTextPresentationContextReader(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> TransactionEditTextPresentationContext | None: ...


type TransactionEditTextInputReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TransactionEditTextInputReceiptSnapshot,
    ],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _TransactionEditTextMutation:
    result: TransactionEditTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int = field(repr=False)
    history_page: int = field(repr=False)


def _build_receipt(
    mutation: _TransactionEditTextMutation,
) -> TransactionEditTextInputReceiptSnapshot:
    return TransactionEditTextInputReceiptSnapshot(
        result=mutation.result,
        input_message_id=mutation.input_message_id,
        target_message_id=mutation.target_message_id,
        history_page=mutation.history_page,
    )


class TransactionEditTextInputController:
    """Validate exact presentation, mutate, queue, and commit one edit value."""

    __slots__ = (
        "_enqueue_receipt",
        "_executor",
        "_presentation_context",
        "_targets",
        "_use_cases",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TransactionEditTextInputUseCaseFactory,
        target_repository_factory: TransactionEditTextTargetRepositoryFactory,
        presentation_context_reader: TransactionEditTextPresentationContextReader,
        enqueue_receipt: TransactionEditTextInputReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._targets = target_repository_factory
        self._presentation_context = presentation_context_reader
        self._enqueue_receipt = enqueue_receipt

    async def submit(
        self,
        context: TelegramTransactionEditTextInputContext,
        text: str,
    ) -> TransactionEditTextInputReceiptSnapshot | None:
        if not isinstance(text, str):
            raise TypeError("Transaction edit text must be a string")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _TransactionEditTextMutation:
            targets = self._targets(session)
            active = await targets.lock_active(owner_id)
            target_message_id = await targets.presentation_message_id(
                owner_id,
                active.ref,
                context.request.chat_id,
            )
            if target_message_id is None:
                raise DraftRevisionConflictError(current_revision=active.revision)
            presentation = await self._presentation_context(
                session,
                owner_id,
                active.ref,
                context.request.chat_id,
                target_message_id,
                allow_suspended=False,
            )
            if presentation is None:
                raise DraftRevisionConflictError(current_revision=active.revision)
            if presentation.pending_history_page is not None:
                raise InvalidStateError("Экран редактирования содержит конфликт навигации")
            history_page = presentation.history_page
            if history_page is None:
                history_page = 0
            _validate_history_page(history_page)

            result = await self._use_cases(session).text_input.execute(
                TransactionEditTextInputCommand(owner_id, active.ref, text)
            )
            return _TransactionEditTextMutation(
                result=result,
                input_message_id=context.input_message_id,
                target_message_id=target_message_id,
                history_page=history_page,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_build_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
