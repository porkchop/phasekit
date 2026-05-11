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
#
# Two files are produced per attempt:
#   *.jsonl  raw stream-json events from the claude CLI (machine-readable,
#            full fidelity — keep this for forensics on a mid-stream abort
#            like an API content-filter trip)
#   *.log    human-readable rendering produced by phasekit-log-fmt.sh
#            (tail -F this for a live view of the loop)
LOG_DIR="$ROOT_DIR/artifacts/logs"
mkdir -p "$LOG_DIR"
ATTEMPT_TAG=""
if [[ "${PHASEKIT_RETRY_ATTEMPT:-0}" -gt 0 ]]; then
  ATTEMPT_TAG="-retry${PHASEKIT_RETRY_ATTEMPT}"
fi
RAW_LOG="$LOG_DIR/claude-iter-${PHASEKIT_ITER:-manual}${ATTEMPT_TAG}.jsonl"
LOG_FILE="$LOG_DIR/claude-iter-${PHASEKIT_ITER:-manual}${ATTEMPT_TAG}.log"
FORMATTER="$ROOT_DIR/scripts/phasekit-log-fmt.sh"
echo "Logging claude output to: $LOG_FILE (raw JSONL: $RAW_LOG)"

# Observability must never break the loop. If the formatter script is
# missing (e.g. a downstream project vendored an older copy of the scripts
# directory and hasn't synced this file yet), fall back to `cat` so the
# pipeline still runs end-to-end. The .log just mirrors the raw .jsonl in
# that case; the loop, the retry budget, and claude's exit code are all
# preserved.
if [[ -r "$FORMATTER" ]]; then
  FORMAT_CMD=(bash "$FORMATTER")
else
  echo "WARN: $FORMATTER not found — .log will mirror raw .jsonl. See docs/EXECUTION_MODES.md." >&2
  FORMAT_CMD=(cat)
fi

# stream-json emits realtime events (assistant text, tool_use, tool_result,
# partial message chunks before the model finalizes a response). Without it,
# -p text only prints the final response — useless when claude crashes
# mid-stream. --include-partial-messages captures the in-flight text right
# up to the moment of a filter trip or other API abort.
#
# 2>&1 mixes claude's stderr (e.g. "API Error: ...") into the pipe; the
# formatter passes non-JSON lines through unchanged so those errors still
# land in *.log alongside the JSON events.
#
# pipefail propagates a non-zero exit anywhere in the pipeline (most
# importantly claude's), so the caller still sees failure.
CLAUDE_FLAGS=(--permission-mode bypassPermissions --verbose
              --output-format stream-json --include-partial-messages)
if [[ "$CLAUDE_MODE" == "continue" ]]; then
  CLAUDE_FLAGS+=(-c)
fi

claude "${CLAUDE_FLAGS[@]}" -p "$PROMPT_CONTENT" 2>&1 \
  | tee "$RAW_LOG" \
  | "${FORMAT_CMD[@]}" \
  | tee "$LOG_FILE"