from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.transaction_draft_selection import (
    TelegramTransactionDraftSelectionContext,
    TransactionDraftSelectionController,
    TransactionDraftSelectionReceiptSnapshot,
    TransactionDraftSelectionSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.dto import DraftRef, OwnerSnapshot, TransactionSnapshot
from finbot.application.errors import DraftRevisionConflictError, ObjectVersionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
    TransactionDraftSelectionResult,
    TransactionDraftSelectionStatus,
)
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000301")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000401")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000501")
SELECTED_ID = UUID("00000000-0000-7000-8000-000000000601")


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


class _Selections:
    def __init__(
        self,
        events: list[str],
        result: TransactionDraftSelectionResult,
    ) -> None:
        self.events = events
        self.result = result
        self.error: Exception | None = None
        self.commands: list[TransactionDraftSelectionCommand] = []

    async def execute(
        self,
        command: TransactionDraftSelectionCommand,
    ) -> TransactionDraftSelectionResult:
        self.events.append("selections.execute")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.result


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru_RU",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _result() -> TransactionDraftSelectionResult:
    transaction = TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Закрытый счёт",
        category_id=CATEGORY_ID,
        category_name="Закрытая категория",
        category_emoji="▫️",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        description="закрытое описание",
        version=2,
    )
    return TransactionDraftSelectionResult(
        action=TransactionDraftSelectionAction.CATEGORY,
        status=TransactionDraftSelectionStatus.TRANSACTION_UPDATED,
        owner=OwnerSnapshot(OWNER_ID, "ru_RU", "Europe/Moscow", "RUB", ACCOUNT_ID),
        transaction=transaction,
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    selections: _Selections,
    receipts: list[TransactionDraftSelectionReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_is_current: bool = True,
) -> TransactionDraftSelectionController:
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
        assert (locale, timezone, currency) == ("ru_RU", "Europe/Moscow", "RUB")
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
    ) -> TransactionDraftSelectionSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return TransactionDraftSelectionSessionUseCases(
            cast(TransactionDraftSelectionUseCases, selections)
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TransactionDraftSelectionReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return TransactionDraftSelectionController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        guard,
        enqueue,
    )


@pytest.mark.asyncio
async def test_controller_guards_mutates_and_enqueues_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    controller = _controller(monkeypatch, events, selections, receipts)
    expected = DraftRef(DRAFT_ID, 7)
    reference = DraftCatalogRef(SELECTED_ID, 5)

    receipt = await controller.execute(
        TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
        expected,
        TransactionDraftSelectionAction.CATEGORY,
        reference,
        history_page=3,
    )

    assert receipt is receipts[0]
    assert receipt.expected is expected
    assert receipt.result is selections.result
    assert receipt.history_page == 3
    assert selections.commands == [
        TransactionDraftSelectionCommand(
            OWNER_ID,
            expected,
            TransactionDraftSelectionAction.CATEGORY,
            reference,
        )
    ]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "selections.execute",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_before_guard_and_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    controller = _controller(monkeypatch, events, selections, receipts, claimed=False)

    receipt = await controller.execute(
        TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        TransactionDraftSelectionAction.CATEGORY,
        DraftCatalogRef(SELECTED_ID, 5),
    )

    assert receipt is None
    assert selections.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_stale_presentation_rolls_back_without_constructing_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    controller = _controller(
        monkeypatch,
        events,
        selections,
        receipts,
        presentation_is_current=False,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.execute(
            TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftSelectionAction.CATEGORY,
            DraftCatalogRef(SELECTED_ID, 5),
        )

    assert selections.commands == []
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
async def test_mutation_failure_rolls_back_claim_and_never_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    selections.error = ObjectVersionConflictError(current_version=6)
    controller = _controller(monkeypatch, events, selections, receipts)

    with pytest.raises(ObjectVersionConflictError):
        await controller.execute(
            TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftSelectionAction.CATEGORY,
            DraftCatalogRef(SELECTED_ID, 5),
        )

    assert receipts == []
    assert events[-3:] == ["selections.execute", "rollback", "session.exit"]
    assert "outbox" not in events
    assert "commit" not in events


@pytest.mark.asyncio
async def test_untracked_update_returns_receipt_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    controller = _controller(monkeypatch, events, selections, receipts)

    receipt = await controller.execute(
        TelegramTransactionDraftSelectionContext(_request(update_id=None), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        TransactionDraftSelectionAction.CATEGORY,
        DraftCatalogRef(SELECTED_ID, 5),
    )

    assert isinstance(receipt, TransactionDraftSelectionReceiptSnapshot)
    assert receipts == []
    assert events[-2:] == ["commit", "session.exit"]
    assert "outbox" not in events


def test_controller_contracts_hide_private_values_from_repr() -> None:
    receipt = TransactionDraftSelectionReceiptSnapshot(
        DraftRef(DRAFT_ID, 7),
        _result(),
        94_000_004,
    )
    values = (
        TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
        TransactionDraftSelectionSessionUseCases(cast(TransactionDraftSelectionUseCases, object())),
        receipt,
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        "12345",
        "Закрытый",
        "закрытое",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("history_page", [True, -1, MAX_PAGE + 1, "broken"])
async def test_controller_rejects_invalid_history_page_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
    history_page: object,
) -> None:
    events: list[str] = []
    receipts: list[TransactionDraftSelectionReceiptSnapshot] = []
    selections = _Selections(events, _result())
    controller = _controller(monkeypatch, events, selections, receipts)

    with pytest.raises((TypeError, ValueError)):
        await controller.execute(
            TelegramTransactionDraftSelectionContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            TransactionDraftSelectionAction.CATEGORY,
            DraftCatalogRef(SELECTED_ID, 5),
            history_page=history_page,  # type: ignore[arg-type]
        )

    assert events == []
    assert selections.commands == []
    assert receipts == []
