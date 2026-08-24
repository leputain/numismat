from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from finbot.application.draft_composition import ComposedDraftInput
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.draft_preparation import (
    PreparedDraftResult,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import (
    DraftConflictResolution,
    DraftRef,
    DraftSnapshot,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    ReviewedTransactionInput,
)
from finbot.application.errors import InvalidStateError
from finbot.domain.money import MoneyError, validate_minor
from finbot.domain.transactions import TransactionType

PENDING_DRAFT_INTENT_KEY = "pending_intent"
_MAX_QUICK_TEXT_LENGTH = 4096
_MAX_CATALOG_NAME_LENGTH = 100
_MAX_CATEGORY_EMOJI_LENGTH = 16
_SAFE_INVALID_INTENT_MESSAGE = "Новое действие устарело"
_FORBIDDEN_REPLACEMENT_KEYS = frozenset(
    {
        PENDING_DRAFT_INTENT_KEY,
        "history_page",
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
    }
)

_REPEAT_DATA_KEYS = frozenset(
    {
        "account_id",
        "account_name",
        "amount",
        "amount_minor",
        "category_emoji",
        "category_id",
        "category_name",
        "currency",
        "description",
        "flow",
        "occurred_at",
        "source_transaction_id",
        "source_version",
        "type",
    }
)
_REPEAT_LEGACY_METADATA_KEYS = frozenset(
    {
        "account_slug",
        "category_slug",
        "history_page",
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
    }
)
_EDIT_LEGACY_METADATA_KEYS = frozenset(
    {
        "history_page",
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
    }
)


class PendingDraftIntentKind(StrEnum):
    WIZARD = "wizard"
    QUICK = "quick"
    COMPOSE = "compose"
    REPEAT = "repeat"
    EDIT = "edit"


@dataclass(frozen=True, slots=True)
class PendingWizardIntent:
    kind: PendingDraftIntentKind = field(default=PendingDraftIntentKind.WIZARD, init=False)


@dataclass(frozen=True, slots=True)
class PendingQuickIntent:
    text: str = field(repr=False)
    kind: PendingDraftIntentKind = field(default=PendingDraftIntentKind.QUICK, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Quick draft text must be a string")
        if not self.text.strip() or len(self.text) > _MAX_QUICK_TEXT_LENGTH:
            raise ValueError("Quick draft text is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PendingComposeIntent:
    values: ComposedDraftInput = field(repr=False)
    kind: PendingDraftIntentKind = field(default=PendingDraftIntentKind.COMPOSE, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.values, ComposedDraftInput):
            raise TypeError("Composed draft intent is invalid")


@dataclass(frozen=True, slots=True)
class PendingRepeatIntent:
    transaction: ReviewedTransactionInput = field(repr=False)
    currency: str = field(repr=False)
    account_name: str = field(repr=False)
    category_name: str = field(repr=False)
    category_emoji: str = field(default="", repr=False)
    source_transaction_id: UUID | None = field(default=None, repr=False)
    source_version: int | None = field(default=None, repr=False)
    kind: PendingDraftIntentKind = field(default=PendingDraftIntentKind.REPEAT, init=False)

    @classmethod
    def from_prepared(cls, prepared: PreparedTransactionDraft) -> PendingRepeatIntent:
        if not isinstance(prepared, PreparedTransactionDraft):
            raise TypeError("Prepared repeat draft is invalid")
        return cls(
            transaction=prepared.transaction,
            currency=prepared.currency,
            account_name=prepared.account_name,
            category_name=prepared.category_name,
            category_emoji=prepared.category_emoji,
            source_transaction_id=prepared.source_transaction_id,
            source_version=prepared.source_version,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.transaction, ReviewedTransactionInput):
            raise TypeError("Repeated transaction must be reviewed input")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError("Repeated transaction currency is invalid")
        for value in (self.account_name, self.category_name):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _MAX_CATALOG_NAME_LENGTH
            ):
                raise ValueError("Repeated transaction catalog name is invalid")
        if (
            not isinstance(self.category_emoji, str)
            or len(self.category_emoji) > _MAX_CATEGORY_EMOJI_LENGTH
        ):
            raise ValueError("Repeated transaction category emoji is invalid")
        if (self.source_transaction_id is None) != (self.source_version is None):
            raise ValueError("Repeated transaction source reference is incomplete")
        if self.source_transaction_id is not None and not isinstance(
            self.source_transaction_id, UUID
        ):
            raise TypeError("Repeated transaction source id is invalid")
        if self.source_version is not None and (
            isinstance(self.source_version, bool)
            or not isinstance(self.source_version, int)
            or self.source_version < 1
        ):
            raise ValueError("Repeated transaction source version is invalid")

    def to_draft_payload(self) -> dict[str, object]:
        payload = dict(self.transaction.to_payload())
        payload.update(
            {
                "flow": "repeat",
                "currency": self.currency,
                "account_name": self.account_name,
                "category_name": self.category_name,
                "category_emoji": self.category_emoji,
            }
        )
        if self.source_transaction_id is not None and self.source_version is not None:
            payload.update(
                {
                    "source_transaction_id": str(self.source_transaction_id),
                    "source_version": self.source_version,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class PendingEditIntent:
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)
    kind: PendingDraftIntentKind = field(default=PendingDraftIntentKind.EDIT, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.transaction_id, UUID):
            raise TypeError("Edited transaction id must be a UUID")
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 1
        ):
            raise ValueError("Edited transaction version is invalid")


type PendingDraftIntent = (
    PendingWizardIntent
    | PendingQuickIntent
    | PendingComposeIntent
    | PendingRepeatIntent
    | PendingEditIntent
)


@dataclass(frozen=True, slots=True)
class PreparedDraftConflictReplacement:
    """A validated channel-neutral draft that may replace the exact active row."""

    state: str
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or not self.state or len(self.state) > 30:
            raise ValueError("Replacement draft state is invalid")
        forbidden = {
            key
            for key in self.payload
            if key in _FORBIDDEN_REPLACEMENT_KEYS or key == "slug" or key.endswith("_slug")
        }
        if forbidden:
            raise ValueError("Replacement draft payload contains adapter state")
        object.__setattr__(self, "payload", MappingProxyType(deepcopy(dict(self.payload))))


class QuickDraftConflictPreparer(Protocol):
    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult: ...


class DraftConflictReplacementTargets(Protocol):
    """Authoritative transaction checks executed while the owner/draft lock is held."""

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft: ...

    async def validate_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> None: ...


class DraftConflictReplacementPreparer(Protocol):
    async def prepare(
        self,
        owner_id: UUID,
        intent: PendingDraftIntent,
    ) -> PreparedDraftConflictReplacement: ...


def _invalid_intent() -> InvalidStateError:
    return InvalidStateError(_SAFE_INVALID_INTENT_MESSAGE)


def _require_exact_keys(value: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if set(value) - allowed:
        raise _invalid_intent()


def _required_string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid_intent()
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise _invalid_intent()
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise _invalid_intent()
    if parsed < 1:
        raise _invalid_intent()
    return parsed


def _decode_repeat(value: Mapping[str, Any]) -> PendingRepeatIntent:
    _require_exact_keys(value, frozenset({"kind", "payload", "state"}))
    state = value.get("state", "review")
    if state not in {"quick_confirm", "review"}:
        raise _invalid_intent()
    raw_payload = value.get("payload")
    if not isinstance(raw_payload, Mapping):
        raise _invalid_intent()
    _require_exact_keys(raw_payload, _REPEAT_DATA_KEYS | _REPEAT_LEGACY_METADATA_KEYS)
    if raw_payload.get("flow", "repeat") != "repeat":
        raise _invalid_intent()
    try:
        transaction = ReviewedTransactionInput.from_payload(raw_payload)
        currency = _required_string(raw_payload.get("currency"), maximum=3)
        account_name = _required_string(
            raw_payload.get("account_name"), maximum=_MAX_CATALOG_NAME_LENGTH
        )
        category_name = _required_string(
            raw_payload.get("category_name"), maximum=_MAX_CATALOG_NAME_LENGTH
        )
        category_emoji_raw = raw_payload.get("category_emoji", "")
        if not isinstance(category_emoji_raw, str):
            raise _invalid_intent()
        source_id_raw = raw_payload.get("source_transaction_id")
        source_version_raw = raw_payload.get("source_version")
        if source_id_raw is not None and not isinstance(source_id_raw, str):
            raise _invalid_intent()
        source_transaction_id = UUID(source_id_raw) if source_id_raw is not None else None
        source_version = (
            _positive_int(source_version_raw) if source_version_raw is not None else None
        )
        return PendingRepeatIntent(
            transaction=transaction,
            currency=currency,
            account_name=account_name,
            category_name=category_name,
            category_emoji=category_emoji_raw,
            source_transaction_id=source_transaction_id,
            source_version=source_version,
        )
    except InvalidStateError:
        raise
    except TypeError, ValueError:
        raise _invalid_intent() from None


def _decode_edit(value: Mapping[str, Any]) -> PendingEditIntent:
    _require_exact_keys(
        value,
        frozenset({"kind", "transaction_id", "version"}) | _EDIT_LEGACY_METADATA_KEYS,
    )
    try:
        transaction_id = UUID(str(value.get("transaction_id", "")))
        expected_version = _positive_int(value.get("version"))
        return PendingEditIntent(transaction_id, expected_version)
    except InvalidStateError:
        raise
    except TypeError, ValueError:
        raise _invalid_intent() from None


_COMPOSE_REQUIRED_KEYS = frozenset({"kind", "type", "amount_minor", "occurred_at", "description"})
_COMPOSE_OPTIONAL_KEYS = frozenset(
    {"account_id", "account_version", "category_id", "category_version"}
)


def _decode_compose_reference(
    value: Mapping[str, Any],
    *,
    id_key: str,
    version_key: str,
) -> DraftCatalogRef | None:
    id_present = id_key in value
    version_present = version_key in value
    if not id_present and not version_present:
        return None
    if id_present != version_present:
        raise _invalid_intent()
    raw_id = value.get(id_key)
    raw_version = value.get(version_key)
    if not isinstance(raw_id, str):
        raise _invalid_intent()
    try:
        entity_id = UUID(raw_id)
        if str(entity_id) != raw_id:
            raise _invalid_intent()
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise _invalid_intent()
        return DraftCatalogRef(entity_id, raw_version)
    except InvalidStateError:
        raise
    except TypeError, ValueError:
        raise _invalid_intent() from None


def _decode_compose(value: Mapping[str, Any]) -> PendingComposeIntent:
    _require_exact_keys(value, _COMPOSE_REQUIRED_KEYS | _COMPOSE_OPTIONAL_KEYS)
    if not _COMPOSE_REQUIRED_KEYS.issubset(value):
        raise _invalid_intent()
    raw_amount = value.get("amount_minor")
    raw_occurred = value.get("occurred_at")
    description = value.get("description")
    if (
        isinstance(raw_amount, bool)
        or not isinstance(raw_amount, int)
        or not isinstance(raw_occurred, str)
        or not isinstance(description, str)
        or len(description) > 500
    ):
        raise _invalid_intent()
    try:
        validate_minor(raw_amount)
        occurred_at = datetime.fromisoformat(raw_occurred)
        if occurred_at.utcoffset() is None or occurred_at.isoformat() != raw_occurred:
            raise _invalid_intent()
        values = ComposedDraftInput(
            kind=TransactionType(str(value.get("type", ""))),
            amount_minor=raw_amount,
            occurred_at=occurred_at,
            account=_decode_compose_reference(
                value,
                id_key="account_id",
                version_key="account_version",
            ),
            category=_decode_compose_reference(
                value,
                id_key="category_id",
                version_key="category_version",
            ),
            description=description,
        )
        return PendingComposeIntent(values)
    except InvalidStateError:
        raise
    except MoneyError, TypeError, ValueError:
        raise _invalid_intent() from None


def decode_pending_draft_intent(value: object) -> PendingDraftIntent:
    """Decode the bounded business intent while dropping known legacy UI metadata."""

    if not isinstance(value, Mapping):
        raise _invalid_intent()
    try:
        kind = PendingDraftIntentKind(str(value.get("kind", "")))
    except ValueError:
        raise _invalid_intent() from None
    if kind is PendingDraftIntentKind.WIZARD:
        _require_exact_keys(value, frozenset({"kind"}))
        return PendingWizardIntent()
    if kind is PendingDraftIntentKind.QUICK:
        _require_exact_keys(value, frozenset({"kind", "text"}))
        try:
            return PendingQuickIntent(
                _required_string(value.get("text"), maximum=_MAX_QUICK_TEXT_LENGTH)
            )
        except TypeError, ValueError:
            raise _invalid_intent() from None
    if kind is PendingDraftIntentKind.COMPOSE:
        return _decode_compose(value)
    if kind is PendingDraftIntentKind.REPEAT:
        return _decode_repeat(value)
    return _decode_edit(value)


def encode_pending_draft_intent(intent: PendingDraftIntent) -> dict[str, object]:
    if isinstance(intent, PendingWizardIntent):
        return {"kind": intent.kind.value}
    if isinstance(intent, PendingQuickIntent):
        return {"kind": intent.kind.value, "text": intent.text}
    if isinstance(intent, PendingComposeIntent):
        values = intent.values
        encoded: dict[str, object] = {
            "kind": intent.kind.value,
            "type": values.kind.value,
            "amount_minor": values.amount_minor,
            "occurred_at": values.occurred_at.isoformat(),
            "description": values.description,
        }
        if values.account is not None:
            encoded.update(
                {
                    "account_id": str(values.account.entity_id),
                    "account_version": values.account.version,
                }
            )
        if values.category is not None:
            encoded.update(
                {
                    "category_id": str(values.category.entity_id),
                    "category_version": values.category.version,
                }
            )
        return encoded
    if isinstance(intent, PendingRepeatIntent):
        return {
            "kind": intent.kind.value,
            "state": "review",
            "payload": intent.to_draft_payload(),
        }
    if isinstance(intent, PendingEditIntent):
        return {
            "kind": intent.kind.value,
            "transaction_id": str(intent.transaction_id),
            "version": intent.expected_version,
        }
    raise TypeError("Pending draft intent is invalid")


@dataclass(frozen=True, slots=True)
class ResolveDraftConflictCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    resolution: DraftConflictResolution

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected draft reference must be a DraftRef")
        if not isinstance(self.resolution, DraftConflictResolution):
            raise TypeError("Draft conflict resolution is invalid")


@dataclass(frozen=True, slots=True)
class DraftConflictResult:
    resolution: DraftConflictResolution
    draft: DraftSnapshot = field(repr=False)


class DraftConflictRepository(Protocol):
    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def lock_active(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> DraftSnapshot: ...

    async def continue_existing(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot: ...

    async def replace(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot: ...
