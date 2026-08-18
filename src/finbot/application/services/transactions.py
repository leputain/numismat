from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from finbot.domain.transactions import TransactionDraft, TransactionView


@dataclass(frozen=True, slots=True)
class TransactionPatch:
    """Persistence-neutral optimistic update request."""

    transaction_id: UUID = field(repr=False)
    version: int = field(repr=False)
    amount_minor: int | None = field(default=None, repr=False)
    category_id: UUID | None = field(default=None, repr=False)
    account_id: UUID | None = field(default=None, repr=False)
    occurred_at: datetime | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)


class TransactionCommands(Protocol):
    """Write-side contract implemented by a persistence adapter."""

    async def save(
        self, user_id: UUID, draft: TransactionDraft, *, update_id: int | None = None
    ) -> TransactionView: ...

    async def edit(self, user_id: UUID, patch: TransactionPatch) -> TransactionView: ...

    async def delete(self, user_id: UUID, transaction_id: UUID, version: int) -> None: ...

    async def restore(self, user_id: UUID, transaction_id: UUID, version: int) -> None: ...
