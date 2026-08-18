import ast
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.drafts import (
    DraftController,
    DraftSessionUseCases,
    DraftSettingsReceiptSnapshot,
    TelegramDraftContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
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


class _RecordingDraftRepository:
    """Strict one-draft fake; only cancel is expected through this controller."""

    def __init__(self, events: list[str], active: DraftSnapshot | None) -> None:
        self.events = events
        self.active = active
        self.cancel_calls: list[tuple[UUID, DraftRef]] = []

    def _require(self, expected: DraftRef) -> DraftSnapshot:
        if self.active is None or self.active.ref != expected:
            raise DraftRevisionConflictError(
                current_revision=self.active.revision if self.active is not None else None
            )
        return self.active

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        return self.active

    async def create_if_absent(
        self,
        owner_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        del owner_id, state, payload
        raise AssertionError("discard controller created a draft")

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        del owner_id, expected, state, payload
        raise AssertionError("discard controller updated a draft")

    async def replace(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        del owner_id, expected, state, payload
        raise AssertionError("discard controller replaced a draft")

    async def set_suspended(
        self,
        owner_id: UUID,
        expected: DraftRef,
        suspended: bool,
    ) -> DraftSnapshot:
        del owner_id, expected, suspended
        raise AssertionError("discard controller changed draft suspension")

    async def delete(self, owner_id: UUID, expected: DraftRef) -> None:
        self.events.append("command.cancel")
        self.cancel_calls.append((owner_id, expected))
        self._require(expected)
        self.active = None


class _RecordingSettingsReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner_ids: list[UUID] = []
        self.owner = OwnerSnapshot(
            OWNER_ID,
            "ru",
            "Europe/Moscow",
            "RUB",
            ACCOUNT_ID,
            fast_mode=True,
        )
        self.active_accounts = (
            AccountSnapshot(
                ACCOUNT_ID,
                "Тайный основной счёт",
                "card",
                "RUB",
                None,
                8,
            ),
        )

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.events.append("query.owner")
        self.owner_ids.append(owner_id)
        return self.owner if owner_id == OWNER_ID else None

    async def list_accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        self.events.append(f"query.accounts:{archived}")
        self.owner_ids.append(owner_id)
        assert owner_id == OWNER_ID
        return () if archived else self.active_accounts

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        del owner_id, kind, archived
        raise AssertionError("discard controller queried categories")


def _active_draft() -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state="wizard_amount",
        payload={"description": "секретное описание", "amount_minor": 12_345},
        revision=7,
        updated_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
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
    repository: _RecordingDraftRepository,
    reader: _RecordingSettingsReader,
    receipts: list[DraftSettingsReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_current: bool = True,
) -> DraftController:
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

    def use_case_factory(actual_session: AsyncSession) -> DraftSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftSessionUseCases(
            drafts=DraftUseCases(repository),
            get_owner_settings=GetOwnerSettings(reader),
            list_accounts=ListAccounts(reader),
        )

    async def presentation_guard(
        actual_session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        assert actual_session is cast(Any, session)
        assert owner_id == OWNER_ID
        assert expected.draft_id == DRAFT_ID
        assert chat_id == 93_000_003
        assert message_id == 94_000_004
        events.append("presentation.guard")
        return presentation_current

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftSettingsReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return DraftController(
        executor,
        use_case_factory,
        presentation_guard,
        enqueue_receipt,
    )


@pytest.mark.asyncio
async def test_discard_cancels_exact_ref_queries_settings_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    active = _active_draft()
    expected = active.ref
    repository = _RecordingDraftRepository(events, active)
    reader = _RecordingSettingsReader(events)
    controller = _controller(monkeypatch, events, repository, reader, receipts)

    result = await controller.discard(
        TelegramDraftContext(_request(), message_id=94_000_004),
        expected,
    )

    assert result is receipts[0]
    assert result.owner is reader.owner
    assert result.owner.fast_mode is True
    assert result.default_account == reader.active_accounts[0]
    assert result.message_id == 94_000_004
    assert repository.active is None
    assert repository.cancel_calls == [(OWNER_ID, expected)]
    assert repository.cancel_calls[0][1] is expected
    assert reader.owner_ids == [OWNER_ID, OWNER_ID]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "command.cancel",
        "query.owner",
        "query.accounts:False",
        "outbox",
        "commit",
        "session.exit",
    ]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_stale_ref_rolls_back_without_settings_query_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    active = _active_draft()
    stale = DraftRef(active.draft_id, active.revision - 1)
    repository = _RecordingDraftRepository(events, active)
    reader = _RecordingSettingsReader(events)
    controller = _controller(monkeypatch, events, repository, reader, receipts)

    with pytest.raises(DraftRevisionConflictError, match="Черновик был изменён"):
        await controller.discard(
            TelegramDraftContext(_request(), message_id=94_000_004),
            stale,
        )

    assert repository.active is active
    assert repository.cancel_calls == [(OWNER_ID, stale)]
    assert reader.owner_ids == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "command.cancel",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_wrong_presentation_message_rolls_back_before_cancel_or_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    active = _active_draft()
    repository = _RecordingDraftRepository(events, active)
    reader = _RecordingSettingsReader(events)
    controller = _controller(
        monkeypatch,
        events,
        repository,
        reader,
        receipts,
        presentation_current=False,
    )

    with pytest.raises(DraftRevisionConflictError, match="Черновик был изменён"):
        await controller.discard(
            TelegramDraftContext(_request(), message_id=94_000_004),
            active.ref,
        )

    assert repository.active is active
    assert repository.cancel_calls == []
    assert reader.owner_ids == []
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
async def test_duplicate_returns_none_without_constructing_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    repository = _RecordingDraftRepository(events, _active_draft())
    reader = _RecordingSettingsReader(events)
    controller = _controller(
        monkeypatch,
        events,
        repository,
        reader,
        receipts,
        claimed=False,
    )

    result = await controller.discard(
        TelegramDraftContext(_request(), message_id=94_000_004),
        cast(DraftSnapshot, repository.active).ref,
    )

    assert result is None
    assert repository.cancel_calls == []
    assert reader.owner_ids == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_none_update_returns_post_commit_settings_snapshot_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    active = _active_draft()
    repository = _RecordingDraftRepository(events, active)
    reader = _RecordingSettingsReader(events)
    controller = _controller(monkeypatch, events, repository, reader, receipts)

    result = await controller.discard(
        TelegramDraftContext(_request(update_id=None), message_id=94_000_004),
        active.ref,
    )

    assert isinstance(result, DraftSettingsReceiptSnapshot)
    assert result.message_id == 94_000_004
    assert repository.active is None
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "command.cancel",
        "query.owner",
        "query.accounts:False",
        "commit",
        "session.exit",
    ]


def test_draft_controller_contracts_hide_all_private_values_from_repr() -> None:
    events: list[str] = []
    reader = _RecordingSettingsReader(events)
    use_cases = DraftSessionUseCases(
        drafts=DraftUseCases(_RecordingDraftRepository(events, _active_draft())),
        get_owner_settings=GetOwnerSettings(reader),
        list_accounts=ListAccounts(reader),
    )
    values = (
        TelegramDraftContext(_request(), message_id=94_000_004),
        DraftSettingsReceiptSnapshot(
            owner=reader.owner,
            default_account=reader.active_accounts[0],
            message_id=94_000_004,
        ),
        use_cases,
        _active_draft().ref,
    )

    for value in values:
        rendered = repr(value)
        assert str(OWNER_ID) not in rendered
        assert str(ACCOUNT_ID) not in rendered
        assert str(DRAFT_ID) not in rendered
        assert "Тайн" not in rendered
        assert "RUB" not in rendered
        assert "91000001" not in rendered
        assert "92000002" not in rendered
        assert "93000003" not in rendered
        assert "94000004" not in rendered


@pytest.mark.parametrize("message_id", [None, True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: object) -> None:
    expected_error = (
        TypeError if isinstance(message_id, bool) or not isinstance(message_id, int) else ValueError
    )
    with pytest.raises(expected_error):
        TelegramDraftContext(_request(), cast(Any, message_id))


@pytest.mark.asyncio
async def test_discard_rejects_boolean_revision_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftSettingsReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _RecordingDraftRepository(events, _active_draft()),
        _RecordingSettingsReader(events),
        receipts,
    )

    with pytest.raises(TypeError, match="revision"):
        await controller.discard(
            TelegramDraftContext(_request(), message_id=94_000_004),
            DraftRef(DRAFT_ID, True),
        )

    assert events == []
    assert receipts == []


def test_draft_controller_has_no_framework_database_or_bootstrap_dependency() -> None:
    path = Path(__file__).parents[2] / "src/finbot/adapters/telegram/controllers/drafts.py"
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
    forbidden = ("aiogram", "sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imports
        for prefix in forbidden
    )
