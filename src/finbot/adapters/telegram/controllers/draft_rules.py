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
from finbot.application.draft_rules import DraftRuleAction, StageDraftRuleCommand
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.draft_rules import DraftRuleUseCases
from finbot.application.use_cases.queries import GetOwnerSettings


@dataclass(frozen=True, slots=True)
class TelegramDraftRuleContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class DraftRuleReceiptSnapshot:
    """Pure renderer input captured inside the mutation transaction."""

    action: DraftRuleAction
    expected: DraftRef = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if self.draft.draft_id != self.expected.draft_id:
            raise ValueError("Draft rule receipt references another draft")
        if self.draft.revision != self.expected.revision + 1:
            raise ValueError("Draft rule receipt requires the next draft revision")


@dataclass(frozen=True, slots=True)
class DraftRuleSessionUseCases:
    rules: DraftRuleUseCases = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)


class DraftRuleUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftRuleSessionUseCases: ...


class DraftRulePresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type DraftRuleReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftRuleReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(receipt: DraftRuleReceiptSnapshot) -> DraftRuleReceiptSnapshot:
    return receipt


def _validate_expected(expected: DraftRef) -> None:
    if not isinstance(expected, DraftRef):
        raise TypeError("Expected draft reference must be a DraftRef")
    if not isinstance(expected.draft_id, UUID):
        raise TypeError("Draft id must be a UUID")
    if isinstance(expected.revision, bool) or not isinstance(expected.revision, int):
        raise TypeError("Draft revision must be an integer")
    if expected.revision < 1:
        raise ValueError("Draft revision must be positive")


class DraftRuleController:
    """Atomically stage one rule choice and its durable Telegram receipt."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftRuleUseCaseFactory,
        presentation_guard: DraftRulePresentationGuard,
        enqueue_receipt: DraftRuleReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def execute(
        self,
        context: TelegramDraftRuleContext,
        expected: DraftRef,
        action: DraftRuleAction,
    ) -> DraftRuleReceiptSnapshot | None:
        _validate_expected(expected)
        if not isinstance(action, DraftRuleAction):
            raise TypeError("Draft rule action is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftRuleReceiptSnapshot:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_cases(session)
            result = await use_cases.rules.execute(
                StageDraftRuleCommand(owner_id, expected, action)
            )
            owner = await use_cases.get_owner_settings(owner_id)
            return DraftRuleReceiptSnapshot(
                action=result.action,
                expected=expected,
                owner=owner,
                draft=result.draft,
                message_id=context.message_id,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_same_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
