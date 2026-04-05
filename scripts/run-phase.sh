#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="${1:?Usage: run-phase.sh <prompt-file>}"
CLAUDE_MODE="${CLAUDE_MODE:-new}"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

PROMPT_CONTENT="$(cat "$PROMPT_FILE")"

cd "$ROOT_DIR"

if [[ "$CLAUDE_MODE" == "continue" ]]; then
  claude --permission-mode bypassPermissions -c -p "$PROMPT_CONTENT"
else
  claude --permission-mode bypassPermissions -p "$PROMPT_CONTENT"
fi