from collections.abc import Mapping
from typing import Any
from uuid import UUID

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.draft_rules import (
    DraftRuleAction,
    DraftRuleStagingResult,
    StageDraftRuleCommand,
)
from finbot.application.dto import DraftRef, DraftSnapshot, UpdateDraftCommand
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.rules import validate_staged_rule
from finbot.application.use_cases.drafts import DraftUseCases

_REVIEW_STATES = frozenset({"wizard_confirm", "quick_confirm", "review"})


def _require_current(current: DraftSnapshot | None, expected: DraftRef) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    if current.suspended:
        raise InvalidStateError("Черновик приостановлен")
    if PENDING_DRAFT_INTENT_KEY in current.payload:
        raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
    if current.state not in _REVIEW_STATES:
        raise InvalidStateError("Предложение правила для этого экрана неактуально")
    return current


def _required_identifier(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ApplicationValidationError("Данные операции уже изменились")
    try:
        UUID(value)
    except ValueError:
        raise ApplicationValidationError("Данные операции уже изменились") from None
    return value


def _staged_rule(
    payload: Mapping[str, Any],
    action: DraftRuleAction,
) -> dict[str, str]:
    raw_pattern = payload.get("rule_offer_pattern")
    pattern = raw_pattern.strip() if isinstance(raw_pattern, str) else ""
    if not pattern:
        raise ApplicationValidationError("Предложение правила уже неактуально")

    scope = action.value
    validated = validate_staged_rule(
        pattern,
        scope,
        str(payload.get("description", "")),
        flow=str(payload.get("flow", "")),
        category_explicit=bool(payload.get("category_explicit")),
    )
    if validated is None:
        raise ApplicationValidationError("Предложение правила уже неактуально")

    account_id = _required_identifier(payload, "account_id")
    category_id = _required_identifier(payload, "category_id")
    _normalized_pattern, validated_scope = validated
    return {
        # Keep the offer text byte-for-byte compatible with the confirmation
        # revalidation, which compares it with pending_rule before normalizing.
        "pattern": pattern,
        "scope": validated_scope,
        "account_id": account_id,
        "category_id": category_id,
    }


class DraftRuleUseCases:
    """Stage or remove category-rule metadata on one exact reviewed draft."""

    __slots__ = ("_drafts",)

    def __init__(self, drafts: DraftUseCases) -> None:
        self._drafts = drafts

    async def execute(self, command: StageDraftRuleCommand) -> DraftRuleStagingResult:
        current = _require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
        )
        payload = dict(current.payload)
        if command.action is DraftRuleAction.REMOVE:
            payload.pop("pending_rule", None)
        else:
            payload["pending_rule"] = _staged_rule(payload, command.action)

        updated = await self._drafts.update(
            UpdateDraftCommand(
                owner_id=command.owner_id,
                expected=current.ref,
                state=current.state,
                payload=payload,
            )
        )
        return DraftRuleStagingResult(command.action, updated)
