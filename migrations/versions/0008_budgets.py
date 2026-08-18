"""Add owner-scoped expense budgets.

Revision ID: 0008_budgets
Revises: 0007_http_security_state
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_budgets"
down_revision: str | None = "0007_http_security_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _idempotency_result_kind_check(*, include_budget: bool) -> sa.CheckConstraint:
    values = "'none', 'draft', 'transaction', 'account', 'category'"
    if include_budget:
        values += ", 'budget'"
    return sa.CheckConstraint(
        f"result_kind IS NULL OR result_kind IN ({values})",
        name="http_idempotency_result_kind_check",
    )


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("limit_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "category_kind",
            sa.String(length=10),
            server_default=sa.text("'expense'"),
            nullable=False,
        ),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 60 AND name !~ '[[:cntrl:]]'",
            name="budgets_name_check",
        ),
        sa.CheckConstraint(
            "limit_minor BETWEEN 1 AND 9223372036854775807",
            name="budgets_limit_minor_check",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="budgets_currency_check"),
        sa.CheckConstraint("category_kind = 'expense'", name="budgets_category_kind_check"),
        sa.CheckConstraint(
            "ends_on >= starts_on AND ends_on < starts_on + 366",
            name="budgets_period_check",
        ),
        sa.CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="budgets_timezone_check",
        ),
        sa.CheckConstraint("version >= 1", name="budgets_version_check"),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="budgets_deletion_order_check",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "user_id", "category_kind"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_budgets_category_owner_kind",
        ),
    )
    op.create_index(
        "ix_budgets_user_period_active",
        "budgets",
        ["user_id", "starts_on", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_budgets_user_period_deleted",
        "budgets",
        ["user_id", "starts_on", "id"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    op.create_check_constraint(
        _idempotency_result_kind_check(include_budget=True).name,
        "http_idempotency",
        _idempotency_result_kind_check(include_budget=True).sqltext,
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade budgets in offline mode; "
            "a live database is required for the data guard"
        )
    op.execute("LOCK TABLE budgets, http_idempotency IN ACCESS EXCLUSIVE MODE")
    retained_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM budgets) OR EXISTS ("
            "SELECT 1 FROM http_idempotency WHERE result_kind = 'budget'"
            ")"
        )
    )
    if retained_state:
        raise RuntimeError(
            "Cannot downgrade while budgets or budget idempotency receipts exist; "
            "delete both retained datasets first"
        )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    previous = _idempotency_result_kind_check(include_budget=False)
    op.create_check_constraint(previous.name, "http_idempotency", previous.sqltext)
    op.drop_index("ix_budgets_user_period_deleted", table_name="budgets")
    op.drop_index("ix_budgets_user_period_active", table_name="budgets")
    op.drop_table("budgets")
