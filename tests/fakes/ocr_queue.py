from collections.abc import Callable
from uuid import UUID

from finbot.application.dto import (
    OcrOwnerContext,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    PreparedOcrDraft,
)
from finbot.domain.transactions import TransactionDraft


class FakeImageTextExtractor:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def extract_text(self, content: bytes, mime_type: str) -> str:
        self.calls += 1
        return self._text


class InMemoryOcrContextReader:
    def __init__(self, contexts: dict[UUID, OcrOwnerContext]) -> None:
        self._contexts = dict(contexts)

    async def get_ocr_context(self, owner_id: UUID) -> OcrOwnerContext | None:
        return self._contexts.get(owner_id)


class FakeOcrDraftPreparer:
    def __init__(self, prepare: Callable[[TransactionDraft], PreparedOcrDraft]) -> None:
        self._prepare = prepare
        self.calls: list[UUID] = []

    async def prepare(self, owner_id: UUID, draft: TransactionDraft) -> PreparedOcrDraft:
        self.calls.append(owner_id)
        return self._prepare(draft)


class InMemoryOcrQueueCommandRepository:
    def __init__(self, result: OcrQueueMutationResult) -> None:
        self.result = result
        self.calls: list[tuple[str, OcrQueueMutationCommand]] = []

    async def confirm_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        self.calls.append(("confirm", command))
        return self.result

    async def skip_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        self.calls.append(("skip", command))
        return self.result

    async def cancel(self, command: OcrQueueMutationCommand) -> OcrQueueMutationResult:
        self.calls.append(("cancel", command))
        return self.result
