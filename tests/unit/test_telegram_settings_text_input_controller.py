from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
)
from finbot.adapters.telegram.controllers.settings_text_input import (
    SettingsTextInputController,
    SettingsTextInputReceiptSnapshot,
    SettingsTextInputSessionUseCases,
    SettingsTextPresentationContext,
    SettingsTextPresentationContextReader,
    TelegramSettingsTextInputContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    AccountSnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.settings_text_input import (
    SettingsTextInputCommand,
    SettingsTextInputOperation,
    SettingsTextInputResult,
    SettingsTextInputStatus,
)

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
DEFAULT_PRESENTATION = TelegramDraftPresentationContext()


@dataclass(slots=True)
class _Owner:
    id: UUID


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Session:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _Sessions:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


class _Targets:
    def __init__(self, events: list[str], *, target_message_id: int | None = 77) -> None:
        self.events = events
        self.target_message_id = target_message_id
        self.active = DraftSnapshot(
            DRAFT_ID,
            "settings_account_create",
            {},
            revision=3,
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("target.lock")
        return self.active

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        assert (owner_id, expected, chat_id) == (OWNER_ID, self.active.ref, 93_000_003)
        self.events.append("target.message")
        return self.target_message_id


class _UseCase:
    def __init__(self, events: list[str], active: DraftSnapshot) -> None:
        self.events = events
        self.active = active
        self.commands: list[SettingsTextInputCommand] = []

    async def execute(self, command: SettingsTextInputCommand) -> SettingsTextInputResult:
        self.events.append("use_case")
        self.commands.append(command)
        return SettingsTextInputResult(
            operation=SettingsTextInputOperation.ACCOUNT_CREATE,
            status=SettingsTextInputStatus.UPDATED,
            owner=OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID),
            account=AccountSnapshot(
                ACCOUNT_ID,
                "Скрытый счёт",
                "other",
                "RUB",
                None,
                1,
            ),
        )


def _request(update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id,
        92_000_002,
        93_000_003,
        "ru",
        "Europe/Moscow",
        "RUB",
    )


def _subject(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claimed: bool = True,
    projection: TelegramDraftPresentationContext | None = DEFAULT_PRESENTATION,
    target_message_id: int | None = 77,
) -> tuple[SettingsTextInputController, list[str], _UseCase, list[object]]:
    events: list[str] = []
    session = _Session(events)
    targets = _Targets(events, target_message_id=target_message_id)
    use_case = _UseCase(events, targets.active)
    receipts: list[object] = []

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        events.append("claim")
        return claimed

    async def ensure_owner(actual_session: AsyncSession, **kwargs: object) -> _Owner:
        assert actual_session is cast(Any, session)
        assert kwargs["telegram_user_id"] == 92_000_002
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    async def read_projection(
        actual_session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> SettingsTextPresentationContext | None:
        assert actual_session is cast(Any, session)
        assert (owner_id, expected, chat_id, message_id, allow_suspended) == (
            OWNER_ID,
            targets.active.ref,
            93_000_003,
            77,
            False,
        )
        events.append("projection")
        return projection

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: SettingsTextInputReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    executor = TelegramMutationExecutor(cast(async_sessionmaker[AsyncSession], _Sessions(session)))

    def use_cases(actual_session: AsyncSession) -> SettingsTextInputSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return SettingsTextInputSessionUseCases(cast(Any, use_case))

    def target_factory(actual_session: AsyncSession) -> _Targets:
        assert actual_session is cast(Any, session)
        events.append("target_factory")
        return targets

    return (
        SettingsTextInputController(
            executor,
            use_cases,
            target_factory,
            cast(SettingsTextPresentationContextReader, read_projection),
            enqueue,
        ),
        events,
        use_case,
        receipts,
    )


@pytest.mark.asyncio
async def test_controller_locks_projection_enqueues_receipt_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, events, use_case, receipts = _subject(
        monkeypatch,
        projection=TelegramDraftPresentationContext(history_page=4),
    )

    result = await controller.submit(
        TelegramSettingsTextInputContext(_request(), input_message_id=88),
        "Скрытое новое имя",
    )

    assert result is not None
    assert result.target_message_id == 77
    assert result.history_page == 4
    assert result.input_message_id == 88
    assert receipts == [result]
    assert use_case.commands == [
        SettingsTextInputCommand(OWNER_ID, use_case.active.ref, "Скрытое новое имя")
    ]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "target_factory",
        "target.lock",
        "target.message",
        "projection",
        "use_case_factory",
        "use_case",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_missing_exact_projection_rolls_back_without_mutation_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, events, use_case, receipts = _subject(monkeypatch, projection=None)

    with pytest.raises(DraftRevisionConflictError):
        await controller.submit(
            TelegramSettingsTextInputContext(_request(), input_message_id=88),
            "Не применяется",
        )

    assert use_case.commands == []
    assert receipts == []
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_input_returns_receipt_without_outbox_and_allows_no_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, events, use_case, receipts = _subject(
        monkeypatch,
        target_message_id=None,
    )

    result = await controller.submit(
        TelegramSettingsTextInputContext(_request(None), input_message_id=88),
        "Локальное имя",
    )

    assert result is not None and result.target_message_id is None
    assert receipts == []
    assert len(use_case.commands) == 1
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


def test_context_and_receipt_hide_input_catalog_and_identity_values() -> None:
    receipt = SettingsTextInputReceiptSnapshot(
        result=SettingsTextInputResult(
            operation=SettingsTextInputOperation.ACCOUNT_CREATE,
            status=SettingsTextInputStatus.UPDATED,
            owner=OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID),
            account=AccountSnapshot(
                ACCOUNT_ID,
                "Скрытый счёт",
                "other",
                "RUB",
                None,
                1,
            ),
        ),
        input_message_id=88,
        target_message_id=77,
    )
    rendered = repr((TelegramSettingsTextInputContext(_request(), 88), receipt))
    for private in (
        str(OWNER_ID),
        str(ACCOUNT_ID),
        "Скрытый",
        "RUB",
        "91000001",
        "92000002",
        "93000003",
    ):
        assert private not in rendered
