from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from finbot.application.dto import DraftRef, DraftSnapshot


class DraftRuleAction(StrEnum):
    """Closed set of category-rule choices available on a draft review."""

    GLOBAL = "global"
    ACCOUNT = "account"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class StageDraftRuleCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    action: DraftRuleAction

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected draft reference must be a DraftRef")
        if not isinstance(self.action, DraftRuleAction):
            raise TypeError("Draft rule action is invalid")


@dataclass(frozen=True, slots=True)
class DraftRuleStagingResult:
    action: DraftRuleAction
    draft: DraftSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action, DraftRuleAction):
            raise TypeError("Draft rule action is invalid")
        if not isinstance(self.draft, DraftSnapshot):
            raise TypeError("Draft rule result requires a draft snapshot")
