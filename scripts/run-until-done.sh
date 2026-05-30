#!/usr/bin/env bash
set -euo pipefail
# PHASEKIT_TRACE=1 turns on bash xtrace so every wrapper command is visible.
# Loud but useful for debugging the autonomous loop. See docs/EXECUTION_MODES.md.
[[ "${PHASEKIT_TRACE:-}" == "1" ]] && set -x

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

# Canonical upstream remote, used only when a downstream manifest predates the
# origin_url field. Keep in sync with CANONICAL_ORIGIN_URL in scripts/enrich-project.py.
PHASEKIT_CANONICAL_ORIGIN="https://github.com/porkchop/phasekit.git"

check_for_scaffold_update() {
  # Best-effort "a newer phasekit release exists" nudge, printed once at loop
  # start. Self-contained (bash + git + jq, both required above) — never depends
  # on the Python engine being vendored downstream. MUST NEVER block or fail the
  # loop: the network call is hard-bounded and every failure path is swallowed
  # (consistent with "observability must never break the loop"). The call site
  # invokes this as `... || true`, which also disables `set -e` for the body.
  # Opt out with PHASEKIT_NO_UPDATE_CHECK=1.
  [[ "${PHASEKIT_NO_UPDATE_CHECK:-}" == "1" ]] && return 0
  local manifest="$ROOT_DIR/.scaffold/manifest.json"
  [[ -f "$manifest" ]] || return 0

  local local_ver url latest
  local_ver="$(jq -r '.scaffold_version // empty' "$manifest" 2>/dev/null)" || return 0
  [[ -n "$local_ver" ]] || return 0
  url="$(jq -r '.origin_url // empty' "$manifest" 2>/dev/null)" || true
  [[ -n "$url" ]] || url="$PHASEKIT_CANONICAL_ORIGIN"
  # Normalize SSH/scp-style remotes to anonymous HTTPS so the check works
  # without SSH keys (phasekit is public; manifests often record the SSH origin).
  url="$(printf '%s' "$url" | sed -E 's#^git@([^:]+):#https://\1/#; s#^ssh://git@#https://#')"

  # Highest release tag upstream. One network call, hard-capped; any failure
  # (offline, firewall, timeout) just skips the nudge.
  latest="$(timeout 5 git ls-remote --tags --refs "$url" 'v*' 2>/dev/null \
    | sed -E 's#.*refs/tags/##' | sort -V | tail -n1)" || return 0
  [[ -n "$latest" ]] || return 0

  # Normalize both to bare semver: strip a leading 'v' and any describe suffix
  # (`-N-gSHA`, `-dirty`) or `+build` metadata. Legacy '0.0.0+git.*' has no 'v'
  # and normalizes to 0.0.0, so any real tag reads as newer.
  local norm_local norm_latest highest
  norm_local="$(printf '%s' "$local_ver" | sed -E 's/^v//; s/[-+].*$//')"
  norm_latest="$(printf '%s' "$latest" | sed -E 's/^v//; s/[-+].*$//')"
  [[ -n "$norm_latest" ]] || return 0
  [[ "$norm_local" == "$norm_latest" ]] && return 0

  highest="$(printf '%s\n%s\n' "$norm_local" "$norm_latest" | sort -V | tail -n1)"
  if [[ "$highest" == "$norm_latest" ]]; then
    echo "ℹ phasekit ${local_ver} → ${latest} available — run 'phasekit --upgrade' (see docs/RELEASING.md)" >&2
  fi
  return 0
}

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

  # Never commit per-iteration logs. run-phase.sh rewrites artifacts/logs/*
  # every iteration (the iteration counter resets on each run), so committing
  # them floods history with churn AND lets a no-progress iteration look like
  # a real change. Keep them on disk for live tailing/forensics; just don't
  # stage them. (Autonomous-loop-only — logs only exist during loop runs.)
  git reset -q -- "$ARTIFACTS_DIR/logs" 2>/dev/null || true

  # Substantive-change gate. A blocked or stalled iteration must still write
  # *some* signal artifact (the loop contract requires one), and a prior
  # phase-approval.json persists on disk as the durable approval record. Left
  # unchecked, that persisted approval alone drives the commit path, so the
  # only staged content ends up being the re-emitted transient signal — an
  # inconsequential commit with no progress behind it (see foundry debe2d7).
  # Treat the transient signals as non-substantive: if nothing else is staged,
  # skip the commit and return 2 so the caller falls through to its blocked
  # handler instead of committing churn.
  if git diff --cached --quiet -- ':/' \
       ":(exclude)$ARTIFACTS_DIR/phase-blocked.json" \
       ":(exclude)$ARTIFACTS_DIR/phase-verify-failed.json"; then
    echo "No substantive change staged (only logs or transient signals); skipping commit."
    return 2
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
  local iter_num="$3"
  local retry_attempt="${4:-0}"

  if [[ "$mode" == "continue" ]]; then
    CLAUDE_MODE=continue \
      PHASEKIT_ITER="$iter_num" \
      PHASEKIT_RETRY_ATTEMPT="$retry_attempt" \
      "$RUN_PHASE_SCRIPT" "$prompt_file"
  else
    CLAUDE_MODE=new \
      PHASEKIT_ITER="$iter_num" \
      PHASEKIT_RETRY_ATTEMPT="$retry_attempt" \
      "$RUN_PHASE_SCRIPT" "$prompt_file"
  fi
}

iteration=1

# Per-iteration retry budget for transient claude CLI failures (e.g. an
# API-side content-filter trip that aborts a response mid-stream, a 5xx, or
# a transient network blip). On a non-zero exit from claude we re-attempt
# the same iteration in `continue` mode, up to PHASEKIT_ITER_RETRY times,
# without advancing the iteration counter. Set to 0 to disable retries and
# exit on the first failure (the pre-retry historical behavior).
ITER_RETRY_LIMIT="${PHASEKIT_ITER_RETRY:-1}"
retries_used=0

# Fresh-kickoff reset: phase-verify-failed.json is intentionally preserved
# across iterations within a run, but a *new* run starts a fresh attempt
# budget. Without this reset, a prior run interrupted at attempt 2 would
# circuit-break on the very next failure even after the user has fixed
# the underlying issue.
if [[ "$CLAUDE_MODE" == "new" && -f "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
  echo "Fresh kickoff (CLAUDE_MODE=new) — clearing stale phase-verify-failed.json from prior run."
  rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
fi

# Once-per-run, non-fatal nudge if a newer phasekit release is available.
check_for_scaffold_update || true

while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do
  echo "=== Iteration $iteration ==="
  cleanup_artifacts

  # First attempt of iteration 1 in `new` mode uses fresh-session semantics;
  # retries (and every later iteration) use `continue` so they resume the
  # session that was just established rather than starting a new one.
  rc=0
  if [[ "$iteration" -eq 1 && "$CLAUDE_MODE" == "new" && "$retries_used" -eq 0 ]]; then
    run_once "$PROMPT_FILE" "new" "$iteration" "$retries_used" || rc=$?
  else
    run_once "$PROMPT_FILE" "continue" "$iteration" "$retries_used" || rc=$?
  fi

  if [[ "$rc" -ne 0 ]]; then
    if [[ "$retries_used" -lt "$ITER_RETRY_LIMIT" ]]; then
      retries_used=$((retries_used + 1))
      echo "Iteration $iteration: claude exited $rc; retrying in continue mode (retry $retries_used/$ITER_RETRY_LIMIT)." >&2
      continue
    fi
    echo "Iteration $iteration: claude exited $rc; per-iteration retry budget exhausted." >&2
    exit "$rc"
  fi
  retries_used=0

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
    # No commit was made: either the verify gate failed, or there was no
    # substantive change to commit (only logs/transient signals). In both
    # cases, if phase-blocked.json is present the iteration is genuinely
    # blocked — stop cleanly rather than spinning to MAX_ITERATIONS or
    # committing churn. Otherwise re-enter so Claude can make progress
    # (or fix a verify failure) on the next iteration.
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Phase blocked; no substantive change to commit:"
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
      echo "Phase blocked; no substantive change to commit:"
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