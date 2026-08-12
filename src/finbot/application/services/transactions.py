from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from finbot.domain.transactions import TransactionDraft, TransactionView


@dataclass(frozen=True, slots=True)
class TransactionPatch:
    """Persistence-neutral optimistic update request."""

    transaction_id: UUID
    version: int
    amount_minor: int | None = None
    category_id: UUID | None = None
    account_id: UUID | None = None
    occurred_at: datetime | None = None
    description: str | None = None


class TransactionCommands(Protocol):
    """Write-side contract implemented by a persistence adapter."""

    async def save(
        self, user_id: UUID, draft: TransactionDraft, *, update_id: int | None = None
    ) -> TransactionView: ...

    async def edit(self, user_id: UUID, patch: TransactionPatch) -> TransactionView: ...

    async def delete(self, user_id: UUID, transaction_id: UUID, version: int) -> None: ...

    async def restore(self, user_id: UUID, transaction_id: UUID, version: int) -> None: ...
