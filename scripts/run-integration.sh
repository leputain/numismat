#!/usr/bin/env bash
set -euo pipefail

project="${FINBOT_INTEGRATION_PROJECT:-finbot-integration}"
[[ "$project" =~ ^finbot-integration(-[a-z0-9-]+)?$ ]] || {
  echo "FINBOT_INTEGRATION_PROJECT must start with finbot-integration." >&2
  exit 1
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -p "$project" -f "$root/compose.integration.yaml")

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

cleanup
"${compose[@]}" config --quiet
"${compose[@]}" build migrate
"${compose[@]}" up --detach --wait --wait-timeout 90 db
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate alembic downgrade 0011_bank_imports
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py seed-0011
if "${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0010_exchange_rates; then
  echo "0011 downgrade unexpectedly accepted retained bank-import state." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py cleanup-0011
"${compose[@]}" run --rm --no-deps migrate alembic downgrade 0010_exchange_rates
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py seed-0010
if "${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0009_recurring_transactions; then
  echo "0010 downgrade unexpectedly accepted retained exchange-rate state." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py cleanup-0010
"${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0009_recurring_transactions
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py seed-0009
if "${compose[@]}" run --rm --no-deps migrate alembic downgrade 0008_budgets; then
  echo "0009 downgrade unexpectedly accepted retained recurring state." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py cleanup-0009
"${compose[@]}" run --rm --no-deps migrate alembic downgrade 0008_budgets
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py seed-0008
if "${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0007_http_security_state; then
  echo "0008 downgrade unexpectedly accepted retained budget state." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0011_to_0008_fixture.py cleanup-0008
"${compose[@]}" run --rm --no-deps migrate alembic downgrade 0007_http_security_state
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0007_fixture.py seed
if "${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0006_csv_export_outbox_job; then
  echo "0007 downgrade unexpectedly accepted unexpired HTTP security state." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0007_fixture.py cleanup
"${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0006_csv_export_outbox_job
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0006_fixture.py seed
if "${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0005_channel_neutral_drafts; then
  echo "0006 downgrade unexpectedly accepted a durable CSV export job." >&2
  exit 1
fi
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0006_fixture.py cleanup
"${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0005_channel_neutral_drafts
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0005_fixture.py seed
"${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0004_telegram_response_outbox
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0005_fixture.py verify
"${compose[@]}" run --rm --no-deps migrate alembic upgrade head
"${compose[@]}" run --rm --no-deps migrate alembic downgrade 0002_ux_and_audit
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0003_fixture.py seed
"${compose[@]}" run --rm --no-deps migrate \
  alembic upgrade 0003_reliability_and_smart_input
"${compose[@]}" run --rm --no-deps tests \
  python tests/integration/migration_0003_fixture.py verify
"${compose[@]}" run --rm --no-deps migrate alembic upgrade head
"${compose[@]}" run --rm --no-deps migrate \
  alembic downgrade 0003_reliability_and_smart_input
"${compose[@]}" run --rm --no-deps migrate alembic upgrade head
"${compose[@]}" run --rm --no-deps migrate alembic check
"${compose[@]}" run --rm --no-deps tests
