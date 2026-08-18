from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingEditIntent,
    decode_pending_draft_intent,
)
from finbot.application.draft_ingress import BankImportDraftIngressBlockedError
from finbot.application.dto import DraftRef
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    BeginTransactionEditResult,
    TransactionEditIngressStatus,
)
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit


def _validate_context_values(message_id: int, history_page: int) -> None:
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise TypeError("Telegram message id must be an integer")
    if message_id <= 0:
        raise ValueError("Telegram message id must be positive")
    if isinstance(history_page, bool) or not isinstance(history_page, int):
        raise TypeError("Transaction history page must be an integer")
    if not 0 <= history_page <= MAX_PAGE:
        raise ValueError(f"Transaction history page must be between 0 and {MAX_PAGE}")


@dataclass(frozen=True, slots=True)
class TelegramTransactionEditContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_context_values(self.message_id, self.history_page)


@dataclass(frozen=True, slots=True)
class TransactionEditReceiptSnapshot:
    """Revision-bound renderer input; history location is adapter-only state."""

    result: BeginTransactionEditResult = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_context_values(self.message_id, self.history_page)

        expected_intent = PendingEditIntent(
            self.result.transaction.transaction_id,
            self.result.transaction.version,
        )
        if self.result.status is TransactionEditIngressStatus.DRAFT_CREATED:
            if dict(self.result.draft.payload) != {
                "transaction_id": str(self.result.transaction.transaction_id),
                "version": self.result.transaction.version,
            }:
                raise ValueError("Created transaction edit draft is not canonical")
            return
        if self.result.status is TransactionEditIngressStatus.BLOCKED_BANK_IMPORT:
            if (
                self.result.draft.payload.get("flow") != "bank_import"
                or PENDING_DRAFT_INTENT_KEY in self.result.draft.payload
            ):
                raise ValueError("Blocked transaction edit receipt is invalid")
            return
        pending = self.result.draft.payload.get(PENDING_DRAFT_INTENT_KEY)
        if (
            not isinstance(pending, Mapping)
            or set(pending) != {"kind", "transaction_id", "version"}
            or "history_page" in self.result.draft.payload
        ):
            raise ValueError("Transaction edit conflict draft is not channel-neutral")
        if decode_pending_draft_intent(pending) != expected_intent:
            raise ValueError("Transaction edit conflict intent is not authoritative")

    @property
    def draft_ref(self) -> DraftRef:
        return self.result.draft.ref


@dataclass(frozen=True, slots=True)
class TransactionEditSessionUseCases:
    begin: BeginTransactionEdit = field(repr=False)


class TransactionEditUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> TransactionEditSessionUseCases: ...


type TransactionEditReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TransactionEditReceiptSnapshot],
    Awaitable[None],
]


class TransactionEditIngressController:
    """Begin one exact saved-transaction edit and queue its receipt atomically."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TransactionEditUseCaseFactory,
        enqueue_receipt: TransactionEditReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def begin(
        self,
        context: TelegramTransactionEditContext,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionEditReceiptSnapshot | None:
        if not isinstance(transaction_id, UUID):
            raise TypeError("Edited transaction id must be a UUID")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("Edited transaction version must be positive")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> BeginTransactionEditResult:
            use_cases = self._use_cases(session)
            try:
                return await use_cases.begin.execute(
                    BeginTransactionEditCommand(owner_id, transaction_id, expected_version)
                )
            except BankImportDraftIngressBlockedError as exc:
                if exc.transaction is None:
                    raise RuntimeError("Blocked transaction edit has no target") from exc
                return BeginTransactionEditResult(
                    TransactionEditIngressStatus.BLOCKED_BANK_IMPORT,
                    exc.owner,
                    exc.transaction,
                    exc.draft,
                )

        def build_receipt(result: BeginTransactionEditResult) -> TransactionEditReceiptSnapshot:
            return TransactionEditReceiptSnapshot(
                result=result,
                message_id=context.message_id,
                history_page=context.history_page,
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
