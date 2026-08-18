from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TransactionDetails:
    """Persistence-neutral transaction projection used by application outputs."""

    id: UUID = field(repr=False)
    type: str = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    account_id: UUID = field(repr=False)
    account_name: str = field(repr=False)
    category_id: UUID = field(repr=False)
    category_name: str = field(repr=False)
    category_emoji: str = field(repr=False)
    occurred_at: datetime = field(repr=False)
    description: str = field(repr=False)
    source: str = field(repr=False)
    deleted_at: datetime | None = field(repr=False)
    version: int = field(repr=False)


class TransactionQueries(Protocol):
    """Read-side contract; adapters decide how projections are loaded."""

    async def get(self, user_id: UUID, transaction_id: UUID) -> TransactionDetails | None: ...

    async def list_active(
        self, user_id: UUID, *, page: int = 0, page_size: int = 5
    ) -> tuple[list[TransactionDetails], int]: ...

    async def list_deleted(
        self, user_id: UUID, *, page: int = 0, page_size: int = 5
    ) -> tuple[list[TransactionDetails], int]: ...

    async def export(
        self,
        user_id: UUID,
        *,
        limit: int,
    ) -> list[TransactionDetails]: ...
