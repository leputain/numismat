from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.application.dto import CreateDraftCommand, OwnerSnapshot
from finbot.application.errors import ApplicationValidationError
from finbot.application.settings_mutations import (
    BeginAccountCreateCommand,
    BeginAccountRenameCommand,
    BeginCategoryCreateCommand,
    BeginCategoryRenameCommand,
    ChangeTimezoneCommand,
    SettingsInputIngressOperation,
    SettingsInputIngressResult,
    SettingsMutationRepository,
)
from finbot.application.use_cases.drafts import DraftUseCases


class BeginSettingsInput:
    """Create one canonical settings text-input draft under the owner mutex."""

    __slots__ = ("_drafts", "_targets")

    def __init__(
        self,
        targets: SettingsMutationRepository,
        drafts: DraftUseCases,
    ) -> None:
        self._targets = targets
        self._drafts = drafts

    async def account_create(
        self,
        command: BeginAccountCreateCommand,
    ) -> SettingsInputIngressResult:
        owner = await self._targets.lock_owner(command.owner_id)
        self._require_owner(owner, command.owner_id)
        draft = await self._drafts.create(
            CreateDraftCommand(command.owner_id, "settings_account_create", {})
        )
        return SettingsInputIngressResult(
            SettingsInputIngressOperation.ACCOUNT_CREATE,
            owner,
            draft,
        )

    async def account_rename(
        self,
        command: BeginAccountRenameCommand,
    ) -> SettingsInputIngressResult:
        account = await self._targets.lock_account(
            command.owner_id,
            command.account_id,
            command.expected_version,
        )
        if account.account_id != command.account_id or account.version != command.expected_version:
            raise ApplicationValidationError("Контекст переименования счёта повреждён")
        owner = await self._targets.lock_owner(command.owner_id)
        self._require_owner(owner, command.owner_id)
        draft = await self._drafts.create(
            CreateDraftCommand(
                command.owner_id,
                "settings_account_rename",
                {
                    "account_id": str(account.account_id),
                    "object_version": account.version,
                },
            )
        )
        return SettingsInputIngressResult(
            SettingsInputIngressOperation.ACCOUNT_RENAME,
            owner,
            draft,
            account=account,
        )

    async def category_create(
        self,
        command: BeginCategoryCreateCommand,
    ) -> SettingsInputIngressResult:
        owner = await self._targets.lock_owner(command.owner_id)
        self._require_owner(owner, command.owner_id)
        draft = await self._drafts.create(
            CreateDraftCommand(
                command.owner_id,
                "settings_category_create",
                {"kind": command.kind.value},
            )
        )
        return SettingsInputIngressResult(
            SettingsInputIngressOperation.CATEGORY_CREATE,
            owner,
            draft,
        )

    async def category_rename(
        self,
        command: BeginCategoryRenameCommand,
    ) -> SettingsInputIngressResult:
        category = await self._targets.lock_category(
            command.owner_id,
            command.category_id,
            command.expected_version,
        )
        if (
            category.category_id != command.category_id
            or category.version != command.expected_version
        ):
            raise ApplicationValidationError("Контекст переименования категории повреждён")
        owner = await self._targets.lock_owner(command.owner_id)
        self._require_owner(owner, command.owner_id)
        draft = await self._drafts.create(
            CreateDraftCommand(
                command.owner_id,
                "settings_category_rename",
                {
                    "category_id": str(category.category_id),
                    "kind": category.kind.value,
                    "object_version": category.version,
                },
            )
        )
        return SettingsInputIngressResult(
            SettingsInputIngressOperation.CATEGORY_RENAME,
            owner,
            draft,
            category=category,
        )

    @staticmethod
    def _require_owner(owner: OwnerSnapshot, owner_id: UUID) -> None:
        if owner.owner_id != owner_id:
            raise ApplicationValidationError("Контекст владельца настроек повреждён")


class ChangeSettingsTimezone:
    """CAS-update one owner's IANA timezone inside the caller's UoW."""

    __slots__ = ("_repository",)

    def __init__(self, repository: SettingsMutationRepository) -> None:
        self._repository = repository

    async def execute(self, command: ChangeTimezoneCommand) -> OwnerSnapshot:
        try:
            ZoneInfo(command.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ApplicationValidationError("Неизвестный часовой пояс") from error
        owner = await self._repository.change_timezone(
            command.owner_id,
            command.expected_timezone,
            command.timezone,
        )
        if owner.owner_id != command.owner_id or owner.timezone != command.timezone:
            raise ApplicationValidationError("Результат смены часового пояса повреждён")
        return owner
