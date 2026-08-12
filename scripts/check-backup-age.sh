#!/usr/bin/env bash
set -euo pipefail

[[ "${FINBOT_OPS_CONTAINER:-}" == "1" ]] || {
  echo "Backup freshness must be checked in the Finbot ops container." >&2
  exit 1
}

[[ -f "${RESTIC_REPOSITORY_FILE:-}" ]] || { echo "Restic repository secret is missing." >&2; exit 1; }
[[ -f "${RESTIC_PASSWORD_FILE:-}" ]] || { echo "Restic password secret is missing." >&2; exit 1; }

max_age_hours="${MAX_BACKUP_AGE_HOURS:-30}"
[[ "$max_age_hours" =~ ^[1-9][0-9]*$ ]] || {
  echo "MAX_BACKUP_AGE_HOURS must be a positive integer." >&2
  exit 1
}

snapshots="$(restic snapshots --host finbot-backup --tag finbot --latest 1 --json)"
latest_time="$(sed -n 's/.*"time"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$snapshots" | head -n 1)"
[[ -n "$latest_time" ]] || { echo "No Finbot backup snapshot found." >&2; exit 1; }

latest_epoch="$(date --date="$latest_time" +%s)"
now="$(date -u +%s)"
(( latest_epoch <= now )) || { echo "Latest backup timestamp is in the future." >&2; exit 1; }
age_hours="$(( (now - latest_epoch) / 3600 ))"
if (( age_hours > max_age_hours )); then
  echo "Latest encrypted backup is ${age_hours}h old (limit ${max_age_hours}h)." >&2
  exit 1
fi
echo "Latest encrypted backup age: ${age_hours}h."
