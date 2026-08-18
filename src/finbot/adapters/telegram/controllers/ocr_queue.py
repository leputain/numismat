from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    OcrQueueActionCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    OwnerSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.ocr_queue import (
    CancelOcrQueue,
    ConfirmOcrQueueItem,
    SkipOcrQueueItem,
)
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts, ListCategories
from finbot.domain.transactions import TransactionType


class OcrQueueOperation(StrEnum):
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TelegramOcrQueueContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class OcrQueueReceiptSnapshot:
    """Pure input for rendering the committed queue result.

    ``result`` contains the next exact draft snapshot when the queue advances,
    and the saved transaction snapshot when confirmation created one. Owner
    settings provide the timezone and fallback currency needed by existing
    presenters without querying outside the atomic executor transaction.
    """

    operation: OcrQueueOperation
    expected: DraftRef = field(repr=False)
    result: OcrQueueMutationResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    active_accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    active_categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.operation is OcrQueueOperation.CANCELLED:
            if self.result.status is not OcrQueueStatus.CANCELLED:
                raise ValueError("Cancel receipt requires a cancelled queue result")
        elif self.result.status is OcrQueueStatus.CANCELLED:
            raise ValueError("Confirm or skip receipt cannot contain a cancelled result")


type OcrQueuePresentationGuard = Callable[[UUID, DraftRef, int], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class OcrQueueSessionUseCases:
    """Session-scoped application use cases built by the composition root."""

    confirm_current: ConfirmOcrQueueItem = field(repr=False)
    skip_current: SkipOcrQueueItem = field(repr=False)
    cancel_queue: CancelOcrQueue = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)
    list_accounts: ListAccounts | None = field(default=None, repr=False)
    list_categories: ListCategories | None = field(default=None, repr=False)
    presentation_is_current: OcrQueuePresentationGuard | None = field(default=None, repr=False)


class OcrQueueUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession) -> OcrQueueSessionUseCases: ...


type OcrQueueReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, OcrQueueReceiptSnapshot],
    Awaitable[None],
]
type QueueMutation = Callable[
    [OcrQueueSessionUseCases, OcrQueueActionCommand],
    Awaitable[OcrQueueMutationResult],
]


def _same_receipt(receipt: OcrQueueReceiptSnapshot) -> OcrQueueReceiptSnapshot:
    return receipt


class OcrQueueController:
    """Execute exact-revision OCR queue actions with a durable Telegram receipt."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_case_factory")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: OcrQueueUseCaseFactory,
        enqueue_receipt: OcrQueueReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_case_factory = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def _execute(
        self,
        context: TelegramOcrQueueContext,
        expected: DraftRef,
        operation: OcrQueueOperation,
        queue_mutation: QueueMutation,
    ) -> OcrQueueReceiptSnapshot | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> OcrQueueReceiptSnapshot:
            use_cases = self._use_case_factory(session)
            if (
                context.message_id is not None
                and use_cases.presentation_is_current is not None
                and not await use_cases.presentation_is_current(
                    owner_id,
                    expected,
                    context.message_id,
                )
            ):
                raise DraftRevisionConflictError(current_revision=None)
            command = OcrQueueActionCommand(owner_id=owner_id, expected=expected)
            result = await queue_mutation(use_cases, command)
            owner = await use_cases.get_owner_settings(owner_id)
            active_accounts: tuple[AccountSnapshot, ...] = ()
            active_categories: tuple[CategorySnapshot, ...] = ()
            if result.status is OcrQueueStatus.ADVANCED:
                if result.draft is None:  # pragma: no cover - DTO invariant
                    raise RuntimeError("Advanced OCR result has no draft")
                if result.draft.state == "account_required":
                    if use_cases.list_accounts is None:
                        raise RuntimeError("OCR account receipt reader is not configured")
                    active_accounts = await use_cases.list_accounts(owner_id)
                elif result.draft.state == "category_required":
                    try:
                        kind = TransactionType(str(result.draft.payload.get("type", "")))
                    except ValueError:
                        raise RuntimeError("Prepared OCR draft has an invalid type") from None
                    if use_cases.list_categories is None:
                        raise RuntimeError("OCR category receipt reader is not configured")
                    active_categories = await use_cases.list_categories(owner_id, kind=kind)
            return OcrQueueReceiptSnapshot(
                operation=operation,
                expected=expected,
                result=result,
                owner=owner,
                active_accounts=active_accounts,
                active_categories=active_categories,
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

    async def confirm_current(
        self,
        context: TelegramOcrQueueContext,
        expected: DraftRef,
    ) -> OcrQueueReceiptSnapshot | None:
        async def mutation(
            use_cases: OcrQueueSessionUseCases,
            command: OcrQueueActionCommand,
        ) -> OcrQueueMutationResult:
            return await use_cases.confirm_current(command)

        return await self._execute(
            context,
            expected,
            OcrQueueOperation.CONFIRMED,
            mutation,
        )

    async def skip_current(
        self,
        context: TelegramOcrQueueContext,
        expected: DraftRef,
    ) -> OcrQueueReceiptSnapshot | None:
        async def mutation(
            use_cases: OcrQueueSessionUseCases,
            command: OcrQueueActionCommand,
        ) -> OcrQueueMutationResult:
            return await use_cases.skip_current(command)

        return await self._execute(
            context,
            expected,
            OcrQueueOperation.SKIPPED,
            mutation,
        )

    async def cancel_queue(
        self,
        context: TelegramOcrQueueContext,
        expected: DraftRef,
    ) -> OcrQueueReceiptSnapshot | None:
        async def mutation(
            use_cases: OcrQueueSessionUseCases,
            command: OcrQueueActionCommand,
        ) -> OcrQueueMutationResult:
            return await use_cases.cancel_queue(command)

        return await self._execute(
            context,
            expected,
            OcrQueueOperation.CANCELLED,
            mutation,
        )
