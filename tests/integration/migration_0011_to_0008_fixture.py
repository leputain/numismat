"""Exercise retained-state downgrade guards from revision 0011 through 0008.

The integration harness invokes one seed and cleanup phase around each Alembic
downgrade attempt.  This is intentionally not a pytest module because the schema
must change between phases.
"""

import argparse
import os
from collections.abc import Callable, Sequence

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REVISION_0008 = "0008_budgets"
REVISION_0009 = "0009_recurring_transactions"
REVISION_0010 = "0010_exchange_rates"
REVISION_0011 = "0011_bank_imports"

OWNER_ID = "00000000-0000-7000-8000-000000021001"
ACCOUNT_ID = "00000000-0000-7000-8000-000000021002"
CATEGORY_ID = "00000000-0000-7000-8000-000000021003"

BUDGET_ID = "00000000-0000-7000-8000-000000021008"
BUDGET_RECEIPT_ID = "00000000-0000-7000-8000-000000028008"

SCHEDULE_ID = "00000000-0000-7000-8000-000000021009"
INSTANCE_ID = "00000000-0000-7000-8000-000000021019"
RECURRING_TRANSACTION_ID = "00000000-0000-7000-8000-000000021029"
RECURRING_RECEIPT_ID = "00000000-0000-7000-8000-000000028009"

RATE_SOURCE_ID = "00000000-0000-7000-8000-000000021010"
RATE_VERSION_ID = "00000000-0000-7000-8000-000000021020"
RATE_RECEIPT_ID = "00000000-0000-7000-8000-000000028010"

IMPORT_BATCH_ID = "00000000-0000-7000-8000-000000021011"
IMPORT_ROW_ID = "00000000-0000-7000-8000-000000021021"
IMPORT_TRANSACTION_ID = "00000000-0000-7000-8000-000000021031"
IMPORT_RECEIPT_ID = "00000000-0000-7000-8000-000000028011"


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
    revisions = tuple(
        connection.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num"))
    )
    if revisions != (expected_revision,):
        raise RuntimeError(f"expected sole Alembic revision {expected_revision}, got {revisions!r}")


def _owner_exists(connection: Connection) -> bool:
    return bool(
        connection.scalar(
            text("SELECT count(*) FROM users WHERE id = CAST(:owner_id AS uuid)"),
            {"owner_id": OWNER_ID},
        )
    )


def _assert_owner_absent(connection: Connection) -> None:
    if _owner_exists(connection):
        raise RuntimeError("synthetic migration fixture already exists")


def _assert_owner_present(connection: Connection) -> None:
    if not _owner_exists(connection):
        raise RuntimeError("synthetic migration fixture is missing")


def _insert_owner(connection: Connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO users (
                id, telegram_user_id, telegram_chat_id, locale,
                timezone, base_currency, fast_mode
            ) VALUES (
                CAST(:owner_id AS uuid), 99210001, 99210001,
                'ru_RU', 'UTC', 'RUB', false
            )
            """
        ),
        {"owner_id": OWNER_ID},
    )


def _insert_catalog(connection: Connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO accounts (
                id, user_id, name, slug, type, currency,
                initial_balance_minor, version
            ) VALUES (
                CAST(:account_id AS uuid), CAST(:owner_id AS uuid),
                'Migration fixture account', 'migration-fixture-account',
                'cash', 'RUB', 0, 1
            )
            """
        ),
        {"account_id": ACCOUNT_ID, "owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO categories (
                id, user_id, kind, name, slug, emoji, version
            ) VALUES (
                CAST(:category_id AS uuid), CAST(:owner_id AS uuid),
                'expense', 'Migration fixture category',
                'migration-fixture-category', 'X', 1
            )
            """
        ),
        {"category_id": CATEGORY_ID, "owner_id": OWNER_ID},
    )


def _insert_receipt(
    connection: Connection,
    *,
    receipt_id: str,
    result_kind: str,
    result_id: str,
    operation: str,
    key_octet: str,
    fingerprint_octet: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO http_idempotency (
                id, user_id, idempotency_key_hash, request_fingerprint,
                operation, status, http_status, result_kind, result_id,
                result_revision, created_at, completed_at, expires_at
            ) VALUES (
                CAST(:receipt_id AS uuid), CAST(:owner_id AS uuid),
                decode(repeat(:key_octet, 32), 'hex'),
                decode(repeat(:fingerprint_octet, 32), 'hex'),
                :operation, 'completed', 200, :result_kind,
                CAST(:result_id AS uuid), 1, now(), now(), now() + INTERVAL '1 hour'
            )
            """
        ),
        {
            "receipt_id": receipt_id,
            "owner_id": OWNER_ID,
            "key_octet": key_octet,
            "fingerprint_octet": fingerprint_octet,
            "operation": operation,
            "result_kind": result_kind,
            "result_id": result_id,
        },
    )


def _delete_catalog_and_owner(connection: Connection) -> None:
    connection.execute(
        text("DELETE FROM categories WHERE id = CAST(:category_id AS uuid)"),
        {"category_id": CATEGORY_ID},
    )
    connection.execute(
        text("DELETE FROM accounts WHERE id = CAST(:account_id AS uuid)"),
        {"account_id": ACCOUNT_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id = CAST(:owner_id AS uuid)"),
        {"owner_id": OWNER_ID},
    )


def _seed_0011(connection: Connection) -> None:
    _assert_database(connection, REVISION_0011)
    _assert_owner_absent(connection)
    _insert_owner(connection)
    _insert_catalog(connection)
    connection.execute(
        text(
            """
            INSERT INTO import_batches (
                id, user_id, account_id, profile, encoding,
                status, row_count, version, completed_at
            ) VALUES (
                CAST(:batch_id AS uuid), CAST(:owner_id AS uuid),
                CAST(:account_id AS uuid), 'canonical_v1', 'utf-8',
                'completed', 1, 1, now()
            )
            """
        ),
        {"batch_id": IMPORT_BATCH_ID, "owner_id": OWNER_ID, "account_id": ACCOUNT_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO import_rows (
                id, batch_id, user_id, position, occurred_at, type,
                amount_minor, currency, description, fingerprint,
                reference_digest, status, resolved_at, version
            ) VALUES (
                CAST(:row_id AS uuid), CAST(:batch_id AS uuid), CAST(:owner_id AS uuid),
                1, TIMESTAMPTZ '2026-01-02 09:00:00+00', 'expense',
                100, 'RUB', 'migration fixture', decode(repeat('b1', 32), 'hex'),
                decode(repeat('b2', 32), 'hex'), 'confirmed', now(), 1
            )
            """
        ),
        {"row_id": IMPORT_ROW_ID, "batch_id": IMPORT_BATCH_ID, "owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO transactions (
                id, user_id, type, amount_minor, currency, account_id,
                category_id, occurred_at, description, source, version, import_row_id
            ) VALUES (
                CAST(:transaction_id AS uuid), CAST(:owner_id AS uuid),
                'expense', 100, 'RUB', CAST(:account_id AS uuid),
                CAST(:category_id AS uuid), TIMESTAMPTZ '2026-01-02 09:00:00+00',
                'migration fixture', 'bank_import', 1, CAST(:row_id AS uuid)
            )
            """
        ),
        {
            "transaction_id": IMPORT_TRANSACTION_ID,
            "owner_id": OWNER_ID,
            "account_id": ACCOUNT_ID,
            "category_id": CATEGORY_ID,
            "row_id": IMPORT_ROW_ID,
        },
    )
    _insert_receipt(
        connection,
        receipt_id=IMPORT_RECEIPT_ID,
        result_kind="bank_import_row",
        result_id=IMPORT_ROW_ID,
        operation="migration.fixture.0011",
        key_octet="b3",
        fingerprint_octet="b4",
    )


def _cleanup_0011(connection: Connection) -> None:
    _assert_database(connection, REVISION_0011)
    _assert_owner_present(connection)
    connection.execute(
        text("DELETE FROM http_idempotency WHERE id = CAST(:receipt_id AS uuid)"),
        {"receipt_id": IMPORT_RECEIPT_ID},
    )
    connection.execute(
        text("DELETE FROM transactions WHERE id = CAST(:transaction_id AS uuid)"),
        {"transaction_id": IMPORT_TRANSACTION_ID},
    )
    connection.execute(
        text("DELETE FROM import_rows WHERE id = CAST(:row_id AS uuid)"),
        {"row_id": IMPORT_ROW_ID},
    )
    connection.execute(
        text("DELETE FROM import_batches WHERE id = CAST(:batch_id AS uuid)"),
        {"batch_id": IMPORT_BATCH_ID},
    )
    _delete_catalog_and_owner(connection)
    _assert_owner_absent(connection)


def _seed_0010(connection: Connection) -> None:
    _assert_database(connection, REVISION_0010)
    _assert_owner_absent(connection)
    _insert_owner(connection)
    connection.execute(
        text(
            """
            INSERT INTO exchange_rate_sources (
                id, user_id, kind, target_currency, latest_version
            ) VALUES (
                CAST(:source_id AS uuid), CAST(:owner_id AS uuid),
                'manual', 'USD', 1
            )
            """
        ),
        {"source_id": RATE_SOURCE_ID, "owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO exchange_rate_versions (
                id, source_id, user_id, target_currency, version, effective_at
            ) VALUES (
                CAST(:version_id AS uuid), CAST(:source_id AS uuid),
                CAST(:owner_id AS uuid), 'USD', 1,
                TIMESTAMPTZ '2026-01-02 09:00:00+00'
            )
            """
        ),
        {"version_id": RATE_VERSION_ID, "source_id": RATE_SOURCE_ID, "owner_id": OWNER_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO exchange_rate_entries (
                rate_version_id, source_currency, target_currency,
                coefficient, scale, source_minor_digits, target_minor_digits
            ) VALUES (
                CAST(:version_id AS uuid), 'RUB', 'USD', 12345, 2, 2, 2
            )
            """
        ),
        {"version_id": RATE_VERSION_ID},
    )
    _insert_receipt(
        connection,
        receipt_id=RATE_RECEIPT_ID,
        result_kind="exchange_rate_version",
        result_id=RATE_VERSION_ID,
        operation="migration.fixture.0010",
        key_octet="a3",
        fingerprint_octet="a4",
    )


def _cleanup_0010(connection: Connection) -> None:
    _assert_database(connection, REVISION_0010)
    _assert_owner_present(connection)
    connection.execute(
        text("DELETE FROM http_idempotency WHERE id = CAST(:receipt_id AS uuid)"),
        {"receipt_id": RATE_RECEIPT_ID},
    )
    connection.execute(
        text("DELETE FROM exchange_rate_entries WHERE rate_version_id = CAST(:version_id AS uuid)"),
        {"version_id": RATE_VERSION_ID},
    )
    connection.execute(
        text("DELETE FROM exchange_rate_versions WHERE id = CAST(:version_id AS uuid)"),
        {"version_id": RATE_VERSION_ID},
    )
    connection.execute(
        text("DELETE FROM exchange_rate_sources WHERE id = CAST(:source_id AS uuid)"),
        {"source_id": RATE_SOURCE_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id = CAST(:owner_id AS uuid)"),
        {"owner_id": OWNER_ID},
    )
    _assert_owner_absent(connection)


def _seed_0009(connection: Connection) -> None:
    _assert_database(connection, REVISION_0009)
    _assert_owner_absent(connection)
    _insert_owner(connection)
    _insert_catalog(connection)
    connection.execute(
        text(
            """
            INSERT INTO recurring_schedules (
                id, user_id, name, type, amount_minor, currency,
                account_id, category_id, cadence, "interval",
                anchor_date, local_time, timezone
            ) VALUES (
                CAST(:schedule_id AS uuid), CAST(:owner_id AS uuid),
                'Migration fixture schedule', 'expense', 100, 'RUB',
                CAST(:account_id AS uuid), CAST(:category_id AS uuid),
                'monthly', 1, DATE '2026-01-02', TIME '09:00:00', 'UTC'
            )
            """
        ),
        {
            "schedule_id": SCHEDULE_ID,
            "owner_id": OWNER_ID,
            "account_id": ACCOUNT_ID,
            "category_id": CATEGORY_ID,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO recurring_instances (
                id, schedule_id, user_id, occurrence_index, nominal_local,
                scheduled_for, timezone, type, amount_minor, currency,
                account_id, category_id, description, status,
                next_attempt_at, generated_at, version
            ) VALUES (
                CAST(:instance_id AS uuid), CAST(:schedule_id AS uuid),
                CAST(:owner_id AS uuid), 0, TIMESTAMP '2026-01-02 09:00:00',
                TIMESTAMPTZ '2026-01-02 09:00:00+00', 'UTC', 'expense', 100, 'RUB',
                CAST(:account_id AS uuid), CAST(:category_id AS uuid),
                'migration fixture', 'generated', now(), now(), 1
            )
            """
        ),
        {
            "instance_id": INSTANCE_ID,
            "schedule_id": SCHEDULE_ID,
            "owner_id": OWNER_ID,
            "account_id": ACCOUNT_ID,
            "category_id": CATEGORY_ID,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO transactions (
                id, user_id, type, amount_minor, currency, account_id,
                category_id, occurred_at, description, source, version,
                recurring_instance_id
            ) VALUES (
                CAST(:transaction_id AS uuid), CAST(:owner_id AS uuid),
                'expense', 100, 'RUB', CAST(:account_id AS uuid),
                CAST(:category_id AS uuid), TIMESTAMPTZ '2026-01-02 09:00:00+00',
                'migration fixture', 'recurring', 1, CAST(:instance_id AS uuid)
            )
            """
        ),
        {
            "transaction_id": RECURRING_TRANSACTION_ID,
            "owner_id": OWNER_ID,
            "account_id": ACCOUNT_ID,
            "category_id": CATEGORY_ID,
            "instance_id": INSTANCE_ID,
        },
    )
    _insert_receipt(
        connection,
        receipt_id=RECURRING_RECEIPT_ID,
        result_kind="recurring_instance",
        result_id=INSTANCE_ID,
        operation="migration.fixture.0009",
        key_octet="93",
        fingerprint_octet="94",
    )


def _cleanup_0009(connection: Connection) -> None:
    _assert_database(connection, REVISION_0009)
    _assert_owner_present(connection)
    connection.execute(
        text("DELETE FROM http_idempotency WHERE id = CAST(:receipt_id AS uuid)"),
        {"receipt_id": RECURRING_RECEIPT_ID},
    )
    connection.execute(
        text("DELETE FROM transactions WHERE id = CAST(:transaction_id AS uuid)"),
        {"transaction_id": RECURRING_TRANSACTION_ID},
    )
    connection.execute(
        text("DELETE FROM recurring_instances WHERE id = CAST(:instance_id AS uuid)"),
        {"instance_id": INSTANCE_ID},
    )
    connection.execute(
        text("DELETE FROM recurring_schedules WHERE id = CAST(:schedule_id AS uuid)"),
        {"schedule_id": SCHEDULE_ID},
    )
    _delete_catalog_and_owner(connection)
    _assert_owner_absent(connection)


def _seed_0008(connection: Connection) -> None:
    _assert_database(connection, REVISION_0008)
    _assert_owner_absent(connection)
    _insert_owner(connection)
    connection.execute(
        text(
            """
            INSERT INTO budgets (
                id, user_id, name, limit_minor, currency,
                starts_on, ends_on, timezone, version
            ) VALUES (
                CAST(:budget_id AS uuid), CAST(:owner_id AS uuid),
                'Migration fixture budget', 1000, 'RUB',
                DATE '2026-01-01', DATE '2026-01-31', 'UTC', 1
            )
            """
        ),
        {"budget_id": BUDGET_ID, "owner_id": OWNER_ID},
    )
    _insert_receipt(
        connection,
        receipt_id=BUDGET_RECEIPT_ID,
        result_kind="budget",
        result_id=BUDGET_ID,
        operation="migration.fixture.0008",
        key_octet="83",
        fingerprint_octet="84",
    )


def _cleanup_0008(connection: Connection) -> None:
    _assert_database(connection, REVISION_0008)
    _assert_owner_present(connection)
    connection.execute(
        text("DELETE FROM http_idempotency WHERE id = CAST(:receipt_id AS uuid)"),
        {"receipt_id": BUDGET_RECEIPT_ID},
    )
    connection.execute(
        text("DELETE FROM budgets WHERE id = CAST(:budget_id AS uuid)"),
        {"budget_id": BUDGET_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id = CAST(:owner_id AS uuid)"),
        {"owner_id": OWNER_ID},
    )
    _assert_owner_absent(connection)


ACTIONS: dict[str, Callable[[Connection], None]] = {
    "seed-0011": _seed_0011,
    "cleanup-0011": _cleanup_0011,
    "seed-0010": _seed_0010,
    "cleanup-0010": _cleanup_0010,
    "seed-0009": _seed_0009,
    "cleanup-0009": _cleanup_0009,
    "seed-0008": _seed_0008,
    "cleanup-0008": _cleanup_0008,
}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=tuple(ACTIONS))
    args = parser.parse_args(argv)
    action = str(args.action)

    engine = create_engine(_database_url())
    try:
        with engine.begin() as connection:
            ACTIONS[action](connection)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
