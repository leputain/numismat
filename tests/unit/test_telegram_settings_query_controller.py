import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.settings_queries import (
    DefaultAccountArchiveError,
    InvalidSettingsQueryCallback,
    SettingsCatalogTarget,
    SettingsInputDraftConflictError,
    SettingsQueryController,
    StaleSettingsQueryCallback,
    TelegramSettingsQueryContext,
    TelegramSettingsQueryReceipt,
    parse_settings_catalog_callback,
    parse_settings_category_kind,
)
from finbot.adapters.telegram.executor import (
    TelegramMutationExecutor,
    TelegramMutationRequest,
)
from finbot.adapters.telegram.ui import resume_draft_keyboard
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.interactions import MAX_OBJECT_VERSION
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
SECOND_ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000202")
ARCHIVED_ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000203")
EXPENSE_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
INCOME_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000302")
ARCHIVED_CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000303")
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


class _RecordingReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner = OwnerSnapshot(
            owner_id=OWNER_ID,
            locale="ru",
            timezone="Europe/Moscow",
            base_currency="RUB",
            default_account_id=DEFAULT_ACCOUNT_ID,
        )
        archived_at = datetime(2026, 8, 13, tzinfo=UTC)
        self.accounts = (
            AccountSnapshot(
                DEFAULT_ACCOUNT_ID,
                "Основной <счёт>",
                "card",
                "RUB",
                None,
                4,
            ),
            AccountSnapshot(
                SECOND_ACCOUNT_ID,
                "Резерв & наличные",
                "cash",
                "RUB",
                None,
                2,
            ),
            AccountSnapshot(
                ARCHIVED_ACCOUNT_ID,
                "Старый счёт",
                "card",
                "RUB",
                archived_at,
                7,
            ),
        )
        self.categories = (
            CategorySnapshot(
                EXPENSE_CATEGORY_ID,
                TransactionType.EXPENSE,
                "Кафе <еда>",
                "🍽",
                None,
                3,
            ),
            CategorySnapshot(
                INCOME_CATEGORY_ID,
                TransactionType.INCOME,
                "Зарплата & премия",
                "💼",
                None,
                5,
            ),
            CategorySnapshot(
                ARCHIVED_CATEGORY_ID,
                TransactionType.EXPENSE,
                "Старая категория",
                "▫️",
                archived_at,
                8,
            ),
        )

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.events.append("query.owner")
        return self.owner if owner_id == OWNER_ID else None

    async def list_accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        self.events.append(f"query.accounts:{archived}")
        if owner_id != OWNER_ID:
            return ()
        return tuple(
            account for account in self.accounts if (account.archived_at is not None) is archived
        )

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        self.events.append(f"query.categories:{kind}:{archived}")
        if owner_id != OWNER_ID:
            return ()
        return tuple(
            category
            for category in self.categories
            if (category.archived_at is not None) is archived
            and (kind is None or category.kind.value == kind)
        )


class _RecordingDrafts:
    def __init__(
        self,
        events: list[str],
        active: DraftSnapshot | None = None,
    ) -> None:
        self.events = events
        self.active = active
        self.suspend_calls: list[DraftRef] = []

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        assert self.active is not None and self.active.ref == expected
        self.events.append("draft.suspend")
        self.suspend_calls.append(expected)
        self.active = replace(
            self.active,
            suspended=True,
            revision=self.active.revision + 1,
        )
        return self.active


def _draft(*, state: str = "quick_confirm", suspended: bool = False) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=DRAFT_ID,
        state=state,
        payload={"private": "must-not-leak"},
        revision=6,
        suspended=suspended,
    )


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    reader: _RecordingReader,
    drafts: _RecordingDrafts,
    receipts: list[TelegramSettingsQueryReceipt],
    *,
    claimed: bool = True,
) -> SettingsQueryController:
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
        assert (locale, timezone, currency) == ("ru", "UTC", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    executor = TelegramMutationExecutor(
        cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))
    )

    def reader_factory(actual_session: AsyncSession) -> _RecordingReader:
        assert actual_session is cast(Any, session)
        events.append("reader_factory")
        return reader

    def draft_factory(actual_session: AsyncSession) -> _RecordingDrafts:
        assert actual_session is cast(Any, session)
        events.append("draft_factory")
        return drafts

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TelegramSettingsQueryReceipt,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return SettingsQueryController(
        executor,
        reader_factory,
        draft_factory,
        enqueue_receipt,
    )


def _context(
    *,
    update_id: int | None = 91_000_001,
    message_id: int | None = 94_000_004,
) -> TelegramSettingsQueryContext:
    return TelegramSettingsQueryContext(
        _request(update_id=update_id),
        message_id=message_id,
    )


def _callback_values(receipt: TelegramSettingsQueryReceipt) -> set[str]:
    return {
        button.callback_data
        for row in receipt.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }


def test_callback_parsers_accept_only_canonical_bounded_values() -> None:
    target = parse_settings_catalog_callback(
        f"sa:view:{SECOND_ACCOUNT_ID}:{MAX_OBJECT_VERSION}",
        prefix="sa:view:",
    )

    assert target == SettingsCatalogTarget(SECOND_ACCOUNT_ID, MAX_OBJECT_VERSION)
    assert (
        parse_settings_category_kind(
            "sc:list:expense",
            prefix="sc:list:",
        )
        is TransactionType.EXPENSE
    )


@pytest.mark.parametrize(
    ("data", "prefix"),
    [
        (f"sa:view:{SECOND_ACCOUNT_ID}:0", "sa:view:"),
        (f"sa:view:{SECOND_ACCOUNT_ID}:{MAX_OBJECT_VERSION + 1}", "sa:view:"),
        ("sa:view:not-a-uuid:1", "sa:view:"),
        (f"wrong:{SECOND_ACCOUNT_ID}:1", "sa:view:"),
        (f"sa:view:{SECOND_ACCOUNT_ID}:１", "sa:view:"),
    ],
)
def test_catalog_callback_parser_rejects_malformed_values(data: str, prefix: str) -> None:
    with pytest.raises(InvalidSettingsQueryCallback, match="Кнопка повреждена"):
        parse_settings_catalog_callback(data, prefix=prefix)


def test_category_kind_parser_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidSettingsQueryCallback, match="Кнопка повреждена"):
        parse_settings_category_kind("sc:list:other", prefix="sc:list:")


@pytest.mark.asyncio
async def test_settings_command_suspends_draft_and_enqueues_exact_resume_receipt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    drafts = _RecordingDrafts(events, _draft())
    controller = _controller(monkeypatch, events, reader, drafts, receipts)

    result = await controller.settings_main(
        _context(message_id=None),
        suspend_active_draft=True,
    )

    assert result is receipts[0]
    assert result.draft_ref == DraftRef(DRAFT_ID, 7)
    assert "Основной &lt;счёт&gt;" in result.text
    callbacks = _callback_values(result)
    assert any(callback.startswith("dm.") for callback in callbacks)
    assert any(callback.startswith("dg.") for callback in callbacks)
    assert drafts.suspend_calls == [DraftRef(DRAFT_ID, 6)]
    assert events.index("draft.suspend") < events.index("outbox") < events.index("commit")
    assert "must-not-leak" not in caplog.text
    assert "Основной <счёт>" not in caplog.text
    assert str(DRAFT_ID) not in repr(result)


@pytest.mark.asyncio
async def test_settings_back_does_not_mutate_an_already_active_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    drafts = _RecordingDrafts(events, _draft())
    controller = _controller(monkeypatch, events, reader, drafts, receipts)

    result = await controller.settings_main(_context(), suspend_active_draft=False)

    assert result is not None
    assert result.draft_ref == DraftRef(DRAFT_ID, 6)
    assert drafts.suspend_calls == []
    assert "draft.suspend" not in events


@pytest.mark.asyncio
async def test_account_list_preserves_active_and_archived_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events),
        receipts,
    )

    result = await controller.accounts(_context())

    assert result is receipts[0]
    keyboard = str(result.reply_markup.model_dump())
    assert f"sa:view:{DEFAULT_ACCOUNT_ID}:4" in keyboard
    assert f"sa:view:{SECOND_ACCOUNT_ID}:2" in keyboard
    assert "📦 Архив · 1" in keyboard
    assert f"sa:view:{ARCHIVED_ACCOUNT_ID}" not in keyboard
    assert events.index("query.accounts:True") < events.index("outbox")


@pytest.mark.asyncio
async def test_catalog_navigation_rolls_back_for_active_settings_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events, _draft(state="settings_account_rename")),
        receipts,
    )

    with pytest.raises(
        SettingsInputDraftConflictError,
        match="Сначала отмените текущий ввод",
    ):
        await controller.accounts(_context())

    assert receipts == []
    assert "reader_factory" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_account_view_checks_exact_owner_scoped_version_and_escapes_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events),
        receipts,
    )

    result = await controller.account(
        _context(),
        SettingsCatalogTarget(SECOND_ACCOUNT_ID, 2),
    )

    assert result is not None
    assert "Резерв &amp; наличные" in result.text
    assert f"sa:default:{SECOND_ACCOUNT_ID}:2" in str(result.reply_markup.model_dump())

    with pytest.raises(StaleSettingsQueryCallback, match="Счёт изменился"):
        await controller.account(
            _context(update_id=None),
            SettingsCatalogTarget(SECOND_ACCOUNT_ID, 1),
        )
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_account_archive_confirmation_rejects_default_and_renders_non_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events),
        receipts,
    )

    with pytest.raises(DefaultAccountArchiveError, match="другой основной счёт"):
        await controller.account_archive_confirmation(
            _context(update_id=None),
            SettingsCatalogTarget(DEFAULT_ACCOUNT_ID, 4),
        )

    result = await controller.account_archive_confirmation(
        _context(),
        SettingsCatalogTarget(SECOND_ACCOUNT_ID, 2),
    )
    assert result is not None
    assert "Резерв &amp; наличные" in result.text
    assert f"sa:archive:do:{SECOND_ACCOUNT_ID}:2" in str(result.reply_markup.model_dump())


@pytest.mark.asyncio
async def test_category_root_list_view_and_archived_use_shared_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events),
        receipts,
    )

    root = await controller.categories(_context(update_id=None))
    category_list = await controller.category_list(
        _context(update_id=None),
        TransactionType.EXPENSE,
    )
    category = await controller.category(
        _context(update_id=None),
        SettingsCatalogTarget(EXPENSE_CATEGORY_ID, 3),
    )
    archived = await controller.archived_categories(
        _context(update_id=None),
        TransactionType.EXPENSE,
    )

    assert root is not None
    assert "Расходы · 1" in str(root.reply_markup.model_dump())
    assert "Доходы · 1" in str(root.reply_markup.model_dump())
    assert category_list is not None
    assert f"sc:view:{EXPENSE_CATEGORY_ID}:3" in str(category_list.reply_markup.model_dump())
    assert "📦 Архив · 1" in str(category_list.reply_markup.model_dump())
    assert category is not None and "Кафе &lt;еда&gt;" in category.text
    assert archived is not None
    assert f"sc:restore:{ARCHIVED_CATEGORY_ID}:8" in str(archived.reply_markup.model_dump())


@pytest.mark.asyncio
async def test_archive_lists_and_timezone_listing_do_not_bypass_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    reader = _RecordingReader(events)
    controller = _controller(
        monkeypatch,
        events,
        reader,
        _RecordingDrafts(events),
        receipts,
    )

    accounts = await controller.archived_accounts(_context(update_id=None))
    timezones = await controller.timezones(_context())

    assert accounts is not None
    assert f"sa:restore:{ARCHIVED_ACCOUNT_ID}:7" in str(accounts.reply_markup.model_dump())
    assert timezones is receipts[0]
    keyboard = str(timezones.reply_markup.model_dump())
    assert "✓ Москва · UTC+3" in keyboard
    assert "s:timezone:1:1" in keyboard
    owner_index = max(index for index, event in enumerate(events) if event == "query.owner")
    outbox_index = max(index for index, event in enumerate(events) if event == "outbox")
    commit_index = max(index for index, event in enumerate(events) if event == "commit")
    assert owner_index < outbox_index < commit_index


@pytest.mark.asyncio
async def test_help_is_deduplicated_and_queued_without_database_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    controller = _controller(
        monkeypatch,
        events,
        _RecordingReader(events),
        _RecordingDrafts(events),
        receipts,
    )

    result = await controller.help(_context())

    assert result is receipts[0]
    assert "Как пользоваться Numismat" in result.text
    assert "query.owner" not in events
    assert events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_duplicate_update_returns_none_without_reads_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TelegramSettingsQueryReceipt] = []
    controller = _controller(
        monkeypatch,
        events,
        _RecordingReader(events),
        _RecordingDrafts(events),
        receipts,
        claimed=False,
    )

    result = await controller.accounts(_context())

    assert result is None
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


def test_contracts_hide_catalog_telegram_and_draft_values_from_repr() -> None:
    values = (
        TelegramSettingsQueryContext(_request(), message_id=94_000_004),
        SettingsCatalogTarget(SECOND_ACCOUNT_ID, 2),
        TelegramSettingsQueryReceipt(
            text="private account 123,45 ₽",
            reply_markup=resume_draft_keyboard(DRAFT_ID, 6),
            message_id=94_000_004,
            draft_ref=DraftRef(DRAFT_ID, 6),
        ),
    )

    for value in values:
        rendered = repr(value)
        assert "private" not in rendered
        assert "123,45" not in rendered
        assert "94000004" not in rendered
        assert str(SECOND_ACCOUNT_ID) not in rendered
        assert str(DRAFT_ID) not in rendered
