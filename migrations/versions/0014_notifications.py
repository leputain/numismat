"""Add privacy-safe opt-in notification preferences and delivery queue.

Revision ID: 0014_notifications
Revises: 0013_settings_version
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_notifications"
down_revision: str | None = "0013_settings_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("budget_80_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("budget_100_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "recurring_ready_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "weekly_digest_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("quiet_start", sa.Time(timezone=False)),
        sa.Column("quiet_end", sa.Time(timezone=False)),
        sa.Column("weekly_weekday", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "weekly_time",
            sa.Time(timezone=False),
            server_default=sa.text("'09:00:00'"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "(quiet_start IS NULL) = (quiet_end IS NULL)",
            name="notification_preferences_quiet_pair_check",
        ),
        sa.CheckConstraint(
            "quiet_start IS NULL OR quiet_start <> quiet_end",
            name="notification_preferences_quiet_range_check",
        ),
        sa.CheckConstraint(
            "EXTRACT(SECOND FROM quiet_start) = 0 AND EXTRACT(SECOND FROM quiet_end) = 0",
            name="notification_preferences_quiet_minute_check",
        ),
        sa.CheckConstraint(
            "weekly_weekday BETWEEN 0 AND 6",
            name="notification_preferences_weekday_check",
        ),
        sa.CheckConstraint(
            "EXTRACT(SECOND FROM weekly_time) = 0",
            name="notification_preferences_weekly_minute_check",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="notification_preferences_version_check",
        ),
    )

    op.create_table(
        "notification_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True)),
        sa.Column("dedupe_digest", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(length=32)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
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
            "kind IN ('budget_80', 'budget_100', 'recurring_ready', 'weekly_digest')",
            name="notification_jobs_kind_check",
        ),
        sa.CheckConstraint(
            "(kind = 'weekly_digest' AND reference_id IS NULL) OR "
            "(kind <> 'weekly_digest' AND reference_id IS NOT NULL)",
            name="notification_jobs_reference_check",
        ),
        sa.CheckConstraint(
            "octet_length(dedupe_digest) = 32",
            name="notification_jobs_dedupe_digest_check",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 5",
            name="notification_jobs_attempt_count_check",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('chat_invalid', 'owner_revoked', 'preference_disabled', "
            "'reference_invalid', 'retry_exhausted', 'telegram_rejected', "
            "'telegram_retryable')",
            name="notification_jobs_failure_code_check",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_until IS NULL)",
            name="notification_jobs_lease_pair_check",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND lease_token IS NULL AND delivered_at IS NULL "
            "AND attempt_count < 5) OR "
            "(status = 'leased' AND lease_token IS NOT NULL AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND lease_token IS NULL AND delivered_at IS NOT NULL "
            "AND failure_code IS NULL) OR "
            "(status = 'failed' AND lease_token IS NULL AND delivered_at IS NULL "
            "AND failure_code IS NOT NULL)",
            name="notification_jobs_state_check",
        ),
    )
    op.create_index(
        "ix_notification_jobs_pending",
        "notification_jobs",
        ["available_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_notification_jobs_lease",
        "notification_jobs",
        ["lease_until", "id"],
        postgresql_where=sa.text("status = 'leased'"),
    )
    op.create_index(
        "ix_notification_jobs_owner",
        "notification_jobs",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_notification_jobs_terminal_cleanup",
        "notification_jobs",
        ["updated_at", "id"],
        postgresql_where=sa.text("status IN ('delivered', 'failed')"),
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade notifications in offline mode; a live database is required"
        )
    op.execute("LOCK TABLE notification_jobs, notification_preferences IN ACCESS EXCLUSIVE MODE")
    retained_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM notification_jobs) OR "
            "EXISTS (SELECT 1 FROM notification_preferences)"
        )
    )
    if retained_state:
        raise RuntimeError(
            "Cannot downgrade while notification preferences or jobs exist; "
            "remove retained state first"
        )
    op.drop_index("ix_notification_jobs_terminal_cleanup", table_name="notification_jobs")
    op.drop_index("ix_notification_jobs_owner", table_name="notification_jobs")
    op.drop_index("ix_notification_jobs_lease", table_name="notification_jobs")
    op.drop_index("ix_notification_jobs_pending", table_name="notification_jobs")
    op.drop_table("notification_jobs")
    op.drop_table("notification_preferences")
