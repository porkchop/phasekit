#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS_DIR="$ROOT_DIR/artifacts"
RUN_PHASE_SCRIPT="$ROOT_DIR/scripts/run-phase.sh"
KICKOFF_PROMPT="${1:-$ROOT_DIR/KICKOFF_PROMPT.txt}"
CONTINUE_PROMPT="$ROOT_DIR/CONTINUE_PROMPT.txt"
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
  rm -f \
    "$ARTIFACTS_DIR/phase-approval.json" \
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

  if git diff --quiet && git diff --cached --quiet; then
    echo "No repo changes detected; skipping commit."
    return 0
  fi

  git add -A
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
    run_once "$KICKOFF_PROMPT" "new"
  else
    run_once "$CONTINUE_PROMPT" "continue"
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