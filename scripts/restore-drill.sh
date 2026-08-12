#!/usr/bin/env bash
set -euo pipefail

[[ "${FINBOT_OPS_CONTAINER:-}" == "1" ]] || {
  echo "Restore drill must run in the Finbot ops container." >&2
  exit 1
}

for variable in PGHOST PGPORT PGDATABASE PGUSER PGPASS_SECRET_FILE \
  RESTIC_REPOSITORY_FILE RESTIC_PASSWORD_FILE; do
  [[ -n "${!variable:-}" ]] || {
    echo "Required restore setting is missing: ${variable}" >&2
    exit 1
  }
done

[[ "$PGDATABASE" == *_test ]] || {
  echo "Restore database name must end in _test." >&2
  exit 1
}
[[ -f "$PGPASS_SECRET_FILE" ]] || { echo "PostgreSQL password secret is missing." >&2; exit 1; }
[[ -f "$RESTIC_REPOSITORY_FILE" ]] || { echo "Restic repository secret is missing." >&2; exit 1; }
[[ -f "$RESTIC_PASSWORD_FILE" ]] || { echo "Restic password secret is missing." >&2; exit 1; }

scratch=/scratch
dump="$scratch/finbot.dump"
pgpass="$scratch/.pgpass"

cleanup() {
  rm -f -- "$dump" "$pgpass"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

umask 077
install -m 0600 "$PGPASS_SECRET_FILE" "$pgpass"
export PGPASSFILE="$pgpass"

current_database="$(
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --dbname="$PGDATABASE" \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --command='SELECT current_database()'
)"
[[ "$current_database" == "$PGDATABASE" && "$current_database" == *_test ]] || {
  echo "Connected restore database failed the _test safety check." >&2
  exit 1
}

restic dump \
  --host finbot-backup \
  --tag finbot \
  --target "$dump" \
  latest \
  /scratch/finbot.dump
pg_restore --list "$dump" >/dev/null
pg_restore \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --clean \
  --if-exists \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  "$dump"

cleanup
echo "Encrypted backup restored into the isolated _test database."
