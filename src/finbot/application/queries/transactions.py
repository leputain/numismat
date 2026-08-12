from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TransactionDetails:
    """Persistence-neutral transaction projection used by application outputs."""

    id: UUID
    type: str
    amount_minor: int
    currency: str
    account_id: UUID
    account_name: str
    category_id: UUID
    category_name: str
    category_emoji: str
    occurred_at: datetime
    description: str
    source: str
    deleted_at: datetime | None
    version: int


class TransactionQueries(Protocol):
    """Read-side contract; adapters decide how projections are loaded."""

    async def get(self, user_id: UUID, transaction_id: UUID) -> TransactionDetails | None: ...

    async def list_active(
        self, user_id: UUID, *, page: int = 0, page_size: int = 5
    ) -> tuple[list[TransactionDetails], int]: ...

    async def list_deleted(
        self, user_id: UUID, *, page: int = 0, page_size: int = 5
    ) -> tuple[list[TransactionDetails], int]: ...

    async def export(self, user_id: UUID) -> list[TransactionDetails]: ...
