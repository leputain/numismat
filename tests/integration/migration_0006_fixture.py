"""Seed and remove one privacy-safe job around the 0006 downgrade guard."""

import argparse
import os

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REVISION_0006 = "0006_csv_export_outbox_job"
UPDATE_ID = 99_600_091
OUTBOX_ID = "00000000-0000-7000-8000-000000006091"


def _database_url() -> URL:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if not configured:
        raise RuntimeError("TEST_DATABASE_URL is required")
    url = make_url(configured)
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("migration fixture requires a database ending in _test")
    return url


def _assert_head(connection: Connection) -> None:
    if connection.scalar(text("SELECT current_database()")) != _database_url().database:
        raise RuntimeError("connected database does not match TEST_DATABASE_URL")
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != REVISION_0006:
        raise RuntimeError(f"expected Alembic revision {REVISION_0006}, got {revision}")


def _seed(connection: Connection) -> None:
    _assert_head(connection)
    existing = connection.scalar(
        text("SELECT count(*) FROM processed_updates WHERE update_id = :update_id"),
        {"update_id": UPDATE_ID},
    )
    if existing:
        raise RuntimeError("synthetic 0006 migration fixture already exists")
    connection.execute(
        text("INSERT INTO processed_updates (update_id) VALUES (:update_id)"),
        {"update_id": UPDATE_ID},
    )
    connection.execute(
        text(
            "INSERT INTO telegram_response_outbox ("
            "id, update_id, sequence, owner_telegram_user_id, chat_id, method, "
            "message_id, text, parse_mode, reply_markup, draft_id, draft_revision, "
            "history_page, pending_history_page, sent_at"
            ") VALUES ("
            "CAST(:id AS uuid), :update_id, 0, 99600092, 99600093, "
            "'send_csv_export', NULL, 'csv_export:v1', NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL"
            ")"
        ),
        {"id": OUTBOX_ID, "update_id": UPDATE_ID},
    )


def _cleanup(connection: Connection) -> None:
    _assert_head(connection)
    connection.execute(
        text("DELETE FROM processed_updates WHERE update_id = :update_id"),
        {"update_id": UPDATE_ID},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "cleanup"))
    args = parser.parse_args()
    engine = create_engine(_database_url())
    try:
        with engine.begin() as connection:
            if args.action == "seed":
                _seed(connection)
            else:
                _cleanup(connection)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
