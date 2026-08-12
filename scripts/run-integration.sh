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
