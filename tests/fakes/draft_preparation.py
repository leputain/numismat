from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from finbot.application.draft_preparation import QuickDraftParseResult
from finbot.application.dto import AccountSnapshot, CategorySnapshot, OwnerSnapshot
from finbot.domain.category_rules import CategoryRuleSpec
from finbot.domain.transactions import TransactionType


@dataclass(slots=True)
class FixedClock:
    value: datetime
    requested_timezones: list[str] = field(default_factory=list)

    def now(self, timezone: str) -> datetime:
        self.requested_timezones.append(timezone)
        return self.value


@dataclass(slots=True)
class StubOwnerReader:
    owners: dict[UUID, OwnerSnapshot]
    calls: list[UUID] = field(default_factory=list)

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.calls.append(owner_id)
        return self.owners.get(owner_id)


@dataclass(slots=True)
class StubQuickDraftParser:
    result: QuickDraftParseResult | None = None
    error: ValueError | None = None
    calls: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def parse(self, text: str, *, timezone: str) -> QuickDraftParseResult:
        self.calls.append((text, timezone))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("Stub parser has no configured result")
        return self.result


@dataclass(slots=True)
class StubDraftCatalogResolver:
    account: AccountSnapshot | None
    category: CategorySnapshot | None
    categories_by_id: dict[UUID, CategorySnapshot] = field(default_factory=dict)
    account_calls: list[tuple[UUID, str | None, UUID | None]] = field(default_factory=list)
    category_calls: list[tuple[UUID, TransactionType, str | None]] = field(default_factory=list)
    get_category_calls: list[tuple[UUID, TransactionType, UUID]] = field(default_factory=list)

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        self.account_calls.append((owner_id, hint, default_account_id))
        return self.account

    async def resolve_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        hint: str | None,
    ) -> CategorySnapshot | None:
        self.category_calls.append((owner_id, kind, hint))
        return self.category

    async def get_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        category_id: UUID,
    ) -> CategorySnapshot | None:
        self.get_category_calls.append((owner_id, kind, category_id))
        return self.categories_by_id.get(category_id)


@dataclass(slots=True)
class StubCategoryRuleReader:
    rules: tuple[CategoryRuleSpec, ...] = ()
    calls: list[tuple[UUID, TransactionType, UUID | None]] = field(default_factory=list)

    async def list_applicable(
        self,
        user_id: UUID,
        kind: TransactionType,
        account_id: UUID | None,
    ) -> tuple[CategoryRuleSpec, ...]:
        self.calls.append((user_id, kind, account_id))
        return self.rules
