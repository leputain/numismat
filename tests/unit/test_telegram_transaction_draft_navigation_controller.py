from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.transaction_draft_navigation import (
    TelegramTransactionDraftNavigationContext,
    TransactionDraftNavigationController,
    TransactionDraftNavigationReceiptSnapshot,
    TransactionDraftNavigationSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot, TransactionSnapshot
from finbot.application.errors import DraftRevisionConflictError, ObjectVersionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
    TransactionDraftNavigationCommand,
    TransactionDraftNavigationResult,
)
from finbot.application.use_cases.transaction_draft_navigation import (
    TransactionDraftNavigationUseCases,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


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
    def __init__(
        self,
        events: list[str],
        result: TransactionDraftNavigationResult,
    ) -> None:
        self.events = events
        self.result = result
        self.commands: list[TransactionDraftNavigationCommand] = []
        self.error: Exception | None = None

    async def execute(
        self,
        command: TransactionDraftNavigationCommand,
    ) -> TransactionDraftNavigationResult:
        self.events.append("navigation.execute")
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


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Закрытый счёт",
        category_id=CATEGORY_ID,
        category_name="Закрытая категория",
        category_emoji="▫️",
        occurred_at=NOW,
        description="закрытое описание",
        version=4,
    )


def _result() -> TransactionDraftNavigationResult:
    return TransactionDraftNavigationResult(
        action=TransactionDraftNavigationAction.EDIT_AMOUNT,
        owner=OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID),
        draft=DraftSnapshot(
            DRAFT_ID,
            "edit_amount",
            {
                "transaction_id": str(TRANSACTION_ID),
                "version": 4,
            },
            revision=8,
        ),
        transaction=_transaction(),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    navigation: _Navigation,
    receipts: list[TransactionDraftNavigationReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_is_current: bool = True,
) -> TransactionDraftNavigationController:
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

    def use_case_factory(
        actual_session: AsyncSession,
    ) -> TransactionDraftNavigationSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return TransactionDraftNavigationSessionUseCases(
            cast(TransactionDraftNavigationUseCases, navigation)
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TransactionDraftNavigationReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return TransactionDraftNavigationController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        guard,
        enqueue,
    )


@pytest.mark.asyncio
async def test_controller_guards_mutates_and_enqueues_revision_bound_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)
    expected = DraftRef(DRAFT_ID, 7)

    receipt = await controller.execute(
        TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
        expected,
        TransactionDraftNavigationAction.EDIT_AMOUNT,
        history_page=3,
    )

    assert receipt is receipts[0]
    assert receipt.expected is expected
    assert receipt.result is navigation.result
    assert receipt.result.draft.ref == DraftRef(DRAFT_ID, 8)
    assert receipt.message_id == 94_000_004
    assert receipt.history_page == 3
    assert navigation.commands == [
        TransactionDraftNavigationCommand(
            OWNER_ID,
            expected,
            TransactionDraftNavigationAction.EDIT_AMOUNT,
        )
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
async def test_stale_presentation_rolls_back_before_use_case_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
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
            TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftNavigationAction.EDIT_AMOUNT,
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
async def test_use_case_failure_rolls_back_without_outbox_or_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    navigation.error = ObjectVersionConflictError(current_version=9)
    controller = _controller(monkeypatch, events, navigation, receipts)

    with pytest.raises(ObjectVersionConflictError):
        await controller.execute(
            TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftNavigationAction.EDIT_AMOUNT,
        )

    assert receipts == []
    assert "outbox" not in events
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_guard_use_case_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts, claimed=False)

    receipt = await controller.execute(
        TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        TransactionDraftNavigationAction.EDIT_AMOUNT,
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
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)

    receipt = await controller.execute(
        TelegramTransactionDraftNavigationContext(_request(update_id=None), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        TransactionDraftNavigationAction.EDIT_AMOUNT,
    )

    assert isinstance(receipt, TransactionDraftNavigationReceiptSnapshot)
    assert receipt.result.draft.revision == 8
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
        TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
        TransactionDraftNavigationReceiptSnapshot(DraftRef(DRAFT_ID, 7), result, 94_000_004),
        TransactionDraftNavigationSessionUseCases(
            cast(TransactionDraftNavigationUseCases, object())
        ),
        result,
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        "RUB",
        "12345",
        "Закрытый",
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
        TelegramTransactionDraftNavigationContext(_request(), message_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("history_page", [True, -1, MAX_PAGE + 1, "broken"])
async def test_controller_rejects_invalid_history_page_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
    history_page: object,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftNavigationReceiptSnapshot] = []
    navigation = _Navigation(events, _result())
    controller = _controller(monkeypatch, events, navigation, receipts)

    with pytest.raises((TypeError, ValueError)):
        await controller.execute(
            TelegramTransactionDraftNavigationContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftNavigationAction.EDIT_AMOUNT,
            history_page=history_page,  # type: ignore[arg-type]
        )

    assert events == []
    assert navigation.commands == []
    assert receipts == []
