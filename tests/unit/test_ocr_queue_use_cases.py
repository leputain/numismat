from datetime import UTC, datetime
from uuid import uuid7

import pytest
from fakes.ocr_queue import (
    FakeImageTextExtractor,
    FakeOcrDraftPreparer,
    InMemoryOcrContextReader,
    InMemoryOcrQueueCommandRepository,
)
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import (
    DraftRef,
    OcrImageIngressStatus,
    OcrOwnerContext,
    OcrQueueActionCommand,
    OcrQueueCandidate,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    PreparedOcrDraft,
    ProcessOcrImageCommand,
)
from finbot.application.errors import (
    DraftRevisionConflictError,
    InvalidStateError,
    OcrQueueInvalidError,
)
from finbot.application.ocr import MAX_OCR_IMAGE_BYTES, OcrImportError
from finbot.application.ocr_queue import (
    OcrQueueState,
    decode_ocr_queue,
    encode_ocr_queue,
    serialize_ocr_candidate,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.ocr_queue import (
    CancelOcrQueue,
    ConfirmOcrQueueItem,
    GetOcrQueueItem,
    ProcessOcrImage,
    SkipOcrQueueItem,
)
from finbot.domain.transactions import TransactionDraft, TransactionType


def _candidate(amount: int = 1000) -> OcrQueueCandidate:
    return OcrQueueCandidate(
        kind=TransactionType.EXPENSE,
        amount_minor=amount,
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="private-description",
    )


def _prepared(draft: TransactionDraft) -> PreparedOcrDraft:
    return PreparedOcrDraft(
        "quick_confirm",
        {
            "flow": "ocr",
            "type": draft.type.value,
            "amount_minor": draft.amount_minor,
            "occurred_at": (draft.occurred_at or datetime(2026, 8, 13, tzinfo=UTC)).isoformat(),
            "description": draft.description,
            "account_id": str(uuid7()),
            "category_id": str(uuid7()),
        },
    )


def test_queue_codec_emits_canonical_money_key_and_hides_values_from_repr() -> None:
    candidate = _candidate()
    queue = OcrQueueState.initial((candidate,))
    encoded = encode_ocr_queue(queue)
    first = encoded["remaining"][0]

    assert first["amount_minor"] == 1000
    assert "amount" not in first
    assert decode_ocr_queue({"ocr_batch": encoded}) == queue
    assert "1000" not in repr(candidate)
    assert "private-description" not in repr(candidate)


def test_queue_commands_hide_owner_ref_image_and_continuation_from_repr() -> None:
    owner_id = uuid7()
    draft_id = uuid7()
    active_ref = DraftRef(draft_id, 1)
    action = OcrQueueActionCommand(owner_id, active_ref)
    mutation = OcrQueueMutationCommand(owner_id, active_ref)
    process = ProcessOcrImageCommand(owner_id, b"private-image-marker", "image/png")

    combined = repr((action, mutation, process))
    assert str(owner_id) not in combined
    assert str(draft_id) not in combined
    assert "private-image-marker" not in combined
    assert "image/png" not in combined


def test_queue_codec_reads_legacy_amount_but_rejects_inconsistent_counters() -> None:
    legacy = serialize_ocr_candidate(_candidate())
    legacy["amount"] = legacy.pop("amount_minor")
    payload = {
        "ocr_batch": {
            "version": 1,
            "index": 1,
            "total": 2,
            "saved": 0,
            "skipped": 0,
            "remaining": [legacy],
        }
    }
    assert decode_ocr_queue(payload).remaining == (_candidate(),)

    payload["ocr_batch"]["saved"] = 1
    with pytest.raises(OcrQueueInvalidError):
        decode_ocr_queue(payload)


@pytest.mark.asyncio
async def test_process_image_retains_candidates_not_raw_text_or_image() -> None:
    owner_id = uuid7()
    drafts = InMemoryDraftRepository()
    extractor = FakeImageTextExtractor("12.08.2026\n10:15 Кофейня -250,00 ₽\n11:20 Метро -100,00 ₽")
    preparer = FakeOcrDraftPreparer(_prepared)
    use_case = ProcessOcrImage(
        extractor,
        InMemoryOcrContextReader(
            {owner_id: OcrOwnerContext(timezone="Europe/Moscow", currency="RUB")}
        ),
        preparer,
        DraftUseCases(drafts),
    )
    marker = b"private-image-marker"

    result = await use_case(ProcessOcrImageCommand(owner_id, marker, "image/png"))

    assert result.status is OcrImageIngressStatus.STARTED
    assert result.queue_item is not None
    assert result.queue_item.position == 1
    assert result.queue_item.total == 2
    assert extractor.calls == 1
    assert len(preparer.calls) == 1
    stored = await drafts.get_active(owner_id)
    assert stored is not None
    assert marker.decode() not in repr(stored.payload)
    assert "12.08.2026" not in repr(stored.payload)
    assert len(decode_ocr_queue(stored.payload).remaining) == 1


@pytest.mark.asyncio
async def test_process_image_returns_bounded_rejection_without_exposing_adapter_text() -> None:
    class FailingExtractor:
        async def extract_text(self, content: bytes, mime_type: str) -> str:
            raise OcrImportError("private-adapter-marker")

    owner_id = uuid7()
    use_case = ProcessOcrImage(
        FailingExtractor(),
        InMemoryOcrContextReader(
            {owner_id: OcrOwnerContext(timezone="Europe/Moscow", currency="RUB")}
        ),
        FakeOcrDraftPreparer(_prepared),
        DraftUseCases(InMemoryDraftRepository()),
    )

    result = await use_case(ProcessOcrImageCommand(owner_id, b"private-image", "image/png"))

    assert result.status is OcrImageIngressStatus.REJECTED
    assert result.draft is None
    assert "private-adapter-marker" not in repr(result)
    assert "private-image" not in repr(result)


@pytest.mark.asyncio
async def test_process_image_returns_active_draft_without_extracting_or_overwriting_pending() -> (
    None
):
    owner_id = uuid7()
    drafts = InMemoryDraftRepository()
    active = await drafts.create_if_absent(
        owner_id,
        "wizard_amount",
        {
            "flow": "wizard",
            "pending_intent": {"kind": "wizard"},
        },
    )
    extractor = FakeImageTextExtractor("private OCR text")
    use_case = ProcessOcrImage(
        extractor,
        InMemoryOcrContextReader({}),
        FakeOcrDraftPreparer(_prepared),
        DraftUseCases(drafts),
    )

    result = await use_case(ProcessOcrImageCommand(owner_id, b"private-image", "image/png"))

    assert result.status is OcrImageIngressStatus.ACTIVE_DRAFT
    assert result.active_draft == active
    assert extractor.calls == 0
    current = await drafts.get_active(owner_id)
    assert current == active
    assert current is not None
    assert current.payload["pending_intent"] == {"kind": "wizard"}


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("empty", "must not be empty"),
        ("oversized", "too large"),
        ("mime", "not supported"),
    ],
)
def test_process_image_command_rejects_unbounded_or_unsupported_input(
    case: str,
    error: str,
) -> None:
    content = b"x" * (MAX_OCR_IMAGE_BYTES + 1) if case == "oversized" else b"image"
    mime_type = "application/pdf" if case == "mime" else "image/png"
    if case == "empty":
        content = b""
    with pytest.raises(ValueError, match=error):
        ProcessOcrImageCommand(uuid7(), content, mime_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "skip"])
async def test_queue_mutations_prepare_exact_next_candidate(action: str) -> None:
    owner_id = uuid7()
    drafts = InMemoryDraftRepository()
    queue = OcrQueueState.initial((_candidate(2000),))
    active = await drafts.create_if_absent(
        owner_id,
        "quick_confirm",
        {"flow": "ocr", "ocr_batch": encode_ocr_queue(queue)},
    )
    result = OcrQueueMutationResult(
        status=OcrQueueStatus.ADVANCED,
        saved=int(action == "confirm"),
        skipped=int(action == "skip"),
        draft=active,
    )
    commands = InMemoryOcrQueueCommandRepository(result)
    preparer = FakeOcrDraftPreparer(_prepared)
    command = OcrQueueActionCommand(owner_id, active.ref)

    if action == "confirm":
        returned = await ConfirmOcrQueueItem(commands, drafts, preparer)(command)
    else:
        returned = await SkipOcrQueueItem(commands, drafts, preparer)(command)

    assert returned is result
    _, repository_command = commands.calls[0]
    assert repository_command.continuation is not None
    assert repository_command.continuation.expected_candidate.amount_minor == 2000
    assert len(preparer.calls) == 1


@pytest.mark.asyncio
async def test_get_cancel_and_stale_mutation_are_revision_safe() -> None:
    owner_id = uuid7()
    drafts = InMemoryDraftRepository()
    queue = OcrQueueState.initial(())
    active = await drafts.create_if_absent(
        owner_id,
        "quick_confirm",
        {"flow": "ocr", "ocr_batch": encode_ocr_queue(queue)},
    )
    result = OcrQueueMutationResult(
        status=OcrQueueStatus.CANCELLED,
        saved=0,
        skipped=0,
    )
    commands = InMemoryOcrQueueCommandRepository(result)
    current = await GetOcrQueueItem(drafts)(owner_id)
    assert current.draft == active

    cancelled = await CancelOcrQueue(commands, drafts)(OcrQueueActionCommand(owner_id, active.ref))
    assert cancelled is result
    assert commands.calls[0][1].continuation is None

    with pytest.raises(DraftRevisionConflictError):
        await CancelOcrQueue(commands, drafts)(
            OcrQueueActionCommand(owner_id, active.ref.__class__(uuid7(), active.revision))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "skip", "cancel"])
async def test_queue_mutations_reject_pending_intent_before_preparation_or_command(
    action: str,
) -> None:
    owner_id = uuid7()
    drafts = InMemoryDraftRepository()
    active = await drafts.create_if_absent(
        owner_id,
        "quick_confirm",
        {
            "flow": "ocr",
            "ocr_batch": encode_ocr_queue(OcrQueueState.initial((_candidate(2000),))),
            "pending_intent": {"kind": "wizard"},
        },
    )
    commands = InMemoryOcrQueueCommandRepository(
        OcrQueueMutationResult(
            status=OcrQueueStatus.CANCELLED,
            saved=0,
            skipped=0,
        )
    )
    preparer = FakeOcrDraftPreparer(_prepared)
    command = OcrQueueActionCommand(owner_id, active.ref)

    with pytest.raises(InvalidStateError):
        if action == "confirm":
            await ConfirmOcrQueueItem(commands, drafts, preparer)(command)
        elif action == "skip":
            await SkipOcrQueueItem(commands, drafts, preparer)(command)
        else:
            await CancelOcrQueue(commands, drafts)(command)

    assert commands.calls == []
    assert preparer.calls == []
    assert await drafts.get_active(owner_id) == active
