#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <step-name> <command...>" >&2
  exit 2
fi

step_name="$1"
shift

start_ns=$(date +%s%N)
set +e
"$@"
status=$?
set -e
end_ns=$(date +%s%N)

elapsed_ms=$(awk -v start="$start_ns" -v end="$end_ns" 'BEGIN { printf "%.3f", (end-start)/1000000.0 }')
elapsed_seconds=$(awk -v ms="$elapsed_ms" 'BEGIN { printf "%.3f", ms/1000 }')

if [ "$status" -eq 0 ]; then
  line="- **${step_name}**: ${elapsed_seconds}s (${elapsed_ms} ms)"
  summary_level="notice"
else
  line="- **${step_name}**: FAIL (${status}) ${elapsed_seconds}s (${elapsed_ms} ms)"
  summary_level="error"
fi

echo "$line"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "$line" >> "$GITHUB_STEP_SUMMARY"
fi

echo "::${summary_level} title=release-gate::${step_name} completed in ${elapsed_seconds}s"

exit "$status"
