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

result="$(
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --dbname="$PGDATABASE" \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --set=ON_ERROR_STOP=1 \
    --command="SELECT current_database() || ':' || marker FROM ops_backup_probe WHERE singleton"
)"

[[ "$result" == "${PGDATABASE}:synthetic-ops-backup-ok" ]] || {
  echo "Restored synthetic probe did not match." >&2
  exit 1
}
echo "Synthetic encrypted backup and restore probe verified."
