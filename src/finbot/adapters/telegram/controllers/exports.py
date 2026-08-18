from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.dto import DraftRef
from finbot.application.export import CsvExportReceiptSnapshot
from finbot.application.ports import DraftRepository
from finbot.application.use_cases.drafts import DraftUseCases


class CsvExportDraftRepositoryFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftRepository: ...


type CsvExportReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, CsvExportReceiptSnapshot],
    Awaitable[None],
]


class CsvExportController:
    """Atomically claim export intent, suspend the draft, and queue a safe job."""

    __slots__ = ("_draft_repositories", "_enqueue_receipt", "_executor")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        draft_repository_factory: CsvExportDraftRepositoryFactory,
        enqueue_receipt: CsvExportReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._draft_repositories = draft_repository_factory
        self._enqueue_receipt = enqueue_receipt

    async def request(
        self,
        request: TelegramMutationRequest,
    ) -> CsvExportReceiptSnapshot | None:
        if not isinstance(request, TelegramMutationRequest):
            raise TypeError("Telegram CSV export request is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftRef | None:
            drafts = DraftUseCases(self._draft_repositories(session))
            active = await drafts.get_active(owner_id)
            if active is None:
                return None
            return (await drafts.suspend(owner_id, active.ref)).ref

        execution = await self._executor.execute(
            request,
            mutate=mutate,
            build_receipt=CsvExportReceiptSnapshot,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
