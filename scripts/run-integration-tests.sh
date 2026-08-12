#!/usr/bin/env bash
set -euo pipefail

report=/tmp/finbot-integration.xml
rm -f -- "$report"

python - <<'PY'
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


async def assert_safe_database() -> None:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if not configured:
        raise SystemExit("TEST_DATABASE_URL is required; integration tests may not skip.")
    database = make_url(configured).database or ""
    if not database.endswith("_test"):
        raise SystemExit("TEST_DATABASE_URL database name must end in _test.")

    engine = create_async_engine(configured)
    try:
        async with engine.connect() as connection:
            current = await connection.scalar(text("SELECT current_database()"))
    finally:
        await engine.dispose()
    if current != database or not str(current).endswith("_test"):
        raise SystemExit("Connected database failed the _test safety check.")


asyncio.run(assert_safe_database())
PY

pytest_status=0
pytest tests/integration --junitxml="$report" -ra "$@" || pytest_status=$?
(( pytest_status == 0 )) || exit "$pytest_status"

if grep -Eq 'skipped="[1-9][0-9]*"|<skipped([[:space:]]|/|>)' "$report"; then
  echo "Integration suite skipped at least one test; skips are forbidden." >&2
  exit 1
fi

echo "Integration suite completed without skipped tests."
