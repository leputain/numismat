"""Add review-first recurring schedules and due instances.

Revision ID: 0009_recurring_transactions
Revises: 0008_budgets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_recurring_transactions"
down_revision: str | None = "0008_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _idempotency_result_kind_check(*, include_recurring: bool) -> sa.CheckConstraint:
    values = "'none', 'draft', 'transaction', 'account', 'category', 'budget'"
    if include_recurring:
        values += ", 'recurring_schedule', 'recurring_instance'"
    return sa.CheckConstraint(
        f"result_kind IS NULL OR result_kind IN ({values})",
        name="http_idempotency_result_kind_check",
    )


def upgrade() -> None:
    op.create_table(
        "recurring_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cadence", sa.String(length=10), nullable=False),
        sa.Column("interval", sa.SmallInteger(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("local_time", sa.Time(timezone=False), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("ends_on", sa.Date()),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column(
            "next_occurrence_index",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_due_local", sa.DateTime(timezone=False)),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("pause_reason", sa.String(length=32)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.UniqueConstraint("id", "user_id", name="uq_recurring_schedules_id_user_id"),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 60 AND name !~ '[[:cntrl:]]'",
            name="recurring_schedules_name_check",
        ),
        sa.CheckConstraint(
            "type IN ('expense', 'income')",
            name="recurring_schedules_type_check",
        ),
        sa.CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="recurring_schedules_amount_check",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="recurring_schedules_currency_check",
        ),
        sa.CheckConstraint(
            "(cadence = 'daily' AND interval BETWEEN 1 AND 365) OR "
            "(cadence = 'weekly' AND interval BETWEEN 1 AND 52) OR "
            "(cadence = 'monthly' AND interval BETWEEN 1 AND 24)",
            name="recurring_schedules_rule_check",
        ),
        sa.CheckConstraint(
            "EXTRACT(SECOND FROM local_time) = 0",
            name="recurring_schedules_minute_precision_check",
        ),
        sa.CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="recurring_schedules_timezone_check",
        ),
        sa.CheckConstraint(
            "ends_on IS NULL OR ends_on >= anchor_date",
            name="recurring_schedules_end_check",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="recurring_schedules_description_check",
        ),
        sa.CheckConstraint(
            "next_occurrence_index >= 0",
            name="recurring_schedules_occurrence_index_check",
        ),
        sa.CheckConstraint(
            "(next_due_local IS NULL) = (next_due_at IS NULL)",
            name="recurring_schedules_due_pair_check",
        ),
        sa.CheckConstraint(
            "pause_reason IS NULL OR (paused_at IS NOT NULL AND pause_reason IN "
            "('account_unavailable', 'category_unavailable', 'currency_mismatch', "
            "'schedule_invalid'))",
            name="recurring_schedules_pause_check",
        ),
        sa.CheckConstraint("version >= 1", name="recurring_schedules_version_check"),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="recurring_schedules_deletion_order_check",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_recurring_schedules_account_owner",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "user_id", "type"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_recurring_schedules_category_owner_kind",
        ),
    )
    op.create_index(
        "ix_recurring_schedules_due_active",
        "recurring_schedules",
        ["next_due_at", "id"],
        postgresql_where=sa.text(
            "deleted_at IS NULL AND paused_at IS NULL AND next_due_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_recurring_schedules_user_active",
        "recurring_schedules",
        ["user_id", "created_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_recurring_schedules_user_deleted",
        "recurring_schedules",
        ["user_id", "created_at", "id"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    op.create_table(
        "recurring_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("nominal_local", sa.DateTime(timezone=False), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("dst_adjusted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=32)),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drafts.id", ondelete="SET NULL"),
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("skipped_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.UniqueConstraint(
            "schedule_id",
            "occurrence_index",
            name="uq_recurring_instances_schedule_occurrence",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_recurring_instances_id_user_id"),
        sa.UniqueConstraint("draft_id", name="uq_recurring_instances_draft_id"),
        sa.CheckConstraint(
            "occurrence_index >= 0",
            name="recurring_instances_occurrence_index_check",
        ),
        sa.CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="recurring_instances_timezone_check",
        ),
        sa.CheckConstraint(
            "type IN ('expense', 'income')",
            name="recurring_instances_type_check",
        ),
        sa.CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="recurring_instances_amount_check",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="recurring_instances_currency_check",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="recurring_instances_description_check",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 32767",
            name="recurring_instances_attempt_count_check",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('account_unavailable', 'category_unavailable', 'currency_mismatch', "
            "'schedule_invalid')",
            name="recurring_instances_failure_code_check",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND generated_at IS NULL AND skipped_at IS NULL "
            "AND failure_code IS NULL AND draft_id IS NULL) OR "
            "(status = 'generated' AND generated_at IS NOT NULL AND skipped_at IS NULL "
            "AND failure_code IS NULL) OR "
            "(status = 'blocked' AND generated_at IS NULL AND skipped_at IS NULL "
            "AND failure_code IS NOT NULL AND draft_id IS NULL) OR "
            "(status = 'skipped' AND generated_at IS NULL AND skipped_at IS NOT NULL "
            "AND failure_code IS NULL AND draft_id IS NULL)",
            name="recurring_instances_state_check",
        ),
        sa.CheckConstraint("version >= 1", name="recurring_instances_version_check"),
        sa.ForeignKeyConstraint(
            ["schedule_id", "user_id"],
            ["recurring_schedules.id", "recurring_schedules.user_id"],
            name="fk_recurring_instances_schedule_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_recurring_instances_account_owner",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "user_id", "type"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_recurring_instances_category_owner_kind",
        ),
    )
    op.create_index(
        "ix_recurring_instances_pending_due",
        "recurring_instances",
        ["next_attempt_at", "user_id", "scheduled_for", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_recurring_instances_schedule_page",
        "recurring_instances",
        ["user_id", "schedule_id", "scheduled_for", "id"],
    )

    op.add_column(
        "transactions",
        sa.Column("recurring_instance_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_transactions_recurring_instance_owner",
        "transactions",
        "recurring_instances",
        ["recurring_instance_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_transactions_recurring_instance_id",
        "transactions",
        ["recurring_instance_id"],
    )
    op.create_check_constraint(
        "transactions_recurring_source_check",
        "transactions",
        "(recurring_instance_id IS NULL AND source <> 'recurring') OR "
        "(recurring_instance_id IS NOT NULL AND source = 'recurring')",
    )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    recurring_check = _idempotency_result_kind_check(include_recurring=True)
    op.create_check_constraint(
        recurring_check.name,
        "http_idempotency",
        recurring_check.sqltext,
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade recurring transactions in offline mode; "
            "a live database is required for the data guard"
        )
    op.execute(
        "LOCK TABLE recurring_schedules, recurring_instances, transactions, "
        "http_idempotency IN ACCESS EXCLUSIVE MODE"
    )
    retained_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM recurring_schedules) OR "
            "EXISTS (SELECT 1 FROM recurring_instances) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE recurring_instance_id IS NOT NULL) OR "
            "EXISTS (SELECT 1 FROM http_idempotency WHERE result_kind IN "
            "('recurring_schedule', 'recurring_instance'))"
        )
    )
    if retained_state:
        raise RuntimeError(
            "Cannot downgrade while recurring schedules, instances, transaction provenance, "
            "or recurring idempotency receipts exist; remove retained state first"
        )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    previous = _idempotency_result_kind_check(include_recurring=False)
    op.create_check_constraint(previous.name, "http_idempotency", previous.sqltext)

    op.drop_constraint(
        "transactions_recurring_source_check",
        "transactions",
        type_="check",
    )
    op.drop_constraint(
        "uq_transactions_recurring_instance_id",
        "transactions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_transactions_recurring_instance_owner",
        "transactions",
        type_="foreignkey",
    )
    op.drop_column("transactions", "recurring_instance_id")

    op.drop_index("ix_recurring_instances_schedule_page", table_name="recurring_instances")
    op.drop_index("ix_recurring_instances_pending_due", table_name="recurring_instances")
    op.drop_table("recurring_instances")
    op.drop_index("ix_recurring_schedules_user_deleted", table_name="recurring_schedules")
    op.drop_index("ix_recurring_schedules_user_active", table_name="recurring_schedules")
    op.drop_index("ix_recurring_schedules_due_active", table_name="recurring_schedules")
    op.drop_table("recurring_schedules")
