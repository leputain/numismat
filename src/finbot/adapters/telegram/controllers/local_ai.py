from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.draft_ingress import DraftIngressResult, DraftIngressStatus
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    EntityNotFoundError,
)
from finbot.application.local_ai import (
    CreateLocalAiDraftCommand,
    LocalAiDisabledError,
    LocalAiInvalidSuggestionError,
    LocalAiUnavailableError,
    SuggestLocalTransactionCommand,
)
from finbot.application.use_cases.local_ai import CreateLocalAiDraft, SuggestLocalTransaction


class LocalAiDraftOutcome(StrEnum):
    CREATED = "created"
    ACTIVE_DRAFT = "active_draft"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    INVALID_INPUT = "invalid_input"
    INVALID_RESPONSE = "invalid_response"


_LOCAL_AI_NOTICE_TEXT = {
    LocalAiDraftOutcome.ACTIVE_DRAFT: (
        "Уже есть активный черновик. Завершите или отмените его, затем повторите /ai."
    ),
    LocalAiDraftOutcome.DISABLED: (
        "Локальный AI отключён. Обычный детерминированный ввод работает как раньше."
    ),
    LocalAiDraftOutcome.UNAVAILABLE: (
        "Локальный AI сейчас недоступен. Ничего не изменено; повторите команду позже."
    ),
    LocalAiDraftOutcome.INVALID_INPUT: (
        "После /ai укажите короткое описание операции — не более 1024 символов."
    ),
    LocalAiDraftOutcome.INVALID_RESPONSE: (
        "Локальный AI не подготовил корректное предложение. Ничего не изменено."
    ),
}


def local_ai_notice_text(outcome: LocalAiDraftOutcome) -> str:
    try:
        return _LOCAL_AI_NOTICE_TEXT[outcome]
    except KeyError:
        raise ValueError("Created local AI receipt must render as a draft") from None


@dataclass(frozen=True, slots=True)
class LocalAiDraftReceiptSnapshot:
    outcome: LocalAiDraftOutcome
    ingress: DraftIngressReceiptSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, LocalAiDraftOutcome):
            raise TypeError("Local AI receipt outcome is invalid")
        if (self.outcome is LocalAiDraftOutcome.CREATED) != (self.ingress is not None):
            raise ValueError("Only a created local AI receipt may carry a draft")


@dataclass(frozen=True, slots=True)
class LocalAiDraftSessionUseCases:
    create_draft: CreateLocalAiDraft = field(repr=False)


class LocalAiDraftUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> LocalAiDraftSessionUseCases: ...


type LocalAiDraftReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, LocalAiDraftReceiptSnapshot],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _LocalAiMutationResult:
    outcome: LocalAiDraftOutcome
    ingress: DraftIngressResult | None = field(default=None, repr=False)


class LocalAiDraftController:
    """Call the local provider before opening the mutation transaction."""

    __slots__ = ("_enqueue_receipt", "_executor", "_suggestions", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        suggestions: SuggestLocalTransaction,
        use_case_factory: LocalAiDraftUseCaseFactory,
        enqueue_receipt: LocalAiDraftReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._suggestions = suggestions
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def begin(
        self,
        context: TelegramDraftIngressContext,
        text: str,
    ) -> LocalAiDraftReceiptSnapshot | None:
        # This is intentionally outside TelegramMutationExecutor: no network call may
        # hold a database transaction, owner lock, or idempotency claim.
        try:
            suggested = await self._suggestions.execute(SuggestLocalTransactionCommand(text))
        except LocalAiDisabledError:
            return await self._commit_notice(context, LocalAiDraftOutcome.DISABLED)
        except LocalAiUnavailableError:
            return await self._commit_notice(context, LocalAiDraftOutcome.UNAVAILABLE)
        except LocalAiInvalidSuggestionError:
            return await self._commit_notice(context, LocalAiDraftOutcome.INVALID_RESPONSE)
        except ApplicationValidationError:
            return await self._commit_notice(context, LocalAiDraftOutcome.INVALID_INPUT)

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _LocalAiMutationResult:
            try:
                result = await self._use_cases(session).create_draft.execute(
                    CreateLocalAiDraftCommand(owner_id, suggested)
                )
            except (
                ApplicationValidationError,
                CatalogUnavailableError,
                EntityNotFoundError,
            ):
                # Preparation has not created a draft yet; commit only the fixed receipt.
                return _LocalAiMutationResult(LocalAiDraftOutcome.INVALID_RESPONSE)
            if result.status is DraftIngressStatus.CONFLICT:
                return _LocalAiMutationResult(LocalAiDraftOutcome.ACTIVE_DRAFT)
            return _LocalAiMutationResult(LocalAiDraftOutcome.CREATED, result)

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=lambda result: self._build_receipt(context, result),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def _commit_notice(
        self,
        context: TelegramDraftIngressContext,
        outcome: LocalAiDraftOutcome,
    ) -> LocalAiDraftReceiptSnapshot | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _LocalAiMutationResult:
            del session, owner_id
            return _LocalAiMutationResult(outcome)

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=lambda result: self._build_receipt(context, result),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    @staticmethod
    def _build_receipt(
        context: TelegramDraftIngressContext,
        result: _LocalAiMutationResult,
    ) -> LocalAiDraftReceiptSnapshot:
        if result.outcome is not LocalAiDraftOutcome.CREATED:
            return LocalAiDraftReceiptSnapshot(result.outcome)
        if result.ingress is None:
            raise RuntimeError("Created local AI mutation has no draft result")
        ingress = DraftIngressReceiptSnapshot(
            result.ingress,
            context.message_id,
            context.history_page,
        )
        return LocalAiDraftReceiptSnapshot(result.outcome, ingress)


__all__ = [
    "LocalAiDraftController",
    "LocalAiDraftOutcome",
    "LocalAiDraftReceiptSnapshot",
    "LocalAiDraftSessionUseCases",
    "LocalAiDraftUseCaseFactory",
    "local_ai_notice_text",
]
