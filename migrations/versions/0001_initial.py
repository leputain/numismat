import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None


def upgrade() -> None:
    uid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "users",
        sa.Column("id", uid, primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger, unique=True, nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("fast_mode", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "accounts",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("initial_balance_minor", sa.BigInteger, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer, nullable=False),
        sa.UniqueConstraint("user_id", "slug"),
    )
    op.create_table(
        "categories",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("emoji", sa.String(8), nullable=False),
        sa.Column("parent_id", uid, sa.ForeignKey("categories.id")),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "kind", "slug"),
    )
    op.create_table(
        "transactions",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_id", uid, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("category_id", uid, sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer, nullable=False),
        sa.CheckConstraint("amount_minor > 0"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'"),
    )
    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger, primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "drafts",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "category_rules",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("pattern", sa.String(200), nullable=False),
        sa.Column("category_id", uid, sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("account_id", uid, sa.ForeignKey("accounts.id")),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", uid, primary_key=True),
        sa.Column("user_id", uid, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("transaction_id", uid, sa.ForeignKey("transactions.id")),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in (
        "audit_events",
        "category_rules",
        "drafts",
        "processed_updates",
        "transactions",
        "categories",
        "accounts",
        "users",
    ):
        op.drop_table(table)
