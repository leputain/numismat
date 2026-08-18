from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueOperation,
    OcrQueueReceiptSnapshot,
    OcrQueueSessionUseCases,
)
from finbot.adapters.telegram.controllers.plain_drafts import (
    PlainDraftOperation,
    PlainDraftReceiptSnapshot,
    PlainDraftSessionUseCases,
)
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    ConfirmTransactionDraftCommand,
    DraftRef,
    DraftSnapshot,
    OcrQueueActionCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.domain.transactions import TransactionType


class DraftCompletionAction(StrEnum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    SKIP_OCR_ITEM = "skip_ocr_item"


class DraftCompletionKind(StrEnum):
    PLAIN = "plain"
    OCR_QUEUE = "ocr_queue"


@dataclass(frozen=True, slots=True)
class TelegramDraftCompletionContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class DraftCompletionReceiptSnapshot:
    """Discriminated, privacy-safe renderer input for one committed action."""

    kind: DraftCompletionKind
    plain: PlainDraftReceiptSnapshot | None = field(default=None, repr=False)
    ocr_queue: OcrQueueReceiptSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.kind is DraftCompletionKind.PLAIN:
            if self.plain is None or self.ocr_queue is not None:
                raise ValueError("Plain completion receipt requires only a plain payload")
        elif self.ocr_queue is None or self.plain is not None:
            raise ValueError("OCR completion receipt requires only an OCR payload")


@dataclass(frozen=True, slots=True)
class DraftCompletionSessionUseCases:
    """All collaborators share the executor-owned persistence transaction."""

    plain: PlainDraftSessionUseCases = field(repr=False)
    ocr_queue: OcrQueueSessionUseCases = field(repr=False)


class DraftCompletionUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> DraftCompletionSessionUseCases: ...


class DraftCompletionPresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type DraftCompletionReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftCompletionReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(receipt: DraftCompletionReceiptSnapshot) -> DraftCompletionReceiptSnapshot:
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


def _require_exact_active(
    active: DraftSnapshot | None,
    expected: DraftRef,
) -> DraftSnapshot:
    if active is None or active.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=active.revision if active is not None else None
        )
    return active


async def _ocr_receipt(
    use_cases: OcrQueueSessionUseCases,
    owner_id: UUID,
    expected: DraftRef,
    message_id: int,
    action: DraftCompletionAction,
) -> OcrQueueReceiptSnapshot:
    command = OcrQueueActionCommand(owner_id=owner_id, expected=expected)
    operation: OcrQueueOperation
    result: OcrQueueMutationResult
    if action is DraftCompletionAction.CONFIRM:
        operation = OcrQueueOperation.CONFIRMED
        result = await use_cases.confirm_current(command)
    elif action is DraftCompletionAction.SKIP_OCR_ITEM:
        operation = OcrQueueOperation.SKIPPED
        result = await use_cases.skip_current(command)
    elif action is DraftCompletionAction.CANCEL:
        operation = OcrQueueOperation.CANCELLED
        result = await use_cases.cancel_queue(command)
    else:  # pragma: no cover - closed enum, defensive against untyped callers
        raise InvalidStateError("Неизвестное действие с очередью OCR")

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
        message_id=message_id,
    )


async def _plain_receipt(
    use_cases: PlainDraftSessionUseCases,
    owner_id: UUID,
    expected: DraftRef,
    message_id: int,
    action: DraftCompletionAction,
) -> PlainDraftReceiptSnapshot:
    transaction: TransactionSnapshot | None = None
    if action is DraftCompletionAction.CONFIRM:
        result = await use_cases.transactions.confirm(
            ConfirmTransactionDraftCommand(owner_id=owner_id, expected=expected)
        )
        transaction = result.transaction
        operation = PlainDraftOperation.CONFIRMED
    elif action is DraftCompletionAction.CANCEL:
        await use_cases.drafts.cancel(owner_id, expected)
        operation = PlainDraftOperation.CANCELLED
    else:  # pragma: no cover - guarded by the classifier
        raise InvalidStateError("Действие доступно только для очереди OCR")

    return PlainDraftReceiptSnapshot(
        operation=operation,
        expected=expected,
        owner=await use_cases.get_owner_settings(owner_id),
        message_id=message_id,
        transaction=transaction,
    )


class DraftCompletionController:
    """Classify and complete an exact draft inside one executor-owned UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftCompletionUseCaseFactory,
        presentation_guard: DraftCompletionPresentationGuard,
        enqueue_receipt: DraftCompletionReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def _execute(
        self,
        context: TelegramDraftCompletionContext,
        expected: DraftRef,
        action: DraftCompletionAction,
    ) -> DraftCompletionReceiptSnapshot | None:
        _validate_expected(expected)

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftCompletionReceiptSnapshot:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)

            use_cases = self._use_cases(session)
            active = _require_exact_active(
                await use_cases.plain.drafts.get_active(owner_id),
                expected,
            )
            is_ocr_queue = "ocr_batch" in active.payload
            if action is DraftCompletionAction.SKIP_OCR_ITEM and not is_ocr_queue:
                raise InvalidStateError("Действие доступно только для очереди OCR")

            if is_ocr_queue:
                ocr_receipt = await _ocr_receipt(
                    use_cases.ocr_queue,
                    owner_id,
                    expected,
                    context.message_id,
                    action,
                )
                return DraftCompletionReceiptSnapshot(
                    kind=DraftCompletionKind.OCR_QUEUE,
                    ocr_queue=ocr_receipt,
                )

            plain_receipt = await _plain_receipt(
                use_cases.plain,
                owner_id,
                expected,
                context.message_id,
                action,
            )
            return DraftCompletionReceiptSnapshot(
                kind=DraftCompletionKind.PLAIN,
                plain=plain_receipt,
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

    async def confirm(
        self,
        context: TelegramDraftCompletionContext,
        expected: DraftRef,
    ) -> DraftCompletionReceiptSnapshot | None:
        return await self._execute(context, expected, DraftCompletionAction.CONFIRM)

    async def cancel(
        self,
        context: TelegramDraftCompletionContext,
        expected: DraftRef,
    ) -> DraftCompletionReceiptSnapshot | None:
        return await self._execute(context, expected, DraftCompletionAction.CANCEL)

    async def skip_ocr_item(
        self,
        context: TelegramDraftCompletionContext,
        expected: DraftRef,
    ) -> DraftCompletionReceiptSnapshot | None:
        return await self._execute(context, expected, DraftCompletionAction.SKIP_OCR_ITEM)
