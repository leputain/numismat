from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingDraftIntentKind,
    decode_pending_draft_intent,
)
from finbot.application.dto import DraftSnapshot
from finbot.domain.money import MoneyError, validate_minor
from finbot.domain.transactions import TransactionType

MAX_PUBLIC_REVISION = 2**31 - 1
SUPPORTED_DRAFT_SCHEMA_VERSION = 1


class PublicDraftState(StrEnum):
    WIZARD_TYPE = "wizard_type"
    WIZARD_AMOUNT = "wizard_amount"
    WIZARD_CATEGORY = "wizard_category"
    CUSTOM_CATEGORY = "custom_category"
    WIZARD_ACCOUNT = "wizard_account"
    CUSTOM_ACCOUNT = "custom_account"
    WIZARD_DATE = "wizard_date"
    CUSTOM_DATE = "custom_date"
    WIZARD_DESCRIPTION = "wizard_description"
    WIZARD_CONFIRM = "wizard_confirm"
    QUICK_CATEGORY = "quick_category"
    CATEGORY_REQUIRED = "category_required"
    QUICK_ACCOUNT = "quick_account"
    ACCOUNT_REQUIRED = "account_required"
    QUICK_CONFIRM = "quick_confirm"
    REVIEW = "review"
    REVIEW_TYPE = "review_type"
    REVIEW_AMOUNT = "review_amount"
    REVIEW_CATEGORY = "review_category"
    REVIEW_ACCOUNT = "review_account"
    REVIEW_DATE = "review_date"
    REVIEW_DATE_INPUT = "review_date_input"
    EDIT_MENU = "edit_menu"
    EDIT_TYPE = "edit_type"
    EDIT_AMOUNT = "edit_amount"
    EDIT_CATEGORY = "edit_category"
    EDIT_ACCOUNT = "edit_account"
    EDIT_DATE_MENU = "edit_date_menu"
    EDIT_DATE = "edit_date"
    EDIT_DESCRIPTION = "edit_description"
    UNSUPPORTED = "unsupported"


class PublicDraftFlow(StrEnum):
    WIZARD = "wizard"
    QUICK = "quick"
    REPEAT = "repeat"
    RECURRING = "recurring"
    BANK_IMPORT = "bank_import"
    TRANSACTION_EDIT = "transaction_edit"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class DraftCatalogView:
    entity_id: UUID = field(repr=False)
    name: str = field(repr=False)
    emoji: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DraftTransactionView:
    kind: TransactionType | None = field(default=None, repr=False)
    amount_minor: int | None = field(default=None, repr=False)
    currency: str | None = field(default=None, repr=False)
    account: DraftCatalogView | None = field(default=None, repr=False)
    category: DraftCatalogView | None = field(default=None, repr=False)
    occurred_at: datetime | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DraftEditTargetView:
    transaction_id: UUID = field(repr=False)
    version: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftConflictView:
    pending_kind: PendingDraftIntentKind


@dataclass(frozen=True, slots=True)
class DraftRuleView:
    offered: bool
    selection: Literal["global", "account"] | None


@dataclass(frozen=True, slots=True)
class PublicDraftView:
    draft_id: UUID = field(repr=False)
    revision: int = field(repr=False)
    state: PublicDraftState
    suspended: bool
    flow: PublicDraftFlow
    transaction: DraftTransactionView | None = field(default=None, repr=False)
    edit_target: DraftEditTargetView | None = field(default=None, repr=False)
    conflict: DraftConflictView | None = field(default=None, repr=False)
    rule: DraftRuleView | None = field(default=None, repr=False)


def _invalid() -> ValueError:
    return ValueError("draft cannot be projected safely")


def _optional_string(payload: dict[str, Any], key: str, *, maximum: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not str or len(value) > maximum:
        raise _invalid()
    return value


def _optional_uuid(payload: dict[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not str:
        raise _invalid()
    try:
        parsed = UUID(value)
    except ValueError:
        raise _invalid() from None
    if str(parsed) != value:
        raise _invalid()
    return parsed


def _optional_positive_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) not in {int, str}:
        raise _invalid()
    if type(value) is str and (not value.isascii() or not value.isdecimal()):
        raise _invalid()
    parsed = int(value)
    if not 1 <= parsed <= MAX_PUBLIC_REVISION:
        raise _invalid()
    return parsed


def _catalog(
    payload: dict[str, Any],
    *,
    id_key: str,
    name_key: str,
    emoji_key: str | None = None,
) -> DraftCatalogView | None:
    entity_id = _optional_uuid(payload, id_key)
    name = _optional_string(payload, name_key, maximum=100)
    emoji = _optional_string(payload, emoji_key, maximum=16) if emoji_key else None
    if entity_id is None and name is None and emoji is None:
        return None
    if entity_id is None or name is None or not name:
        raise _invalid()
    return DraftCatalogView(entity_id, name, emoji)


def _transaction(payload: dict[str, Any]) -> DraftTransactionView | None:
    raw_kind = payload.get("type")
    kind: TransactionType | None = None
    if raw_kind is not None:
        if type(raw_kind) is not str:
            raise _invalid()
        try:
            kind = TransactionType(raw_kind)
        except MoneyError, ValueError:
            raise _invalid() from None

    raw_amount = payload.get("amount_minor", payload.get("amount"))
    amount: int | None = None
    if raw_amount is not None:
        if type(raw_amount) not in {int, str}:
            raise _invalid()
        if type(raw_amount) is str and (not raw_amount.isascii() or not raw_amount.isdecimal()):
            raise _invalid()
        try:
            amount = validate_minor(int(raw_amount))
        except ValueError:
            raise _invalid() from None

    currency = _optional_string(payload, "currency", maximum=3)
    if currency is not None and (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise _invalid()
    occurred_raw = payload.get("occurred_at")
    occurred_at: datetime | None = None
    if occurred_raw is not None:
        if type(occurred_raw) is not str:
            raise _invalid()
        try:
            occurred_at = datetime.fromisoformat(occurred_raw)
        except ValueError:
            raise _invalid() from None
        if occurred_at.utcoffset() is None:
            raise _invalid()
    description = _optional_string(payload, "description", maximum=500)
    account = _catalog(payload, id_key="account_id", name_key="account_name")
    category = _catalog(
        payload,
        id_key="category_id",
        name_key="category_name",
        emoji_key="category_emoji",
    )
    if all(
        item is None
        for item in (kind, amount, currency, account, category, occurred_at, description)
    ):
        return None
    return DraftTransactionView(
        kind=kind,
        amount_minor=amount,
        currency=currency,
        account=account,
        category=category,
        occurred_at=occurred_at,
        description=description,
    )


def _unsupported(draft: DraftSnapshot, conflict: DraftConflictView | None) -> PublicDraftView:
    return PublicDraftView(
        draft_id=draft.draft_id,
        revision=draft.revision,
        state=PublicDraftState.UNSUPPORTED,
        suspended=draft.suspended,
        flow=PublicDraftFlow.UNSUPPORTED,
        conflict=conflict,
    )


def project_public_draft(draft: DraftSnapshot) -> PublicDraftView:
    """Fail closed to a bounded view without exposing the persistent payload."""

    if (
        not isinstance(draft.draft_id, UUID)
        or type(draft.revision) is not int
        or not 1 <= draft.revision <= MAX_PUBLIC_REVISION
        or type(draft.suspended) is not bool
    ):
        raise _invalid()
    if (
        type(draft.schema_version) is not int
        or draft.schema_version != SUPPORTED_DRAFT_SCHEMA_VERSION
    ):
        return _unsupported(draft, None)
    payload = dict(draft.payload)
    conflict: DraftConflictView | None = None
    raw_pending = payload.get(PENDING_DRAFT_INTENT_KEY)
    if raw_pending is not None:
        try:
            conflict = DraftConflictView(decode_pending_draft_intent(raw_pending).kind)
        except Exception:
            return _unsupported(draft, None)

    if "ocr_batch" in payload or draft.state.startswith("settings_"):
        return _unsupported(draft, conflict)
    try:
        state = PublicDraftState(draft.state)
    except ValueError:
        return _unsupported(draft, conflict)
    if state is PublicDraftState.UNSUPPORTED:
        return _unsupported(draft, conflict)

    is_edit = state.value.startswith("edit_")
    if is_edit:
        transaction_id = _optional_uuid(payload, "transaction_id")
        version = _optional_positive_int(payload, "version")
        if transaction_id is None or version is None:
            return _unsupported(draft, conflict)
        return PublicDraftView(
            draft_id=draft.draft_id,
            revision=draft.revision,
            state=state,
            suspended=draft.suspended,
            flow=PublicDraftFlow.TRANSACTION_EDIT,
            edit_target=DraftEditTargetView(transaction_id, version),
            conflict=conflict,
        )

    raw_flow = payload.get("flow")
    if type(raw_flow) is not str:
        return _unsupported(draft, conflict)
    try:
        flow = PublicDraftFlow(raw_flow)
    except ValueError:
        return _unsupported(draft, conflict)
    if flow is PublicDraftFlow.TRANSACTION_EDIT or flow is PublicDraftFlow.UNSUPPORTED:
        return _unsupported(draft, conflict)
    if flow is PublicDraftFlow.BANK_IMPORT and state not in {
        PublicDraftState.REVIEW,
        PublicDraftState.REVIEW_CATEGORY,
        PublicDraftState.WIZARD_DESCRIPTION,
    }:
        return _unsupported(draft, conflict)
    try:
        transaction = _transaction(payload)
        offered = bool(_optional_string(payload, "rule_offer_pattern", maximum=500))
        pending_rule = payload.get("pending_rule")
        selection: Literal["global", "account"] | None = None
        if pending_rule is not None:
            if not isinstance(pending_rule, dict):
                raise _invalid()
            raw_scope = pending_rule.get("scope")
            if raw_scope not in {"global", "account"}:
                raise _invalid()
            selection = raw_scope
        if selection is not None and not offered:
            raise _invalid()
        if flow is PublicDraftFlow.BANK_IMPORT and (offered or selection is not None):
            raise _invalid()
        rule = DraftRuleView(offered=offered, selection=selection) if offered else None
    except ValueError:
        return _unsupported(draft, conflict)
    return PublicDraftView(
        draft_id=draft.draft_id,
        revision=draft.revision,
        state=state,
        suspended=draft.suspended,
        flow=flow,
        transaction=transaction,
        conflict=conflict,
        rule=rule,
    )
