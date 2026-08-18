from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import InvalidStateError
from finbot.domain.transactions import TransactionType

MAX_SETTINGS_TEXT_LENGTH = 4096
SETTINGS_TEXT_INPUT_STATES = frozenset(
    {
        "settings_account_create",
        "settings_account_rename",
        "settings_category_create",
        "settings_category_rename",
    }
)


class SettingsTextInputOperation(StrEnum):
    ACCOUNT_CREATE = "account_create"
    ACCOUNT_RENAME = "account_rename"
    CATEGORY_CREATE = "category_create"
    CATEGORY_RENAME = "category_rename"


class SettingsTextInputStatus(StrEnum):
    UPDATED = "updated"
    RETRY = "retry"


class SettingsTextInputError(StrEnum):
    """Safe retry reasons that never embed owner-submitted catalog names."""

    INVALID_NAME = "invalid_name"
    VERSION_CONFLICT = "version_conflict"
    TARGET_UNAVAILABLE = "target_unavailable"


class SettingsTextInputNotApplicableError(InvalidStateError):
    """The active draft belongs to another bounded text-input flow."""


@dataclass(frozen=True, slots=True)
class SettingsTextInputCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Settings text owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected settings draft must be a DraftRef")
        if not isinstance(self.text, str):
            raise TypeError("Settings text must be a string")
        if len(self.text) > MAX_SETTINGS_TEXT_LENGTH:
            raise ValueError("Settings text is too long")


def operation_for_state(state: str) -> SettingsTextInputOperation:
    try:
        return {
            "settings_account_create": SettingsTextInputOperation.ACCOUNT_CREATE,
            "settings_account_rename": SettingsTextInputOperation.ACCOUNT_RENAME,
            "settings_category_create": SettingsTextInputOperation.CATEGORY_CREATE,
            "settings_category_rename": SettingsTextInputOperation.CATEGORY_RENAME,
        }[state]
    except KeyError:
        raise SettingsTextInputNotApplicableError(
            "Активный черновик не принимает ввод настроек"
        ) from None


def _positive_version(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Settings catalog version is invalid")
    try:
        version = int(str(value))
    except TypeError, ValueError:
        raise ValueError("Settings catalog version is invalid") from None
    if version < 1:
        raise ValueError("Settings catalog version is invalid")
    return version


@dataclass(frozen=True, slots=True)
class SettingsTextInputTarget:
    """Authoritative owner/draft/catalog tuple locked by the DB adapter."""

    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    account: AccountSnapshot | None = field(default=None, repr=False)
    category: CategorySnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.draft.suspended:
            raise ValueError("Settings text draft must be active")
        operation = operation_for_state(self.draft.state)
        payload = self.draft.payload

        if operation is SettingsTextInputOperation.ACCOUNT_CREATE:
            if payload or self.account is not None or self.category is not None:
                raise ValueError("Account create draft is not canonical")
            return

        if operation is SettingsTextInputOperation.ACCOUNT_RENAME:
            if set(payload) != {"account_id", "object_version"}:
                raise ValueError("Account rename draft is not canonical")
            account_id = UUID(str(payload["account_id"]))
            _positive_version(payload["object_version"])
            if self.category is not None:
                raise ValueError("Account rename target contains a category")
            if self.account is not None and self.account.account_id != account_id:
                raise ValueError("Account rename target id does not match its draft")
            return

        kind = TransactionType(str(payload.get("kind", "")))
        if operation is SettingsTextInputOperation.CATEGORY_CREATE:
            if set(payload) != {"kind"} or self.account is not None or self.category is not None:
                raise ValueError("Category create draft is not canonical")
            return

        if set(payload) != {"category_id", "kind", "object_version"}:
            raise ValueError("Category rename draft is not canonical")
        category_id = UUID(str(payload["category_id"]))
        _positive_version(payload["object_version"])
        if self.account is not None:
            raise ValueError("Category rename target contains an account")
        if self.category is not None and (
            self.category.category_id != category_id or self.category.kind is not kind
        ):
            raise ValueError("Category rename target does not match its draft")

    @property
    def operation(self) -> SettingsTextInputOperation:
        return operation_for_state(self.draft.state)


@dataclass(frozen=True, slots=True)
class SettingsTextInputResult:
    operation: SettingsTextInputOperation
    status: SettingsTextInputStatus
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot | None = field(default=None, repr=False)
    account: AccountSnapshot | None = field(default=None, repr=False)
    category: CategorySnapshot | None = field(default=None, repr=False)
    retry_error: SettingsTextInputError | None = None

    def __post_init__(self) -> None:
        is_retry = self.status is SettingsTextInputStatus.RETRY
        if is_retry != (self.retry_error is not None):
            raise ValueError("Settings retry status and reason do not match")
        if is_retry != (self.draft is not None):
            raise ValueError("Settings retry status and draft do not match")
        if self.draft is not None:
            if self.draft.suspended or operation_for_state(self.draft.state) is not self.operation:
                raise ValueError("Settings retry draft does not match its operation")

        is_account = self.operation in {
            SettingsTextInputOperation.ACCOUNT_CREATE,
            SettingsTextInputOperation.ACCOUNT_RENAME,
        }
        if is_account and self.category is not None:
            raise ValueError("Account result contains a category")
        if not is_account and self.account is not None:
            raise ValueError("Category result contains an account")
        if not is_retry and (
            (is_account and self.account is None) or (not is_account and self.category is None)
        ):
            raise ValueError("Successful settings result requires its catalog snapshot")


class SettingsTextInputTargetRepository(Protocol):
    """Lock exact settings input state and expose resulting typed snapshots."""

    async def lock_target(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> SettingsTextInputTarget: ...

    async def get_account(
        self,
        owner_id: UUID,
        account_id: UUID,
    ) -> AccountSnapshot | None: ...

    async def get_category(
        self,
        owner_id: UUID,
        category_id: UUID,
    ) -> CategorySnapshot | None: ...
