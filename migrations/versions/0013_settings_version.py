"""Add the optimistic settings version to every owner.

Revision ID: 0013_settings_version
Revises: 0012_multitenant_integrity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_settings_version"
down_revision: str | None = "0012_multitenant_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "settings_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "users_settings_version_check",
        "users",
        "settings_version >= 1",
    )


def downgrade() -> None:
    op.drop_constraint("users_settings_version_check", "users", type_="check")
    op.drop_column("users", "settings_version")
