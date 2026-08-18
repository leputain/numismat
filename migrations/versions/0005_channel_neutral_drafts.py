"""Separate Telegram presentation state from channel-neutral drafts.

Revision ID: 0005_channel_neutral_drafts
Revises: 0004_telegram_response_outbox

This is the rolling-compatible half of the migration. Legacy ``presentation_ref``
and ``payload.ui_message_id`` values remain readable until every Telegram handler
has moved to the adapter-specific projection. A later revision may remove them
after the application rollout is complete.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_channel_neutral_drafts"
down_revision: str | None = "0004_telegram_response_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_response_outbox",
        sa.Column("draft_revision", sa.Integer()),
    )
    op.add_column(
        "telegram_response_outbox",
        sa.Column("history_page", sa.Integer()),
    )
    op.add_column(
        "telegram_response_outbox",
        sa.Column("pending_history_page", sa.Integer()),
    )
    op.create_check_constraint(
        "telegram_response_outbox_draft_revision_check",
        "telegram_response_outbox",
        "draft_revision IS NULL OR draft_revision >= 1",
    )
    op.create_check_constraint(
        "telegram_response_outbox_history_page_check",
        "telegram_response_outbox",
        "history_page IS NULL OR history_page BETWEEN 0 AND 1295",
    )
    op.create_check_constraint(
        "telegram_response_outbox_pending_history_page_check",
        "telegram_response_outbox",
        "pending_history_page IS NULL OR pending_history_page BETWEEN 0 AND 1295",
    )
    op.create_check_constraint(
        "telegram_response_outbox_presentation_context_check",
        "telegram_response_outbox",
        "(history_page IS NULL AND pending_history_page IS NULL) OR draft_revision IS NOT NULL",
    )
    op.create_table(
        "telegram_draft_presentations",
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("drafts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("rendered_revision", sa.Integer(), nullable=False),
        sa.Column("history_page", sa.Integer()),
        sa.Column("pending_history_page", sa.Integer()),
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
            "chat_id", "message_id", name="uq_telegram_draft_presentations_chat_message"
        ),
        sa.CheckConstraint("chat_id <> 0", name="telegram_draft_presentations_chat_check"),
        sa.CheckConstraint("message_id > 0", name="telegram_draft_presentations_message_check"),
        sa.CheckConstraint(
            "rendered_revision >= 1",
            name="telegram_draft_presentations_revision_check",
        ),
        sa.CheckConstraint(
            "history_page IS NULL OR history_page BETWEEN 0 AND 1295",
            name="telegram_draft_presentations_history_page_check",
        ),
        sa.CheckConstraint(
            "pending_history_page IS NULL OR pending_history_page BETWEEN 0 AND 1295",
            name="telegram_draft_presentations_pending_history_page_check",
        ),
    )

    # Only unambiguous positive values are imported. Limiting the decimal token to
    # 18 digits makes the bigint cast safe without relying on exception-prone data
    # cleanup. The payload value matches the existing runtime's lookup priority;
    # presentation_ref is only a fallback when that value is absent or invalid.
    op.execute(
        """
        WITH legacy AS (
            SELECT
                draft.id AS draft_id,
                app_user.telegram_chat_id AS chat_id,
                draft.revision AS rendered_revision,
                CASE
                    WHEN draft.payload ->> 'ui_message_id' ~ '^[1-9][0-9]{0,17}$'
                        THEN (draft.payload ->> 'ui_message_id')::bigint
                    WHEN draft.presentation_ref ~ '^[1-9][0-9]{0,17}$'
                        THEN draft.presentation_ref::bigint
                    ELSE NULL
                END AS message_id
            FROM drafts AS draft
            JOIN users AS app_user ON app_user.id = draft.user_id
            WHERE app_user.telegram_chat_id IS NOT NULL
              AND app_user.telegram_chat_id <> 0
        )
        INSERT INTO telegram_draft_presentations (
            draft_id,
            chat_id,
            message_id,
            rendered_revision
        )
        SELECT draft_id, chat_id, message_id, rendered_revision
        FROM legacy
        WHERE message_id IS NOT NULL
        ON CONFLICT (draft_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # A new runtime no longer updates the legacy binding after outbox delivery.
    # Restore the current projection before dropping it so a rolled-back 0004
    # runtime can still authorize buttons that were already delivered.  An older
    # projection is deliberately ignored: it does not present the current draft
    # revision and must not become authoritative again.
    op.execute(
        """
        UPDATE drafts AS draft
        SET
            presentation_ref = presentation.message_id::text,
            payload = jsonb_set(
                draft.payload,
                '{ui_message_id}',
                to_jsonb(presentation.message_id),
                true
            )
        FROM telegram_draft_presentations AS presentation
        WHERE presentation.draft_id = draft.id
          AND presentation.rendered_revision = draft.revision
          AND presentation.message_id > 0
        """
    )
    op.drop_table("telegram_draft_presentations")
    op.drop_constraint(
        "telegram_response_outbox_presentation_context_check",
        "telegram_response_outbox",
        type_="check",
    )
    op.drop_constraint(
        "telegram_response_outbox_pending_history_page_check",
        "telegram_response_outbox",
        type_="check",
    )
    op.drop_constraint(
        "telegram_response_outbox_history_page_check",
        "telegram_response_outbox",
        type_="check",
    )
    op.drop_constraint(
        "telegram_response_outbox_draft_revision_check",
        "telegram_response_outbox",
        type_="check",
    )
    op.drop_column("telegram_response_outbox", "pending_history_page")
    op.drop_column("telegram_response_outbox", "history_page")
    op.drop_column("telegram_response_outbox", "draft_revision")
