from dataclasses import dataclass
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
    PendingQuickIntent,
    PendingWizardIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    BeginQuickDraftCommand,
    DraftIngressOperation,
    DraftIngressResult,
    DraftIngressStatus,
    QuickDraftIngressNotApplicableError,
)
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
PRIVATE_TEXT = "1450 закрытое описание"


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


def _quick_payload() -> dict[str, object]:
    return {
        "flow": "quick",
        "type": "expense",
        "amount_minor": 145_000,
        "occurred_at": "2026-08-13T12:30:00+03:00",
        "description": "закрытое описание",
        "category_explicit": False,
        "needs_confirmation": False,
    }


def _started_result() -> DraftIngressResult:
    return DraftIngressResult(
        DraftIngressOperation.QUICK,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "review", _quick_payload(), revision=4),
    )


def _conflict_result() -> DraftIngressResult:
    return DraftIngressResult(
        DraftIngressOperation.QUICK,
        DraftIngressStatus.CONFLICT,
        _owner_snapshot(),
        DraftSnapshot(
            DRAFT_ID,
            "edit_menu",
            {
                "flow": "edit",
                "pending_intent": encode_pending_draft_intent(PendingQuickIntent(PRIVATE_TEXT)),
            },
            revision=8,
        ),
    )


class _Ingress:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands: list[BeginQuickDraftCommand] = []
        self.result = _started_result()
        self.error: Exception | None = None

    async def begin_quick(self, command: BeginQuickDraftCommand) -> DraftIngressResult:
        self.events.append("ingress.begin_quick")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result


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
@pytest.mark.parametrize("result", [_started_result(), _conflict_result()])
async def test_quick_controller_claims_mutates_enqueues_and_commits_one_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
    result: DraftIngressResult,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    ingress.result = result
    controller = _controller(monkeypatch, events, ingress, receipts)

    receipt = await controller.begin_quick(
        TelegramDraftIngressContext(_request(), 94_000_004),
        PRIVATE_TEXT,
    )

    assert ingress.commands == [BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT)]
    assert receipt is receipts[0]
    assert receipt.result is result
    assert receipt.draft_ref == result.draft.ref
    assert receipt.message_id == 94_000_004
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "ingress.begin_quick",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_quick_update_returns_without_use_case_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    controller = _controller(monkeypatch, events, ingress, receipts, claimed=False)

    receipt = await controller.begin_quick(
        TelegramDraftIngressContext(_request(), 94_000_004),
        PRIVATE_TEXT,
    )

    assert receipt is None
    assert ingress.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_not_applicable_rolls_back_for_the_next_bounded_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    ingress.error = QuickDraftIngressNotApplicableError("bounded fallback")
    controller = _controller(monkeypatch, events, ingress, receipts)

    with pytest.raises(QuickDraftIngressNotApplicableError):
        await controller.begin_quick(
            TelegramDraftIngressContext(_request(), 94_000_004),
            PRIVATE_TEXT,
        )

    assert receipts == []
    assert "outbox" not in events
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_quick_outbox_failure_rolls_back_the_business_mutation(
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
        await controller.begin_quick(
            TelegramDraftIngressContext(_request(), 94_000_004),
            PRIVATE_TEXT,
        )

    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_quick_update_commits_without_outbox_and_returns_exact_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    ingress = _Ingress(events)
    ingress.result = _conflict_result()
    controller = _controller(monkeypatch, events, ingress, receipts)

    receipt = await controller.begin_quick(
        TelegramDraftIngressContext(_request(update_id=None)),
        PRIVATE_TEXT,
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
        "ingress.begin_quick",
        "commit",
        "session.exit",
    ]


def test_quick_receipt_rejects_noncanonical_started_and_cross_intent_conflicts() -> None:
    wrong_state = DraftIngressResult(
        DraftIngressOperation.QUICK,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "wizard_type", _quick_payload()),
    )
    wrong_flow = DraftIngressResult(
        DraftIngressOperation.QUICK,
        DraftIngressStatus.STARTED,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "review", {**_quick_payload(), "flow": "wizard"}),
    )
    wrong_conflict = DraftIngressResult(
        DraftIngressOperation.QUICK,
        DraftIngressStatus.CONFLICT,
        _owner_snapshot(),
        DraftSnapshot(
            DRAFT_ID,
            "wizard_type",
            {
                "flow": "wizard",
                "pending_intent": encode_pending_draft_intent(PendingWizardIntent()),
            },
        ),
    )

    for result in (wrong_state, wrong_flow):
        with pytest.raises(ValueError, match="quick draft"):
            DraftIngressReceiptSnapshot(result, 94_000_004)
    with pytest.raises(ValueError, match="conflict intent"):
        DraftIngressReceiptSnapshot(wrong_conflict, 94_000_004)


@pytest.mark.asyncio
async def test_quick_text_validation_happens_before_opening_the_executor_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftIngressReceiptSnapshot] = []
    controller = _controller(monkeypatch, events, _Ingress(events), receipts)
    context = TelegramDraftIngressContext(_request(), 94_000_004)

    with pytest.raises(TypeError):
        await controller.begin_quick(context, 1450)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await controller.begin_quick(context, "   ")
    with pytest.raises(ValueError):
        await controller.begin_quick(context, "x" * 4097)

    assert events == []
    assert receipts == []


def test_quick_controller_contracts_are_repr_safe() -> None:
    request = _request()
    values = (
        TelegramDraftIngressContext(request, 94_000_004),
        DraftIngressReceiptSnapshot(_started_result(), 94_000_004),
        BeginQuickDraftCommand(OWNER_ID, PRIVATE_TEXT),
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(ACCOUNT_ID),
        PRIVATE_TEXT,
        "145000",
        "RUB",
        "закрытое",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered
