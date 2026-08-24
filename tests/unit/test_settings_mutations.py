from dataclasses import replace
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import AccountSnapshot, CategorySnapshot, OwnerSnapshot
from finbot.application.errors import (
    ActiveDraftConflictError,
    ApplicationValidationError,
    ObjectVersionConflictError,
)
from finbot.application.settings_mutations import (
    BeginAccountCreateCommand,
    BeginAccountRenameCommand,
    BeginCategoryCreateCommand,
    BeginCategoryRenameCommand,
    ChangeTimezoneCommand,
    SettingsInputIngressOperation,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.settings_mutations import (
    BeginSettingsInput,
    ChangeSettingsTimezone,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")


class _SettingsRepository:
    def __init__(self) -> None:
        self.owner = OwnerSnapshot(
            OWNER_ID,
            "ru",
            "Europe/Moscow",
            "RUB",
            ACCOUNT_ID,
        )
        self.account = AccountSnapshot(ACCOUNT_ID, "Карта", "card", "RUB", None, 4)
        self.category = CategorySnapshot(
            CATEGORY_ID,
            TransactionType.EXPENSE,
            "Кафе",
            "🍽",
            None,
            7,
        )
        self.events: list[str] = []

    async def lock_owner(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("owner")
        return self.owner

    async def lock_account(
        self,
        owner_id: UUID,
        account_id: UUID,
        expected_version: int,
    ) -> AccountSnapshot:
        assert (owner_id, account_id) == (OWNER_ID, ACCOUNT_ID)
        self.events.append("account")
        if expected_version != self.account.version:
            raise ObjectVersionConflictError(current_version=self.account.version)
        return self.account

    async def lock_category(
        self,
        owner_id: UUID,
        category_id: UUID,
        expected_version: int,
    ) -> CategorySnapshot:
        assert (owner_id, category_id) == (OWNER_ID, CATEGORY_ID)
        self.events.append("category")
        if expected_version != self.category.version:
            raise ObjectVersionConflictError(current_version=self.category.version)
        return self.category

    async def change_timezone(
        self,
        owner_id: UUID,
        expected_version: int,
        timezone: str,
    ) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("timezone")
        if self.owner.settings_version != expected_version:
            raise ObjectVersionConflictError(current_version=self.owner.settings_version)
        self.owner = replace(
            self.owner,
            timezone=timezone,
            settings_version=self.owner.settings_version + 1,
        )
        return self.owner


def _ingress(
    repository: _SettingsRepository,
    drafts: InMemoryDraftRepository | None = None,
) -> BeginSettingsInput:
    return BeginSettingsInput(
        repository,
        DraftUseCases(drafts or InMemoryDraftRepository()),
    )


@pytest.mark.asyncio
async def test_create_ingress_drafts_are_canonical_and_channel_neutral() -> None:
    account_repository = _SettingsRepository()
    account = await _ingress(account_repository).account_create(BeginAccountCreateCommand(OWNER_ID))

    assert account.operation is SettingsInputIngressOperation.ACCOUNT_CREATE
    assert account.draft.state == "settings_account_create"
    assert dict(account.draft.payload) == {}
    assert "ui_message_id" not in account.draft.payload

    category_repository = _SettingsRepository()
    category = await _ingress(category_repository).category_create(
        BeginCategoryCreateCommand(OWNER_ID, TransactionType.INCOME)
    )

    assert category.operation is SettingsInputIngressOperation.CATEGORY_CREATE
    assert category.draft.state == "settings_category_create"
    assert dict(category.draft.payload) == {"kind": "income"}
    assert "ui_message_id" not in category.draft.payload


@pytest.mark.asyncio
async def test_rename_ingress_uses_authoritative_target_and_exact_version() -> None:
    account_repository = _SettingsRepository()
    account = await _ingress(account_repository).account_rename(
        BeginAccountRenameCommand(OWNER_ID, ACCOUNT_ID, 4)
    )
    assert account.account is account_repository.account
    assert dict(account.draft.payload) == {
        "account_id": str(ACCOUNT_ID),
        "object_version": 4,
    }
    assert account_repository.events == ["account", "owner"]

    category_repository = _SettingsRepository()
    category = await _ingress(category_repository).category_rename(
        BeginCategoryRenameCommand(OWNER_ID, CATEGORY_ID, 7)
    )
    assert category.category is category_repository.category
    assert dict(category.draft.payload) == {
        "category_id": str(CATEGORY_ID),
        "kind": "expense",
        "object_version": 7,
    }
    assert category_repository.events == ["category", "owner"]


@pytest.mark.asyncio
async def test_stale_rename_fails_before_draft_creation() -> None:
    drafts = InMemoryDraftRepository()
    repository = _SettingsRepository()

    with pytest.raises(ObjectVersionConflictError) as conflict:
        await _ingress(repository, drafts).account_rename(
            BeginAccountRenameCommand(OWNER_ID, ACCOUNT_ID, 3)
        )

    assert conflict.value.current_version == 4
    assert await drafts.get_active(OWNER_ID) is None
    assert repository.events == ["account"]


@pytest.mark.asyncio
async def test_active_draft_is_preserved_when_settings_ingress_conflicts() -> None:
    drafts = InMemoryDraftRepository()
    original = await drafts.create_if_absent(OWNER_ID, "wizard_type", {"flow": "wizard"})

    with pytest.raises(ActiveDraftConflictError):
        await _ingress(_SettingsRepository(), drafts).category_create(
            BeginCategoryCreateCommand(OWNER_ID, TransactionType.EXPENSE)
        )

    current = await drafts.get_active(OWNER_ID)
    assert current is not None and current.ref == original.ref
    assert current.payload == {"flow": "wizard"}


@pytest.mark.asyncio
async def test_timezone_mutation_validates_iana_zone_and_authoritative_result() -> None:
    repository = _SettingsRepository()
    result = await ChangeSettingsTimezone(repository).execute(
        ChangeTimezoneCommand(OWNER_ID, 1, "Asia/Yekaterinburg")
    )
    assert result.timezone == "Asia/Yekaterinburg"
    assert result.settings_version == 2

    with pytest.raises(ApplicationValidationError, match="Неизвестный"):
        await ChangeSettingsTimezone(repository).execute(
            ChangeTimezoneCommand(OWNER_ID, 2, "Not/AZone")
        )


def test_settings_commands_hide_owner_and_target_values_from_repr() -> None:
    commands = (
        BeginAccountCreateCommand(OWNER_ID),
        BeginAccountRenameCommand(OWNER_ID, ACCOUNT_ID, 4),
        BeginCategoryCreateCommand(OWNER_ID, TransactionType.EXPENSE),
        BeginCategoryRenameCommand(OWNER_ID, CATEGORY_ID, 7),
        ChangeTimezoneCommand(OWNER_ID, 1, "Asia/Omsk"),
    )
    for command in commands:
        rendered = repr(command)
        assert str(OWNER_ID) not in rendered
        assert str(ACCOUNT_ID) not in rendered
        assert str(CATEGORY_ID) not in rendered
        assert "Europe/Moscow" not in rendered
