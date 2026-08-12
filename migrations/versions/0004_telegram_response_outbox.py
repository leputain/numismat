"""Add a durable Telegram response outbox.

Revision ID: 0004_telegram_response_outbox
Revises: 0003_reliability_and_smart_input
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_telegram_response_outbox"
down_revision: str | None = "0003_reliability_and_smart_input"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_response_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "update_id",
            sa.BigInteger(),
            sa.ForeignKey("processed_updates.update_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("owner_telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=24), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("parse_mode", sa.String(length=16)),
        sa.Column("reply_markup", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drafts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "update_id", "sequence", name="uq_telegram_response_outbox_update_sequence"
        ),
        sa.CheckConstraint("sequence >= 0", name="telegram_response_outbox_sequence_check"),
        sa.CheckConstraint(
            "method IN ('send_message', 'edit_message_text')",
            name="telegram_response_outbox_method_check",
        ),
        sa.CheckConstraint(
            "(method = 'send_message' AND message_id IS NULL) "
            "OR (method = 'edit_message_text' AND message_id > 0)",
            name="telegram_response_outbox_target_check",
        ),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 4096",
            name="telegram_response_outbox_text_check",
        ),
        sa.CheckConstraint(
            "parse_mode IS NULL OR parse_mode = 'HTML'",
            name="telegram_response_outbox_parse_mode_check",
        ),
        sa.CheckConstraint(
            "reply_markup IS NULL OR jsonb_typeof(reply_markup) = 'object'",
            name="telegram_response_outbox_markup_check",
        ),
    )
    op.create_index(
        "ix_telegram_response_outbox_pending",
        "telegram_response_outbox",
        ["update_id", "sequence"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_response_outbox_pending", table_name="telegram_response_outbox")
    op.drop_table("telegram_response_outbox")
