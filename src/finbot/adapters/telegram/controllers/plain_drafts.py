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
    ConfirmTransactionDraftCommand,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transactions import TransactionUseCases


class PlainDraftOperation(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TelegramPlainDraftContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class PlainDraftReceiptSnapshot:
    """Pure renderer input captured inside the committed mutation transaction."""

    operation: PlainDraftOperation
    expected: DraftRef = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    message_id: int = field(repr=False)
    transaction: TransactionSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        has_transaction = self.transaction is not None
        if (self.operation is PlainDraftOperation.CONFIRMED) != has_transaction:
            raise ValueError("Confirmed plain draft receipt requires one transaction")


class PlainDraftCancellation(Protocol):
    """Minimum channel-neutral draft lifecycle required by plain CANCEL."""

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def cancel(self, owner_id: UUID, expected: DraftRef) -> None: ...


@dataclass(frozen=True, slots=True)
class PlainDraftSessionUseCases:
    transactions: TransactionUseCases = field(repr=False)
    drafts: PlainDraftCancellation = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)


class PlainDraftUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> PlainDraftSessionUseCases: ...


class PlainDraftPresentationGuard(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type PlainDraftReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, PlainDraftReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(receipt: PlainDraftReceiptSnapshot) -> PlainDraftReceiptSnapshot:
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


class PlainDraftController:
    """Confirm or cancel an exact non-OCR review with a durable Telegram receipt."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_guard", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: PlainDraftUseCaseFactory,
        presentation_guard: PlainDraftPresentationGuard,
        enqueue_receipt: PlainDraftReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def _execute(
        self,
        context: TelegramPlainDraftContext,
        expected: DraftRef,
        operation: PlainDraftOperation,
    ) -> PlainDraftReceiptSnapshot | None:
        _validate_expected(expected)

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> PlainDraftReceiptSnapshot:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)

            use_cases = self._use_cases(session)
            transaction: TransactionSnapshot | None = None
            if operation is PlainDraftOperation.CONFIRMED:
                result = await use_cases.transactions.confirm(
                    ConfirmTransactionDraftCommand(owner_id=owner_id, expected=expected)
                )
                transaction = result.transaction
            else:
                active = await use_cases.drafts.get_active(owner_id)
                if active is None or active.ref != expected:
                    raise DraftRevisionConflictError(
                        current_revision=active.revision if active is not None else None
                    )
                if "ocr_batch" in active.payload:
                    raise InvalidStateError("Черновик требует специализированной обработки")
                await use_cases.drafts.cancel(owner_id, expected)
            owner = await use_cases.get_owner_settings(owner_id)
            return PlainDraftReceiptSnapshot(
                operation=operation,
                expected=expected,
                owner=owner,
                message_id=context.message_id,
                transaction=transaction,
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
        context: TelegramPlainDraftContext,
        expected: DraftRef,
    ) -> PlainDraftReceiptSnapshot | None:
        return await self._execute(context, expected, PlainDraftOperation.CONFIRMED)

    async def cancel(
        self,
        context: TelegramPlainDraftContext,
        expected: DraftRef,
    ) -> PlainDraftReceiptSnapshot | None:
        return await self._execute(context, expected, PlainDraftOperation.CANCELLED)
