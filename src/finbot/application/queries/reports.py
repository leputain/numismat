from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    name: str = field(repr=False)
    emoji: str = field(repr=False)
    currency: str = field(repr=False)
    amount_minor: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReportTransaction:
    id: UUID = field(repr=False)
    type: str = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    occurred_at: datetime = field(repr=False)
    description: str = field(repr=False)
    version: int = field(repr=False)
    category_name: str = field(repr=False)
    category_emoji: str = field(repr=False)
    account_name: str = field(repr=False)


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
