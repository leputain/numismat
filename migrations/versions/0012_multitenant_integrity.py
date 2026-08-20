"""Add database-enforced tenant ownership and private-chat integrity.

Revision ID: 0012_multitenant_integrity
Revises: 0011_bank_imports
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_multitenant_integrity"
down_revision: str | None = "0011_bank_imports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INTEGRITY_GUARD = sa.text(
    """
    SELECT
        EXISTS (
            SELECT 1
            FROM users AS app_user
            WHERE app_user.telegram_chat_id IS NOT NULL
              AND app_user.telegram_chat_id <> app_user.telegram_user_id
        )
        OR EXISTS (
            SELECT 1
            FROM users AS app_user
            WHERE app_user.default_account_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM accounts AS account
                  WHERE account.id = app_user.default_account_id
                    AND account.user_id = app_user.id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM categories AS category
            WHERE category.parent_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM categories AS parent
                  WHERE parent.id = category.parent_id
                    AND parent.user_id = category.user_id
                    AND parent.kind = category.kind
              )
        )
        OR EXISTS (
            SELECT 1
            FROM audit_events AS event
            WHERE event.transaction_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM transactions AS txn
                  WHERE txn.id = event.transaction_id
                    AND txn.user_id = event.user_id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM recurring_instances AS instance
            WHERE instance.draft_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM drafts AS draft
                  WHERE draft.id = instance.draft_id
                    AND draft.user_id = instance.user_id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM import_rows AS import_row
            WHERE import_row.draft_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM drafts AS draft
                  WHERE draft.id = import_row.draft_id
                    AND draft.user_id = import_row.user_id
              )
        )
        OR EXISTS (
            SELECT 1
            FROM telegram_response_outbox AS response
            WHERE NOT EXISTS (
                SELECT 1
                FROM users AS app_user
                WHERE app_user.telegram_user_id = response.owner_telegram_user_id
                  AND app_user.telegram_chat_id = response.chat_id
            )
        )
        OR EXISTS (
            SELECT 1
            FROM telegram_response_outbox AS response
            WHERE response.draft_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM drafts AS draft
                  JOIN users AS app_user ON app_user.id = draft.user_id
                  WHERE draft.id = response.draft_id
                    AND app_user.telegram_user_id = response.owner_telegram_user_id
                    AND app_user.telegram_chat_id = response.chat_id
              )
        )
    """
)


def _lock_integrity_tables() -> None:
    op.execute(
        "LOCK TABLE users, accounts, categories, transactions, audit_events, drafts, "
        "recurring_instances, import_rows, telegram_response_outbox "
        "IN ACCESS EXCLUSIVE MODE"
    )


def _assert_existing_integrity() -> None:
    if bool(op.get_bind().scalar(_INTEGRITY_GUARD)):
        raise RuntimeError(
            "Cannot add multi-tenant integrity constraints while cross-owner or "
            "non-private Telegram references exist"
        )


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "0012_multitenant_integrity requires an online database connection "
            "for the fail-closed integrity guard"
        )

    _lock_integrity_tables()
    _assert_existing_integrity()

    op.create_unique_constraint(
        "uq_users_telegram_user_chat",
        "users",
        ["telegram_user_id", "telegram_chat_id"],
    )
    op.create_check_constraint(
        "users_private_telegram_chat_check",
        "users",
        "telegram_chat_id IS NULL OR telegram_chat_id = telegram_user_id",
    )
    op.create_unique_constraint(
        "uq_transactions_id_user_id",
        "transactions",
        ["id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_drafts_id_user_id",
        "drafts",
        ["id", "user_id"],
    )

    op.drop_constraint("fk_users_default_account_id", "users", type_="foreignkey")
    op.create_foreign_key(
        "fk_users_default_account_owner",
        "users",
        "accounts",
        ["default_account_id", "id"],
        ["id", "user_id"],
    )

    op.drop_constraint("categories_parent_id_fkey", "categories", type_="foreignkey")
    op.create_foreign_key(
        "fk_categories_parent_owner_kind",
        "categories",
        "categories",
        ["parent_id", "user_id", "kind"],
        ["id", "user_id", "kind"],
    )

    op.drop_constraint(
        "audit_events_transaction_id_fkey",
        "audit_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_audit_events_transaction_owner",
        "audit_events",
        "transactions",
        ["transaction_id", "user_id"],
        ["id", "user_id"],
    )

    op.drop_constraint(
        "recurring_instances_draft_id_fkey",
        "recurring_instances",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_recurring_instances_draft_owner",
        "recurring_instances",
        "drafts",
        ["draft_id", "user_id"],
        ["id", "user_id"],
    )

    op.drop_constraint("import_rows_draft_id_fkey", "import_rows", type_="foreignkey")
    op.create_foreign_key(
        "fk_import_rows_draft_owner",
        "import_rows",
        "drafts",
        ["draft_id", "user_id"],
        ["id", "user_id"],
    )

    op.create_foreign_key(
        "fk_telegram_response_outbox_user_chat",
        "telegram_response_outbox",
        "users",
        ["owner_telegram_user_id", "chat_id"],
        ["telegram_user_id", "telegram_chat_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_telegram_response_outbox_user_chat",
        "telegram_response_outbox",
        type_="foreignkey",
    )

    op.drop_constraint("fk_import_rows_draft_owner", "import_rows", type_="foreignkey")
    op.create_foreign_key(
        "import_rows_draft_id_fkey",
        "import_rows",
        "drafts",
        ["draft_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint(
        "fk_recurring_instances_draft_owner",
        "recurring_instances",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "recurring_instances_draft_id_fkey",
        "recurring_instances",
        "drafts",
        ["draft_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint(
        "fk_audit_events_transaction_owner",
        "audit_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "audit_events_transaction_id_fkey",
        "audit_events",
        "transactions",
        ["transaction_id"],
        ["id"],
    )

    op.drop_constraint(
        "fk_categories_parent_owner_kind",
        "categories",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "categories_parent_id_fkey",
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
    )

    op.drop_constraint("fk_users_default_account_owner", "users", type_="foreignkey")
    op.create_foreign_key(
        "fk_users_default_account_id",
        "users",
        "accounts",
        ["default_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("uq_drafts_id_user_id", "drafts", type_="unique")
    op.drop_constraint("uq_transactions_id_user_id", "transactions", type_="unique")
    op.drop_constraint("users_private_telegram_chat_check", "users", type_="check")
    op.drop_constraint("uq_users_telegram_user_chat", "users", type_="unique")
