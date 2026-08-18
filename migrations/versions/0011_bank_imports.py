"""Add owner-scoped staged bank imports and transaction provenance.

Revision ID: 0011_bank_imports
Revises: 0010_exchange_rates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_bank_imports"
down_revision: str | None = "0010_exchange_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _idempotency_result_kind_check(*, include_bank_import: bool) -> sa.CheckConstraint:
    values = (
        "'none', 'draft', 'transaction', 'account', 'category', 'budget', "
        "'recurring_schedule', 'recurring_instance', 'exchange_rate_version'"
    )
    if include_bank_import:
        values += ", 'bank_import_batch', 'bank_import_row'"
    return sa.CheckConstraint(
        f"result_kind IS NULL OR result_kind IN ({values})",
        name="http_idempotency_result_kind_check",
    )


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("encoding", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="open",
            nullable=False,
        ),
        sa.Column("row_count", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "user_id", name="uq_import_batches_id_user_id"),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_import_batches_account_owner",
        ),
        sa.CheckConstraint(
            "profile = 'canonical_v1'",
            name="import_batches_profile_check",
        ),
        sa.CheckConstraint(
            "encoding IN ('utf-8', 'windows-1251')",
            name="import_batches_encoding_check",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')",
            name="import_batches_status_check",
        ),
        sa.CheckConstraint(
            "row_count BETWEEN 1 AND 2000",
            name="import_batches_row_count_check",
        ),
        sa.CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="import_batches_version_check",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at AND "
            "(completed_at IS NULL OR completed_at >= created_at) AND "
            "(cancelled_at IS NULL OR cancelled_at >= created_at)",
            name="import_batches_timestamp_order_check",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND completed_at IS NULL)",
            name="import_batches_state_check",
        ),
    )
    op.create_index(
        "ix_import_batches_owner_page",
        "import_batches",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_import_batches_owner_status_page",
        "import_batches",
        ["user_id", "status", "created_at", "id"],
    )

    op.create_table(
        "import_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("reference_digest", sa.LargeBinary()),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drafts.id", ondelete="SET NULL"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
            "batch_id",
            "position",
            name="uq_import_rows_batch_position",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_import_rows_id_user_id"),
        sa.UniqueConstraint("draft_id", name="uq_import_rows_draft_id"),
        sa.ForeignKeyConstraint(
            ["batch_id", "user_id"],
            ["import_batches.id", "import_batches.user_id"],
            name="fk_import_rows_batch_owner",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 2000",
            name="import_rows_position_check",
        ),
        sa.CheckConstraint(
            "type IN ('expense', 'income')",
            name="import_rows_type_check",
        ),
        sa.CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="import_rows_amount_check",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="import_rows_currency_check",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="import_rows_description_check",
        ),
        sa.CheckConstraint(
            "octet_length(fingerprint) = 32",
            name="import_rows_fingerprint_check",
        ),
        sa.CheckConstraint(
            "reference_digest IS NULL OR octet_length(reference_digest) = 32",
            name="import_rows_reference_digest_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'staged', 'confirmed', 'linked', 'skipped', 'cancelled')",
            name="import_rows_status_check",
        ),
        sa.CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="import_rows_version_check",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at AND (resolved_at IS NULL OR resolved_at >= created_at)",
            name="import_rows_timestamp_order_check",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND draft_id IS NULL AND resolved_at IS NULL) OR "
            "(status = 'staged' AND resolved_at IS NULL) OR "
            "(status IN ('confirmed', 'linked', 'skipped', 'cancelled') "
            "AND draft_id IS NULL AND resolved_at IS NOT NULL)",
            name="import_rows_state_check",
        ),
    )
    op.create_index(
        "ix_import_rows_owner_batch_page",
        "import_rows",
        ["user_id", "batch_id", "position", "id"],
    )
    op.create_index(
        "ix_import_rows_owner_batch_status_page",
        "import_rows",
        ["user_id", "batch_id", "status", "position", "id"],
    )
    op.create_index(
        "ix_import_rows_owner_fingerprint",
        "import_rows",
        ["user_id", "fingerprint", "id"],
    )
    op.create_index(
        "ix_import_rows_owner_reference_digest",
        "import_rows",
        ["user_id", "reference_digest", "id"],
        postgresql_where=sa.text("reference_digest IS NOT NULL"),
    )

    op.add_column(
        "transactions",
        sa.Column("import_row_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_transactions_import_row_owner",
        "transactions",
        "import_rows",
        ["import_row_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_transactions_import_row_id",
        "transactions",
        ["import_row_id"],
    )
    op.create_check_constraint(
        "transactions_source_check",
        "transactions",
        "source IN ('manual', 'recurring', 'bank_import')",
    )
    op.create_check_constraint(
        "transactions_import_source_check",
        "transactions",
        "(import_row_id IS NULL AND source <> 'bank_import') OR "
        "(import_row_id IS NOT NULL AND source = 'bank_import')",
    )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    bank_import_check = _idempotency_result_kind_check(include_bank_import=True)
    op.create_check_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        bank_import_check.sqltext,
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade bank imports in offline mode; "
            "a live database is required for the data guard"
        )
    op.execute(
        "LOCK TABLE http_idempotency, import_batches, import_rows, "
        "transactions IN ACCESS EXCLUSIVE MODE"
    )
    retained_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM import_batches) OR "
            "EXISTS (SELECT 1 FROM import_rows) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE import_row_id IS NOT NULL) OR "
            "EXISTS (SELECT 1 FROM http_idempotency WHERE result_kind IN "
            "('bank_import_batch', 'bank_import_row'))"
        )
    )
    if retained_state:
        raise RuntimeError(
            "Cannot downgrade while bank-import batches, rows, transaction provenance, "
            "or idempotency receipts exist; remove retained state first"
        )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    previous = _idempotency_result_kind_check(include_bank_import=False)
    op.create_check_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        previous.sqltext,
    )

    op.drop_constraint("transactions_import_source_check", "transactions", type_="check")
    op.drop_constraint("transactions_source_check", "transactions", type_="check")
    op.drop_constraint(
        "uq_transactions_import_row_id",
        "transactions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_transactions_import_row_owner",
        "transactions",
        type_="foreignkey",
    )
    op.drop_column("transactions", "import_row_id")

    op.drop_index(
        "ix_import_rows_owner_reference_digest",
        table_name="import_rows",
    )
    op.drop_index("ix_import_rows_owner_fingerprint", table_name="import_rows")
    op.drop_index(
        "ix_import_rows_owner_batch_status_page",
        table_name="import_rows",
    )
    op.drop_index("ix_import_rows_owner_batch_page", table_name="import_rows")
    op.drop_table("import_rows")
    op.drop_index(
        "ix_import_batches_owner_status_page",
        table_name="import_batches",
    )
    op.drop_index("ix_import_batches_owner_page", table_name="import_batches")
    op.drop_table("import_batches")
