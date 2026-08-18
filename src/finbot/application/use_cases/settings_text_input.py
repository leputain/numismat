from dataclasses import replace
from uuid import UUID

from finbot.application.catalogs import (
    CreateAccountCommand,
    CreateCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import DraftSnapshot, UpdateDraftCommand
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.services.catalogs import normalize_catalog_name
from finbot.application.settings_text_input import (
    SettingsTextInputCommand,
    SettingsTextInputError,
    SettingsTextInputOperation,
    SettingsTextInputResult,
    SettingsTextInputStatus,
    SettingsTextInputTarget,
    SettingsTextInputTargetRepository,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionType


def _payload_version(draft: DraftSnapshot) -> int:
    value = draft.payload.get("object_version")
    if isinstance(value, bool):
        raise InvalidStateError("Черновик настроек содержит некорректную версию")
    try:
        version = int(str(value))
    except TypeError, ValueError:
        raise InvalidStateError("Черновик настроек содержит некорректную версию") from None
    if version < 1:
        raise InvalidStateError("Черновик настроек содержит некорректную версию")
    return version


class SettingsTextInputUseCase:
    """Complete one exact settings draft inside the caller-owned transaction."""

    __slots__ = ("_catalogs", "_drafts", "_targets")

    def __init__(
        self,
        targets: SettingsTextInputTargetRepository,
        catalogs: CatalogUseCases,
        drafts: DraftUseCases,
    ) -> None:
        self._targets = targets
        self._catalogs = catalogs
        self._drafts = drafts

    async def execute(self, command: SettingsTextInputCommand) -> SettingsTextInputResult:
        target = await self._targets.lock_target(command.owner_id, command.expected)
        if PENDING_DRAFT_INTENT_KEY in target.draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        if target.owner.owner_id != command.owner_id or target.draft.ref != command.expected:
            raise InvalidStateError("Контекст ввода настроек повреждён")

        try:
            normalized_name = normalize_catalog_name(command.text)
        except ValueError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.INVALID_NAME,
            )
        command = replace(command, text=normalized_name)

        operation = target.operation
        if operation is SettingsTextInputOperation.ACCOUNT_CREATE:
            return await self._create_account(command, target)
        if operation is SettingsTextInputOperation.ACCOUNT_RENAME:
            return await self._rename_account(command, target)
        if operation is SettingsTextInputOperation.CATEGORY_CREATE:
            return await self._create_category(command, target)
        return await self._rename_category(command, target)

    async def _retry(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
        error: SettingsTextInputError,
        *,
        payload: dict[str, object] | None = None,
    ) -> SettingsTextInputResult:
        draft = await self._drafts.update(
            UpdateDraftCommand(
                owner_id=command.owner_id,
                expected=command.expected,
                state=target.draft.state,
                payload=payload if payload is not None else target.draft.payload,
            )
        )
        return SettingsTextInputResult(
            operation=target.operation,
            status=SettingsTextInputStatus.RETRY,
            owner=target.owner,
            draft=draft,
            account=target.account,
            category=target.category,
            retry_error=error,
        )

    async def _success_account(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
        account_id: UUID,
        *,
        owner_default_changed: bool = False,
    ) -> SettingsTextInputResult:
        account = await self._targets.get_account(command.owner_id, account_id)
        if account is None:
            raise InvalidStateError("Изменённый счёт не найден")
        await self._drafts.cancel(command.owner_id, command.expected)
        owner = (
            replace(target.owner, default_account_id=account.account_id)
            if owner_default_changed
            else target.owner
        )
        return SettingsTextInputResult(
            operation=target.operation,
            status=SettingsTextInputStatus.UPDATED,
            owner=owner,
            account=account,
        )

    async def _success_category(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
        category_id: UUID,
    ) -> SettingsTextInputResult:
        category = await self._targets.get_category(command.owner_id, category_id)
        if category is None:
            raise InvalidStateError("Изменённая категория не найдена")
        await self._drafts.cancel(command.owner_id, command.expected)
        return SettingsTextInputResult(
            operation=target.operation,
            status=SettingsTextInputStatus.UPDATED,
            owner=target.owner,
            category=category,
        )

    async def _create_account(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
    ) -> SettingsTextInputResult:
        try:
            mutation = await self._catalogs.create_account(
                CreateAccountCommand(
                    command.owner_id,
                    command.text,
                    target.owner.base_currency,
                )
            )
        except ApplicationValidationError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.INVALID_NAME,
            )

        default_changed = target.owner.default_account_id is None
        if default_changed:
            mutation = await self._catalogs.set_default_account(
                SetDefaultAccountCommand(
                    command.owner_id,
                    mutation.entity_id,
                    mutation.version,
                )
            )
        return await self._success_account(
            command,
            target,
            mutation.entity_id,
            owner_default_changed=default_changed,
        )

    async def _rename_account(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
    ) -> SettingsTextInputResult:
        account = target.account
        if account is None:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.TARGET_UNAVAILABLE,
            )
        expected_version = _payload_version(target.draft)
        if expected_version != account.version:
            payload = dict(target.draft.payload)
            payload["object_version"] = account.version
            return await self._retry(
                command,
                target,
                SettingsTextInputError.VERSION_CONFLICT,
                payload=payload,
            )
        try:
            mutation = await self._catalogs.update_account(
                UpdateAccountCommand(
                    command.owner_id,
                    account.account_id,
                    command.text,
                    expected_version,
                )
            )
        except ApplicationValidationError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.INVALID_NAME,
            )
        except ObjectVersionConflictError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.VERSION_CONFLICT,
            )
        except EntityNotFoundError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.TARGET_UNAVAILABLE,
            )
        return await self._success_account(command, target, mutation.entity_id)

    async def _create_category(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
    ) -> SettingsTextInputResult:
        kind = TransactionType(str(target.draft.payload["kind"]))
        try:
            mutation = await self._catalogs.create_category(
                CreateCategoryCommand(command.owner_id, command.text, kind)
            )
        except ApplicationValidationError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.INVALID_NAME,
            )
        return await self._success_category(command, target, mutation.entity_id)

    async def _rename_category(
        self,
        command: SettingsTextInputCommand,
        target: SettingsTextInputTarget,
    ) -> SettingsTextInputResult:
        category = target.category
        if category is None:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.TARGET_UNAVAILABLE,
            )
        expected_version = _payload_version(target.draft)
        if expected_version != category.version:
            payload = dict(target.draft.payload)
            payload["object_version"] = category.version
            return await self._retry(
                command,
                target,
                SettingsTextInputError.VERSION_CONFLICT,
                payload=payload,
            )
        try:
            mutation = await self._catalogs.update_category(
                UpdateCategoryCommand(
                    command.owner_id,
                    category.category_id,
                    command.text,
                    expected_version,
                )
            )
        except ApplicationValidationError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.INVALID_NAME,
            )
        except ObjectVersionConflictError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.VERSION_CONFLICT,
            )
        except EntityNotFoundError:
            return await self._retry(
                command,
                target,
                SettingsTextInputError.TARGET_UNAVAILABLE,
            )
        return await self._success_category(command, target, mutation.entity_id)
