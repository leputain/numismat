from uuid import UUID

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.draft_preparation import PrepareParsedDraftCommand
from finbot.application.dto import (
    CreateDraftCommand,
    DraftSnapshot,
    OcrImageIngressStatus,
    OcrQueueActionCommand,
    OcrQueueCandidate,
    OcrQueueContinuation,
    OcrQueueItemSnapshot,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    PreparedOcrDraft,
    ProcessOcrImageCommand,
    ProcessOcrImageResult,
)
from finbot.application.errors import (
    ActiveDraftConflictError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.application.ocr import ImageTextExtractor, OcrImportError, parse_ocr_transactions
from finbot.application.ocr_queue import OcrQueueState, decode_ocr_queue, encode_ocr_queue
from finbot.application.ports import (
    DraftRepository,
    OcrContextReader,
    OcrDraftPreparer,
    OcrQueueCommandRepository,
)
from finbot.application.use_cases.draft_preparation import PrepareParsedDraft
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionDraft


class SharedOcrDraftPreparer:
    """Adapt the shared parsed-draft policy to the OCR queue port.

    OCR owns sequencing only. Account/category resolution and the resulting
    review state stay in the same preparation policy used by quick input and
    future HTTP adapters.
    """

    __slots__ = ("_prepare",)

    def __init__(self, prepare: PrepareParsedDraft) -> None:
        self._prepare = prepare

    async def prepare(
        self,
        owner_id: UUID,
        draft: TransactionDraft,
    ) -> PreparedOcrDraft:
        prepared = await self._prepare.execute(
            PrepareParsedDraftCommand(owner_id=owner_id, draft=draft, flow="ocr")
        )
        return PreparedOcrDraft(state=prepared.state.value, payload=prepared.payload)


def _snapshot(queue: OcrQueueState, draft: DraftSnapshot) -> OcrQueueItemSnapshot:
    return OcrQueueItemSnapshot(
        position=queue.position,
        total=queue.total,
        saved=queue.saved,
        skipped=queue.skipped,
        draft=draft,
    )


async def _load_queue(
    drafts: DraftRepository,
    command: OcrQueueActionCommand,
) -> tuple[DraftSnapshot, OcrQueueState]:
    draft = await drafts.get_active(command.owner_id)
    if draft is None or draft.ref != command.expected:
        raise DraftRevisionConflictError(
            current_revision=draft.revision if draft is not None else None
        )
    if PENDING_DRAFT_INTENT_KEY in draft.payload:
        raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
    return draft, decode_ocr_queue(draft.payload)


async def _continuation(
    preparer: OcrDraftPreparer,
    owner_id: UUID,
    queue: OcrQueueState,
) -> OcrQueueContinuation | None:
    if not queue.remaining:
        return None
    candidate = queue.remaining[0]
    prepared = await preparer.prepare(owner_id, candidate.to_transaction_draft())
    return OcrQueueContinuation(
        expected_candidate=candidate,
        prepared=PreparedOcrDraft(prepared.state, prepared.payload),
    )


class ProcessOcrImage:
    """Turn bounded local OCR output into one shared sequential review draft.

    Expected recognition failures and an already occupied owner draft slot are
    values, not exceptions.  This lets an outer mutation executor atomically
    commit the processed-update claim and a durable, renderer-safe response.
    """

    __slots__ = ("_contexts", "_drafts", "_extractor", "_preparer")

    def __init__(
        self,
        extractor: ImageTextExtractor,
        contexts: OcrContextReader,
        preparer: OcrDraftPreparer,
        drafts: DraftUseCases,
    ) -> None:
        self._extractor = extractor
        self._contexts = contexts
        self._preparer = preparer
        self._drafts = drafts

    async def __call__(self, command: ProcessOcrImageCommand) -> ProcessOcrImageResult:
        current = await self._drafts.get_active(command.owner_id)
        if current is not None:
            return ProcessOcrImageResult(
                OcrImageIngressStatus.ACTIVE_DRAFT,
                active_draft=current,
            )

        context = await self._contexts.get_ocr_context(command.owner_id)
        if context is None:
            raise EntityNotFoundError("Владелец не найден")

        try:
            recognized_text = await self._extractor.extract_text(command.content, command.mime_type)
            parsed = parse_ocr_transactions(
                recognized_text,
                context.timezone,
                context.currency,
            )
        except OcrImportError:
            return ProcessOcrImageResult(OcrImageIngressStatus.REJECTED)
        # The document text is no longer needed and is never passed to a port or
        # retained in a DTO. Parsed candidates contain only reviewable values.
        del recognized_text

        first = await self._preparer.prepare(command.owner_id, parsed[0])
        remaining = tuple(OcrQueueCandidate.from_transaction_draft(item) for item in parsed[1:])
        queue = OcrQueueState.initial(remaining)
        payload = dict(first.payload)
        payload["flow"] = "ocr"
        payload["ocr_batch"] = encode_ocr_queue(queue)
        try:
            draft = await self._drafts.create(
                CreateDraftCommand(command.owner_id, first.state, payload)
            )
        except ActiveDraftConflictError:
            # A concurrent ingress won the single-draft slot after the initial
            # read.  Never stage or overwrite OCR data: return the exact winner.
            current = await self._drafts.get_active(command.owner_id)
            if current is None:  # pragma: no cover - repository contract violation
                raise
            return ProcessOcrImageResult(
                OcrImageIngressStatus.ACTIVE_DRAFT,
                active_draft=current,
            )
        return ProcessOcrImageResult(
            OcrImageIngressStatus.STARTED,
            queue_item=_snapshot(queue, draft),
        )


class GetOcrQueueItem:
    __slots__ = ("_drafts",)

    def __init__(self, drafts: DraftRepository) -> None:
        self._drafts = drafts

    async def __call__(self, owner_id: UUID) -> OcrQueueItemSnapshot:
        draft = await self._drafts.get_active(owner_id)
        if draft is None:
            raise EntityNotFoundError("OCR-очередь не найдена")
        return _snapshot(decode_ocr_queue(draft.payload), draft)


class _OcrQueueMutation:
    __slots__ = ("_commands", "_drafts", "_preparer")

    def __init__(
        self,
        commands: OcrQueueCommandRepository,
        drafts: DraftRepository,
        preparer: OcrDraftPreparer,
    ) -> None:
        self._commands = commands
        self._drafts = drafts
        self._preparer = preparer

    async def _command(self, command: OcrQueueActionCommand) -> OcrQueueMutationCommand:
        _draft, queue = await _load_queue(self._drafts, command)
        return OcrQueueMutationCommand(
            owner_id=command.owner_id,
            expected=command.expected,
            continuation=await _continuation(self._preparer, command.owner_id, queue),
        )


class ConfirmOcrQueueItem(_OcrQueueMutation):
    async def __call__(self, command: OcrQueueActionCommand) -> OcrQueueMutationResult:
        return await self._commands.confirm_and_continue(await self._command(command))


class SkipOcrQueueItem(_OcrQueueMutation):
    async def __call__(self, command: OcrQueueActionCommand) -> OcrQueueMutationResult:
        return await self._commands.skip_and_continue(await self._command(command))


class CancelOcrQueue:
    __slots__ = ("_commands", "_drafts")

    def __init__(
        self,
        commands: OcrQueueCommandRepository,
        drafts: DraftRepository,
    ) -> None:
        self._commands = commands
        self._drafts = drafts

    async def __call__(self, command: OcrQueueActionCommand) -> OcrQueueMutationResult:
        await _load_queue(self._drafts, command)
        return await self._commands.cancel(
            OcrQueueMutationCommand(
                owner_id=command.owner_id,
                expected=command.expected,
            )
        )
