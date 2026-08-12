#!/usr/bin/env bash
set -euo pipefail

project="${OPS_PROJECT:-finbot-ops}"
[[ "$project" =~ ^finbot-ops(-[a-z0-9-]+)?$ ]] || {
  echo "OPS_PROJECT must start with finbot-ops." >&2
  exit 1
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -p "$project" -f "$root/compose.ops.yaml")

cleanup() {
  "${compose[@]}" rm --stop --force restore-db >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" config --quiet
"${compose[@]}" build restore restore-migrate restore-health
cleanup
"${compose[@]}" up --detach --wait --wait-timeout 90 --force-recreate restore-db
"${compose[@]}" run --rm --no-deps restore
"${compose[@]}" run --rm --no-deps restore-migrate
"${compose[@]}" run --rm --no-deps restore-health
echo "Restore drill, migration upgrade, and application healthcheck completed."
