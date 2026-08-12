#!/usr/bin/env bash
set -euo pipefail

[[ "${FINBOT_OPS_CONTAINER:-}" == "1" ]] || exit 1
[[ "${PGDATABASE:-}" == *_test ]] || { echo "Ops test database must end in _test." >&2; exit 1; }
[[ -f "${PGPASS_SECRET_FILE:-}" ]] || { echo "Ops test pgpass secret is missing." >&2; exit 1; }

pgpass=/scratch/.pgpass
trap 'rm -f -- /scratch/.pgpass' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
install -m 0600 "$PGPASS_SECRET_FILE" "$pgpass"
export PGPASSFILE="$pgpass"

current_database="$(
  psql --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" --dbname="$PGDATABASE" \
    --no-psqlrc --tuples-only --no-align --command='SELECT current_database()'
)"
[[ "$current_database" == "$PGDATABASE" && "$current_database" == *_test ]] || exit 1

psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE ops_backup_probe (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    marker text NOT NULL
);
INSERT INTO ops_backup_probe (marker) VALUES ('synthetic-ops-backup-ok');
SQL
