from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from finbot.adapters.database.models import Category, CategoryRule, Draft, Transaction
from finbot.application.rules import validate_staged_rule
from finbot.domain.category_rules import (
    CategoryRuleSpec,
    choose_category_rule,
    normalize_rule_pattern,
    phrase_occurs,
    suggest_rule_candidates,
    validate_rule_pattern,
)
from finbot.domain.transactions import TransactionType


def test_rule_normalization_is_nfkc_casefolded_and_yo_insensitive() -> None:
    assert normalize_rule_pattern("  КОФЕ\u0308   У ДОМА! ") == "кофе у дома"
    assert phrase_occurs("Кофе у дома", "кофё у дома")
    assert not phrase_occurs("за котиков", "кот")


def test_rule_pattern_must_be_short_and_present_as_whole_phrase() -> None:
    assert validate_rule_pattern("У дома", "Кофе у дома") == "у дома"
    with pytest.raises(ValueError, match="встречаться"):
        validate_rule_pattern("дом", "Кофе у дома")
    with pytest.raises(ValueError, match="не больше"):
        normalize_rule_pattern("один два три четыре пять шесть")


def test_rule_candidates_are_bounded_normalized_and_present() -> None:
    description = "Кофе в маленькой кофейне у дома"
    candidates = suggest_rule_candidates(description)
    assert 1 <= len(candidates) <= 4
    assert len(set(candidates)) == len(candidates)
    assert all(candidate == normalize_rule_pattern(candidate) for candidate in candidates)
    assert all(phrase_occurs(description, candidate) for candidate in candidates)


def test_staged_rule_must_match_the_final_reviewed_operation() -> None:
    assert validate_staged_rule(
        "кофе у дома",
        "account",
        "Кофе у дома",
        flow="quick",
        category_explicit=False,
    ) == ("кофе у дома", "account")
    assert (
        validate_staged_rule(
            "кофе у дома",
            "account",
            "обед в столовой",
            flow="quick",
            category_explicit=False,
        )
        is None
    )
    assert (
        validate_staged_rule(
            "кофе",
            "global",
            "кофе",
            flow="quick",
            category_explicit=True,
        )
        is None
    )


def test_rule_precedence_prefers_scope_specificity_and_recency() -> None:
    account_id = uuid7()
    category_ids = [uuid7() for _ in range(4)]
    now = datetime.now(UTC)
    rules = [
        CategoryRuleSpec(
            id=uuid7(),
            category_id=category_ids[0],
            kind=TransactionType.EXPENSE,
            normalized_pattern="кофе",
            updated_at=now,
        ),
        CategoryRuleSpec(
            id=uuid7(),
            category_id=category_ids[1],
            kind=TransactionType.EXPENSE,
            normalized_pattern="кофе у дома",
            updated_at=now - timedelta(days=1),
        ),
        CategoryRuleSpec(
            id=uuid7(),
            category_id=category_ids[2],
            kind=TransactionType.EXPENSE,
            normalized_pattern="кофе",
            account_id=account_id,
            updated_at=now - timedelta(days=2),
        ),
        CategoryRuleSpec(
            id=uuid7(),
            category_id=category_ids[3],
            kind=TransactionType.INCOME,
            normalized_pattern="кофе у дома",
            updated_at=now + timedelta(days=1),
        ),
    ]

    account_match = choose_category_rule(
        "Кофе у дома",
        rules,
        kind=TransactionType.EXPENSE,
        account_id=account_id,
    )
    assert account_match is not None and account_match.category_id == category_ids[2]
    global_match = choose_category_rule(
        "Кофе у дома", rules, kind=TransactionType.EXPENSE, account_id=uuid7()
    )
    assert global_match is not None and global_match.category_id == category_ids[1]


def test_schema_metadata_has_reliability_columns_constraints_and_indexes() -> None:
    assert {"schema_version", "revision", "suspended", "presentation_ref"} <= set(
        Draft.__table__.columns.keys()
    )
    assert "version" in Category.__table__.columns
    assert {"kind", "normalized_pattern", "version", "created_at", "updated_at"} <= set(
        CategoryRule.__table__.columns.keys()
    )
    transaction_indexes = {index.name for index in Transaction.__table__.indexes}
    assert "uq_transactions_telegram_update_id" in transaction_indexes
    assert "ix_transactions_user_deleted" in transaction_indexes
    rule_indexes = {index.name for index in CategoryRule.__table__.indexes}
    assert rule_indexes >= {
        "uq_category_rules_global_pattern",
        "uq_category_rules_account_pattern",
    }
