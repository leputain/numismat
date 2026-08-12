"""Add default account and reversible audit data.

Revision ID: 0002_ux_and_audit
Revises: 0001_initial
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ux_and_audit"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name, column_name in (
        ("users", "created_at"),
        ("users", "updated_at"),
        ("transactions", "created_at"),
        ("transactions", "updated_at"),
        ("processed_updates", "processed_at"),
        ("drafts", "updated_at"),
        ("audit_events", "created_at"),
    ):
        op.alter_column(
            table_name, column_name, existing_type=sa.DateTime(timezone=True), nullable=False
        )
    op.add_column(
        "accounts",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "categories",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "categories",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("users", sa.Column("default_account_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_users_default_account_id",
        "users",
        "accounts",
        ["default_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE users AS u
        SET default_account_id = (
            SELECT a.id FROM accounts AS a
            WHERE a.user_id = u.id AND a.archived_at IS NULL
            ORDER BY a.name
            LIMIT 1
        )
        """
    )
    op.create_unique_constraint("uq_drafts_user_id", "drafts", ["user_id"])
    op.add_column(
        "audit_events",
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("audit_events", sa.Column("undone_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_transactions_user_occurred_active",
        "transactions",
        ["user_id", "occurred_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_audit_events_user_created",
        "audit_events",
        ["user_id", "created_at"],
    )
    emojis = {
        "продукты": "🛒",
        "кафе и рестораны": "🍽",
        "транспорт": "🚌",
        "автомобиль": "🚗",
        "жильё": "🏠",
        "подписки": "🔄",
        "здоровье": "💊",
        "покупки": "🛍",
        "развлечения": "🎬",
        "путешествия": "✈️",
        "подарки": "🎁",
        "другое": "📦",
        "зарплата": "💼",
        "премия": "🏆",
        "проценты": "📈",
        "возврат": "↩️",
        "прочие доходы": "💰",
    }
    category = sa.table("categories", sa.column("slug", sa.String), sa.column("emoji", sa.String))
    for slug, emoji in emojis.items():
        op.execute(category.update().where(category.c.slug == slug).values(emoji=emoji))


def downgrade() -> None:
    op.drop_index("ix_audit_events_user_created", table_name="audit_events")
    op.drop_index("ix_transactions_user_occurred_active", table_name="transactions")
    op.drop_column("audit_events", "undone_at")
    op.drop_column("audit_events", "data")
    op.drop_constraint("uq_drafts_user_id", "drafts", type_="unique")
    op.drop_constraint("fk_users_default_account_id", "users", type_="foreignkey")
    op.drop_column("users", "default_account_id")
    op.drop_column("categories", "updated_at")
    op.drop_column("categories", "created_at")
    op.drop_column("accounts", "updated_at")
    op.drop_column("accounts", "created_at")
    for table_name, column_name in (
        ("audit_events", "created_at"),
        ("drafts", "updated_at"),
        ("processed_updates", "processed_at"),
        ("transactions", "updated_at"),
        ("transactions", "created_at"),
        ("users", "updated_at"),
        ("users", "created_at"),
    ):
        op.alter_column(
            table_name, column_name, existing_type=sa.DateTime(timezone=True), nullable=True
        )
