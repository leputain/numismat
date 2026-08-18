from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.finance_draft_text_input import (
    FinanceDraftTextInputController,
    FinanceDraftTextInputReceiptSnapshot,
    FinanceDraftTextInputSessionUseCases,
    TelegramFinanceDraftTextInputContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputCommand,
    FinanceDraftTextInputResult,
    FinanceDraftTextInputStatus,
)
from finbot.application.use_cases.finance_draft_text_input import (
    FinanceDraftTextInputUseCase,
)

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")


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


class _TargetRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = DraftSnapshot(
            DRAFT_ID,
            "wizard_amount",
            {"flow": "wizard", "type": "expense"},
            revision=7,
        )
        self.error: Exception | None = None

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("target.lock")
        if self.error is not None:
            raise self.error
        return self.active

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        assert (owner_id, expected, chat_id) == (
            OWNER_ID,
            self.active.ref,
            93_000_003,
        )
        self.events.append("target.presentation")
        return 94_000_004


class _TextInput:
    def __init__(self, events: list[str], result: FinanceDraftTextInputResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[FinanceDraftTextInputCommand] = []

    async def execute(
        self,
        command: FinanceDraftTextInputCommand,
    ) -> FinanceDraftTextInputResult:
        self.events.append("text_input.execute")
        self.commands.append(command)
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


def _result() -> FinanceDraftTextInputResult:
    return FinanceDraftTextInputResult(
        FinanceDraftTextInputStatus.UPDATED,
        OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID),
        DraftSnapshot(
            DRAFT_ID,
            "wizard_category",
            {"flow": "wizard", "type": "expense", "amount_minor": 145_000},
            revision=8,
        ),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    text_input: _TextInput,
    targets: _TargetRepository,
    receipts: list[FinanceDraftTextInputReceiptSnapshot],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> FinanceDraftTextInputController:
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

    def target_factory(actual_session: AsyncSession) -> _TargetRepository:
        assert actual_session is cast(Any, session)
        events.append("target.factory")
        return targets

    def use_case_factory(actual_session: AsyncSession) -> FinanceDraftTextInputSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case.factory")
        return FinanceDraftTextInputSessionUseCases(cast(FinanceDraftTextInputUseCase, text_input))

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: FinanceDraftTextInputReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return FinanceDraftTextInputController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        target_factory,
        enqueue,
    )


@pytest.mark.asyncio
async def test_controller_locks_active_target_mutates_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[FinanceDraftTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result())
    targets = _TargetRepository(events)
    controller = _controller(monkeypatch, events, text_input, targets, receipts)

    receipt = await controller.submit(
        TelegramFinanceDraftTextInputContext(_request(), 95_000_005),
        "1450",
    )

    assert receipt is receipts[0]
    assert receipt.result is text_input.result
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert receipt.input_message_id == 95_000_005
    assert receipt.target_message_id == 94_000_004
    assert text_input.commands == [
        FinanceDraftTextInputCommand(OWNER_ID, DraftRef(DRAFT_ID, 7), "1450")
    ]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "target.factory",
        "target.lock",
        "target.presentation",
        "use_case.factory",
        "text_input.execute",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_target_use_case_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[FinanceDraftTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result())
    targets = _TargetRepository(events)
    controller = _controller(
        monkeypatch,
        events,
        text_input,
        targets,
        receipts,
        claimed=False,
    )

    receipt = await controller.submit(
        TelegramFinanceDraftTextInputContext(_request(), 95_000_005),
        "1450",
    )

    assert receipt is None
    assert text_input.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_target_or_outbox_failure_rolls_back_without_partial_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[FinanceDraftTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result())
    targets = _TargetRepository(events)
    targets.error = DraftRevisionConflictError(current_revision=8)
    controller = _controller(monkeypatch, events, text_input, targets, receipts)

    with pytest.raises(DraftRevisionConflictError):
        await controller.submit(
            TelegramFinanceDraftTextInputContext(_request(), 95_000_005),
            "1450",
        )

    assert text_input.commands == []
    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]

    events.clear()
    targets.error = None
    controller = _controller(
        monkeypatch,
        events,
        text_input,
        targets,
        receipts,
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.submit(
            TelegramFinanceDraftTextInputContext(_request(), 95_000_005),
            "1450",
        )
    assert receipts == []
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_request_returns_receipt_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[FinanceDraftTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result())
    targets = _TargetRepository(events)
    controller = _controller(monkeypatch, events, text_input, targets, receipts)

    receipt = await controller.submit(
        TelegramFinanceDraftTextInputContext(_request(update_id=None), 95_000_005),
        "1450",
    )

    assert receipt is not None
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "target.factory",
        "target.lock",
        "target.presentation",
        "use_case.factory",
        "text_input.execute",
        "commit",
        "session.exit",
    ]


def test_controller_contracts_hide_text_and_all_telegram_finance_identifiers() -> None:
    result = _result()
    context = TelegramFinanceDraftTextInputContext(_request(), 95_000_005)
    receipt = FinanceDraftTextInputReceiptSnapshot(result, 95_000_005, 94_000_004)
    values = (
        context,
        receipt,
        FinanceDraftTextInputSessionUseCases(cast(FinanceDraftTextInputUseCase, object())),
    )

    with pytest.raises(TypeError):
        TelegramFinanceDraftTextInputContext(_request(), True)
    with pytest.raises(ValueError):
        TelegramFinanceDraftTextInputContext(_request(), 0)
    with pytest.raises(ValueError):
        FinanceDraftTextInputReceiptSnapshot(result, 95_000_005, -1)

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(ACCOUNT_ID),
        "145000",
        "RUB",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
        "95000005",
    ):
        assert private not in rendered
