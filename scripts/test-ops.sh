#!/usr/bin/env bash
set -euo pipefail

project="${FINBOT_OPS_TEST_PROJECT:-finbot-ops-test}"
[[ "$project" =~ ^finbot-ops-test(-[a-z0-9-]+)?$ ]] || {
  echo "FINBOT_OPS_TEST_PROJECT must start with finbot-ops-test." >&2
  exit 1
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$(mktemp -d)"
compose=(docker compose -p "$project" -f "$root/compose.ops-test.yaml")

stop_stack() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}

cleanup() {
  stop_stack
  rm -rf -- "$secret_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

password="synthetic$(date -u +%s)${RANDOM}"
printf '%s\n' "$password" >"$secret_dir/postgres_password"
printf 'source-db:5432:finbot_backup_source_test:finbot_test:%s\n' "$password" >"$secret_dir/source_pgpass"
printf 'restore-db:5432:finbot_restore_test:finbot_test:%s\n' "$password" >"$secret_dir/restore_pgpass"
printf 'postgresql+psycopg://finbot_test:%s@source-db:5432/finbot_backup_source_test\n' "$password" >"$secret_dir/source_database_url"
printf 'postgresql+psycopg://finbot_test:%s@restore-db:5432/finbot_restore_test\n' "$password" >"$secret_dir/restore_database_url"
printf '/repository\n' >"$secret_dir/restic_repository"
printf 'synthetic-restic-password-%s\n' "$password" >"$secret_dir/restic_password"
chmod 0755 "$secret_dir"
chmod 0444 "$secret_dir"/*

export OPS_TEST_POSTGRES_PASSWORD_FILE="$secret_dir/postgres_password"
export OPS_TEST_SOURCE_PGPASS_FILE="$secret_dir/source_pgpass"
export OPS_TEST_RESTORE_PGPASS_FILE="$secret_dir/restore_pgpass"
export OPS_TEST_SOURCE_DATABASE_URL_FILE="$secret_dir/source_database_url"
export OPS_TEST_RESTORE_DATABASE_URL_FILE="$secret_dir/restore_database_url"
export OPS_TEST_RESTIC_REPOSITORY_FILE="$secret_dir/restic_repository"
export OPS_TEST_RESTIC_PASSWORD_FILE="$secret_dir/restic_password"

stop_stack
"${compose[@]}" config --quiet
"${compose[@]}" build source-migrate seed
"${compose[@]}" up --detach --wait --wait-timeout 90 source-db restore-db
"${compose[@]}" run --rm --no-deps source-migrate
"${compose[@]}" run --rm --no-deps seed
"${compose[@]}" run --rm --no-deps restic-init
"${compose[@]}" run --rm --no-deps backup
"${compose[@]}" run --rm --no-deps backup-age
"${compose[@]}" run --rm --no-deps restic-check
"${compose[@]}" run --rm --no-deps restore
"${compose[@]}" run --rm --no-deps restore-migrate
"${compose[@]}" run --rm --no-deps restore-health
"${compose[@]}" run --rm --no-deps verify
