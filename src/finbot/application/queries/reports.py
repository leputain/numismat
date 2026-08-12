from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    name: str
    emoji: str
    currency: str
    amount_minor: int


@dataclass(frozen=True, slots=True)
class ReportTransaction:
    id: UUID
    type: str
    amount_minor: int
    currency: str
    occurred_at: datetime
    description: str
    version: int
    category_name: str
    category_emoji: str
    account_name: str


class ReportQueries(Protocol):
    """Persistence-neutral reporting contract."""

    async def totals_by_currency(
        self, user_id: UUID, start: datetime, end: datetime
    ) -> dict[str, dict[str, int]]: ...

    async def category_totals(
        self, user_id: UUID, start: datetime, end: datetime
    ) -> list[CategoryTotal]: ...

    async def period_transactions(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int = 20,
    ) -> list[ReportTransaction]: ...
