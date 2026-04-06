#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS_DIR="$ROOT_DIR/artifacts"
RUN_PHASE_SCRIPT="$ROOT_DIR/scripts/run-phase.sh"
# The prompt file can be overridden via the first argument.
# Default is CONTINUE_PROMPT.txt which instructs Claude to find the
# earliest unapproved phase automatically. KICKOFF_PROMPT.txt and
# META_KICKOFF_PROMPT.txt exist for legacy/manual use but are not
# used by the autonomous loop since they target specific phases.
PROMPT_FILE="${1:-$ROOT_DIR/CONTINUE_PROMPT.txt}"
MAX_ITERATIONS="${MAX_ITERATIONS:-50}"
CLAUDE_MODE="${CLAUDE_MODE:-new}"

mkdir -p "$ARTIFACTS_DIR"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd jq
require_cmd git

cleanup_artifacts() {
  # Remove transient signal artifacts from the previous iteration.
  # phase-approval.json is NOT deleted — it persists as the durable
  # record of the last approved phase so the next iteration can read it.
  # Claude overwrites it when a new phase is approved.
  rm -f \
    "$ARTIFACTS_DIR/phase-update.json" \
    "$ARTIFACTS_DIR/phase-blocked.json" \
    "$ARTIFACTS_DIR/project-complete.json"
}

print_json_summary() {
  local file="$1"
  jq -r '.' "$file"
}

commit_from_artifact() {
  local file="$1"
  local fallback_msg="$2"

  local msg
  msg="$(jq -r '.suggested_commit_message // empty' "$file")"
  if [[ -z "$msg" ]]; then
    msg="$fallback_msg"
  fi

  # Force-add tracked artifact files (they may be partially gitignored)
  git add -f "$file" 2>/dev/null || true

  # Also stage any other repo changes
  git add -A

  if git diff --cached --quiet; then
    echo "No repo changes detected; skipping commit."
    return 0
  fi

  git commit -m "$msg"
}

run_once() {
  local prompt_file="$1"
  local mode="$2"

  if [[ "$mode" == "continue" ]]; then
    CLAUDE_MODE=continue "$RUN_PHASE_SCRIPT" "$prompt_file"
  else
    CLAUDE_MODE=new "$RUN_PHASE_SCRIPT" "$prompt_file"
  fi
}

iteration=1
while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do
  echo "=== Iteration $iteration ==="
  cleanup_artifacts

  if [[ "$iteration" -eq 1 && "$CLAUDE_MODE" == "new" ]]; then
    run_once "$PROMPT_FILE" "new"
  else
    run_once "$PROMPT_FILE" "continue"
  fi

  if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
    echo "Project complete artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/project-complete.json"
    echo "Run finished successfully."
    exit 0
  fi

  if [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    echo "Phase approval artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-approval.json"
    commit_from_artifact \
      "$ARTIFACTS_DIR/phase-approval.json" \
      "chore(workflow): approve completed phase"
    iteration=$((iteration + 1))
    continue
  fi

  if [[ -f "$ARTIFACTS_DIR/phase-update.json" ]]; then
    echo "Phase update artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-update.json"
    commit_from_artifact \
      "$ARTIFACTS_DIR/phase-update.json" \
      "chore(workflow): update phase plan and roadmap"
    iteration=$((iteration + 1))
    continue
  fi

  if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
    echo "Phase blocked artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
    echo "Stopping because external input is required."
    exit 2
  fi

  echo "No expected artifact found in $ARTIFACTS_DIR"
  echo "Expected one of:"
  echo "  - phase-approval.json"
  echo "  - phase-update.json"
  echo "  - phase-blocked.json"
  echo "  - project-complete.json"
  exit 1
done

echo "Reached MAX_ITERATIONS=$MAX_ITERATIONS without project completion."
exit 3