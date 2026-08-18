from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.ocr_images import (
    OcrImageController,
    OcrImageReceiptSnapshot,
    OcrImageSessionUseCases,
    TelegramOcrImageContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftSnapshot,
    OcrImageIngressStatus,
    OcrQueueItemSnapshot,
    OwnerSnapshot,
    ProcessOcrImageCommand,
    ProcessOcrImageResult,
)
from finbot.application.use_cases.ocr_queue import ProcessOcrImage
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts, ListCategories
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")


@dataclass(slots=True)
class _Owner:
    id: UUID


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _FakeSession:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


class _ProcessImage:
    def __init__(self, events: list[str], result: ProcessOcrImageResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[ProcessOcrImageCommand] = []

    async def __call__(self, command: ProcessOcrImageCommand) -> ProcessOcrImageResult:
        self.events.append("process_image")
        self.commands.append(command)
        return self.result


class _GetOwner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("get_owner")
        return _owner_snapshot()


class _ListAccounts:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __call__(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID
        assert archived is False
        self.events.append("list_accounts")
        return (_account(),)


class _ListCategories:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert owner_id == OWNER_ID
        assert kind is TransactionType.EXPENSE
        assert archived is False
        self.events.append("list_categories")
        return (_category(),)


def _owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _account() -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, "Закрытый счёт", "cash", "RUB", None, 1)


def _category() -> CategorySnapshot:
    return CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        "Закрытая категория",
        "▫️",
        None,
        1,
    )


def _draft(state: str = "category_required") -> DraftSnapshot:
    return DraftSnapshot(
        DRAFT_ID,
        state,
        {
            "flow": "ocr",
            "type": "expense",
            "amount_minor": 12_345,
            "occurred_at": datetime(2026, 8, 13, 12, tzinfo=UTC).isoformat(),
            "description": "закрытое описание",
            "ocr_batch": {
                "version": 1,
                "index": 1,
                "total": 1,
                "saved": 0,
                "skipped": 0,
                "remaining": [],
            },
        },
        revision=3,
    )


def _started_result(state: str = "category_required") -> ProcessOcrImageResult:
    draft = _draft(state)
    return ProcessOcrImageResult(
        OcrImageIngressStatus.STARTED,
        queue_item=OcrQueueItemSnapshot(1, 1, 0, 0, draft),
    )


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    result: ProcessOcrImageResult,
    receipts: list[OcrImageReceiptSnapshot],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> tuple[OcrImageController, _ProcessImage]:
    session = _FakeSession(events)
    process_image = _ProcessImage(events, result)

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual_session: AsyncSession,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        locale: str,
        timezone: str,
        currency: str,
    ) -> _Owner:
        assert actual_session is cast(Any, session)
        assert (telegram_user_id, telegram_chat_id) == (92_000_002, 93_000_003)
        assert (locale, timezone, currency) == ("ru", "Europe/Moscow", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    sessions = cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))

    def use_case_factory(actual_session: AsyncSession) -> OcrImageSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return OcrImageSessionUseCases(
            cast(ProcessOcrImage, process_image),
            cast(GetOwnerSettings, _GetOwner(events)),
            cast(ListAccounts, _ListAccounts(events)),
            cast(ListCategories, _ListCategories(events)),
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: OcrImageReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return (
        OcrImageController(TelegramMutationExecutor(sessions), use_case_factory, enqueue),
        process_image,
    )


@pytest.mark.asyncio
async def test_controller_claims_before_ocr_and_enqueues_render_complete_queue_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrImageReceiptSnapshot] = []
    result = _started_result()
    controller, process_image = _controller(monkeypatch, events, result, receipts)
    context = TelegramOcrImageContext(_request(), b"private-image", "image/png")

    receipt = await controller.process(context)

    assert receipt is receipts[0]
    assert receipt.result is result
    assert result.draft is not None
    assert receipt.draft_ref == result.draft.ref
    assert receipt.active_accounts == ()
    assert receipt.active_categories == (_category(),)
    assert len(process_image.commands) == 1
    assert process_image.commands[0].owner_id == OWNER_ID
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "process_image",
        "get_owner",
        "list_categories",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_skips_local_ocr_and_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrImageReceiptSnapshot] = []
    controller, process_image = _controller(
        monkeypatch,
        events,
        _started_result(),
        receipts,
        claimed=False,
    )

    receipt = await controller.process(
        TelegramOcrImageContext(_request(), b"private-image", "image/png")
    )

    assert receipt is None
    assert process_image.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_invalid_bounded_input_commits_safe_rejection_without_calling_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrImageReceiptSnapshot] = []
    controller, process_image = _controller(
        monkeypatch,
        events,
        _started_result(),
        receipts,
    )

    receipt = await controller.process(
        TelegramOcrImageContext(_request(), b"private-document", "application/pdf")
    )

    assert receipt is receipts[0]
    assert receipt.result.status is OcrImageIngressStatus.REJECTED
    assert receipt.draft_ref is None
    assert process_image.commands == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "get_owner",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_active_draft_conflict_receipt_has_exact_draft_and_no_catalog_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrImageReceiptSnapshot] = []
    active = _draft("wizard_amount")
    result = ProcessOcrImageResult(
        OcrImageIngressStatus.ACTIVE_DRAFT,
        active_draft=active,
    )
    controller, _process_image = _controller(monkeypatch, events, result, receipts)

    receipt = await controller.process(
        TelegramOcrImageContext(_request(), b"private-image", "image/png")
    )

    assert receipt is not None
    assert receipt.draft_ref == active.ref
    assert receipt.active_accounts == ()
    assert receipt.active_categories == ()
    assert "list_accounts" not in events
    assert "list_categories" not in events


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_ocr_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrImageReceiptSnapshot] = []
    controller, _process_image = _controller(
        monkeypatch,
        events,
        _started_result("review"),
        receipts,
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.process(TelegramOcrImageContext(_request(), b"private-image", "image/png"))

    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


def test_ocr_image_contracts_hide_bytes_text_mime_and_identifiers_from_repr() -> None:
    context = TelegramOcrImageContext(_request(), b"private-image-marker", "image/png")
    result = _started_result("review")
    receipt = OcrImageReceiptSnapshot(result, _owner_snapshot())
    use_cases = OcrImageSessionUseCases(
        cast(ProcessOcrImage, object()),
        cast(GetOwnerSettings, object()),
        cast(ListAccounts, object()),
        cast(ListCategories, object()),
    )

    rendered = repr((context, result, receipt, use_cases))
    for private in (
        "private-image-marker",
        "image/png",
        "закрытое описание",
        "RUB",
        str(OWNER_ID),
        str(DRAFT_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "91000001",
        "92000002",
        "93000003",
        "12345",
    ):
        assert private not in rendered

    with pytest.raises(TypeError):
        TelegramOcrImageContext(_request(), "not-bytes", "image/png")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OcrImageReceiptSnapshot(object(), _owner_snapshot())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProcessOcrImageResult(OcrImageIngressStatus.STARTED)
