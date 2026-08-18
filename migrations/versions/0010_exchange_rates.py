"""Add immutable owner-scoped manual exchange-rate versions.

Revision ID: 0010_exchange_rates
Revises: 0009_recurring_transactions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_exchange_rates"
down_revision: str | None = "0009_recurring_transactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _idempotency_result_kind_check(*, include_exchange_rate: bool) -> sa.CheckConstraint:
    values = (
        "'none', 'draft', 'transaction', 'account', 'category', 'budget', "
        "'recurring_schedule', 'recurring_instance'"
    )
    if include_exchange_rate:
        values += ", 'exchange_rate_version'"
    return sa.CheckConstraint(
        f"result_kind IS NULL OR result_kind IN ({values})",
        name="http_idempotency_result_kind_check",
    )


def upgrade() -> None:
    op.create_table(
        "exchange_rate_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_currency", sa.String(length=3), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
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
            "user_id",
            "kind",
            "target_currency",
            name="uq_exchange_rate_sources_owner_kind_target",
        ),
        sa.UniqueConstraint(
            "id",
            "user_id",
            "target_currency",
            name="uq_exchange_rate_sources_id_owner_target",
        ),
        sa.CheckConstraint("kind = 'manual'", name="exchange_rate_sources_kind_check"),
        sa.CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_sources_target_currency_check",
        ),
        sa.CheckConstraint(
            "latest_version BETWEEN 1 AND 2147483647",
            name="exchange_rate_sources_latest_version_check",
        ),
    )
    op.create_index(
        "ix_exchange_rate_sources_owner_target",
        "exchange_rate_sources",
        ["user_id", "target_currency", "id"],
    )

    op.create_table(
        "exchange_rate_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_currency", sa.String(length=3), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_id",
            "version",
            name="uq_exchange_rate_versions_source_version",
        ),
        sa.UniqueConstraint(
            "id",
            "target_currency",
            name="uq_exchange_rate_versions_id_target",
        ),
        sa.CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_versions_target_currency_check",
        ),
        sa.CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="exchange_rate_versions_version_check",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id", "target_currency"],
            [
                "exchange_rate_sources.id",
                "exchange_rate_sources.user_id",
                "exchange_rate_sources.target_currency",
            ],
            name="fk_exchange_rate_versions_source_owner_target",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_exchange_rate_versions_source_page",
        "exchange_rate_versions",
        ["source_id", "created_at", "id"],
    )

    op.create_table(
        "exchange_rate_entries",
        sa.Column(
            "rate_version_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("source_currency", sa.String(length=3), primary_key=True),
        sa.Column("target_currency", sa.String(length=3), nullable=False),
        sa.Column("coefficient", sa.BigInteger(), nullable=False),
        sa.Column("scale", sa.SmallInteger(), nullable=False),
        sa.Column("source_minor_digits", sa.SmallInteger(), nullable=False),
        sa.Column("target_minor_digits", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint(
            "source_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_entries_source_currency_check",
        ),
        sa.CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_entries_target_currency_check",
        ),
        sa.CheckConstraint(
            "source_currency <> target_currency",
            name="exchange_rate_entries_non_identity_check",
        ),
        sa.CheckConstraint(
            "coefficient BETWEEN 1 AND 9223372036854775807",
            name="exchange_rate_entries_coefficient_check",
        ),
        sa.CheckConstraint(
            "scale BETWEEN 0 AND 12",
            name="exchange_rate_entries_scale_check",
        ),
        sa.CheckConstraint(
            "char_length(coefficient::text) <= 6 + scale",
            name="exchange_rate_entries_integer_digits_check",
        ),
        sa.CheckConstraint(
            "scale = 0 OR coefficient % 10 <> 0",
            name="exchange_rate_entries_canonical_check",
        ),
        sa.CheckConstraint(
            "source_minor_digits = 2 AND target_minor_digits = 2",
            name="exchange_rate_entries_minor_digits_check",
        ),
        sa.ForeignKeyConstraint(
            ["rate_version_id", "target_currency"],
            ["exchange_rate_versions.id", "exchange_rate_versions.target_currency"],
            name="fk_exchange_rate_entries_version_target",
            ondelete="CASCADE",
        ),
    )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    exchange_rate_check = _idempotency_result_kind_check(include_exchange_rate=True)
    op.create_check_constraint(
        exchange_rate_check.name,
        "http_idempotency",
        exchange_rate_check.sqltext,
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade exchange rates in offline mode; "
            "a live database is required for the data guard"
        )
    op.execute(
        "LOCK TABLE exchange_rate_sources, exchange_rate_versions, "
        "exchange_rate_entries, http_idempotency IN ACCESS EXCLUSIVE MODE"
    )
    retained_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM exchange_rate_sources) OR "
            "EXISTS (SELECT 1 FROM exchange_rate_versions) OR "
            "EXISTS (SELECT 1 FROM exchange_rate_entries) OR "
            "EXISTS (SELECT 1 FROM http_idempotency "
            "WHERE result_kind = 'exchange_rate_version')"
        )
    )
    if retained_state:
        raise RuntimeError(
            "Cannot downgrade while exchange-rate sources, versions, entries, or "
            "idempotency receipts exist; remove retained state first"
        )

    op.drop_constraint(
        "http_idempotency_result_kind_check",
        "http_idempotency",
        type_="check",
    )
    previous = _idempotency_result_kind_check(include_exchange_rate=False)
    op.create_check_constraint(previous.name, "http_idempotency", previous.sqltext)

    op.drop_table("exchange_rate_entries")
    op.drop_index(
        "ix_exchange_rate_versions_source_page",
        table_name="exchange_rate_versions",
    )
    op.drop_table("exchange_rate_versions")
    op.drop_index(
        "ix_exchange_rate_sources_owner_target",
        table_name="exchange_rate_sources",
    )
    op.drop_table("exchange_rate_sources")
