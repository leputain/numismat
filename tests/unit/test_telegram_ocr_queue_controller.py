import ast
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueController,
    OcrQueueOperation,
    OcrQueueReceiptSnapshot,
    OcrQueueSessionUseCases,
    TelegramOcrQueueContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    OcrQueueCandidate,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    OwnerSnapshot,
    PreparedOcrDraft,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.ocr_queue import OcrQueueState, encode_ocr_queue
from finbot.application.use_cases.ocr_queue import (
    CancelOcrQueue,
    ConfirmOcrQueueItem,
    SkipOcrQueueItem,
)
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.domain.transactions import TransactionDraft, TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")
NEXT_DRAFT_ID = DRAFT_ID
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000301")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
EXPECTED = DraftRef(DRAFT_ID, 3)
OCCURRED_AT = datetime(2026, 8, 13, 10, tzinfo=UTC)


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


class _RecordingDraftRepository:
    def __init__(self, events: list[str], active: DraftSnapshot | None) -> None:
        self.events = events
        self.active = active

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def create_if_absent(
        self,
        _owner_id: UUID,
        _state: str,
        _payload: dict[str, Any],
    ) -> DraftSnapshot:
        raise AssertionError("queue controller attempted to create a draft")

    async def update(
        self,
        _owner_id: UUID,
        _expected: DraftRef,
        _state: str,
        _payload: dict[str, Any],
    ) -> DraftSnapshot:
        raise AssertionError("queue controller attempted to update a draft directly")

    async def replace(
        self,
        _owner_id: UUID,
        _expected: DraftRef,
        _state: str,
        _payload: dict[str, Any],
    ) -> DraftSnapshot:
        raise AssertionError("queue controller attempted to replace a draft directly")

    async def set_suspended(
        self,
        _owner_id: UUID,
        _expected: DraftRef,
        _suspended: bool,
    ) -> DraftSnapshot:
        raise AssertionError("queue controller attempted to suspend a draft")

    async def delete(self, _owner_id: UUID, _expected: DraftRef) -> None:
        raise AssertionError("queue controller attempted to delete a draft directly")


class _RecordingPreparer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def prepare(
        self,
        owner_id: UUID,
        draft: TransactionDraft,
    ) -> PreparedOcrDraft:
        assert owner_id == OWNER_ID
        assert draft.amount_minor == 22_200
        self.events.append("prepare.next")
        return PreparedOcrDraft(
            state="review",
            payload={
                "flow": "ocr",
                "type": "expense",
                "amount_minor": draft.amount_minor,
                "account_id": str(ACCOUNT_ID),
                "category_id": str(CATEGORY_ID),
                "occurred_at": OCCURRED_AT.isoformat(),
                "description": "следующий секрет",
            },
        )


class _RecordingCommands:
    def __init__(
        self,
        events: list[str],
        results: dict[str, OcrQueueMutationResult],
    ) -> None:
        self.events = events
        self.results = results
        self.calls: list[tuple[str, OcrQueueMutationCommand]] = []

    async def confirm_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        self.events.append("command.confirm")
        self.calls.append(("confirm", command))
        return self.results["confirm"]

    async def skip_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        self.events.append("command.skip")
        self.calls.append(("skip", command))
        return self.results["skip"]

    async def cancel(self, command: OcrQueueMutationCommand) -> OcrQueueMutationResult:
        self.events.append("command.cancel")
        self.calls.append(("cancel", command))
        return self.results["cancel"]


class _OwnerReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.events.append("query.owner")
        if owner_id != OWNER_ID:
            return None
        return OwnerSnapshot(
            owner_id=OWNER_ID,
            locale="ru",
            timezone="Europe/Moscow",
            base_currency="RUB",
            default_account_id=ACCOUNT_ID,
        )


def _candidate() -> OcrQueueCandidate:
    return OcrQueueCandidate(
        kind=TransactionType.EXPENSE,
        amount_minor=22_200,
        occurred_at=OCCURRED_AT,
        description="следующий секрет",
    )


def _active_draft() -> DraftSnapshot:
    queue = OcrQueueState.initial((_candidate(),))
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state="review",
        payload={
            "flow": "ocr",
            "type": "expense",
            "amount_minor": 11_100,
            "account_id": str(ACCOUNT_ID),
            "category_id": str(CATEGORY_ID),
            "occurred_at": OCCURRED_AT.isoformat(),
            "description": "текущий секрет",
            "ocr_batch": encode_ocr_queue(queue),
        },
        revision=EXPECTED.revision,
    )


def _next_draft(*, saved: int = 1, skipped: int = 0) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=NEXT_DRAFT_ID,
        state="review",
        payload={
            "flow": "ocr",
            "type": "expense",
            "amount_minor": 22_200,
            "account_id": str(ACCOUNT_ID),
            "category_id": str(CATEGORY_ID),
            "occurred_at": OCCURRED_AT.isoformat(),
            "description": "следующий секрет",
            "ocr_batch": {
                "version": 1,
                "position": 2,
                "total": 2,
                "saved": saved,
                "skipped": skipped,
                "remaining": [],
            },
        },
        revision=EXPECTED.revision + 1,
    )


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=11_100,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Тайный счёт",
        category_id=CATEGORY_ID,
        category_name="Тайная категория",
        category_emoji="▫️",
        occurred_at=OCCURRED_AT,
        description="текущий секрет",
        version=1,
    )


def _results() -> dict[str, OcrQueueMutationResult]:
    return {
        "confirm": OcrQueueMutationResult(
            status=OcrQueueStatus.ADVANCED,
            saved=1,
            skipped=0,
            draft=_next_draft(),
            transaction=_transaction(),
        ),
        "skip": OcrQueueMutationResult(
            status=OcrQueueStatus.ADVANCED,
            saved=0,
            skipped=1,
            draft=_next_draft(saved=0, skipped=1),
        ),
        "cancel": OcrQueueMutationResult(
            status=OcrQueueStatus.CANCELLED,
            saved=0,
            skipped=0,
        ),
    }


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
    commands: _RecordingCommands,
    drafts: _RecordingDraftRepository,
    receipts: list[OcrQueueReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_current: bool | None = None,
) -> OcrQueueController:
    session = _FakeSession(events)

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
    executor = TelegramMutationExecutor(sessions)

    def use_case_factory(actual_session: AsyncSession) -> OcrQueueSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        preparer = _RecordingPreparer(events)

        async def presentation_guard(
            owner_id: UUID,
            expected: DraftRef,
            message_id: int,
        ) -> bool:
            assert owner_id == OWNER_ID
            assert expected == EXPECTED
            assert message_id == 77
            events.append("presentation.guard")
            return bool(presentation_current)

        return OcrQueueSessionUseCases(
            confirm_current=ConfirmOcrQueueItem(commands, drafts, preparer),
            skip_current=SkipOcrQueueItem(commands, drafts, preparer),
            cancel_queue=CancelOcrQueue(commands, drafts),
            get_owner_settings=GetOwnerSettings(_OwnerReader(events)),
            presentation_is_current=(
                presentation_guard if presentation_current is not None else None
            ),
        )

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: OcrQueueReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return OcrQueueController(executor, use_case_factory, enqueue_receipt)


@dataclass(frozen=True, slots=True)
class _ActionCase:
    method: str
    command: str
    operation: OcrQueueOperation
    prepares_next: bool


ACTION_CASES = (
    _ActionCase("confirm_current", "confirm", OcrQueueOperation.CONFIRMED, True),
    _ActionCase("skip_current", "skip", OcrQueueOperation.SKIPPED, True),
    _ActionCase("cancel_queue", "cancel", OcrQueueOperation.CANCELLED, False),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ACTION_CASES, ids=lambda case: case.method)
async def test_controller_runs_exact_queue_action_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _ActionCase,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    receipts: list[OcrQueueReceiptSnapshot] = []
    commands = _RecordingCommands(events, _results())
    controller = _controller(
        monkeypatch,
        events,
        commands,
        _RecordingDraftRepository(events, _active_draft()),
        receipts,
    )

    result = await getattr(controller, case.method)(
        TelegramOcrQueueContext(_request(), message_id=77),
        EXPECTED,
    )

    assert result is not None
    assert result.operation is case.operation
    assert result.expected is EXPECTED
    assert result.owner.timezone == "Europe/Moscow"
    assert result.owner.base_currency == "RUB"
    assert result.message_id == 77
    assert receipts == [result]
    recorded_action, repository_command = commands.calls[0]
    assert recorded_action == case.command
    assert repository_command.expected is EXPECTED
    assert repository_command.owner_id == OWNER_ID
    assert (repository_command.continuation is not None) is case.prepares_next
    expected_events = [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "draft.get",
    ]
    if case.prepares_next:
        expected_events.append("prepare.next")
    expected_events.extend(
        [
            f"command.{case.command}",
            "query.owner",
            "outbox",
            "commit",
            "session.exit",
        ]
    )
    assert events == expected_events
    assert caplog.records == []


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_loading_queue_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrQueueReceiptSnapshot] = []
    commands = _RecordingCommands(events, _results())
    controller = _controller(
        monkeypatch,
        events,
        commands,
        _RecordingDraftRepository(events, _active_draft()),
        receipts,
        claimed=False,
    )

    result = await controller.confirm_current(TelegramOcrQueueContext(_request()), EXPECTED)

    assert result is None
    assert commands.calls == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_exact_draft_ref_conflict_rolls_back_without_command_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrQueueReceiptSnapshot] = []
    commands = _RecordingCommands(events, _results())
    controller = _controller(
        monkeypatch,
        events,
        commands,
        _RecordingDraftRepository(events, _active_draft()),
        receipts,
    )
    stale = DraftRef(UUID("00000000-0000-7000-8000-000000000999"), EXPECTED.revision)

    with pytest.raises(DraftRevisionConflictError) as failure:
        await controller.skip_current(TelegramOcrQueueContext(_request()), stale)

    assert failure.value.details == {"current_revision": EXPECTED.revision}
    assert commands.calls == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "draft.get",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_presentation_race_is_rejected_inside_executor_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrQueueReceiptSnapshot] = []
    commands = _RecordingCommands(events, _results())
    controller = _controller(
        monkeypatch,
        events,
        commands,
        _RecordingDraftRepository(events, _active_draft()),
        receipts,
        presentation_current=False,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.confirm_current(
            TelegramOcrQueueContext(_request(), message_id=77),
            EXPECTED,
        )

    assert commands.calls == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "presentation.guard",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_none_update_commits_and_returns_post_commit_receipt_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[OcrQueueReceiptSnapshot] = []
    commands = _RecordingCommands(events, _results())
    controller = _controller(
        monkeypatch,
        events,
        commands,
        _RecordingDraftRepository(events, _active_draft()),
        receipts,
    )

    result = await controller.cancel_queue(
        TelegramOcrQueueContext(_request(update_id=None), message_id=88),
        EXPECTED,
    )

    assert result is not None
    assert result.operation is OcrQueueOperation.CANCELLED
    assert result.result.status is OcrQueueStatus.CANCELLED
    assert result.message_id == 88
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "draft.get",
        "command.cancel",
        "query.owner",
        "commit",
        "session.exit",
    ]


def test_receipt_has_every_safe_renderer_input_but_repr_hides_financial_data() -> None:
    owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
    result = _results()["confirm"]
    receipt = OcrQueueReceiptSnapshot(
        operation=OcrQueueOperation.CONFIRMED,
        expected=EXPECTED,
        result=result,
        owner=owner,
        message_id=77,
    )

    assert receipt.result.draft is not None
    assert receipt.result.draft.ref == DraftRef(NEXT_DRAFT_ID, EXPECTED.revision + 1)
    assert receipt.result.transaction is not None
    assert receipt.owner.timezone == "Europe/Moscow"
    assert receipt.owner.base_currency == "RUB"
    rendered = repr(receipt)
    for secret in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        str(TRANSACTION_ID),
        "11100",
        "22200",
        "секрет",
        "RUB",
        "Europe/Moscow",
    ):
        assert secret not in rendered
    assert repr(TelegramOcrQueueContext(_request(), 77)) == "TelegramOcrQueueContext()"


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramOcrQueueContext(_request(), message_id)


def test_receipt_rejects_operation_status_mismatch() -> None:
    owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
    with pytest.raises(ValueError, match="cancelled queue"):
        OcrQueueReceiptSnapshot(
            OcrQueueOperation.CANCELLED,
            EXPECTED,
            _results()["confirm"],
            owner,
        )
    with pytest.raises(ValueError, match="cancelled result"):
        OcrQueueReceiptSnapshot(
            OcrQueueOperation.CONFIRMED,
            EXPECTED,
            _results()["cancel"],
            owner,
        )


def test_ocr_queue_controller_has_no_framework_or_database_dependency() -> None:
    path = Path(__file__).parents[2] / "src/finbot/adapters/telegram/controllers/ocr_queue.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("aiogram", "sqlalchemy", "finbot.adapters.database")
    assert not any(module.startswith(forbidden) for module in imports)
