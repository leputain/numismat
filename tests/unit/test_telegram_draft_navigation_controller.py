from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.draft_navigation import (
    DraftNavigationController,
    DraftNavigationReceiptSnapshot,
    DraftNavigationSessionUseCases,
    TelegramDraftNavigationContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.draft_navigation import (
    DraftNavigationAction,
    DraftNavigationCommand,
    DraftNavigationResult,
    DraftNavigationStatus,
)
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")


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


class _Navigation:
    def __init__(self, events: list[str], result: DraftNavigationResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[DraftNavigationCommand] = []

    async def execute(self, command: DraftNavigationCommand) -> DraftNavigationResult:
        self.events.append("navigation.execute")
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


def _result() -> DraftNavigationResult:
    return DraftNavigationResult(
        action=DraftNavigationAction.EDIT_AMOUNT,
        status=DraftNavigationStatus.UPDATED,
        owner=OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", None),
        draft=DraftSnapshot(
            DRAFT_ID,
            "review_amount",
            {"amount_minor": 12_345, "description": "закрытое описание"},
            revision=8,
        ),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    navigation: _Navigation,
    receipts: list[DraftNavigationReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_is_current: bool = True,
) -> DraftNavigationController:
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

    async def guard(
        actual_session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        assert actual_session is cast(Any, session)
        assert owner_id == OWNER_ID
        assert expected == DraftRef(DRAFT_ID, 7)
        assert (chat_id, message_id) == (93_000_003, 94_000_004)
        events.append("presentation.guard")
        return presentation_is_current

    def use_case_factory(actual_session: AsyncSession) -> DraftNavigationSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftNavigationSessionUseCases(cast(DraftNavigationUseCases, navigation))

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftNavigationReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return DraftNavigationController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        guard,
        enqueue,
    )


@pytest.mark.asyncio
async def test_controller_guards_message_mutates_builds_receipt_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)
    expected = DraftRef(DRAFT_ID, 7)

    receipt = await controller.execute(
        TelegramDraftNavigationContext(_request(), 94_000_004),
        expected,
        DraftNavigationAction.EDIT_AMOUNT,
    )

    assert receipt is receipts[0]
    assert receipt.expected is expected
    assert receipt.result is navigation.result
    assert receipt.message_id == 94_000_004
    assert navigation.commands == [
        DraftNavigationCommand(OWNER_ID, expected, DraftNavigationAction.EDIT_AMOUNT)
    ]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "navigation.execute",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_controller_passes_a_typed_selection_choice_inside_the_same_atomic_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)
    expected = DraftRef(DRAFT_ID, 7)

    await controller.execute(
        TelegramDraftNavigationContext(_request(), 94_000_004),
        expected,
        DraftNavigationAction.SELECT_TYPE,
        TransactionType.INCOME,
    )

    assert navigation.commands == [
        DraftNavigationCommand(
            OWNER_ID,
            expected,
            DraftNavigationAction.SELECT_TYPE,
            TransactionType.INCOME,
        )
    ]
    assert events.index("navigation.execute") < events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_stale_message_binding_rolls_back_before_constructing_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(
        monkeypatch,
        events,
        navigation,
        receipts,
        presentation_is_current=False,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.execute(
            TelegramDraftNavigationContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            DraftNavigationAction.EDIT_AMOUNT,
        )

    assert navigation.commands == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_guard_or_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts, claimed=False)

    receipt = await controller.execute(
        TelegramDraftNavigationContext(_request(), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        DraftNavigationAction.EDIT_AMOUNT,
    )

    assert receipt is None
    assert navigation.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_update_returns_receipt_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)

    receipt = await controller.execute(
        TelegramDraftNavigationContext(_request(update_id=None), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        DraftNavigationAction.EDIT_AMOUNT,
    )

    assert isinstance(receipt, DraftNavigationReceiptSnapshot)
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "navigation.execute",
        "commit",
        "session.exit",
    ]


def test_controller_contracts_hide_all_private_values_from_repr() -> None:
    result = _result()
    values = (
        TelegramDraftNavigationContext(_request(), 94_000_004),
        DraftNavigationReceiptSnapshot(DraftRef(DRAFT_ID, 7), result, 94_000_004),
        DraftNavigationSessionUseCases(cast(DraftNavigationUseCases, object())),
        result,
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        "RUB",
        "12345",
        "закрытое",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramDraftNavigationContext(_request(), message_id)
