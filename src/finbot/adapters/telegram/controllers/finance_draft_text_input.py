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
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputCommand,
    FinanceDraftTextInputResult,
)
from finbot.application.use_cases.finance_draft_text_input import (
    FinanceDraftTextInputUseCase,
)


@dataclass(frozen=True, slots=True)
class TelegramFinanceDraftTextInputContext:
    request: TelegramMutationRequest = field(repr=False)
    input_message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")


@dataclass(frozen=True, slots=True)
class FinanceDraftTextInputReceiptSnapshot:
    """Post-commit renderer input with no submitted text or channel-neutral leak."""

    result: FinanceDraftTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")
        if self.target_message_id is not None:
            _validate_message_id(self.target_message_id, label="target")

    @property
    def draft_ref(self) -> DraftRef:
        return self.result.draft.ref


@dataclass(frozen=True, slots=True)
class FinanceDraftTextInputSessionUseCases:
    text_input: FinanceDraftTextInputUseCase = field(repr=False)


class FinanceDraftTextInputUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> FinanceDraftTextInputSessionUseCases: ...


class FinanceDraftTextTargetRepository(Protocol):
    async def lock_active(self, owner_id: UUID) -> DraftSnapshot: ...

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None: ...


class FinanceDraftTextTargetRepositoryFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> FinanceDraftTextTargetRepository: ...


type FinanceDraftTextInputReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        FinanceDraftTextInputReceiptSnapshot,
    ],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _FinanceDraftTextMutation:
    result: FinanceDraftTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int | None = field(default=None, repr=False)


def _validate_message_id(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {label} message id must be an integer")
    if value <= 0:
        raise ValueError(f"Telegram {label} message id must be positive")


def _build_receipt(
    mutation: _FinanceDraftTextMutation,
) -> FinanceDraftTextInputReceiptSnapshot:
    return FinanceDraftTextInputReceiptSnapshot(
        result=mutation.result,
        input_message_id=mutation.input_message_id,
        target_message_id=mutation.target_message_id,
    )


class FinanceDraftTextInputController:
    """Lock, mutate, queue, and commit one inbound finance draft text value."""

    __slots__ = ("_enqueue_receipt", "_executor", "_targets", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: FinanceDraftTextInputUseCaseFactory,
        target_repository_factory: FinanceDraftTextTargetRepositoryFactory,
        enqueue_receipt: FinanceDraftTextInputReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._targets = target_repository_factory
        self._enqueue_receipt = enqueue_receipt

    async def submit(
        self,
        context: TelegramFinanceDraftTextInputContext,
        text: str,
    ) -> FinanceDraftTextInputReceiptSnapshot | None:
        if not isinstance(text, str):
            raise TypeError("Finance draft text must be a string")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _FinanceDraftTextMutation:
            targets = self._targets(session)
            active = await targets.lock_active(owner_id)
            target_message_id = await targets.presentation_message_id(
                owner_id,
                active.ref,
                context.request.chat_id,
            )
            use_cases = self._use_cases(session)
            result = await use_cases.text_input.execute(
                FinanceDraftTextInputCommand(owner_id, active.ref, text)
            )
            return _FinanceDraftTextMutation(
                result=result,
                input_message_id=context.input_message_id,
                target_message_id=target_message_id,
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
