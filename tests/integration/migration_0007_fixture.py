"""Seed and remove synthetic state around the 0007 downgrade guard."""

import argparse
import os

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REVISION_0007 = "0007_http_security_state"
OWNER_ID = "00000000-0000-7000-8000-000000007091"
SESSION_ID = "00000000-0000-7000-8000-000000007092"
IDEMPOTENCY_ID = "00000000-0000-7000-8000-000000007093"


def _database_url() -> URL:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if not configured:
        raise RuntimeError("TEST_DATABASE_URL is required")
    url = make_url(configured)
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("migration fixture requires a database ending in _test")
    return url


def _assert_head_and_tables(connection: Connection) -> None:
    if connection.scalar(text("SELECT current_database()")) != _database_url().database:
        raise RuntimeError("connected database does not match TEST_DATABASE_URL")
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != REVISION_0007:
        raise RuntimeError(f"expected Alembic revision {REVISION_0007}, got {revision}")
    tables = connection.execute(
        text("SELECT to_regclass('public.web_sessions'), to_regclass('public.http_idempotency')")
    ).one()
    if tables != ("web_sessions", "http_idempotency"):
        raise RuntimeError("0007 tables are missing after guarded downgrade")


def _seed(connection: Connection) -> None:
    _assert_head_and_tables(connection)
    if connection.scalar(
        text("SELECT count(*) FROM users WHERE id = CAST(:owner_id AS uuid)"),
        {"owner_id": OWNER_ID},
    ):
        raise RuntimeError("synthetic 0007 migration fixture already exists")
    connection.execute(
        text(
            "INSERT INTO users ("
            "id, telegram_user_id, telegram_chat_id, locale, timezone, base_currency, fast_mode"
            ") VALUES ("
            "CAST(:owner_id AS uuid), 99700091, 99700091, "
            "'ru_RU', 'Europe/Moscow', 'RUB', true"
            ")"
        ),
        {"owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            "INSERT INTO web_sessions ("
            "id, user_id, session_token_hash, csrf_token_hash, created_at, expires_at"
            ") VALUES ("
            "CAST(:session_id AS uuid), CAST(:owner_id AS uuid), "
            "decode(repeat('71', 32), 'hex'), decode(repeat('72', 32), 'hex'), "
            "now(), now() + INTERVAL '1 hour'"
            ")"
        ),
        {"session_id": SESSION_ID, "owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            "INSERT INTO http_idempotency ("
            "id, user_id, idempotency_key_hash, request_fingerprint, operation, "
            "status, created_at, expires_at"
            ") VALUES ("
            "CAST(:idempotency_id AS uuid), CAST(:owner_id AS uuid), "
            "decode(repeat('73', 32), 'hex'), decode(repeat('74', 32), 'hex'), "
            "'migration.fixture', 'in_progress', now(), now() + INTERVAL '1 hour'"
            ")"
        ),
        {"idempotency_id": IDEMPOTENCY_ID, "owner_id": OWNER_ID},
    )


def _cleanup(connection: Connection) -> None:
    _assert_head_and_tables(connection)
    connection.execute(
        text("UPDATE web_sessions SET revoked_at = now() WHERE id = CAST(:session_id AS uuid)"),
        {"session_id": SESSION_ID},
    )
    connection.execute(
        text("DELETE FROM http_idempotency WHERE id = CAST(:idempotency_id AS uuid)"),
        {"idempotency_id": IDEMPOTENCY_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id = CAST(:owner_id AS uuid)"),
        {"owner_id": OWNER_ID},
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
