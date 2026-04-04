#!/usr/bin/env bash
set -euo pipefail

PAYLOAD="${CLAUDE_TOOL_INPUT:-}"
LOWER_PAYLOAD="$(printf '%s' "$PAYLOAD" | tr '[:upper:]' '[:lower:]')"

blocked_patterns=(
  "git push"
  "git tag"
  "git reset --hard"
  "git clean -fd"
  "sudo "
  "shred "
)

for pattern in "${blocked_patterns[@]}"; do
  if printf '%s' "$LOWER_PAYLOAD" | grep -Fq "$pattern"; then
    echo "Blocked dangerous command pattern: $pattern" >&2
    exit 2
  fi
done

exit 0
