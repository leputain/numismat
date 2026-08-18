from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.settings_text_input import SettingsTextInputTarget
from finbot.domain.transactions import TransactionType


class SettingsInputIngressOperation(StrEnum):
    ACCOUNT_CREATE = "account_create"
    ACCOUNT_RENAME = "account_rename"
    CATEGORY_CREATE = "category_create"
    CATEGORY_RENAME = "category_rename"


@dataclass(frozen=True, slots=True)
class BeginAccountCreateCommand:
    owner_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Settings owner id must be a UUID")


@dataclass(frozen=True, slots=True)
class BeginAccountRenameCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_catalog_target(self.owner_id, self.account_id, self.expected_version)


@dataclass(frozen=True, slots=True)
class BeginCategoryCreateCommand:
    owner_id: UUID = field(repr=False)
    kind: TransactionType = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Settings owner id must be a UUID")
        if not isinstance(self.kind, TransactionType):
            raise TypeError("Settings category kind is invalid")


@dataclass(frozen=True, slots=True)
class BeginCategoryRenameCommand:
    owner_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        _validate_catalog_target(self.owner_id, self.category_id, self.expected_version)


@dataclass(frozen=True, slots=True)
class ChangeTimezoneCommand:
    owner_id: UUID = field(repr=False)
    expected_timezone: str = field(repr=False)
    timezone: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Settings owner id must be a UUID")
        for value in (self.expected_timezone, self.timezone):
            if type(value) is not str or not 1 <= len(value) <= 64:
                raise ValueError("Settings timezone is invalid")


def _validate_catalog_target(owner_id: UUID, entity_id: UUID, version: int) -> None:
    if not isinstance(owner_id, UUID) or not isinstance(entity_id, UUID):
        raise TypeError("Settings catalog target ids must be UUID values")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("Settings catalog version must be positive")


@dataclass(frozen=True, slots=True)
class SettingsInputIngressResult:
    operation: SettingsInputIngressOperation
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    account: AccountSnapshot | None = field(default=None, repr=False)
    category: CategorySnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SettingsInputIngressOperation):
            raise TypeError("Settings ingress operation is invalid")
        target = SettingsTextInputTarget(
            owner=self.owner,
            draft=self.draft,
            account=self.account,
            category=self.category,
        )
        if target.operation.value != self.operation.value:
            raise ValueError("Settings ingress draft does not match its operation")


class SettingsMutationRepository(Protocol):
    """Lock owner-scoped settings targets without owning the transaction."""

    async def lock_owner(self, owner_id: UUID) -> OwnerSnapshot: ...

    async def lock_account(
        self,
        owner_id: UUID,
        account_id: UUID,
        expected_version: int,
    ) -> AccountSnapshot: ...

    async def lock_category(
        self,
        owner_id: UUID,
        category_id: UUID,
        expected_version: int,
    ) -> CategorySnapshot: ...

    async def change_timezone(
        self,
        owner_id: UUID,
        expected_timezone: str,
        timezone: str,
    ) -> OwnerSnapshot: ...
