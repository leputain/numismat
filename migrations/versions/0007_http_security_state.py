"""Add keyed HTTP session and idempotency state.

Revision ID: 0007_http_security_state
Revises: 0006_csv_export_outbox_job
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_http_security_state"
down_revision: str | None = "0006_csv_export_outbox_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "session_token_hash",
            name="uq_web_sessions_session_token_hash",
        ),
        sa.UniqueConstraint(
            "csrf_token_hash",
            name="uq_web_sessions_csrf_token_hash",
        ),
        sa.CheckConstraint(
            "octet_length(session_token_hash) = 32",
            name="web_sessions_session_token_hash_check",
        ),
        sa.CheckConstraint(
            "octet_length(csrf_token_hash) = 32",
            name="web_sessions_csrf_token_hash_check",
        ),
        sa.CheckConstraint(
            "session_token_hash <> csrf_token_hash",
            name="web_sessions_distinct_token_hashes_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="web_sessions_expiry_order_check",
        ),
        sa.CheckConstraint(
            "expires_at <= created_at + INTERVAL '24 hours'",
            name="web_sessions_expiry_bound_check",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="web_sessions_revocation_order_check",
        ),
    )
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
    op.create_index(
        "ix_web_sessions_revoked_at",
        "web_sessions",
        ["revoked_at"],
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )
    op.create_index(
        "ix_web_sessions_user_active",
        "web_sessions",
        ["user_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "http_idempotency",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.SmallInteger()),
        sa.Column("result_kind", sa.String(length=32)),
        sa.Column("result_id", postgresql.UUID(as_uuid=True)),
        sa.Column("result_revision", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key_hash",
            name="uq_http_idempotency_owner_key_hash",
        ),
        sa.CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name="http_idempotency_key_hash_check",
        ),
        sa.CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name="http_idempotency_request_fingerprint_check",
        ),
        sa.CheckConstraint(
            "operation ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
            name="http_idempotency_operation_check",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="http_idempotency_status_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="http_idempotency_expiry_order_check",
        ),
        sa.CheckConstraint(
            "expires_at <= created_at + INTERVAL '24 hours'",
            name="http_idempotency_expiry_bound_check",
        ),
        sa.CheckConstraint(
            "result_kind IS NULL OR result_kind IN "
            "('none', 'draft', 'transaction', 'account', 'category')",
            name="http_idempotency_result_kind_check",
        ),
        sa.CheckConstraint(
            "result_kind IS NULL OR "
            "(result_kind = 'none' AND result_id IS NULL AND result_revision IS NULL) OR "
            "(result_kind <> 'none' AND result_id IS NOT NULL "
            "AND result_revision IS NOT NULL AND result_revision >= 1)",
            name="http_idempotency_result_reference_check",
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL AND http_status IS NULL "
            "AND result_kind IS NULL AND result_id IS NULL AND result_revision IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND completed_at >= created_at AND completed_at <= expires_at "
            "AND http_status IS NOT NULL AND http_status BETWEEN 200 AND 299 "
            "AND result_kind IS NOT NULL)",
            name="http_idempotency_state_check",
        ),
    )
    op.create_index(
        "ix_http_idempotency_expires_at",
        "http_idempotency",
        ["expires_at"],
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Cannot downgrade HTTP security state in offline mode; "
            "a live database is required for the retention guard"
        )
    op.execute("LOCK TABLE web_sessions, http_idempotency IN ACCESS EXCLUSIVE MODE")
    active_state = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM web_sessions "
            "WHERE revoked_at IS NULL AND expires_at > now()"
            ") OR EXISTS ("
            "SELECT 1 FROM http_idempotency WHERE expires_at > now()"
            ")"
        )
    )
    if active_state:
        raise RuntimeError(
            "Cannot downgrade while unexpired HTTP security state exists; "
            "revoke sessions and clear retained idempotency records first"
        )
    op.drop_index("ix_http_idempotency_expires_at", table_name="http_idempotency")
    op.drop_table("http_idempotency")
    op.drop_index("ix_web_sessions_user_active", table_name="web_sessions")
    op.drop_index("ix_web_sessions_revoked_at", table_name="web_sessions")
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_table("web_sessions")
