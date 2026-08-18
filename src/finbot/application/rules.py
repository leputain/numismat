from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.domain.category_rules import (
    CategoryRuleSpec,
    choose_category_rule,
    suggest_rule_candidates,
    validate_rule_pattern,
)
from finbot.domain.transactions import TransactionType

MAX_CATEGORY_RULES_PER_OWNER_KIND = 512
CATEGORY_RULE_FETCH_LIMIT = MAX_CATEGORY_RULES_PER_OWNER_KIND + 1


class CategoryRuleReader(Protocol):
    async def list_applicable(
        self,
        user_id: UUID,
        kind: TransactionType,
        account_id: UUID | None,
    ) -> tuple[CategoryRuleSpec, ...]: ...


class CategoryRuleWriter(Protocol):
    async def upsert(
        self,
        user_id: UUID,
        kind: TransactionType,
        category_id: UUID,
        normalized_pattern: str,
        account_id: UUID | None,
    ) -> CategoryRuleSpec: ...


@dataclass(frozen=True, slots=True)
class CategorizationDecision:
    category_id: UUID = field(repr=False)
    rule_id: UUID = field(repr=False)


async def resolve_learned_category(
    reader: CategoryRuleReader,
    *,
    user_id: UUID,
    kind: TransactionType,
    account_id: UUID | None,
    description: str,
) -> CategorizationDecision | None:
    rules = await reader.list_applicable(user_id, kind, account_id)
    selected = choose_category_rule(
        description,
        rules,
        kind=kind,
        account_id=account_id,
    )
    if selected is None:
        return None
    return CategorizationDecision(category_id=selected.category_id, rule_id=selected.id)


def learning_candidates(description: str) -> tuple[str, ...]:
    return suggest_rule_candidates(description, limit=4)


def prepare_rule_pattern(pattern: str, description: str) -> str:
    return validate_rule_pattern(pattern, description)


def validate_staged_rule(
    pattern: str,
    scope: str,
    description: str,
    *,
    flow: str,
    category_explicit: bool,
) -> tuple[str, str] | None:
    """Validate draft learning metadata against the final reviewed operation."""
    if flow != "quick" or category_explicit or scope not in {"global", "account"}:
        return None
    try:
        normalized = prepare_rule_pattern(pattern, description)
    except ValueError:
        return None
    return normalized, scope
