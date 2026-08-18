"""Add privacy-safe durable CSV export jobs.

Revision ID: 0006_csv_export_outbox_job
Revises: 0005_channel_neutral_drafts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_csv_export_outbox_job"
down_revision: str | None = "0005_channel_neutral_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "telegram_response_outbox"


def _drop_delivery_constraints() -> None:
    op.drop_constraint(
        "telegram_response_outbox_target_check",
        _TABLE,
        type_="check",
    )
    op.drop_constraint(
        "telegram_response_outbox_method_check",
        _TABLE,
        type_="check",
    )


def upgrade() -> None:
    _drop_delivery_constraints()
    op.create_check_constraint(
        "telegram_response_outbox_method_check",
        _TABLE,
        "method IN ('send_message', 'edit_message_text', 'send_csv_export')",
    )
    op.create_check_constraint(
        "telegram_response_outbox_target_check",
        _TABLE,
        "(method = 'send_message' AND message_id IS NULL) "
        "OR (method = 'edit_message_text' AND message_id > 0) "
        "OR (method = 'send_csv_export' AND message_id IS NULL)",
    )
    op.create_check_constraint(
        "telegram_response_outbox_csv_export_job_check",
        _TABLE,
        "method <> 'send_csv_export' OR ("
        "text = 'csv_export:v1' AND parse_mode IS NULL AND reply_markup IS NULL "
        "AND draft_id IS NULL AND draft_revision IS NULL "
        "AND history_page IS NULL AND pending_history_page IS NULL)",
    )


def downgrade() -> None:
    export_jobs = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM telegram_response_outbox WHERE method = 'send_csv_export'")
    )
    if export_jobs:
        raise RuntimeError(
            "Cannot downgrade while durable CSV export jobs exist; "
            "remove all CSV export job rows first"
        )
    op.drop_constraint(
        "telegram_response_outbox_csv_export_job_check",
        _TABLE,
        type_="check",
    )
    _drop_delivery_constraints()
    op.create_check_constraint(
        "telegram_response_outbox_method_check",
        _TABLE,
        "method IN ('send_message', 'edit_message_text')",
    )
    op.create_check_constraint(
        "telegram_response_outbox_target_check",
        _TABLE,
        "(method = 'send_message' AND message_id IS NULL) "
        "OR (method = 'edit_message_text' AND message_id > 0)",
    )
