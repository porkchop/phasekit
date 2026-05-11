#!/usr/bin/env bash
set -euo pipefail
# PHASEKIT_TRACE=1 turns on bash xtrace so every wrapper command is visible.
# Loud but useful for debugging the autonomous loop. See docs/EXECUTION_MODES.md.
[[ "${PHASEKIT_TRACE:-}" == "1" ]] && set -x

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="${1:?Usage: run-phase.sh <prompt-file>}"
CLAUDE_MODE="${CLAUDE_MODE:-new}"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

PROMPT_CONTENT="$(cat "$PROMPT_FILE")"

cd "$ROOT_DIR"

# Per-iteration log capture. PHASEKIT_ITER is set by run-until-done.sh; when
# this script is invoked directly we fall back to "manual". On retries the
# loop also passes PHASEKIT_RETRY_ATTEMPT so prior attempts' logs are not
# overwritten — useful when the first attempt and its retry fail differently.
LOG_DIR="$ROOT_DIR/artifacts/logs"
mkdir -p "$LOG_DIR"
ATTEMPT_TAG=""
if [[ "${PHASEKIT_RETRY_ATTEMPT:-0}" -gt 0 ]]; then
  ATTEMPT_TAG="-retry${PHASEKIT_RETRY_ATTEMPT}"
fi
LOG_FILE="$LOG_DIR/claude-iter-${PHASEKIT_ITER:-manual}${ATTEMPT_TAG}.log"
echo "Logging claude output to: $LOG_FILE"

# --verbose makes -p print tool calls and intermediate text as claude works
# (without it, -p is silent until the final response). pipefail propagates
# claude's exit code through tee so the caller still sees non-zero on failure.
if [[ "$CLAUDE_MODE" == "continue" ]]; then
  claude --permission-mode bypassPermissions --verbose -c -p "$PROMPT_CONTENT" 2>&1 | tee "$LOG_FILE"
else
  claude --permission-mode bypassPermissions --verbose -p "$PROMPT_CONTENT" 2>&1 | tee "$LOG_FILE"
fi