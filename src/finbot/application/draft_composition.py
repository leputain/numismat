from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.draft_preparation import PreparedDraftResult
from finbot.domain.money import validate_minor
from finbot.domain.transactions import TransactionType

_MAX_DESCRIPTION_LENGTH = 500


@dataclass(frozen=True, slots=True, repr=False)
class ComposeDraftSpec:
    """Channel-neutral values accepted by the one-screen review composer."""

    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    account: DraftCatalogRef | None = field(default=None, repr=False)
    category: DraftCatalogRef | None = field(default=None, repr=False)
    occurred_on: date | None = field(default=None, repr=False)
    description: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransactionType):
            raise TypeError("Composed transaction type is invalid")
        validate_minor(self.amount_minor)
        for reference in (self.account, self.category):
            if reference is not None and not isinstance(reference, DraftCatalogRef):
                raise TypeError("Composed catalog reference is invalid")
        if self.occurred_on is not None and type(self.occurred_on) is not date:
            raise TypeError("Composed local date is invalid")
        if not isinstance(self.description, str):
            raise TypeError("Composed description must be a string")
        if len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError("Composed description is too long")

    def bind(self, owner_id: UUID) -> BeginComposeDraftCommand:
        return BeginComposeDraftCommand(owner_id, self)


@dataclass(frozen=True, slots=True, repr=False)
class BeginComposeDraftCommand:
    owner_id: UUID = field(repr=False)
    spec: ComposeDraftSpec = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if not isinstance(self.spec, ComposeDraftSpec):
            raise TypeError("Compose draft spec is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ComposedDraftInput:
    """Normalized durable intent; local date has already been resolved once."""

    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    occurred_at: datetime = field(repr=False)
    account: DraftCatalogRef | None = field(default=None, repr=False)
    category: DraftCatalogRef | None = field(default=None, repr=False)
    description: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransactionType):
            raise TypeError("Composed transaction type is invalid")
        validate_minor(self.amount_minor)
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.utcoffset() is None:
            raise ValueError("Composed transaction date must contain a timezone")
        for reference in (self.account, self.category):
            if reference is not None and not isinstance(reference, DraftCatalogRef):
                raise TypeError("Composed catalog reference is invalid")
        if not isinstance(self.description, str):
            raise TypeError("Composed description must be a string")
        if len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError("Composed description is too long")


@dataclass(frozen=True, slots=True, repr=False)
class PrepareComposedDraftCommand:
    owner_id: UUID = field(repr=False)
    values: ComposedDraftInput = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if not isinstance(self.values, ComposedDraftInput):
            raise TypeError("Composed draft input is invalid")


class DraftComposePreparer(Protocol):
    async def execute(self, command: PrepareComposedDraftCommand) -> PreparedDraftResult: ...
