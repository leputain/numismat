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
from finbot.application.settings_text_input import (
    SettingsTextInputCommand,
    SettingsTextInputResult,
)
from finbot.application.use_cases.settings_text_input import SettingsTextInputUseCase


def _validate_message_id(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {label} message id must be an integer")
    if value <= 0:
        raise ValueError(f"Telegram {label} message id must be positive")


def _validate_page(value: int | None, *, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {label} page must be an integer")
    if not 0 <= value <= MAX_PAGE:
        raise ValueError(f"Telegram {label} page must be between 0 and {MAX_PAGE}")


@dataclass(frozen=True, slots=True)
class TelegramSettingsTextInputContext:
    request: TelegramMutationRequest = field(repr=False)
    input_message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")


@dataclass(frozen=True, slots=True)
class SettingsTextInputReceiptSnapshot:
    """Render-complete post-commit receipt without the submitted name."""

    result: SettingsTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int | None = field(default=None, repr=False)
    history_page: int | None = field(default=None, repr=False)
    pending_history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_message_id(self.input_message_id, label="input")
        if self.target_message_id is not None:
            _validate_message_id(self.target_message_id, label="target")
        _validate_page(self.history_page, label="history")
        _validate_page(self.pending_history_page, label="pending history")

    @property
    def draft_ref(self) -> DraftRef | None:
        return self.result.draft.ref if self.result.draft is not None else None


@dataclass(frozen=True, slots=True)
class SettingsTextInputSessionUseCases:
    text_input: SettingsTextInputUseCase = field(repr=False)


class SettingsTextInputUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> SettingsTextInputSessionUseCases: ...


class SettingsTextInputTargetRepository(Protocol):
    async def lock_active(self, owner_id: UUID) -> DraftSnapshot: ...

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None: ...


class SettingsTextInputTargetRepositoryFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> SettingsTextInputTargetRepository: ...


class SettingsTextPresentationContext(Protocol):
    @property
    def history_page(self) -> int | None: ...

    @property
    def pending_history_page(self) -> int | None: ...


class SettingsTextPresentationContextReader(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> SettingsTextPresentationContext | None: ...


type SettingsTextInputReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        SettingsTextInputReceiptSnapshot,
    ],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _SettingsTextMutation:
    result: SettingsTextInputResult = field(repr=False)
    input_message_id: int = field(repr=False)
    target_message_id: int | None = field(default=None, repr=False)
    history_page: int | None = field(default=None, repr=False)
    pending_history_page: int | None = field(default=None, repr=False)


def _build_receipt(mutation: _SettingsTextMutation) -> SettingsTextInputReceiptSnapshot:
    return SettingsTextInputReceiptSnapshot(
        result=mutation.result,
        input_message_id=mutation.input_message_id,
        target_message_id=mutation.target_message_id,
        history_page=mutation.history_page,
        pending_history_page=mutation.pending_history_page,
    )


class SettingsTextInputController:
    """Lock, mutate, queue, and commit one settings text-input update."""

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
        use_case_factory: SettingsTextInputUseCaseFactory,
        target_repository_factory: SettingsTextInputTargetRepositoryFactory,
        presentation_context_reader: SettingsTextPresentationContextReader,
        enqueue_receipt: SettingsTextInputReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._targets = target_repository_factory
        self._presentation_context = presentation_context_reader
        self._enqueue_receipt = enqueue_receipt

    async def submit(
        self,
        context: TelegramSettingsTextInputContext,
        text: str,
    ) -> SettingsTextInputReceiptSnapshot | None:
        if not isinstance(text, str):
            raise TypeError("Settings text must be a string")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _SettingsTextMutation:
            targets = self._targets(session)
            active = await targets.lock_active(owner_id)
            target_message_id = await targets.presentation_message_id(
                owner_id,
                active.ref,
                context.request.chat_id,
            )
            history_page: int | None = None
            pending_history_page: int | None = None
            if target_message_id is not None:
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
                history_page = presentation.history_page
                pending_history_page = presentation.pending_history_page
                _validate_page(history_page, label="history")
                _validate_page(pending_history_page, label="pending history")
                if pending_history_page is not None:
                    raise InvalidStateError("Экран ввода настроек содержит незавершённый конфликт")

            result = await self._use_cases(session).text_input.execute(
                SettingsTextInputCommand(owner_id, active.ref, text)
            )
            return _SettingsTextMutation(
                result=result,
                input_message_id=context.input_message_id,
                target_message_id=target_message_id,
                history_page=history_page,
                pending_history_page=pending_history_page,
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
