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
CLAUDE_MODE="${CLAUDE_MODE:-new}"

# Iteration mode (v0.6.0): "standard" (default) or "light". Light mode is the
# reduced-ceremony path for small, triaged tasks: one collapsed phase (build +
# verify + review), no strategy-planner/architecture-red-team, iteration cap 2,
# a default-model review pass before the final commit, and escalation instead
# of grinding. Set per-session by the outer supervisor via container env
# (PHASEKIT_ITERATION_MODE=light) — never a committed setting. Eligibility
# requires a configured (non-stub) verify gate; see docs/EXECUTION_MODES.md.
ITERATION_MODE="${PHASEKIT_ITERATION_MODE:-standard}"

# Circuit breaker for the pre-commit verify gate. After this many consecutive
# failures on the same approval artifact, the loop writes phase-blocked.json
# and exits so a human can intervene. Override with VERIFY_MAX_ATTEMPTS.
# Both this and MAX_ITERATIONS get their defaults in the iteration-mode
# resolution block below (standard: 50/3; light: 2/2 per the 2026-08-10
# design decision — escalate after 2 verify failures).

# Verify-budget advisory (v0.6.4, fail-open). The gate targets ~30s with a
# 60s ceiling (docs/QUALITY_GATES.md "Verify budget"); when a run exceeds the
# ceiling on 2+ runs in one session, print ONE advisory line pointing at the
# fast/slow split. Never blocks, never edits the project's gate.
VERIFY_BUDGET_SECONDS="${PHASEKIT_VERIFY_BUDGET_SECONDS:-60}"
[[ "$VERIFY_BUDGET_SECONDS" =~ ^[0-9]+$ ]] || VERIFY_BUDGET_SECONDS=60
VERIFY_OVER_BUDGET_RUNS=0
VERIFY_BUDGET_ADVISED=0

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

# Transient-signal family (completed in v0.6.5). Every loop-emitted signal the
# loop (or the orchestrator) later deletes behind git's back. None of these may
# EVER be committed: a tracked copy turns that deletion into a staged deletion
# the substantive-change gate may refuse forever — the tree goes permanently
# dirty and every clean-tree guard downstream trips (foundry-dashboard task
# #100: spec-change.json; the phase-blocked.json stranding before it).
# Deliberate absences — committed on purpose, not transient: phase-approval,
# phase-update, project-complete, session-handoff, ready-to-deploy, and the
# orchestrator's iteration-mode.json (written INSIDE the iteration commit).
TRANSIENT_SIGNALS=(
  "phase-blocked.json"
  "phase-verify-failed.json"
  "spec-change.json"
  "scope-warning.json"
  "scope-refusal.json"
  "light-escalation.json"
  ".scope-check.tmp"
  ".stop-hook-blocks"
  ".wrapup-nudge-sent"
)

# The subset also hidden from `git status` via .git/info/exclude: consumed from
# disk (the orchestrator's session_signals/record_run read-then-delete them, or
# the commit path itself cleans them up), so hiding them hides nothing a human
# needs. phase-blocked.json and phase-verify-failed.json are deliberately NOT
# here — a live blocker must stay visible in `git status`; they are kept
# uncommittable by unstage_transient_adds + the no-churn gate instead.
HIDDEN_TRANSIENTS=(
  "spec-change.json"
  "scope-warning.json"
  "scope-refusal.json"
  "light-escalation.json"
  ".scope-check.tmp"
  ".stop-hook-blocks"
  ".wrapup-nudge-sent"
)

# --- The verdict vocabulary -------------------------------------------------
# The artifacts that constitute a session ENDING WITH AN ANSWER. Derived from
# this file's own dispatch below, and the single source of truth for the
# question "did this iteration produce a verdict?" — asked in three places:
#   1. the Stop hook (.claude/hooks/require-verdict.sh), via the export below
#   2. the loop's own no-verdict retry backstop
#   3. the loop's dispatch, which acts on each in turn
# Exporting it rather than restating it in the hook is what stops the hook and
# the dispatcher from disagreeing about what an ending is.
#
# scope-refusal.json and light-escalation.json are included deliberately: they
# are legitimate ways for a session to end, and a hook that blocked on them
# would be fighting the loop. phase-verify-failed.json, scope-warning.json and
# spec-change.json are NOT verdicts — they are the loop's own commentary on a
# session that is still expected to answer.
VERDICT_ARTIFACTS=(
  "project-complete.json"
  "phase-approval.json"
  "phase-update.json"
  "phase-blocked.json"
  "scope-refusal.json"
  "light-escalation.json"
)
export PHASEKIT_VERDICT_ARTIFACTS="${VERDICT_ARTIFACTS[*]}"
export PHASEKIT_ARTIFACTS_DIR="$ARTIFACTS_DIR"

ensure_transients_excluded() {
  # The loop never commits artifacts/logs/* (see commit_from_artifact), and the
  # wrap-up sentinel (v0.6.0) is an outer-supervisor signal file, but leaving
  # either untracked-and-unignored makes every post-run `git status
  # --porcelain` cleanliness check (e.g. an orchestrator's iterate/intake
  # gate) see a dirty tree. Same for the HIDDEN_TRANSIENTS signal files
  # (v0.6.5). Exclude them repo-locally via .git/info/exclude — unlike
  # .gitignore this ships nothing downstream and can't collide with
  # project-owned ignore rules. Best-effort: never blocks the loop.
  # (A custom PHASEKIT_WRAPUP_SENTINEL path outside artifacts/ is the
  # overrider's responsibility to keep out of git status.)
  local exclude_file line sig
  exclude_file="$(git -C "$ROOT_DIR" rev-parse --git-path info/exclude 2>/dev/null)" || return 0
  [[ -n "$exclude_file" ]] || return 0
  # rev-parse --git-path may return a relative path; resolve from ROOT_DIR.
  [[ "$exclude_file" = /* ]] || exclude_file="$ROOT_DIR/$exclude_file"
  mkdir -p "$(dirname "$exclude_file")" 2>/dev/null || return 0
  local lines=("artifacts/logs/" "artifacts/wrapup-requested")
  for sig in "${HIDDEN_TRANSIENTS[@]}"; do
    lines+=("artifacts/$sig")
  done
  for line in "${lines[@]}"; do
    grep -qxF "$line" "$exclude_file" 2>/dev/null && continue
    echo "$line" >> "$exclude_file" 2>/dev/null || true
  done
  return 0
}

unstage_transient_adds() {
  # `git add -A` must never ADD a transient signal (v0.6.5) — that is exactly
  # how spec-change.json became tracked in foundry-dashboard: written by the
  # previous commit's own bookkeeping, still on disk at the next commit,
  # swept in by add -A. The HIDDEN_TRANSIENTS are already invisible to add -A
  # via info/exclude; this covers the visible pair (and belt-and-braces the
  # rest, e.g. against a project .gitignore rule that re-includes artifacts/).
  # For a member a pre-v0.6.5 history still tracks, the just-run `git add -A`
  # has CANCELLED any heal deletion staged at loop start (the on-disk copy
  # re-enters the index byte-identical to HEAD), so re-stage the untracking
  # here rather than trusting the deferred heal to survive (v0.6.6) — this is
  # the last point before the commit where it can be restored
  # (see heal_tracked_transients).
  local sig
  for sig in "${TRANSIENT_SIGNALS[@]}"; do
    if git cat-file -e "HEAD:artifacts/$sig" 2>/dev/null; then
      git rm --cached -q --ignore-unmatch -- "$ARTIFACTS_DIR/$sig" 2>/dev/null || true
    else
      git reset -q -- "$ARTIFACTS_DIR/$sig" 2>/dev/null || true
    fi
  done
  return 0
}

heal_tracked_transients() {
  # Self-heal for a pre-v0.6.5 history that already tracks a transient signal
  # (v0.6.5). The loop/orchestrator deletes these files behind git's back, so
  # a tracked copy strands the tree: for the no-churn-exempt pair the staged
  # deletion can never satisfy the substantive-change gate on its own, and for
  # the rest it only heals by riding along with unrelated work. Untrack them
  # mechanically at loop start — never depend on the model noticing.
  #
  # The heal commit is index-only (files stay on disk where present) and runs
  # WITHOUT the verify gate: the working tree is byte-identical before and
  # after, so verify's inputs are unchanged, and gating it would block the
  # heal exactly when the tree is broken for unrelated reasons (gate-recovery
  # principle). It is only created when the index is clean apart from this
  # family; otherwise the staged untracking rides with the session's next
  # commit — whose own `git add -A` would cancel it if the file is still on
  # disk, so unstage_transient_adds re-stages it there (v0.6.6).
  # Best-effort throughout: never blocks the loop.
  local sig tracked=()
  git cat-file -e HEAD 2>/dev/null || return 0
  for sig in "${TRANSIENT_SIGNALS[@]}"; do
    git cat-file -e "HEAD:artifacts/$sig" 2>/dev/null || continue
    tracked+=("artifacts/$sig")
  done
  [[ ${#tracked[@]} -gt 0 ]] || return 0
  echo "Transient signal artifact(s) are tracked from a pre-v0.6.5 history — untracking: ${tracked[*]}"
  git rm --cached -q --ignore-unmatch -- "${tracked[@]}" 2>/dev/null || return 0
  local excl=(':/')
  for sig in "${TRANSIENT_SIGNALS[@]}"; do
    excl+=(":(exclude)artifacts/$sig")
  done
  if git diff --cached --quiet -- "${excl[@]}"; then
    if git commit -q -m "chore(workflow): untrack transient signal artifacts (phasekit v0.6.5 heal)"; then
      echo "  Heal commit created (index-only; files remain on disk where present)."
    else
      echo "  WARN: heal commit failed — staged untracking left to ride with the next commit." >&2
    fi
  else
    echo "  Index has other staged changes — the untracking will ride with the session's next commit."
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
  # session-handoff.json (v0.6.1) is deliberately NOT removed here — it is the
  # previous session's wrap-up baton and must survive into the next session's
  # first iteration; the next session's orientation (CONTINUE_PROMPT) deletes
  # it after reading.
  rm -f \
    "$ARTIFACTS_DIR/phase-update.json" \
    "$ARTIFACTS_DIR/phase-blocked.json" \
    "$ARTIFACTS_DIR/project-complete.json" \
    "$ARTIFACTS_DIR/light-escalation.json"
  # The Stop hook's block budget is per-iteration: a session that was nudged
  # last iteration starts the next one with a full allowance, and a healthy
  # iteration never creates the file at all.
  rm -f "$ARTIFACTS_DIR/.stop-hook-blocks"
  # The wrap-up nudge (PostToolUse hook) is once-per-iteration by the same
  # freshness idiom; clearing its marker here keeps the two hook budgets on
  # one lifecycle. A healthy iteration (no sentinel) never creates it.
  rm -f "$ARTIFACTS_DIR/.wrapup-nudge-sent"
}

print_json_summary() {
  local file="$1"
  jq -r '.' "$file"
}

record_verify_failure() {
  # Single source of truth for the verify-failure capture. Both the contracts
  # gate and the project's verify script fail through here, so the next
  # iteration's recovery path, the attempts counter and the VERIFY_MAX_ATTEMPTS
  # breaker behave identically no matter which gate refused. (A second copy of
  # this logic is exactly how wrapup_commit silently lost the post-verify gates
  # before v0.6.6.)
  local cmd="$1" label="$2" exit_code="$3" log="$4"

  local prior_attempts=0
  # A zero-byte artifact (crashed earlier writer) makes `jq -r` emit nothing
  # with exit 0, so prior_attempts became "" and the arithmetic below aborted
  # the whole capture under set -e — permanently re-poisoning the file and
  # defeating the VERIFY_MAX_ATTEMPTS breaker (foundry-orchestrator run 49,
  # 2026-08-08). Purge empty files and sanitize the read to digits.
  if [[ -f "$ARTIFACTS_DIR/phase-verify-failed.json" && ! -s "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
    rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
  fi
  if [[ -f "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
    prior_attempts="$(jq -r '.attempts // 0' "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null || echo 0)"
  fi
  [[ "$prior_attempts" =~ ^[0-9]+$ ]] || prior_attempts=0
  local attempts=$((prior_attempts + 1))
  local tail_output
  tail_output="$(tail -n 200 "$log")"
  if ! jq -n \
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
    }' > "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null; then
    # jq can choke on pathological log bytes; never leave a zero-byte
    # artifact behind — write a minimal valid capture instead.
    printf '{"verify_failed": true, "label": "%s", "exit_code": %s, "attempts": %s, "log_tail": "(unavailable: capture failed)", "ts": "%s"}\n' \
      "$label" "$exit_code" "$attempts" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > "$ARTIFACTS_DIR/phase-verify-failed.json"
  fi

  echo "  Verify FAILED (attempt $attempts/$VERIFY_MAX_ATTEMPTS); see artifacts/phase-verify-failed.json" >&2
  echo "----- last 50 lines of verify output -----" >&2
  tail -n 50 "$log" >&2
  echo "------------------------------------------" >&2

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
}

run_contracts_gate() {
  # Cross-project contracts (v0.7.0). Refuses the commit when this repo's OWN
  # contracts.yaml declares a dependency whose contract is unobtainable, whose
  # vendored copy has drifted from the provider's authoritative one, or which
  # nothing present can verify at all (v0.7.1).
  #
  # Three deliberate properties:
  #
  # 1. INERT without a declaration. A repo with no contracts.yaml never reaches
  #    the checker's failure paths, so phasekit keeps working with no
  #    orchestrator at all — a public `curl | bash` tool cannot make Foundry a
  #    prerequisite (docs/META_SPEC.md).
  # 2. Runs BEFORE the VERIFY_SKIP bypass. VERIFY_SKIP is the per-iteration
  #    hatch for red TDD commits and docs-only phases, and a builder sets it
  #    routinely; letting it also switch off contract authenticity would neuter
  #    the gate exactly when a red gate is applying the pressure to cheat.
  #    PHASEKIT_CONTRACTS_SKIP=1 is the separate, loud, operator-only hatch.
  # 3. Lives HERE, in a phasekit-owned script, not only in the project-owned
  #    scripts/phasekit-verify.sh. A repo that can edit away the check that
  #    polices it is not policed. Same principle as the secret-lint allowlist
  #    living on the operator side of the deploy boundary.
  local checker="$ROOT_DIR/scripts/phasekit-contracts.py"
  [[ -f "$ROOT_DIR/contracts.yaml" ]] || return 0

  if [[ "${PHASEKIT_CONTRACTS_SKIP:-}" == "1" ]]; then
    echo "PHASEKIT_CONTRACTS_SKIP=1 — bypassing the cross-project contracts gate (contracts.yaml IS present)." >&2
    return 0
  fi

  if [[ ! -f "$checker" ]]; then
    # contracts.yaml present but the checker absent = a stale vendored
    # scripts/ directory. REFUSE (v0.7.1). This warned and passed in v0.7.0,
    # which was the single fail-open path in the whole feature and contradicted
    # property 3 above: a declaring repo could disable its own gate by deleting
    # one file.
    #
    # The realistic trigger is not malice, it is the upgrade seam. The fleet
    # upgrades project by project, so a project can acquire contracts.yaml from
    # a build while its vendored scripts/ is still pre-v0.7.0 — leaving the gate
    # silently off in exactly the window where drift is most likely.
    #
    # Recoverable in one command, which is why refusing is safe here: the
    # message names `phasekit upgrade`, and PHASEKIT_CONTRACTS_SKIP=1 remains
    # the operator hatch (checked above, so it still wins).
    local log
    log="$(mktemp)"
    {
      echo "phasekit-contracts: REFUSING — contracts.yaml declares contract dependencies"
      echo "  but scripts/phasekit-contracts.py is missing, so nothing can verify them."
      echo "  This repo's phasekit scaffold predates v0.7.0."
      echo "  Fix with:  phasekit upgrade"
      echo "  (Or remove contracts.yaml if this repo no longer depends on another"
      echo "  project's interface. Committing with a declaration nobody checks is the"
      echo "  exact failure this feature exists to prevent.)"
    } >"$log"
    cat "$log" >&2
    # Exit code 5 is emitted by the LOOP, not by phasekit-contracts.py (whose
    # table stops at 4). Kept distinct from 2 (malformed declaration) because
    # the repair is different: 2 means fix your file, 5 means upgrade phasekit.
    record_verify_failure "phasekit upgrade" "contracts" 5 "$log"
    rm -f "$log"
    return 1
  fi

  echo "Pre-commit verify: cross-project contracts (contracts.yaml)"
  local log
  log="$(mktemp)"
  local status=0
  python3 "$checker" --repo "$ROOT_DIR" check >"$log" 2>&1 || status=$?
  if [[ "$status" -eq 0 ]]; then
    cat "$log"
    rm -f "$log"
    return 0
  fi
  record_verify_failure "python3 scripts/phasekit-contracts.py check" "contracts" "$status" "$log"
  rm -f "$log"
  return 1
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
  # Cross-project contracts run first and are NOT covered by VERIFY_SKIP —
  # see run_contracts_gate for why. Inert unless this repo declares.
  if ! run_contracts_gate; then
    return 1
  fi

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
    # Expected on the phasekit source repo itself: the verify script is rendered
    # into downstream projects but not committed here, so self-improvement loops
    # fail-open. Known gap — see docs/QUALITY_GATES.md "Self-hosting gap".
    echo "WARN: no verify configured (scripts/phasekit-verify.sh not present)" >&2
    echo "      see docs/QUALITY_GATES.md 'Pre-commit verification gate' to enable" >&2
    rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
    return 0
  fi

  echo "Pre-commit verify: $label"
  local log
  log="$(mktemp)"
  local verify_status=0
  local verify_start verify_elapsed
  verify_start="$(date +%s)"
  if [[ "$invoke" == "bash" ]]; then
    # Project's script provides its own set -e/pipefail.
    bash "$cmd" >"$log" 2>&1 || verify_status=$?
  else
    # PHASEKIT_VERIFY_CMD may be a multi-command compound (e.g.
    # "lint && test"). Force -eo pipefail so a failing earlier
    # command isn't masked by a successful tail.
    bash -eo pipefail -c "$cmd" >"$log" 2>&1 || verify_status=$?
  fi
  verify_elapsed=$(( $(date +%s) - verify_start ))

  # Verify-budget advisory (v0.6.4). Counts pass and fail alike — the drift
  # being measured is suite growth, not correctness. Once per session.
  if (( verify_elapsed > VERIFY_BUDGET_SECONDS )); then
    VERIFY_OVER_BUDGET_RUNS=$((VERIFY_OVER_BUDGET_RUNS + 1))
    if (( VERIFY_OVER_BUDGET_RUNS >= 2 && VERIFY_BUDGET_ADVISED == 0 )); then
      VERIFY_BUDGET_ADVISED=1
      echo "ADVISORY: verify exceeded its budget (${verify_elapsed}s > ${VERIFY_BUDGET_SECONDS}s, ${VERIFY_OVER_BUDGET_RUNS} runs this session) — see docs/QUALITY_GATES.md 'Verify budget' for the fast/slow split."
    fi
  fi

  if [[ "$verify_status" -eq 0 ]]; then
    echo "  Verify passed."
    rm -f "$log" "$ARTIFACTS_DIR/phase-verify-failed.json"
    return 0
  fi

  # Failure path. Capture context so the next iteration can diagnose.
  record_verify_failure "$cmd" "$label" "$verify_status" "$log"
  rm -f "$log"
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

staged_touches_security_pair() {
  # Single source of truth for the scope-containment hard-refuse pair
  # (v0.4.8): committed .claude/settings.json and .github/workflows/ are
  # security-critical and never committed by the loop, on any commit path.
  git diff --cached --name-only | grep -qE '^\.claude/settings\.json$|^\.github/workflows/'
}

post_verify_commit_gates() {
  # Post-verify gates shared by EVERY commit surface (v0.6.6). wrapup_commit
  # once reimplemented the commit sequence and silently dropped these — a
  # credential-shaped line in docs/LEARNINGS.md could land (and auto-push)
  # via wrap-up when an identical iteration commit would have been refused.
  # Any future commit path must call this after its verify gate.
  #   $1 = context: "iteration" (light-mode scaffold edits escalate, rc 4)
  #        or "wrapup" (the session is ending — record the warning, proceed).
  # Returns 0 to commit, 1 to refuse the commit, 4 to escalate a light task.
  local context="${1:-iteration}"
  local staged
  staged="$(git diff --cached --name-only)"

  # Scope containment warn-path (v0.4.8, ADOPTIONS item 4 warn-first):
  # scaffold-class edits warn via artifact (surfaced by the orchestrator) and
  # proceed — per the gate-recovery principle, build-loop gates must not
  # create stuck states.
  if [[ -f "$ROOT_DIR/.scaffold/manifest.json" ]]; then
    STAGED_FILES="$staged" python3 - "$ROOT_DIR/.scaffold/manifest.json" > "$ARTIFACTS_DIR/.scope-check.tmp" 2>/dev/null <<'PY' || true
import json, os, sys
manifest = json.load(open(sys.argv[1]))
scaffold = {f["path"] for f in manifest.get("files", [])
            if f.get("ownership") == "scaffold"}
hits = sorted(set(os.environ.get("STAGED_FILES", "").split()) & scaffold)
if hits:
    from datetime import datetime, timezone
    print(json.dumps({"scope_warning": True, "files": hits,
                      "ts": datetime.now(timezone.utc).isoformat()}))
PY
    if [[ -s "$ARTIFACTS_DIR/.scope-check.tmp" ]]; then
      mv "$ARTIFACTS_DIR/.scope-check.tmp" "$ARTIFACTS_DIR/scope-warning.json"
      if [[ "$context" == "iteration" && "$ITERATION_MODE" == "light" ]]; then
        # Light tasks are triaged as low-blast-radius; a scaffold-class edit is
        # out-of-scope by definition and escalates instead of warning-and-
        # continuing (DESIGN-light-pipeline.md guardrails). No commit is made;
        # the caller turns rc=4 into a light-escalation exit. (At wrap-up the
        # session is ending anyway — record the warning and let the commit
        # stand rather than strand the work.)
        echo "run-until-done: light mode — staged changes touch scaffold-class files; escalating to a standard iteration instead of committing." >&2
        return 4
      fi
      echo "run-until-done: WARNING — this commit edits scaffold-class files (recorded in artifacts/scope-warning.json; drift-check will also flag them). Proceeding." >&2
    else
      rm -f "$ARTIFACTS_DIR/.scope-check.tmp"
    fi
  fi

  # SPEC change attestation (v0.4.8, ADOPTIONS item 2 simplified): make SPEC
  # edits visible, never gated — record the staged numstat for the
  # orchestrator to surface (brief line; advisory only above its threshold).
  if echo "$staged" | grep -q '^docs/SPEC\.md$'; then
    read -r spec_added spec_removed _ < <(git diff --cached --numstat -- docs/SPEC.md)
    printf '{"spec_changed": true, "added_lines": %s, "removed_lines": %s, "ts": "%s"}\n' \
      "${spec_added:-0}" "${spec_removed:-0}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > "$ARTIFACTS_DIR/spec-change.json"
    echo "run-until-done: note — docs/SPEC.md changed in this commit (+${spec_added:-0}/-${spec_removed:-0}); recorded in artifacts/spec-change.json" >&2
  fi

  # Learnings secret scan (v0.4.7): docs/LEARNINGS.md is agent-appended free
  # text that ships in commits — refuse the commit if it matches obvious
  # credential shapes. Narrow patterns on purpose: false positives here block
  # real work (gate-recovery principle); the promote/mirror gates carry the
  # broad lint. Covers every staged docs/LEARNINGS*.md (not just the main
  # file) so text a curation session moves to an archive sibling cannot
  # dodge the gate by changing filename.
  local learnings_file
  while IFS= read -r learnings_file; do
    [[ -n "$learnings_file" && -f "$ROOT_DIR/$learnings_file" ]] || continue
    if grep -nE 'sk-ant-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' \
        "$ROOT_DIR/$learnings_file" >&2; then
      echo "run-until-done: REFUSED — $learnings_file matches a credential pattern (lines above). Remove the secret and retry." >&2
      return 1
    fi
  done < <(echo "$staged" | grep -E '^docs/LEARNINGS[^/]*\.md$')
  return 0
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

  # Never commit transient signals either (v0.6.5) — fresh adds only; a staged
  # deletion of a legacy tracked copy rides so the untracking lands.
  unstage_transient_adds

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

  # Scope containment (v0.4.8, ADOPTIONS item 4 warn-first). Hard-refuse only
  # the security pair where a bad commit IS the damage; the remaining
  # post-verify gates are shared with the wrap-up path (v0.6.6).
  if staged_touches_security_pair; then
    # Explain the refusal where the NEXT session will find it, so staged-but-
    # uncommitted work is never a mystery state.
    printf '{"scope_refused": true, "reason": "staged changes touch committed .claude/settings.json or .github/workflows/ — security-critical, never committed by the loop (docs/QUALITY_GATES.md scope containment)", "action": "git restore --staged <those files> (and revert them) then re-write your signal artifact; the wrapper will retry the commit", "ts": "%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ARTIFACTS_DIR/scope-refusal.json"
    echo "run-until-done: REFUSED — staged changes touch committed .claude/settings.json or .github/workflows/ (security-critical). See artifacts/scope-refusal.json." >&2
    return 1
  fi
  rm -f "$ARTIFACTS_DIR/scope-refusal.json"

  post_verify_commit_gates iteration || return $?

  git commit -m "$msg"
  auto_push_if_enabled
}

artifact_written_this_iteration() {
  # Phase-commit atomicity (v0.6.0). phase-approval.json persists across
  # iterations as the durable record of the last approved phase, so its mere
  # existence must never drive a commit — that is exactly how later in-flight
  # work got committed under the WRONG phase's message (nine consecutive
  # instances documented in foundry-dashboard's iteration-11 forensics, one of
  # which carried an ungated user-visible defect into the repo). Only an
  # artifact (re)written during THIS iteration may drive a commit and supply
  # its message. ITER_START_MARKER is touched immediately before each claude
  # invocation.
  [[ -f "$1" && "$1" -nt "$ITER_START_MARKER" ]]
}

has_verdict_artifact() {
  # Did THIS iteration produce a verdict? Asked by the no-verdict retry
  # backstop, and by the Stop hook via the exported vocabulary — one list, one
  # freshness rule, so the two can never disagree about what an ending is.
  local name
  for name in "${VERDICT_ARTIFACTS[@]}"; do
    if artifact_written_this_iteration "$ARTIFACTS_DIR/$name"; then
      return 0
    fi
  done
  return 1
}

write_session_handoff() {
  # Handoff baton (v0.6.1): composed by the loop from what it already knows —
  # never by invoking claude again (zero extra tokens). Written on every
  # wrap-up path that leaves standing work, BEFORE the wrap-up commit so it
  # lands inside it (or stays untracked when no commit is made — the case
  # where next-session orientation matters most). Ephemeral: the next
  # session's CONTINUE_PROMPT orientation reads then deletes it; durable
  # learnings belong in docs/LEARNINGS.md.
  local verified="$1"
  local next_step="$2"
  local phase="unknown"
  if [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    phase="$(jq -r '.phase // "unknown"' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)" || phase="unknown"
    [[ -n "$phase" ]] || phase="unknown"
  fi
  local files in_flight
  files="$(git diff --cached --name-only | grep -v '^artifacts/' | head -8 | tr '\n' ' ')" || files=""
  in_flight="uncommitted work in: ${files:-(only artifacts/ signals)}"
  jq -n \
    --arg phase "$phase" \
    --arg in_flight "$in_flight" \
    --argjson verified "$verified" \
    --arg next_step "$next_step" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      stopped_at_phase: $phase,
      in_flight: $in_flight,
      verified: $verified,
      next_step: $next_step,
      note: "ephemeral wrap-up baton: read to orient, then delete (stopped_at_phase = last APPROVED phase; the session stopped somewhere after it)",
      ts: $ts
    }' > "$ARTIFACTS_DIR/session-handoff.json"
}

wrapup_commit() {
  # Soft wrap-up (v0.6.0). When the outer supervisor signals imminent shutdown
  # (see WRAPUP_SENTINEL below) or deadline pacing fires (v0.6.1), commit
  # whatever stands — verify-gated — so a session's end no longer depends on
  # the hard kill that loses in-flight context (every 2026-08-10 session ended
  # exit_reason: timeout). Never creates a commit the normal gates would
  # refuse: verify must pass and the same post-verify gates as an iteration
  # commit apply (v0.6.6 — security pair, scope warning, SPEC attestation,
  # LEARNINGS secret scan). On refusal the work is left in the tree — with a
  # handoff baton — for the next session (and the scheduler's
  # complete-but-dirty backstop) to reconcile.
  git add -A
  git reset -q -- "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  git reset -q -- "$WRAPUP_SENTINEL" 2>/dev/null || true
  unstage_transient_adds
  if git diff --cached --quiet -- ':/' \
       ":(exclude)$ARTIFACTS_DIR/phase-blocked.json" \
       ":(exclude)$ARTIFACTS_DIR/phase-verify-failed.json"; then
    echo "Wrap-up: tree already clean — nothing substantive to commit."
    return 0
  fi
  if staged_touches_security_pair; then
    write_session_handoff false "unstage and revert the staged .claude/settings.json / .github/workflows/ changes — the loop never commits them — then redo the phase work without touching them"
    echo "Wrap-up: staged changes touch committed .claude/settings.json or .github/workflows/ — leaving work uncommitted (security-critical, never committed by the loop). Handoff note written." >&2
    return 0
  fi
  if ! run_verify_gate; then
    write_session_handoff false "fix the verify failure recorded in artifacts/phase-verify-failed.json, then re-commit the standing work"
    echo "Wrap-up: verify failed — leaving work uncommitted (phase-verify-failed.json + session-handoff.json record the state for the next session)." >&2
    return 0
  fi
  if ! post_verify_commit_gates wrapup; then
    write_session_handoff false "the post-verify commit gates refused the staged work (see the REFUSED line in the session log — e.g. remove a credential-shaped line from docs/LEARNINGS.md), then re-commit the standing work"
    echo "Wrap-up: post-verify gates refused the staged work — leaving it uncommitted (session-handoff.json records the state)." >&2
    return 0
  fi
  write_session_handoff true "standing work was committed at wrap-up; re-orient and continue from the next unapproved phase"
  git add -f "$ARTIFACTS_DIR/session-handoff.json" 2>/dev/null || true
  git commit -m "chore(workflow): session wrap-up — soft stop before session end"
  auto_push_if_enabled
}

light_verify_configured() {
  # Light-mode eligibility: reduced ceremony only where mechanical verification
  # is strong (DESIGN-light-pipeline.md guardrail #1). An explicit
  # PHASEKIT_VERIFY_CMD counts as configured; otherwise the project's verify
  # script must exist and must not still carry the stub sentinel that
  # stack-profile seeding replaces.
  [[ -n "${PHASEKIT_VERIFY_CMD:-}" ]] && return 0
  local vs="$ROOT_DIR/scripts/phasekit-verify.sh"
  [[ -f "$vs" ]] || return 1
  if grep -qE '^PHASEKIT_VERIFY_CONFIGURED=0' "$vs"; then
    return 1
  fi
  return 0
}

compose_light_prompt() {
  # Prepend the light-mode overrides to the standard prompt. Composed at
  # runtime into a temp file so no new file ships downstream — the semantics
  # live here, next to the loop that enforces them.
  local base_prompt="$1"
  cat <<'LIGHT_EOF'
=== PHASEKIT LIGHT MODE (this session) ===
This session runs in LIGHT execution mode: the task was triaged as small
(single-surface, low blast radius). Reduced ceremony applies. These rules
OVERRIDE the standard operating rules below wherever they conflict:
- Treat the whole task as ONE collapsed phase: build + verify + review in a
  single pass. Do not decompose it into multiple phases.
- Do NOT use the strategy-planner or architecture-red-team subagents.
- The code-reviewer subagent still reviews the change before you finish.
- The pre-commit verify gate is unchanged and mandatory: run the project's
  verify (scripts/phasekit-verify.sh) yourself and make it pass before
  finishing.
- Stay strictly inside the task's scope. Scaffold-class or config-surface
  edits beyond the task escalate the run instead of committing.
- Mark the task's phase complete in docs/PHASES.md as part of the change.
- When the task is done and verify passes, write
  artifacts/project-complete.json (do not write phase-approval.json for
  intermediate ceremony).
- If you are blocked, or the task turns out bigger than triaged (schema, API
  contract, or dependency changes; multi-surface edits; unclear acceptance),
  write artifacts/phase-blocked.json and stop. Escalation to a standard
  full-ceremony run is automatic — do not grind.
=== END LIGHT MODE OVERRIDES ===

LIGHT_EOF
  cat "$base_prompt"
}

compose_contracts_prompt() {
  # Session awareness for cross-project contracts (v0.7.0). A mount nobody is
  # told about goes unread — that is half of why META_REPO_PATH dangled for
  # months — so when this repo declares dependencies the session is told, in
  # its prompt, that the mounted contract is authoritative and guessing is
  # forbidden. Composed at runtime into a temp file, exactly like light mode:
  # no new file ships downstream and the semantics live next to the gate that
  # enforces them.
  local base_prompt="$1"
  local decl="$ROOT_DIR/contracts.yaml"
  local checker="$ROOT_DIR/scripts/phasekit-contracts.py"
  local listing=""
  if [[ -f "$decl" && -f "$checker" ]]; then
    listing="$(python3 "$checker" --repo "$ROOT_DIR" status 2>/dev/null || true)"
  fi
  # Only speak when there is something to say: a repo that declares nothing
  # (or declares zero entries) gets the v0.6.6 prompt, byte for byte.
  if [[ -z "$listing" ]] || ! grep -q "dependency(ies) declared" <<<"$listing"; then
    cat "$base_prompt"
    return 0
  fi

  cat <<CONTRACTS_EOF
=== CROSS-PROJECT CONTRACTS (this repo declares dependencies) ===
This repo's contracts.yaml declares dependencies on other projects'
interfaces. Their authoritative contracts are vendored in this repo and
mirrored read-only under \${PHASEKIT_CONTRACTS_DIR:-/contracts}:

${listing}

Rules for this session, which OVERRIDE any inference you would otherwise make:
- The vendored contract is AUTHORITATIVE. Read it before writing any code that
  crosses that boundary — field names, types, status codes, exit codes,
  env var names, artifact shapes.
- GUESSING IS FORBIDDEN. Do not infer a field name from a variable name, a
  fixture, a task description, or an older version of the interface. Three
  shipped defects in one week came from exactly that.
- Do NOT edit a vendored contract to make your code or tests pass. It is a
  cache of someone else's file; the pre-commit gate compares it byte-for-byte
  against the mounted original and will refuse the commit.
- If the contract genuinely changed, run:
    python3 scripts/phasekit-contracts.py refresh
  then reconcile this repo's code and tests with the refreshed contract and
  commit both together.
=== END CONTRACTS ===

CONTRACTS_EOF
  cat "$base_prompt"
}

write_light_escalation() {
  # Escalation record (v0.6.0, decided fork C). Light mode never grinds: on
  # 2 verify failures, any blocked artifact, an out-of-scope edit, or the
  # iteration cap, write a plain artifact and stop honestly. The orchestrator
  # re-queues the remainder as a standard (full-ceremony, default-model)
  # iteration and carries this record forward — that half is orchestrator
  # work, not phasekit's.
  local trigger="$1"
  local reason="$2"
  local detail=""
  if [[ -f "$ARTIFACTS_DIR/phase-verify-failed.json" ]]; then
    detail="$(jq -r '.log_tail // ""' "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null | tail -c 2000)" || detail=""
  elif [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
    detail="$(jq -r '.reason // ""' "$ARTIFACTS_DIR/phase-blocked.json" 2>/dev/null)" || detail=""
  fi
  jq -n \
    --arg trigger "$trigger" \
    --arg reason "$reason" \
    --arg detail "$detail" \
    --arg model "${ANTHROPIC_MODEL:-default}" \
    --argjson iterations "${iteration:-0}" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      light_escalation: true,
      trigger: $trigger,
      reason: $reason,
      detail: $detail,
      model: $model,
      iterations_used: $iterations,
      next_step: "re-queue as a standard (full-ceremony, default-model) iteration",
      ts: $ts
    }' > "$ARTIFACTS_DIR/light-escalation.json"
  echo "run-until-done: LIGHT ESCALATION ($trigger) — $reason. See artifacts/light-escalation.json; the task should be re-queued as standard." >&2
}

maybe_escalate_light_commit() {
  # After a failed commit in light mode, decide whether the failure is
  # terminal. Verify failures below VERIFY_MAX_ATTEMPTS are not — the next
  # iteration gets to fix them (the breaker and the iteration cap bound the
  # total attempts). Exits the loop on escalation.
  local rc="$1"
  [[ "$ITERATION_MODE" == "light" ]] || return 0
  if [[ "$rc" -eq 4 || -f "$ARTIFACTS_DIR/scope-refusal.json" ]]; then
    write_light_escalation "scope" "out-of-scope edit during a light task (scope containment escalates instead of warning)"
    exit 2
  fi
  if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
    write_light_escalation "verify_failures" "pre-commit verify failed $VERIFY_MAX_ATTEMPTS times"
    exit 2
  fi
  return 0
}

run_light_final_review() {
  # Model split (v0.6.0, decided fork A): build iterations run the cheap model
  # the supervisor set via ANTHROPIC_MODEL; before the final commit, exactly
  # one review pass runs on the DEFAULT model (ANTHROPIC_MODEL dropped so
  # run-phase.sh omits --model). Two claude invocations with different models
  # — deliberately not a new agent framework. A failed review invocation is
  # non-fatal: the verify gate remains the hard gate on the commit.
  local review_prompt
  review_prompt="$(mktemp)"
  cat > "$review_prompt" <<'REVIEW_EOF'
You are the FINAL REVIEWER for a phasekit LIGHT-mode task, running on the
default model. A cheaper model built the change now sitting uncommitted in
this working tree; your review is the last gate before the wrapper creates
the final commit.

Do, in order:
1. Read artifacts/project-complete.json, docs/PHASES.md (the current task),
   and the uncommitted work: git status, git diff HEAD, and untracked files.
2. Review the change for correctness, completeness against the task, scope
   containment, and quality. Fix any defect you find directly in the working
   tree with minimal edits. Do NOT expand scope or refactor beyond the task.
3. Run the project's pre-commit verify (scripts/phasekit-verify.sh, or the
   configured verify command) and make sure it passes.
4. If the work is sound (with your fixes, if any), re-write
   artifacts/project-complete.json — keep its shape, update the summary if
   you changed anything — so the wrapper can commit.
5. If the work is fundamentally unsound or clearly outgrew a light task,
   delete artifacts/project-complete.json, write artifacts/phase-blocked.json
   explaining why, and stop.

Never run git commit or git push — the wrapper owns commits.
REVIEW_EOF
  echo "Light mode: final review pass on the default model before the final commit."
  local rrc=0
  (
    ANTHROPIC_MODEL=""
    export ANTHROPIC_MODEL
    run_once "$review_prompt" "new" "light-review" 0
  ) || rrc=$?
  rm -f "$review_prompt"
  if [[ "$rrc" -ne 0 ]]; then
    echo "WARN: light final-review pass exited $rrc — proceeding to the verify-gated final commit anyway." >&2
  fi
  return 0
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

# --- Learnings size advisory (doc-rotation descope rider, 2026-08-20) --------
# One log line, once per session, when docs/LEARNINGS.md is over budget.
# Advisory ONLY — never blocks, never rotates, never injects prompt text: the
# safe maintenance is curation per the file's own header rule (merge/tighten,
# judgment), and mechanising that was explicitly descoped
# (foundry-meta kickoffs/KICKOFF-phasekit-doc-rotation.md). A supervisor that
# wants scheduling watches the same file size itself.
LEARNINGS_WARN_KB="${PHASEKIT_LEARNINGS_WARN_KB:-48}"
if [[ -f "$ROOT_DIR/docs/LEARNINGS.md" ]] && [[ "$LEARNINGS_WARN_KB" =~ ^[0-9]+$ ]]; then
  learnings_kb=$(( $(wc -c < "$ROOT_DIR/docs/LEARNINGS.md") / 1024 ))
  if (( learnings_kb >= LEARNINGS_WARN_KB )); then
    echo "run-until-done: note — docs/LEARNINGS.md is ${learnings_kb} KB (advisory threshold ${LEARNINGS_WARN_KB} KB). Consider a curation pass per the file's header rule (merge/tighten; never blind oldest-first pruning)." >&2
  fi
fi

# --- Iteration-mode resolution (v0.6.0) -------------------------------------
# Eligibility guard: light mode with a stub/absent verify gate is refused —
# reduced ceremony only where mechanical verification is strong. Fall back to
# standard with one plain log line.
if [[ "$ITERATION_MODE" == "light" ]] && ! light_verify_configured; then
  echo "run-until-done: light mode requested but the verify gate is absent or still the stub (PHASEKIT_VERIFY_CONFIGURED=1 required) — running standard mode instead."
  ITERATION_MODE="standard"
fi
if [[ "$ITERATION_MODE" == "light" ]]; then
  MAX_ITERATIONS="${MAX_ITERATIONS:-2}"
  VERIFY_MAX_ATTEMPTS="${VERIFY_MAX_ATTEMPTS:-2}"
  LIGHT_PROMPT_FILE="$(mktemp)"
  compose_light_prompt "$PROMPT_FILE" > "$LIGHT_PROMPT_FILE"
  PROMPT_FILE="$LIGHT_PROMPT_FILE"
  echo "Light execution mode: single collapsed phase, iteration cap $MAX_ITERATIONS, verify breaker $VERIFY_MAX_ATTEMPTS, default-model review before the final commit."
else
  MAX_ITERATIONS="${MAX_ITERATIONS:-50}"
  VERIFY_MAX_ATTEMPTS="${VERIFY_MAX_ATTEMPTS:-3}"
fi

# Contracts awareness (v0.7.0) is composed AFTER light mode so it applies to
# both execution modes — a light task is exactly as capable of guessing a
# field name as a standard one. No-op unless this repo declares dependencies.
CONTRACTS_PROMPT_FILE="$(mktemp)"
compose_contracts_prompt "$PROMPT_FILE" > "$CONTRACTS_PROMPT_FILE"
if ! cmp -s "$CONTRACTS_PROMPT_FILE" "$PROMPT_FILE"; then
  PROMPT_FILE="$CONTRACTS_PROMPT_FILE"
  echo "Cross-project contracts: this repo declares dependencies — the session prompt names them as authoritative."
else
  rm -f "$CONTRACTS_PROMPT_FILE"
fi

light_review_done=0
verdict_retry_used=0

# Phase-commit atomicity marker: touched immediately before each claude
# invocation; only artifacts newer than it may drive a commit. PENDING_COMMIT_RETRY
# preserves the one legitimate stale-artifact commit: retrying a phase-approval
# whose verify gate failed (the staged work belongs to that same phase, so its
# message is the right one).
ITER_START_MARKER="$(mktemp)"
# Exported for the Stop hook (.claude/hooks/require-verdict.sh), which must
# answer "was this artifact written during THIS iteration?" exactly as
# artifact_written_this_iteration() does. One marker, one answer.
export PHASEKIT_ITER_MARKER="$ITER_START_MARKER"
PENDING_COMMIT_RETRY=""

# Soft wrap-up sentinel: an outer supervisor (e.g. the orchestrator's
# run-session.sh) touches this file at T-minus-N minutes before its hard kill.
# Between iterations the loop honors it: commit what stands (verify-gated) and
# exit 0 instead of starting an iteration the guillotine would truncate.
WRAPUP_SENTINEL="${PHASEKIT_WRAPUP_SENTINEL:-$ARTIFACTS_DIR/wrapup-requested}"
if [[ -f "$WRAPUP_SENTINEL" ]]; then
  echo "Clearing stale wrap-up sentinel from a prior run: $WRAPUP_SENTINEL"
  rm -f "$WRAPUP_SENTINEL"
fi

# Deadline-aware iteration pacing (v0.6.1): the supervisor forwards the
# session's hard-kill time as PHASEKIT_SESSION_DEADLINE (epoch seconds;
# run-session.sh computes start + MAX_MINUTES). Between iterations the loop
# refuses to start one it likely can't finish — remaining time below ~1.2× the
# average pass so far (floor: 3 minutes) triggers the same path as the wrap-up
# sentinel. No deadline env ⇒ behavior unchanged. Averages are per-run only —
# deliberately no persistence across sessions.
SESSION_DEADLINE="${PHASEKIT_SESSION_DEADLINE:-}"
if [[ -n "$SESSION_DEADLINE" && ! "$SESSION_DEADLINE" =~ ^[0-9]+$ ]]; then
  echo "WARN: ignoring non-numeric PHASEKIT_SESSION_DEADLINE='$SESSION_DEADLINE'" >&2
  SESSION_DEADLINE=""
fi
# Floor override is a test/tuning knob; production default is 3 minutes.
PACING_FLOOR_SECONDS="${PHASEKIT_PACING_FLOOR_SECONDS:-180}"
[[ "$PACING_FLOOR_SECONDS" =~ ^[0-9]+$ ]] || PACING_FLOOR_SECONDS=180
pass_elapsed_total=0
passes_done=0
last_pass_start=""

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

# Once-per-run: keep per-iteration logs, the wrap-up sentinel, and the hidden
# transient signals out of git status, then untrack any transient signal a
# pre-v0.6.5 history committed (see function docs). Exclude-before-untrack
# order is deliberate: it fails closed (an exclude line for a still-tracked
# path is inert; untracked-and-unexcluded is the state to avoid).
ensure_transients_excluded || true
heal_tracked_transients || true

# Stranded-artifact recovery (v0.6.3). v0.6.0's atomicity gate correctly
# refuses to let a stale phase-approval.json drive a commit — but a session
# killed AFTER the artifact write and BEFORE its commit leaves the approval
# stranded: later sessions see approved-artifact + finished work, re-validate
# it (verify green!), end without rewriting the artifact, and the loop exits 1
# uncommitted. Five sessions burned that way on 2026-08-11 before the
# quiet-stall guard fired. Recover mechanically — never depend on the model
# noticing. The stranded signature is git's, not mtime's (clones and rsync
# skew mtimes): an artifact with uncommitted changes IS an approval/completion
# that never got its commit; a landed one is clean in git status.
artifact_never_landed() {
  [[ -f "$1" ]] || return 1
  [[ -n "$(git status --porcelain --ignored=matching -- "$1" 2>/dev/null)" ]]
}

if artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
  # A stranded completion record would be deleted by the first iteration's
  # cleanup_artifacts and silently re-done. Commit it now (all the usual
  # gates apply) — on success the run is already complete, zero claude calls.
  echo "Stranded project-complete.json from a prior session detected — attempting its final commit before starting."
  print_json_summary "$ARTIFACTS_DIR/project-complete.json"
  crc=0
  commit_from_artifact \
    "$ARTIFACTS_DIR/project-complete.json" \
    "chore(workflow): final session work + project completion record" || crc=$?
  if [[ "$crc" -eq 0 || "$crc" -eq 2 ]]; then
    echo "Run finished successfully."
    exit 0
  fi
  echo "Stranded completion did not pass the commit gates — entering the loop to fix and re-complete." >&2
elif artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
  # Schedule the existing verify-gated retry path so the first iteration
  # boundary commits the approval under its own message (wrong-phase risk
  # none: the artifact IS the phase being committed).
  echo "Stranded phase-approval.json from a prior session detected — its commit will be retried at the first iteration boundary."
  PENDING_COMMIT_RETRY="phase-approval"
fi

while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do
  # Pass-duration bookkeeping (v0.6.1): each trip through the loop top closes
  # the previous pass. Retried attempts count as passes too — that keeps the
  # average conservative, which is the right direction for pacing.
  now_ts="$(date +%s)"
  if [[ -n "$last_pass_start" ]]; then
    pass_elapsed_total=$((pass_elapsed_total + now_ts - last_pass_start))
    passes_done=$((passes_done + 1))
  fi
  last_pass_start="$now_ts"

  # Soft wrap-up check (v0.6.0): honored between iterations, never mid-flight.
  if [[ -f "$WRAPUP_SENTINEL" ]]; then
    echo "=== Wrap-up requested (sentinel present) — not starting iteration $iteration ==="
    rm -f "$WRAPUP_SENTINEL"
    wrapup_commit
    echo "Run wrapped up cleanly (soft stop)."
    exit 0
  fi

  # Deadline pacing check (v0.6.1): same wrap-up path, triggered by time math
  # instead of the supervisor's sentinel.
  if [[ -n "$SESSION_DEADLINE" ]]; then
    remaining=$((SESSION_DEADLINE - now_ts))
    pacing_threshold="$PACING_FLOOR_SECONDS"
    if [[ "$passes_done" -gt 0 ]]; then
      pacing_estimate=$((pass_elapsed_total * 12 / (passes_done * 10)))
      [[ "$pacing_estimate" -gt "$pacing_threshold" ]] && pacing_threshold="$pacing_estimate"
    fi
    if [[ "$remaining" -lt "$pacing_threshold" ]]; then
      echo "deadline pacing: not starting iteration $iteration (${remaining}s remain, threshold ${pacing_threshold}s from $passes_done completed passes)"
      wrapup_commit
      echo "Run wrapped up cleanly (deadline pacing)."
      exit 0
    fi
  fi

  echo "=== Iteration $iteration ==="
  # One verdict retry per iteration (see the backstop below).
  verdict_retry_used=0
  cleanup_artifacts
  touch "$ITER_START_MARKER"

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

  # --- No-verdict retry backstop -------------------------------------------
  # The session returned 0 but wrote no verdict, AND left changes in the tree.
  # That combination is unambiguous: work happened and nothing claimed it. Give
  # the session exactly ONE re-invocation to name a verdict before the loop
  # falls through to its existing `exit 1`.
  #
  # This is a BACKSTOP, not the primary fix. It fires after the process has
  # already died, so anything the session had backgrounded is gone by now — the
  # Stop hook catches the same condition while that work is still alive. Both
  # are wanted: the hook prevents the loss, this handles what the hook cannot
  # see (a crash, an external kill, a missing or failing hook).
  #
  # Bounded to one attempt per iteration, and gated twice so it only fires on
  # a state the loop genuinely cannot explain:
  #   - a dirty tree, so a legitimately-empty iteration is never re-prodded;
  #   - no PENDING_COMMIT_RETRY, because a pending approval retry is a dirty
  #     tree the loop ALREADY knows what to do with (the stranded-approval and
  #     verify-retry paths commit it under its own phase's message at this same
  #     boundary). Prodding there would spend a turn asking for a verdict that
  #     already exists — caught by the v0.6.0/v0.6.3 regression tests.
  if [[ "$verdict_retry_used" -eq 0 ]] \
     && [[ -z "$PENDING_COMMIT_RETRY" ]] \
     && ! has_verdict_artifact \
     && [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    verdict_retry_used=1
    echo "No verdict artifact and the tree is dirty — asking once for a verdict before giving up." >&2
    verdict_prompt="$(mktemp)"
    cat > "$verdict_prompt" <<'VERDICT_RETRY_EOF'
Your previous turn ended without writing a verdict artifact, and this working
tree has uncommitted changes. The loop cannot commit work that nothing claims,
so that work is currently stranded.

Note: anything you had running in the background is already dead — the process
exited when your last turn ended. Do not wait for it and do not restart it now.

Decide what the work in the tree is, and write exactly one artifact:

- artifacts/phase-update.json — you made real progress but the phase is not
  finished. This commits the work and the loop continues. Almost always right.
- artifacts/phase-approval.json — the phase is genuinely complete.
- artifacts/phase-blocked.json — you cannot proceed without external input.
- artifacts/project-complete.json — the whole project is done.

Inspect the tree first (git status, git diff), then write the artifact. Do not
start new work and do not run git commit — the wrapper owns commits.
VERDICT_RETRY_EOF
    vrc=0
    run_once "$verdict_prompt" "continue" "$iteration" 0 || vrc=$?
    rm -f "$verdict_prompt"
    if [[ "$vrc" -ne 0 ]]; then
      echo "  Verdict request exited $vrc — continuing to the loop's own handling." >&2
    fi
  fi

  if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
    echo "Project complete artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/project-complete.json"
    # Light mode: one review pass on the default model BEFORE the final commit
    # (decided fork A). The reviewer may fix defects in place, or withdraw the
    # completion by swapping the artifact for phase-blocked.json.
    if [[ "$ITERATION_MODE" == "light" && "$light_review_done" -eq 0 ]]; then
      light_review_done=1
      run_light_final_review
      if [[ ! -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
        if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
          write_light_escalation "review_blocked" "final review pass rejected the work (phase-blocked.json written)"
        else
          write_light_escalation "review_not_reconfirmed" "final review pass did not re-confirm completion"
        fi
        exit 2
      fi
    fi
    # Final-commit gate. The last iteration's work (and project-complete.json
    # itself) must land in git before the loop exits — exiting here without
    # committing left a dirty tree behind every completed run and forced a
    # manual reconcile each time (5 reconciles on 2026-07-25/26).
    crc=0
    commit_from_artifact \
      "$ARTIFACTS_DIR/project-complete.json" \
      "chore(workflow): final session work + project completion record" || crc=$?
    if [[ "$crc" -eq 0 || "$crc" -eq 2 ]]; then
      # 0 = final work committed; 2 = nothing substantive left (already
      # committed) — both are a clean finish.
      echo "Run finished successfully."
      exit 0
    fi
    maybe_escalate_light_commit "$crc"
    # Verify gate failed on the final commit: the completion claim is not
    # backed by passing checks. Re-enter the loop so the next iteration sees
    # phase-verify-failed.json and fixes it (cleanup_artifacts clears the
    # stale project-complete.json; the model re-emits it once green). The
    # VERIFY_MAX_ATTEMPTS circuit breaker still bounds this via
    # phase-blocked.json.
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Final commit blocked; completion not committed:"
      print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
      exit 2
    fi
    echo "Final commit failed verify — re-entering loop to fix before completing." >&2
    iteration=$((iteration + 1))
    continue
  fi

  # Phase-commit atomicity (v0.6.0): phase-approval.json persists on disk as
  # the durable record of the last approved phase, so this branch fires only
  # when the artifact was (re)written during THIS iteration — or when a commit
  # for it failed verify last iteration and is being retried (the staged work
  # belongs to that same phase, so its message is the right one). A stale
  # approval never drives a commit of later in-flight work again.
  approval_retry_pending=0
  if [[ "$PENDING_COMMIT_RETRY" == "phase-approval" && -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    approval_retry_pending=1
  fi
  if artifact_written_this_iteration "$ARTIFACTS_DIR/phase-approval.json" \
     || [[ "$approval_retry_pending" -eq 1 ]]; then
    echo "Phase approval artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-approval.json"
    crc=0
    commit_from_artifact \
      "$ARTIFACTS_DIR/phase-approval.json" \
      "chore(workflow): approve completed phase" || crc=$?
    if [[ "$crc" -eq 0 ]]; then
      PENDING_COMMIT_RETRY=""
      iteration=$((iteration + 1))
      continue
    fi
    maybe_escalate_light_commit "$crc"
    # No commit was made: either the verify gate failed (rc 1 — mark the
    # approval for a commit retry next iteration, even if the model forgets to
    # re-touch it after fixing), or there was no substantive change to commit
    # (rc 2, only logs/transient signals). In both cases, if
    # phase-blocked.json is present the iteration is genuinely blocked — stop
    # cleanly rather than spinning to MAX_ITERATIONS or committing churn.
    # Otherwise re-enter so Claude can make progress (or fix a verify failure)
    # on the next iteration.
    if [[ "$crc" -eq 1 ]]; then
      PENDING_COMMIT_RETRY="phase-approval"
    else
      PENDING_COMMIT_RETRY=""
    fi
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Phase blocked; no substantive change to commit:"
      print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
      exit 2
    fi
    iteration=$((iteration + 1))
    continue
  fi

  if [[ -f "$ARTIFACTS_DIR/phase-update.json" ]]; then
    # phase-update.json is transient (cleared by cleanup_artifacts each
    # iteration), so its existence here means it was written this iteration.
    echo "Phase update artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/phase-update.json"
    crc=0
    commit_from_artifact \
      "$ARTIFACTS_DIR/phase-update.json" \
      "chore(workflow): update phase plan and roadmap" || crc=$?
    if [[ "$crc" -eq 0 ]]; then
      iteration=$((iteration + 1))
      continue
    fi
    maybe_escalate_light_commit "$crc"
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
    if [[ "$ITERATION_MODE" == "light" ]]; then
      write_light_escalation "phase_blocked" "the session wrote phase-blocked.json"
      exit 2
    fi
    echo "Stopping because external input is required."
    exit 2
  fi

  echo "No expected artifact found in $ARTIFACTS_DIR"
  echo "Expected one of:"
  echo "  - phase-approval.json"
  echo "  - phase-update.json"
  echo "  - phase-blocked.json"
  echo "  - project-complete.json"
  if [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    echo "(a phase-approval.json exists but predates this iteration — the durable record of a previously approved phase never drives a new commit)"
  fi
  exit 1
done

echo "Reached MAX_ITERATIONS=$MAX_ITERATIONS without project completion."
if [[ "$ITERATION_MODE" == "light" ]]; then
  write_light_escalation "iteration_cap" "light iteration cap ($MAX_ITERATIONS) reached without completion"
fi
exit 3