from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import TransactionSnapshot


class UndoAction(StrEnum):
    """Reversible transaction actions emitted by the durable audit trail."""

    CREATE = "create"
    DELETE = "delete"
    RESTORE = "restore"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class UndoActionResult:
    """The exact transaction state after one audited action was reversed."""

    action: UndoAction
    transaction: TransactionSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, UndoAction):
            raise TypeError("Undo action is invalid")
        if not isinstance(self.transaction, TransactionSnapshot):
            raise TypeError("Undo transaction snapshot is invalid")


class UndoRepository(Protocol):
    """Reverse only the latest explicit audit event inside the caller's UoW."""

    async def undo_last(self, owner_id: UUID) -> UndoActionResult | None: ...
