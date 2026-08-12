"""Seed and verify synthetic legacy data around the 0003 migration.

This module is called explicitly by ``scripts/run-integration.sh`` and is not a
pytest test because Alembic must run between the two phases.
"""

import argparse
import os
from collections.abc import Sequence

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REVISION_0002 = "0002_ux_and_audit"
REVISION_0003 = "0003_reliability_and_smart_input"

USER_ID = "00000000-0000-0000-0000-000000000001"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000010"
CATEGORY_FIRST_ID = "00000000-0000-0000-0000-000000000101"
CATEGORY_COLLISION_ID = "00000000-0000-0000-0000-000000000102"
INVALID_PUNCTUATION_RULE_ID = "00000000-0000-0000-0000-000000000201"
INVALID_EMPTY_RULE_ID = "00000000-0000-0000-0000-000000000202"
VALID_RULE_ID = "00000000-0000-0000-0000-000000000203"
FIRST_TRANSACTION_ID = "00000000-0000-0000-0000-000000000301"
SECOND_TRANSACTION_ID = "00000000-0000-0000-0000-000000000302"


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


def _seed(connection: Connection) -> None:
    _assert_database(connection, REVISION_0002)
    existing = connection.scalar(
        text("SELECT count(*) FROM users WHERE id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    if existing:
        raise RuntimeError("synthetic migration fixture already exists")

    connection.execute(
        text(
            """
            INSERT INTO users (
                id, telegram_user_id, locale, timezone, base_currency, fast_mode
            ) VALUES (
                CAST(:id AS uuid), :telegram_user_id, 'ru_RU', 'UTC', 'RUB', false
            )
            """
        ),
        {"id": USER_ID, "telegram_user_id": 8_000_000_001},
    )
    connection.execute(
        text(
            """
            INSERT INTO accounts (
                id, user_id, name, slug, type, currency,
                initial_balance_minor, version
            ) VALUES (
                CAST(:id AS uuid), CAST(:user_id AS uuid),
                'Synthetic account', 'synthetic-account', 'cash', 'RUB', 0, 1
            )
            """
        ),
        {"id": ACCOUNT_ID, "user_id": USER_ID},
    )
    connection.execute(
        text(
            "UPDATE users SET default_account_id = CAST(:account_id AS uuid) "
            "WHERE id = CAST(:user_id AS uuid)"
        ),
        {"account_id": ACCOUNT_ID, "user_id": USER_ID},
    )
    connection.execute(
        text(
            """
            INSERT INTO categories (id, user_id, kind, name, slug, emoji)
            VALUES
                (
                    CAST(:first_id AS uuid), CAST(:user_id AS uuid), 'expense',
                    'First synthetic category', '  Кафе   И Рестораны  ', '🧪'
                ),
                (
                    CAST(:collision_id AS uuid), CAST(:user_id AS uuid), 'expense',
                    'Second synthetic category', 'кафе-и-рестораны', '🧪'
                )
            """
        ),
        {
            "first_id": CATEGORY_FIRST_ID,
            "collision_id": CATEGORY_COLLISION_ID,
            "user_id": USER_ID,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO category_rules (id, user_id, pattern, category_id, account_id)
            VALUES
                (
                    CAST(:punctuation_id AS uuid), CAST(:user_id AS uuid), '... !!! ___',
                    CAST(:first_category_id AS uuid), NULL
                ),
                (
                    CAST(:empty_id AS uuid), CAST(:user_id AS uuid), '',
                    CAST(:collision_category_id AS uuid), NULL
                ),
                (
                    CAST(:valid_id AS uuid), CAST(:user_id AS uuid), '  Ёлка!!! ',
                    CAST(:first_category_id AS uuid), NULL
                )
            """
        ),
        {
            "punctuation_id": INVALID_PUNCTUATION_RULE_ID,
            "empty_id": INVALID_EMPTY_RULE_ID,
            "valid_id": VALID_RULE_ID,
            "user_id": USER_ID,
            "first_category_id": CATEGORY_FIRST_ID,
            "collision_category_id": CATEGORY_COLLISION_ID,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO transactions (
                id, user_id, type, amount_minor, currency, account_id, category_id,
                occurred_at, description, source, telegram_update_id, version
            ) VALUES
                (
                    CAST(:first_transaction_id AS uuid), CAST(:user_id AS uuid),
                    'expense', 1, 'RUB', CAST(:account_id AS uuid),
                    CAST(:first_category_id AS uuid), now(), 'synthetic migration row',
                    'manual', 8000000301, 1
                ),
                (
                    CAST(:second_transaction_id AS uuid), CAST(:user_id AS uuid),
                    'expense', 1, 'RUB', CAST(:account_id AS uuid),
                    CAST(:collision_category_id AS uuid), now(), 'synthetic migration row',
                    'manual', 8000000302, 1
                )
            """
        ),
        {
            "first_transaction_id": FIRST_TRANSACTION_ID,
            "second_transaction_id": SECOND_TRANSACTION_ID,
            "user_id": USER_ID,
            "account_id": ACCOUNT_ID,
            "first_category_id": CATEGORY_FIRST_ID,
            "collision_category_id": CATEGORY_COLLISION_ID,
        },
    )


def _catalog_slug(value: str) -> str:
    return "-".join(value.casefold().strip().split())[:100]


def _verify_and_clean(connection: Connection) -> None:
    _assert_database(connection, REVISION_0003)

    categories = connection.execute(
        text(
            "SELECT id::text, slug FROM categories "
            "WHERE user_id = CAST(:user_id AS uuid) ORDER BY id"
        ),
        {"user_id": USER_ID},
    ).all()
    if len(categories) != 2:
        raise AssertionError("0003 changed the number of legacy categories")
    slugs = [str(row.slug) for row in categories]
    if slugs[0] != "кафе-и-рестораны":
        raise AssertionError("the deterministic first category did not receive the base slug")
    if slugs[1] != "кафе-и-рестораны-00000000000000000000000000000102":
        raise AssertionError("the colliding category did not receive its deterministic suffix")
    if len(set(slugs)) != 2 or any(_catalog_slug(slug) != slug for slug in slugs):
        raise AssertionError("migrated category slugs are not canonical and unique")

    references = connection.execute(
        text(
            "SELECT id::text, category_id::text FROM transactions "
            "WHERE user_id = CAST(:user_id AS uuid) ORDER BY id"
        ),
        {"user_id": USER_ID},
    ).all()
    expected_references = [
        (FIRST_TRANSACTION_ID, CATEGORY_FIRST_ID),
        (SECOND_TRANSACTION_ID, CATEGORY_COLLISION_ID),
    ]
    if [(row.id, row.category_id) for row in references] != expected_references:
        raise AssertionError("0003 did not preserve category references")

    rules = connection.execute(
        text(
            "SELECT id::text, normalized_pattern FROM category_rules "
            "WHERE user_id = CAST(:user_id AS uuid) ORDER BY id"
        ),
        {"user_id": USER_ID},
    ).all()
    if [(row.id, row.normalized_pattern) for row in rules] != [(VALID_RULE_ID, "елка")]:
        raise AssertionError("0003 did not discard invalid rules or normalize the valid rule")

    connection.execute(
        text("DELETE FROM category_rules WHERE user_id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    connection.execute(
        text("DELETE FROM transactions WHERE user_id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    connection.execute(
        text("UPDATE users SET default_account_id = NULL WHERE id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    connection.execute(
        text("DELETE FROM categories WHERE user_id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    connection.execute(
        text("DELETE FROM accounts WHERE user_id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
    )
    connection.execute(
        text("DELETE FROM users WHERE id = CAST(:user_id AS uuid)"),
        {"user_id": USER_ID},
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
