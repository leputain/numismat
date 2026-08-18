from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import DraftSnapshot, OwnerSnapshot, TransactionSnapshot


class TransactionEditIngressStatus(StrEnum):
    DRAFT_CREATED = "draft_created"
    CONFLICT_STAGED = "conflict_staged"
    BLOCKED_BANK_IMPORT = "blocked_bank_import"


@dataclass(frozen=True, slots=True)
class BeginTransactionEditCommand:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Transaction edit owner id must be a UUID")
        if not isinstance(self.transaction_id, UUID):
            raise TypeError("Edited transaction id must be a UUID")
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 1
        ):
            raise ValueError("Edited transaction version must be positive")


@dataclass(frozen=True, slots=True)
class BeginTransactionEditResult:
    status: TransactionEditIngressStatus
    owner: OwnerSnapshot = field(repr=False)
    transaction: TransactionSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, TransactionEditIngressStatus):
            raise TypeError("Transaction edit ingress status is invalid")
        if self.transaction.deleted_at is not None:
            raise ValueError("Transaction edit ingress requires an active transaction")
        if self.status is TransactionEditIngressStatus.DRAFT_CREATED:
            if self.draft.state != "edit_menu" or self.draft.suspended:
                raise ValueError("Created transaction edit draft is invalid")
        elif self.status is TransactionEditIngressStatus.BLOCKED_BANK_IMPORT and (
            self.draft.payload.get("flow") != "bank_import"
            or "pending_intent" in self.draft.payload
        ):
            raise ValueError("Blocked transaction edit draft is invalid")


class TransactionEditTargetReader(Protocol):
    """Return an owner-scoped active transaction under an authoritative lock."""

    async def __call__(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionSnapshot: ...


class TransactionEditOwnerReader(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...
