"""Verify presentation compatibility across the 0005 downgrade boundary.

The integration harness invokes this module around an Alembic downgrade.  It
is intentionally separate from pytest because the schema transition must occur
between the seed and verification phases.
"""

import argparse
import json
import os
from collections.abc import Sequence

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REVISION_0004 = "0004_telegram_response_outbox"
REVISION_0005 = "0005_channel_neutral_drafts"

CURRENT_USER_ID = "00000000-0000-0000-0000-000000005001"
CURRENT_DRAFT_ID = "00000000-0000-0000-0000-000000005002"
STALE_USER_ID = "00000000-0000-0000-0000-000000005011"
STALE_DRAFT_ID = "00000000-0000-0000-0000-000000005012"

LEGACY_CURRENT_MESSAGE_ID = 8_500_000_001
PROJECTED_CURRENT_MESSAGE_ID = 8_500_000_002
LEGACY_STALE_MESSAGE_ID = 8_500_000_011
PROJECTED_STALE_MESSAGE_ID = 8_500_000_012


def _database_url() -> URL:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if not configured:
        raise RuntimeError("TEST_DATABASE_URL is required")
    url = make_url(configured)
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("migration fixture requires a database ending in _test")
    return url


def _assert_database(connection: Connection, expected_revision: str) -> None:
    configured_database = _database_url().database
    current_database = connection.scalar(text("SELECT current_database()"))
    if current_database != configured_database:
        raise RuntimeError("connected database does not match TEST_DATABASE_URL")
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != expected_revision:
        raise RuntimeError(f"expected Alembic revision {expected_revision}, got {revision}")


def _insert_user(connection: Connection, user_id: str, telegram_id: int) -> None:
    connection.execute(
        text(
            """
            INSERT INTO users (
                id, telegram_user_id, telegram_chat_id, locale,
                timezone, base_currency, fast_mode
            ) VALUES (
                CAST(:id AS uuid), :telegram_id, :telegram_id,
                'ru_RU', 'UTC', 'RUB', false
            )
            """
        ),
        {"id": user_id, "telegram_id": telegram_id},
    )


def _insert_draft(
    connection: Connection,
    *,
    draft_id: str,
    user_id: str,
    legacy_message_id: int,
    projected_message_id: int,
    rendered_revision: int,
    history_page: int,
    pending_history_page: int,
) -> None:
    payload = json.dumps({"step": "review", "ui_message_id": legacy_message_id})
    connection.execute(
        text(
            """
            INSERT INTO drafts (
                id, user_id, state, payload, schema_version,
                revision, suspended, presentation_ref
            ) VALUES (
                CAST(:draft_id AS uuid), CAST(:user_id AS uuid), 'review',
                CAST(:payload AS jsonb), 1, 7, false, :presentation_ref
            )
            """
        ),
        {
            "draft_id": draft_id,
            "user_id": user_id,
            "payload": payload,
            "presentation_ref": str(legacy_message_id),
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO telegram_draft_presentations (
                draft_id, chat_id, message_id, rendered_revision,
                history_page, pending_history_page
            ) VALUES (
                CAST(:draft_id AS uuid), :chat_id, :message_id, :rendered_revision,
                :history_page, :pending_history_page
            )
            """
        ),
        {
            "draft_id": draft_id,
            "chat_id": projected_message_id,
            "message_id": projected_message_id,
            "rendered_revision": rendered_revision,
            "history_page": history_page,
            "pending_history_page": pending_history_page,
        },
    )


def _seed(connection: Connection) -> None:
    _assert_database(connection, REVISION_0005)
    existing = connection.scalar(
        text("SELECT count(*) FROM users WHERE id = CAST(:user_id AS uuid)"),
        {"user_id": CURRENT_USER_ID},
    )
    if existing:
        raise RuntimeError("synthetic migration fixture already exists")

    _insert_user(connection, CURRENT_USER_ID, 8_500_000_101)
    _insert_user(connection, STALE_USER_ID, 8_500_000_111)
    _insert_draft(
        connection,
        draft_id=CURRENT_DRAFT_ID,
        user_id=CURRENT_USER_ID,
        legacy_message_id=LEGACY_CURRENT_MESSAGE_ID,
        projected_message_id=PROJECTED_CURRENT_MESSAGE_ID,
        rendered_revision=7,
        history_page=3,
        pending_history_page=5,
    )
    _insert_draft(
        connection,
        draft_id=STALE_DRAFT_ID,
        user_id=STALE_USER_ID,
        legacy_message_id=LEGACY_STALE_MESSAGE_ID,
        projected_message_id=PROJECTED_STALE_MESSAGE_ID,
        rendered_revision=6,
        history_page=7,
        pending_history_page=11,
    )


def _legacy_binding(connection: Connection, draft_id: str) -> tuple[str | None, int | None]:
    row = connection.execute(
        text(
            """
            SELECT presentation_ref, (payload ->> 'ui_message_id')::bigint AS message_id
            FROM drafts
            WHERE id = CAST(:draft_id AS uuid)
            """
        ),
        {"draft_id": draft_id},
    ).one()
    return row.presentation_ref, row.message_id


def _verify_and_clean(connection: Connection) -> None:
    _assert_database(connection, REVISION_0004)
    current = _legacy_binding(connection, CURRENT_DRAFT_ID)
    if current != (str(PROJECTED_CURRENT_MESSAGE_ID), PROJECTED_CURRENT_MESSAGE_ID):
        raise AssertionError("0005 downgrade did not restore the current projection")

    stale = _legacy_binding(connection, STALE_DRAFT_ID)
    if stale != (str(LEGACY_STALE_MESSAGE_ID), LEGACY_STALE_MESSAGE_ID):
        raise AssertionError("0005 downgrade restored a projection for an obsolete revision")

    connection.execute(
        text("DELETE FROM drafts WHERE id IN (CAST(:current AS uuid), CAST(:stale AS uuid))"),
        {"current": CURRENT_DRAFT_ID, "stale": STALE_DRAFT_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id IN (CAST(:current AS uuid), CAST(:stale AS uuid))"),
        {"current": CURRENT_USER_ID, "stale": STALE_USER_ID},
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("seed", "verify"))
    args = parser.parse_args(argv)

    engine = create_engine(_database_url())
    try:
        with engine.begin() as connection:
            if args.phase == "seed":
                _seed(connection)
            else:
                _verify_and_clean(connection)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
