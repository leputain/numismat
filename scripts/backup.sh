#!/usr/bin/env bash
set -euo pipefail

[[ "${FINBOT_OPS_CONTAINER:-}" == "1" ]] || {
  echo "Backup must run in the Finbot ops container." >&2
  exit 1
}

for variable in PGHOST PGPORT PGDATABASE PGUSER PGPASS_SECRET_FILE \
  RESTIC_REPOSITORY_FILE RESTIC_PASSWORD_FILE; do
  [[ -n "${!variable:-}" ]] || {
    echo "Required backup setting is missing: ${variable}" >&2
    exit 1
  }
done

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

restic cat config >/dev/null
pg_dump \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --format=custom \
  --file="$dump"
pg_restore --list "$dump" >/dev/null

restic backup --quiet --host finbot-backup --tag finbot "$dump"
restic forget \
  --host finbot-backup \
  --tag finbot \
  --keep-daily 14 \
  --keep-weekly 8 \
  --keep-monthly 12 \
  --prune

cleanup
echo "Encrypted Finbot backup completed and the temporary dump was removed."
