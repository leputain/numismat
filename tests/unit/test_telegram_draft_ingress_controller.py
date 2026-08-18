from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    DraftIngressSessionUseCases,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.draft_conflicts import (
    PendingRepeatIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    BeginRepeatDraftCommand,
    BeginWizardDraftCommand,
    DraftIngressOperation,
    DraftIngressResult,
    DraftIngressStatus,
)
from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    PreparedTransactionDraft,
    ReviewedTransactionInput,
)
from finbot.application.errors import InvalidStateError
from finbot.application.interactions import MAX_PAGE
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)


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


def _owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _prepared() -> PreparedTransactionDraft:
    return PreparedTransactionDraft(
        source_transaction_id=TRANSACTION_ID,
        source_version=7,
        transaction=ReviewedTransactionInput(
            kind=TransactionType.EXPENSE,
            amount_minor=12_345,
            account_id=ACCOUNT_ID,
            category_id=CATEGORY_ID,
            occurred_at=NOW,
            description="закрытое описание",
        ),
        currency="RUB",
        account_name="Закрытый счёт",
        category_name="Закрытая категория",
        category_emoji="▫️",
    )


def _wizard_result() -> DraftIngressResult:
    return DraftIngressResult(
        DraftIngressOperation.WIZARD,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "wizard_type", {"flow": "wizard"}, revision=1),
    )


def _repeat_conflict_result() -> DraftIngressResult:
    return DraftIngressResult(
        DraftIngressOperation.REPEAT,
        DraftIngressStatus.CONFLICT,
        _owner_snapshot(),
        DraftSnapshot(
            DRAFT_ID,
            "wizard_amount",
            {
                "flow": "wizard",
                "pending_intent": encode_pending_draft_intent(
                    PendingRepeatIntent.from_prepared(_prepared())
                ),
            },
            revision=8,
            suspended=True,
        ),
    )


def _repeat_started_result() -> DraftIngressResult:
    return DraftIngressResult(
        DraftIngressOperation.REPEAT,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "review", _prepared().to_payload()),
    )


class _Ingress:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.wizard_commands: list[BeginWizardDraftCommand] = []
        self.repeat_commands: list[BeginRepeatDraftCommand] = []
        self.wizard_result = _wizard_result()
        self.repeat_result = _repeat_conflict_result()
        self.error: Exception | None = None

    async def begin_wizard(self, command: BeginWizardDraftCommand) -> DraftIngressResult:
        self.events.append("ingress.begin_wizard")
        self.wizard_commands.append(command)
        if self.error is not None:
            raise self.error
        return self.wizard_result

    async def begin_repeat(self, command: BeginRepeatDraftCommand) -> DraftIngressResult:
        self.events.append("ingress.begin_repeat")
        self.repeat_commands.append(command)
        if self.error is not None:
            raise self.error
        return self.repeat_result


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
    ingress: _Ingress,
    receipts: list[DraftIngressReceiptSnapshot],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> DraftIngressController:
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

    def use_case_factory(actual_session: AsyncSession) -> DraftIngressSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftIngressSessionUseCases(cast(DraftIngressUseCases, ingress))

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftIngressReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return DraftIngressController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        enqueue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["wizard", "repeat"])
async def test_controller_claims_mutates_and_enqueues_revision_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    controller = _controller(monkeypatch, events, ingress, receipts)
    context = TelegramDraftIngressContext(_request(), 94_000_004, 3)

    if action == "wizard":
        receipt = await controller.begin_wizard(context)
        assert ingress.wizard_commands == [BeginWizardDraftCommand(OWNER_ID)]
        expected_event = "ingress.begin_wizard"
        expected_result = ingress.wizard_result
    else:
        receipt = await controller.begin_repeat(context, TRANSACTION_ID, 7)
        assert ingress.repeat_commands == [BeginRepeatDraftCommand(OWNER_ID, TRANSACTION_ID, 7)]
        expected_event = "ingress.begin_repeat"
        expected_result = ingress.repeat_result

    assert receipt is receipts[0]
    assert receipt.result is expected_result
    assert receipt.draft_ref == expected_result.draft.ref
    assert receipt.message_id == 94_000_004
    assert receipt.history_page == 3
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        expected_event,
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_use_case_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    controller = _controller(monkeypatch, events, ingress, receipts, claimed=False)

    receipt = await controller.begin_wizard(TelegramDraftIngressContext(_request(), 94_000_004))

    assert receipt is None
    assert ingress.wizard_commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_ingress_error_rolls_back_without_receipt_or_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    ingress.error = InvalidStateError("bounded conflict")
    controller = _controller(monkeypatch, events, ingress, receipts)

    with pytest.raises(InvalidStateError):
        await controller.begin_repeat(
            TelegramDraftIngressContext(_request(), 94_000_004),
            TRANSACTION_ID,
            7,
        )

    assert receipts == []
    assert "outbox" not in events
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_business_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    controller = _controller(
        monkeypatch,
        events,
        ingress,
        receipts,
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.begin_wizard(TelegramDraftIngressContext(_request(), 94_000_004))

    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_update_commits_and_returns_none_message_receipt_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    controller = _controller(monkeypatch, events, ingress, receipts)

    receipt = await controller.begin_repeat(
        TelegramDraftIngressContext(_request(update_id=None)),
        TRANSACTION_ID,
        7,
    )

    assert receipt is not None
    assert receipt.message_id is None
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "ingress.begin_repeat",
        "commit",
        "session.exit",
    ]


def test_receipt_rejects_noncanonical_or_cross_operation_drafts() -> None:
    wrong_wizard = DraftIngressResult(
        DraftIngressOperation.WIZARD,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "wizard_type", {"flow": "quick"}),
    )
    wrong_conflict = DraftIngressResult(
        DraftIngressOperation.WIZARD,
        DraftIngressStatus.CONFLICT,
        _owner_snapshot(),
        _repeat_conflict_result().draft,
    )

    with pytest.raises(ValueError, match="wizard draft"):
        DraftIngressReceiptSnapshot(wrong_wizard, 94_000_004)
    with pytest.raises(ValueError, match="conflict intent"):
        DraftIngressReceiptSnapshot(wrong_conflict, 94_000_004)


def test_receipt_accepts_canonical_started_repeat_and_exposes_exact_ref() -> None:
    receipt = DraftIngressReceiptSnapshot(_repeat_started_result(), 94_000_004)

    assert receipt.draft_ref == DraftRef(DRAFT_ID, 1)


def test_controller_contracts_reject_invalid_values_and_hide_private_repr() -> None:
    request = _request()
    context = TelegramDraftIngressContext(request, 94_000_004)
    receipt = DraftIngressReceiptSnapshot(_wizard_result(), 94_000_004)
    values = (
        context,
        receipt,
        DraftIngressSessionUseCases(cast(DraftIngressUseCases, object())),
    )

    with pytest.raises(TypeError):
        TelegramDraftIngressContext(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TelegramDraftIngressContext(request, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TelegramDraftIngressContext(request, 0)
    with pytest.raises(TypeError):
        DraftIngressReceiptSnapshot(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DraftIngressReceiptSnapshot(_wizard_result(), True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between"):
        TelegramDraftIngressContext(request, 94_000_004, MAX_PAGE + 1)
    with pytest.raises(TypeError, match="integer"):
        DraftIngressReceiptSnapshot(_wizard_result(), 94_000_004, True)  # type: ignore[arg-type]

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "RUB",
        "Закрытый",
        "закрытое",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered
