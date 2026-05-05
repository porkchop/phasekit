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
# Circuit breaker for the pre-commit verify gate. After this many consecutive
# failures on the same approval artifact, the loop writes phase-blocked.json
# and exits so a human can intervene. Override with VERIFY_MAX_ATTEMPTS.
VERIFY_MAX_ATTEMPTS="${VERIFY_MAX_ATTEMPTS:-3}"

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
  #
  # phase-verify-failed.json is NOT deleted here either — it's the
  # signal Claude needs to see at the start of the next iteration.
  # It is cleared after a successful verify run.
  rm -f \
    "$ARTIFACTS_DIR/phase-update.json" \
    "$ARTIFACTS_DIR/phase-blocked.json" \
    "$ARTIFACTS_DIR/project-complete.json"
}

print_json_summary() {
  local file="$1"
  jq -r '.' "$file"
}

run_verify_gate() {
  # Pre-commit verification gate. Runs project-defined fast checks (lint,
  # typecheck, unit tests) before any phase commit, regardless of AUTO_PUSH.
  #
  # Resolution order:
  #   1. PHASEKIT_VERIFY_CMD env var (one-shot override)
  #   2. scripts/phasekit-verify.sh (project-owned convention)
  #   3. No verify configured → warn + pass (fail-open for un-instrumented projects)
  #
  # On failure, writes artifacts/phase-verify-failed.json with the failing
  # command and a tail of its output. Returns non-zero so the caller skips
  # the commit; the loop continues so the next iteration can see the artifact
  # and fix the failure before doing new work.
  #
  # Escape hatch: VERIFY_SKIP=1 bypasses the gate entirely (sparingly — e.g.
  # docs-only phases or TDD phases that intentionally commit a red test).
  if [[ "${VERIFY_SKIP:-}" == "1" ]]; then
    echo "VERIFY_SKIP=1 — bypassing pre-commit verify gate."
    rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
    return 0
  fi

  local cmd=""
  local label=""
  local invoke=""
  if [[ -n "${PHASEKIT_VERIFY_CMD:-}" ]]; then
    cmd="$PHASEKIT_VERIFY_CMD"
    label="PHASEKIT_VERIFY_CMD"
    invoke="shell"
  elif [[ -f "$ROOT_DIR/scripts/phasekit-verify.sh" ]]; then
    cmd="$ROOT_DIR/scripts/phasekit-verify.sh"
    label="scripts/phasekit-verify.sh"
    invoke="bash"
  fi

  if [[ -z "$cmd" ]]; then
    echo "WARN: no verify configured (scripts/phasekit-verify.sh not present)" >&2
    echo "      see docs/QUALITY_GATES.md 'Pre-commit verification gate' to enable" >&2
    rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
    return 0
  fi

  echo "Pre-commit verify: $label"
  local log
  log="$(mktemp)"
  local verify_status=0
  if [[ "$invoke" == "bash" ]]; then
    # Project's script provides its own set -e/pipefail.
    bash "$cmd" >"$log" 2>&1 || verify_status=$?
  else
    # PHASEKIT_VERIFY_CMD may be a multi-command compound (e.g.
    # "lint && test"). Force -eo pipefail so a failing earlier
    # command isn't masked by a successful tail.
    bash -eo pipefail -c "$cmd" >"$log" 2>&1 || verify_status=$?
  fi
  if [[ "$verify_status" -eq 0 ]]; then
    echo "  Verify passed."
    rm -f "$log" "$ARTIFACTS_DIR/phase-verify-failed.json"
    return 0
  fi

  # Failure path. Capture context so the next iteration can diagnose.
  local exit_code="$verify_status"
  local prior_attempts=0
  if [[ -f "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
    prior_attempts="$(jq -r '.attempts // 0' "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null || echo 0)"
  fi
  local attempts=$((prior_attempts + 1))
  local tail_output
  tail_output="$(tail -n 200 "$log")"
  jq -n \
    --arg cmd "$cmd" \
    --arg label "$label" \
    --argjson exit_code "$exit_code" \
    --argjson attempts "$attempts" \
    --arg log "$tail_output" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      verify_failed: true,
      command: $cmd,
      label: $label,
      exit_code: $exit_code,
      attempts: $attempts,
      log_tail: $log,
      ts: $ts
    }' > "$ARTIFACTS_DIR/phase-verify-failed.json"

  echo "  Verify FAILED (attempt $attempts/$VERIFY_MAX_ATTEMPTS); see artifacts/phase-verify-failed.json" >&2
  echo "----- last 50 lines of verify output -----" >&2
  tail -n 50 "$log" >&2
  echo "------------------------------------------" >&2
  rm -f "$log"

  if [[ "$attempts" -ge "$VERIFY_MAX_ATTEMPTS" ]]; then
    echo "  Reached VERIFY_MAX_ATTEMPTS=$VERIFY_MAX_ATTEMPTS — writing phase-blocked.json and stopping." >&2
    jq -n \
      --arg cmd "$cmd" \
      --argjson attempts "$attempts" \
      '{
        blocked: true,
        reason: "pre-commit verify failed repeatedly",
        command: $cmd,
        attempts: $attempts,
        next_step: "fix the failing verify or set VERIFY_SKIP=1 for this iteration"
      }' > "$ARTIFACTS_DIR/phase-blocked.json"
  fi
  return 1
}

auto_push_if_enabled() {
  # Opt-in auto-push after a phase commit. Useful when the project needs
  # CI to fire on each phase (e.g. github-pages-as-progress-mirror, deploy
  # previews, integration tests in CI). Default off for safety — pushes are
  # observable and can cascade side effects.
  #
  # Enable: AUTO_PUSH=1 bash scripts/run-until-done.sh
  #
  # Pushes to the current branch's upstream (git push with no args).
  # Failures are non-fatal — the loop continues; the commit is already
  # local and a future push will catch up.
  if [[ "${AUTO_PUSH:-}" != "1" ]]; then
    return 0
  fi
  echo "AUTO_PUSH=1 — pushing to remote..."
  if git push 2>&1; then
    echo "  Pushed."
  else
    echo "  WARN: git push failed (commit is local; continuing loop)" >&2
  fi
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

  # Pre-commit verification gate. On failure, leave changes staged so the
  # next iteration can keep working from the same state, and return non-zero
  # so the caller does not advance the iteration counter.
  if ! run_verify_gate; then
    return 1
  fi

  git commit -m "$msg"
  auto_push_if_enabled
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

# Fresh-kickoff reset: phase-verify-failed.json is intentionally preserved
# across iterations within a run, but a *new* run starts a fresh attempt
# budget. Without this reset, a prior run interrupted at attempt 2 would
# circuit-break on the very next failure even after the user has fixed
# the underlying issue.
if [[ "$CLAUDE_MODE" == "new" && -f "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
  echo "Fresh kickoff (CLAUDE_MODE=new) — clearing stale phase-verify-failed.json from prior run."
  rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
fi

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
    if commit_from_artifact \
      "$ARTIFACTS_DIR/phase-approval.json" \
      "chore(workflow): approve completed phase"; then
      iteration=$((iteration + 1))
      continue
    fi
    # Verify gate blocked the commit. If the circuit breaker wrote
    # phase-blocked.json, the next loop iteration's blocker check will
    # exit cleanly. Otherwise re-enter so Claude can fix the failure.
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Phase blocked by verify circuit breaker:"
      print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
      exit 2
    fi
    iteration=$((iteration + 1))
    continue
  fi

  if [[ -f "$ARTIFACTS_DIR/phase-update.json" ]]; then
    echo "Phase update artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-update.json"
    if commit_from_artifact \
      "$ARTIFACTS_DIR/phase-update.json" \
      "chore(workflow): update phase plan and roadmap"; then
      iteration=$((iteration + 1))
      continue
    fi
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Phase blocked by verify circuit breaker:"
      print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
      exit 2
    fi
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