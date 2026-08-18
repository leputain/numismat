from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.draft_conflicts import PendingQuickIntent
from finbot.application.draft_navigation import DRAFT_NAVIGATION_ALLOWED_STATES
from finbot.application.draft_preparation import (
    PreparedDraftResult,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import (
    DraftSnapshot,
    OwnerSnapshot,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    TransactionSnapshot,
)
from finbot.application.errors import InvalidStateError
from finbot.application.finance_draft_text_input import FINANCE_DRAFT_TEXT_INPUT_STATES
from finbot.application.transaction_draft_navigation import (
    TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES,
    TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES,
)

SETTINGS_DRAFT_TEXT_INPUT_STATES = frozenset(
    {
        "settings_account_create",
        "settings_account_rename",
        "settings_category_create",
        "settings_category_rename",
    }
)
TRANSACTION_DRAFT_TEXT_INPUT_STATES = frozenset(
    {
        "edit_amount",
        "edit_date",
        "edit_description",
    }
)
QUICK_DRAFT_INGRESS_DELEGATED_STATES = frozenset(
    FINANCE_DRAFT_TEXT_INPUT_STATES
    | SETTINGS_DRAFT_TEXT_INPUT_STATES
    | TRANSACTION_DRAFT_TEXT_INPUT_STATES
)

_FINANCE_NAVIGATION_STATES = frozenset(
    state for states in DRAFT_NAVIGATION_ALLOWED_STATES.values() for state in states
)
_TRANSACTION_NAVIGATION_STATES = frozenset(
    {state for states in TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES.values() for state in states}
    | set(TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES.values())
)
QUICK_DRAFT_INGRESS_CONFLICT_STATES = frozenset(
    (_FINANCE_NAVIGATION_STATES | _TRANSACTION_NAVIGATION_STATES)
    - QUICK_DRAFT_INGRESS_DELEGATED_STATES
)


class DraftIngressOperation(StrEnum):
    WIZARD = "wizard"
    QUICK = "quick"
    REPEAT = "repeat"
    LOCAL_AI = "local_ai"


class DraftIngressStatus(StrEnum):
    STARTED = "started"
    CONFLICT = "conflict"
    BLOCKED_BANK_IMPORT = "blocked_bank_import"


class BankImportDraftIngressBlockedError(InvalidStateError):
    """A generic ingress must not attach intent state to an import review draft."""

    def __init__(
        self,
        owner: OwnerSnapshot,
        draft: DraftSnapshot,
        transaction: TransactionSnapshot | None = None,
    ) -> None:
        super().__init__("Сначала завершите банковскую строку")
        if draft.payload.get("flow") != "bank_import":
            raise ValueError("Blocked draft must be a bank import")
        self.owner = owner
        self.draft = draft
        self.transaction = transaction


@dataclass(frozen=True, slots=True)
class BeginWizardDraftCommand:
    owner_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")


@dataclass(frozen=True, slots=True)
class BeginQuickDraftCommand:
    owner_id: UUID = field(repr=False)
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        # Keep the boundary identical to the persisted conflict-intent codec.
        # Construction validates type, non-blank content, and the 4096-char cap.
        PendingQuickIntent(self.text)


@dataclass(frozen=True, slots=True)
class BeginRepeatDraftCommand:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(self.transaction_id, UUID):
            raise TypeError("Repeat draft identifiers must be UUIDs")
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 1
        ):
            raise ValueError("Repeat transaction version must be positive")


@dataclass(frozen=True, slots=True)
class DraftIngressResult:
    operation: DraftIngressOperation
    status: DraftIngressStatus
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, DraftIngressOperation):
            raise TypeError("Draft ingress operation is invalid")
        if not isinstance(self.status, DraftIngressStatus):
            raise TypeError("Draft ingress status is invalid")
        if not isinstance(self.owner, OwnerSnapshot):
            raise TypeError("Draft ingress owner snapshot is invalid")
        if not isinstance(self.draft, DraftSnapshot):
            raise TypeError("Draft ingress snapshot is invalid")


class DraftIngressOwnerQuery(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...


class DraftIngressRepeatPreparer(Protocol):
    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft: ...


class DraftIngressClock(Protocol):
    def now(self, timezone: str) -> datetime: ...


class DraftIngressQuickPreparer(Protocol):
    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult: ...


class QuickDraftIngressNotApplicableError(InvalidStateError):
    """The submitted text belongs to another bounded active-draft controller."""
