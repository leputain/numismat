#!/usr/bin/env bash
set -euo pipefail

[[ "${FINBOT_OPS_CONTAINER:-}" == "1" ]] || {
  echo "Restic initialization must run in the Finbot ops container." >&2
  exit 1
}
[[ -f "${RESTIC_REPOSITORY_FILE:-}" ]] || { echo "Restic repository secret is missing." >&2; exit 1; }
[[ -f "${RESTIC_PASSWORD_FILE:-}" ]] || { echo "Restic password secret is missing." >&2; exit 1; }

if restic cat config >/dev/null 2>&1; then
  echo "Restic repository is already initialized."
  exit 0
fi

restic init
echo "Encrypted Restic repository initialized."
