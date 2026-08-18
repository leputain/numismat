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
    OwnerSnapshot,
    TransactionMutationResult,
    VersionedTransactionCommand,
)
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transactions import TransactionUseCases


class TransactionLifecycleOperation(StrEnum):
    DELETE = "delete"
    RESTORE = "restore"


def _validate_presentation_context(
    message_id: int,
    history_page: int | None,
) -> None:
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise TypeError("Telegram message id must be an integer")
    if message_id <= 0:
        raise ValueError("Telegram message id must be positive")
    if isinstance(history_page, bool) or (
        history_page is not None and not isinstance(history_page, int)
    ):
        raise TypeError("Transaction history page must be an integer")
    if history_page is not None and history_page < 0:
        raise ValueError("Transaction history page must not be negative")


@dataclass(frozen=True, slots=True)
class TelegramTransactionLifecycleContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram transaction lifecycle request is invalid")
        _validate_presentation_context(self.message_id, self.history_page)


@dataclass(frozen=True, slots=True)
class TransactionLifecycleReceiptSnapshot:
    operation: TransactionLifecycleOperation
    mutation: TransactionMutationResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, TransactionLifecycleOperation):
            raise TypeError("Transaction lifecycle operation is invalid")
        if not isinstance(self.mutation, TransactionMutationResult):
            raise TypeError("Transaction lifecycle mutation is invalid")
        if not isinstance(self.owner, OwnerSnapshot):
            raise TypeError("Transaction lifecycle owner is invalid")
        _validate_presentation_context(self.message_id, self.history_page)


@dataclass(frozen=True, slots=True)
class TransactionLifecycleSessionUseCases:
    transactions: TransactionUseCases = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)


class TransactionLifecycleUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> TransactionLifecycleSessionUseCases: ...


type TransactionLifecycleReceiptEnqueuer = Callable[
    [
        TelegramMutationSession,
        TelegramMutationRequest,
        TransactionLifecycleReceiptSnapshot,
    ],
    Awaitable[None],
]


def _same_receipt(
    receipt: TransactionLifecycleReceiptSnapshot,
) -> TransactionLifecycleReceiptSnapshot:
    return receipt


class TransactionLifecycleController:
    """Execute an exact versioned delete or restore in one Telegram UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TransactionLifecycleUseCaseFactory,
        enqueue_receipt: TransactionLifecycleReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def _execute(
        self,
        context: TelegramTransactionLifecycleContext,
        transaction_id: UUID,
        expected_version: int,
        operation: TransactionLifecycleOperation,
    ) -> TransactionLifecycleReceiptSnapshot | None:
        if not isinstance(context, TelegramTransactionLifecycleContext):
            raise TypeError("Telegram transaction lifecycle context is invalid")
        if not isinstance(transaction_id, UUID):
            raise TypeError("Transaction id must be a UUID")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise TypeError("Transaction version must be an integer")
        if expected_version < 1:
            raise ValueError("Transaction version must be positive")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> TransactionLifecycleReceiptSnapshot:
            use_cases = self._use_cases(session)
            command = VersionedTransactionCommand(
                owner_id=owner_id,
                transaction_id=transaction_id,
                expected_version=expected_version,
            )
            mutation = (
                await use_cases.transactions.delete(command)
                if operation is TransactionLifecycleOperation.DELETE
                else await use_cases.transactions.restore(command)
            )
            owner = await use_cases.get_owner_settings(owner_id)
            return TransactionLifecycleReceiptSnapshot(
                operation=operation,
                mutation=mutation,
                owner=owner,
                message_id=context.message_id,
                history_page=context.history_page,
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

    async def delete(
        self,
        context: TelegramTransactionLifecycleContext,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionLifecycleReceiptSnapshot | None:
        return await self._execute(
            context,
            transaction_id,
            expected_version,
            TransactionLifecycleOperation.DELETE,
        )

    async def restore(
        self,
        context: TelegramTransactionLifecycleContext,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionLifecycleReceiptSnapshot | None:
        return await self._execute(
            context,
            transaction_id,
            expected_version,
            TransactionLifecycleOperation.RESTORE,
        )


__all__ = [
    "TelegramTransactionLifecycleContext",
    "TransactionLifecycleController",
    "TransactionLifecycleOperation",
    "TransactionLifecycleReceiptSnapshot",
    "TransactionLifecycleSessionUseCases",
]
