"""Add reliability constraints and smart categorization foundations.

Revision ID: 0003_reliability_and_smart_input
Revises: 0002_ux_and_audit
"""

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_reliability_and_smart_input"
down_revision: str | None = "0002_ux_and_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKEN = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
_MAX_RULE_TOKENS = 5
_MAX_RULE_PATTERN_LENGTH = 200
_MAX_CATALOG_SLUG_LENGTH = 100


def _normalize_rule_pattern(value: str) -> str | None:
    """Apply the revision-pinned runtime rule policy to a legacy pattern.

    Invalid legacy rules cannot be matched safely by the current domain model, so
    callers discard them before the new NOT NULL/check constraints are installed.
    """
    folded = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    tokens = tuple(match.group(0) for match in _TOKEN.finditer(folded))
    if not tokens or len(tokens) > _MAX_RULE_TOKENS:
        return None
    normalized = " ".join(tokens)
    if len(normalized) > _MAX_RULE_PATTERN_LENGTH:
        return None
    return normalized


def _catalog_slug(value: str) -> str:
    """Mirror ``catalog_slug`` semantics as they existed for this revision."""
    return "-".join(value.casefold().strip().split())[:_MAX_CATALOG_SLUG_LENGTH]


def _unique_category_slug(base: str, row_id: object, occupied: set[str]) -> str:
    if base not in occupied:
        return base

    token = str(row_id).replace("-", "").casefold()
    attempt = 0
    while True:
        suffix = f"-{token}" if attempt == 0 else f"-{token}-{attempt}"
        prefix = base[: _MAX_CATALOG_SLUG_LENGTH - len(suffix)]
        candidate = f"{prefix}{suffix}"
        if candidate not in occupied:
            return candidate
        attempt += 1


def _temporary_category_slug(row_id: object, reserved: set[str]) -> str:
    token = str(row_id).replace("-", "").casefold()
    attempt = 0
    while True:
        candidate = f"__finbot_0003_{token}"
        if attempt:
            suffix = f"_{attempt}"
            candidate = f"{candidate[: _MAX_CATALOG_SLUG_LENGTH - len(suffix)]}{suffix}"
        if candidate not in reserved:
            return candidate
        attempt += 1


def _backfill_category_slugs(connection: sa.Connection) -> None:
    rows = list(
        connection.execute(
            sa.text(
                """
                SELECT id, user_id, kind, name, slug
                FROM categories
                ORDER BY user_id, kind, id
                """
            )
        ).mappings()
    )
    if not rows:
        return

    occupied_by_scope: dict[tuple[object, str], set[str]] = {}
    final_slugs: list[dict[str, object]] = []
    for row in rows:
        scope = (row["user_id"], str(row["kind"]))
        occupied = occupied_by_scope.setdefault(scope, set())
        base = _catalog_slug(str(row["slug"])) or _catalog_slug(str(row["name"]))
        if not base:
            base = _catalog_slug(f"category {row['id']}")
        slug = _unique_category_slug(base, row["id"], occupied)
        occupied.add(slug)
        final_slugs.append({"category_id": row["id"], "slug": slug})

    # Moving every row through a per-row temporary key avoids transient conflicts
    # with the legacy (user_id, kind, slug) unique constraint when canonical values
    # swap ownership or collide.
    reserved_by_scope: dict[tuple[object, str], set[str]] = {}
    for row in rows:
        scope = (row["user_id"], str(row["kind"]))
        reserved_by_scope.setdefault(scope, set()).add(str(row["slug"]))

    temporary_slugs: list[dict[str, object]] = []
    for row in rows:
        scope = (row["user_id"], str(row["kind"]))
        reserved = reserved_by_scope[scope]
        slug = _temporary_category_slug(row["id"], reserved)
        reserved.add(slug)
        temporary_slugs.append({"category_id": row["id"], "slug": slug})

    update = sa.text("UPDATE categories SET slug = :slug WHERE id = :category_id")
    connection.execute(update, temporary_slugs)
    connection.execute(update, final_slugs)


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0003_reliability_and_smart_input requires an online database connection "
            "for deterministic Unicode data normalization"
        )

    op.add_column(
        "drafts",
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "drafts",
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "drafts",
        sa.Column("suspended", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("drafts", sa.Column("presentation_ref", sa.String(length=64)))
    op.create_check_constraint("drafts_schema_version_check", "drafts", "schema_version >= 1")
    op.create_check_constraint("drafts_revision_check", "drafts", "revision >= 1")

    op.add_column(
        "categories",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint("categories_version_check", "categories", "version >= 1")
    op.create_check_constraint(
        "categories_kind_check", "categories", "kind IN ('expense', 'income')"
    )

    connection = op.get_bind()
    _backfill_category_slugs(connection)

    op.add_column("category_rules", sa.Column("kind", sa.String(length=10)))
    op.add_column("category_rules", sa.Column("normalized_pattern", sa.String(length=200)))
    op.add_column(
        "category_rules",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "category_rules",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "category_rules",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE category_rules AS rule
        SET kind = category.kind
        FROM categories AS category
        WHERE category.id = rule.category_id
        """
    )
    rules = list(
        connection.execute(sa.text("SELECT id, pattern FROM category_rules ORDER BY id")).mappings()
    )
    for rule in rules:
        normalized = _normalize_rule_pattern(str(rule["pattern"]))
        if normalized is None:
            connection.execute(
                sa.text("DELETE FROM category_rules WHERE id = :rule_id"),
                {"rule_id": rule["id"]},
            )
            continue
        connection.execute(
            sa.text("UPDATE category_rules SET normalized_pattern = :pattern WHERE id = :rule_id"),
            {"pattern": normalized, "rule_id": rule["id"]},
        )
    op.alter_column("category_rules", "kind", existing_type=sa.String(length=10), nullable=False)
    op.alter_column(
        "category_rules",
        "normalized_pattern",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.create_check_constraint(
        "category_rules_kind_check", "category_rules", "kind IN ('expense', 'income')"
    )
    op.create_check_constraint("category_rules_version_check", "category_rules", "version >= 1")
    op.create_check_constraint(
        "category_rules_normalized_pattern_check",
        "category_rules",
        "length(normalized_pattern) > 0",
    )
    # Older releases did not constrain rule uniqueness. Keep one deterministic row
    # per new scope key before adding partial unique indexes.
    op.execute(
        """
        DELETE FROM category_rules AS duplicate
        USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY user_id, kind, normalized_pattern, account_id
                       ORDER BY id
                   ) AS position
            FROM category_rules
        ) AS ranked
        WHERE duplicate.id = ranked.id AND ranked.position > 1
        """
    )

    op.create_unique_constraint("uq_accounts_id_user_id", "accounts", ["id", "user_id"])
    op.create_unique_constraint(
        "uq_categories_id_user_kind", "categories", ["id", "user_id", "kind"]
    )
    op.create_foreign_key(
        "fk_transactions_account_owner",
        "transactions",
        "accounts",
        ["account_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_foreign_key(
        "fk_transactions_category_owner_kind",
        "transactions",
        "categories",
        ["category_id", "user_id", "type"],
        ["id", "user_id", "kind"],
    )
    op.create_foreign_key(
        "fk_category_rules_account_owner",
        "category_rules",
        "accounts",
        ["account_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_foreign_key(
        "fk_category_rules_category_owner_kind",
        "category_rules",
        "categories",
        ["category_id", "user_id", "kind"],
        ["id", "user_id", "kind"],
    )

    op.create_check_constraint(
        "transactions_amount_minor_bigint_check",
        "transactions",
        "amount_minor <= 9223372036854775807",
    )
    op.create_check_constraint(
        "transactions_type_check", "transactions", "type IN ('expense', 'income')"
    )
    # Preserve one replay marker and clear only duplicate markers that could exist
    # before idempotency was enforced at the transaction table.
    op.execute(
        """
        UPDATE transactions AS duplicate
        SET telegram_update_id = NULL
        FROM (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY telegram_update_id
                       ORDER BY created_at, id
                   ) AS position
            FROM transactions
            WHERE telegram_update_id IS NOT NULL
        ) AS ranked
        WHERE duplicate.id = ranked.id AND ranked.position > 1
        """
    )
    op.create_index(
        "uq_transactions_telegram_update_id",
        "transactions",
        ["telegram_update_id"],
        unique=True,
        postgresql_where=sa.text("telegram_update_id IS NOT NULL"),
    )
    op.create_index(
        "ix_transactions_user_deleted",
        "transactions",
        ["user_id", "deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    op.create_index(
        "uq_category_rules_global_pattern",
        "category_rules",
        ["user_id", "kind", "normalized_pattern"],
        unique=True,
        postgresql_where=sa.text("account_id IS NULL"),
    )
    op.create_index(
        "uq_category_rules_account_pattern",
        "category_rules",
        ["user_id", "account_id", "kind", "normalized_pattern"],
        unique=True,
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_category_rules_account_pattern", table_name="category_rules")
    op.drop_index("uq_category_rules_global_pattern", table_name="category_rules")
    op.drop_index("ix_transactions_user_deleted", table_name="transactions")
    op.drop_index("uq_transactions_telegram_update_id", table_name="transactions")
    op.drop_constraint("transactions_type_check", "transactions", type_="check")
    op.drop_constraint("transactions_amount_minor_bigint_check", "transactions", type_="check")

    op.drop_constraint(
        "fk_category_rules_category_owner_kind", "category_rules", type_="foreignkey"
    )
    op.drop_constraint("fk_category_rules_account_owner", "category_rules", type_="foreignkey")
    op.drop_constraint("fk_transactions_category_owner_kind", "transactions", type_="foreignkey")
    op.drop_constraint("fk_transactions_account_owner", "transactions", type_="foreignkey")
    op.drop_constraint("uq_categories_id_user_kind", "categories", type_="unique")
    op.drop_constraint("uq_accounts_id_user_id", "accounts", type_="unique")

    op.drop_constraint("category_rules_normalized_pattern_check", "category_rules", type_="check")
    op.drop_constraint("category_rules_version_check", "category_rules", type_="check")
    op.drop_constraint("category_rules_kind_check", "category_rules", type_="check")
    op.drop_column("category_rules", "updated_at")
    op.drop_column("category_rules", "created_at")
    op.drop_column("category_rules", "version")
    op.drop_column("category_rules", "normalized_pattern")
    op.drop_column("category_rules", "kind")

    op.drop_constraint("categories_kind_check", "categories", type_="check")
    op.drop_constraint("categories_version_check", "categories", type_="check")
    op.drop_column("categories", "version")

    op.drop_constraint("drafts_revision_check", "drafts", type_="check")
    op.drop_constraint("drafts_schema_version_check", "drafts", type_="check")
    op.drop_column("drafts", "presentation_ref")
    op.drop_column("drafts", "suspended")
    op.drop_column("drafts", "revision")
    op.drop_column("drafts", "schema_version")
