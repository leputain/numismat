from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from finbot.application.draft_navigation import (
    DraftCatalogChoice,
    DraftDateChoice,
)
from finbot.application.draft_rules import DraftRuleAction
from finbot.application.dto import DraftRef, DraftSnapshot, TransactionMutationResult
from finbot.domain.transactions import TransactionType


class DraftPatchAction(StrEnum):
    INPUT_TEXT = "input_text"
    EDIT_TYPE = "edit_type"
    EDIT_AMOUNT = "edit_amount"
    EDIT_CATEGORY = "edit_category"
    EDIT_ACCOUNT = "edit_account"
    EDIT_DATE = "edit_date"
    EDIT_DESCRIPTION = "edit_description"
    SKIP_DESCRIPTION = "skip_description"
    BACK = "back"
    SELECT_TYPE = "select_type"
    SELECT_EDIT_TYPE = "select_edit_type"
    SELECT_CATEGORY = "select_category"
    SELECT_ACCOUNT = "select_account"
    SELECT_DATE = "select_date"
    SET_RULE = "set_rule"


@dataclass(frozen=True, slots=True)
class DraftExistingSelection:
    entity_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, UUID):
            raise TypeError("selection id must be a UUID")
        if type(self.version) is not int or not 1 <= self.version <= 2**31 - 1:
            raise ValueError("selection version must be a positive integer")


def _validate_patch_fields(
    expected: DraftRef,
    action: DraftPatchAction,
    text: str | None,
    value: TransactionType | DraftDateChoice | DraftRuleAction | None,
    selection: DraftExistingSelection | DraftCatalogChoice | None,
) -> None:
    if not isinstance(expected, DraftRef):
        raise TypeError("expected draft reference must be a DraftRef")
    if (
        not isinstance(expected.draft_id, UUID)
        or type(expected.revision) is not int
        or not 1 <= expected.revision <= 2**31 - 1
    ):
        raise ValueError("expected draft reference is invalid")
    if not isinstance(action, DraftPatchAction):
        raise TypeError("draft patch action is invalid")

    if action is DraftPatchAction.INPUT_TEXT:
        valid = type(text) is str and len(text) <= 4096 and value is None and selection is None
    elif action is DraftPatchAction.SELECT_TYPE:
        valid = text is None and isinstance(value, TransactionType) and selection is None
    elif action is DraftPatchAction.SELECT_EDIT_TYPE:
        valid = (
            text is None
            and isinstance(value, TransactionType)
            and isinstance(selection, DraftExistingSelection)
        )
    elif action is DraftPatchAction.SELECT_DATE:
        valid = text is None and isinstance(value, DraftDateChoice) and selection is None
    elif action is DraftPatchAction.SET_RULE:
        valid = text is None and isinstance(value, DraftRuleAction) and selection is None
    elif action in {
        DraftPatchAction.SELECT_CATEGORY,
        DraftPatchAction.SELECT_ACCOUNT,
    }:
        valid = (
            text is None
            and value is None
            and (
                isinstance(selection, DraftExistingSelection)
                or selection is DraftCatalogChoice.CUSTOM
            )
        )
    else:
        valid = text is None and value is None and selection is None
    if not valid:
        raise TypeError("draft patch fields do not match the selected action")


@dataclass(frozen=True, slots=True)
class DraftPatchSpec:
    expected: DraftRef = field(repr=False)
    action: DraftPatchAction
    text: str | None = field(default=None, repr=False)
    value: TransactionType | DraftDateChoice | DraftRuleAction | None = field(
        default=None,
        repr=False,
    )
    selection: DraftExistingSelection | DraftCatalogChoice | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _validate_patch_fields(
            self.expected,
            self.action,
            self.text,
            self.value,
            self.selection,
        )

    def bind(self, owner_id: UUID) -> DraftPatchCommand:
        return DraftPatchCommand(
            owner_id=owner_id,
            expected=self.expected,
            action=self.action,
            text=self.text,
            value=self.value,
            selection=self.selection,
        )


@dataclass(frozen=True, slots=True)
class DraftPatchCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    action: DraftPatchAction
    text: str | None = field(default=None, repr=False)
    value: TransactionType | DraftDateChoice | DraftRuleAction | None = field(
        default=None,
        repr=False,
    )
    selection: DraftExistingSelection | DraftCatalogChoice | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("draft owner id must be a UUID")
        _validate_patch_fields(
            self.expected,
            self.action,
            self.text,
            self.value,
            self.selection,
        )


@dataclass(frozen=True, slots=True)
class DraftPatchResult:
    draft: DraftSnapshot | None = field(default=None, repr=False)
    transaction: TransactionMutationResult | None = field(default=None, repr=False)
    closed: bool = False

    def __post_init__(self) -> None:
        if type(self.closed) is not bool:
            raise TypeError("draft patch closed flag must be boolean")
        if self.draft is not None and not isinstance(self.draft, DraftSnapshot):
            raise TypeError("draft patch draft outcome is invalid")
        if self.transaction is not None and not isinstance(
            self.transaction,
            TransactionMutationResult,
        ):
            raise TypeError("draft patch transaction outcome is invalid")
        populated = (
            int(self.draft is not None) + int(self.transaction is not None) + int(self.closed)
        )
        if populated != 1:
            raise ValueError("draft patch result must contain exactly one outcome")
