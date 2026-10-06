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

# Branch-per-iteration + squash-to-target (v0.14.0). Opt-in per session via
# PHASEKIT_SQUASH_TARGET=<integration branch>; unset = every commit path below
# behaves exactly as v0.13.x (the flag-off pin in tests/test_branch_squash.py).
# See the function block above staged_touches_security_pair for the model.
SQUASH_TARGET="${PHASEKIT_SQUASH_TARGET:-}"
# Set by write_branch_integrity_block; the loop-start sites branch on THIS,
# not on phase-blocked.json's presence — a block left by the previous session
# is still on disk until cleanup_artifacts (v0.14.0 review, MINOR-3).
BRANCH_INTEGRITY_BLOCKED=0

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
# orchestrator's iteration-mode.json (written INSIDE the iteration commit;
# the loop reads its `iteration` into boundary-state.json — v0.14.8).
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
  ".wrapup-in-progress"
  "session-interrupted.json"
  "boundary-state.json"
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
  ".wrapup-in-progress"
  "session-interrupted.json"
  "boundary-state.json"
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
  # v0.18.2: artifacts/scratch/ is the session's sanctioned scratch space —
  # ignored, never committed, cleared when an iteration starts.
  # v0.18.6: the engine's runtime-only flock target (.scaffold/), never committed.
  local lines=("artifacts/logs/" "artifacts/wrapup-requested" "artifacts/scratch/" ".scaffold/manifest.json.lock")
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
  # v0.14.4: the COMPLETION commit never carries the promoted baton — a
  # concluded iteration explains its own tree, and a baton in history is a
  # stale note the next session would read as current. It stays on disk
  # until the commit is known to succeed (clear_consumed_batons_at_completion).
  if [[ "${COMPLETION_COMMIT_IN_PROGRESS:-0}" == 1 ]]; then  # untracked only: a TRACKED baton's staged deletion must ride the commit
    git cat-file -e "HEAD:artifacts/session-handoff.json" 2>/dev/null || git reset -q -- "$ARTIFACTS_DIR/session-handoff.json" 2>/dev/null || true
  fi
  for sig in "${TRANSIENT_SIGNALS[@]}"; do
    if git cat-file -e "HEAD:artifacts/$sig" 2>/dev/null; then
      git rm --cached -q --ignore-unmatch -- "$ARTIFACTS_DIR/$sig" 2>/dev/null || true
    else
      git reset -q -- "$ARTIFACTS_DIR/$sig" 2>/dev/null || true
    fi
  done
  # v0.18.6: nor the engine's flock target — fresh adds only; a copy an older
  # history tracks is `phasekit upgrade`'s to untrack, in its own commit.
  git cat-file -e "HEAD:.scaffold/manifest.json.lock" 2>/dev/null \
    || git reset -q -- "$ROOT_DIR/.scaffold/manifest.json.lock" 2>/dev/null || true
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
    if git commit -q -m "chore(workflow): untrack transient signal artifacts (phasekit v0.6.5 heal)" -m "$(phasekit_trailers heal)"; then
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
  # v0.18.0 (§3.2): a completion record whose landing the gate REFUSED is
  # carried into the repair turn — kept on disk, unstaged, for the model to
  # edit (rebuilding it from an archive is how iteration 88 committed under
  # iteration 87's subject). The loop's carry marker decides this, never the
  # file's freshness; the carried record is set aside around the deletion
  # below and put back (mtime kept), any other record is last iteration's
  # signal and goes, as before. A carried record claims nothing until the
  # session re-writes it (completion_record_claims), and a carry never
  # outlives its session (drop_unclaimed_carried_record) — between sessions
  # the record is deleted exactly as before.
  local carried=""
  if completion_record_carried; then
    carried="$ARTIFACTS_DIR/logs/.carried-completion.json"
    mv -f "$ARTIFACTS_DIR/project-complete.json" "$carried" 2>/dev/null || carried=""
  fi
  # Unstage first where HEAD lacks it: a record a killed `phasekit verify`
  # left staged must never become the `AD` state here.
  if ! git cat-file -e "HEAD:artifacts/project-complete.json" 2>/dev/null; then
    git reset -q -- "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
  fi
  rm -f \
    "$ARTIFACTS_DIR/phase-update.json" \
    "$ARTIFACTS_DIR/phase-blocked.json" \
    "$ARTIFACTS_DIR/light-escalation.json"
  # v0.18.2 (review rounds 1-8): THIS iteration's committed completion,
  # still landing (completion_in_flight), is never deleted here — it is kept
  # (restored to HEAD's bytes when the tree holds other ones): a deletion on
  # disk is what a later checkpoint commits, un-recording it. Any other
  # record goes, as before (a record carried over a refused landing is set
  # aside above).
  retire_completion_record
  if [[ -n "$carried" ]]; then
    mv -f "$carried" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
  else
    rm -f "$ARTIFACTS_DIR/logs/.carried-completion"
  fi
  # The Stop hook's block budget is per-iteration: a session that was nudged
  # last iteration starts the next one with a full allowance, and a healthy
  # iteration never creates the file at all.
  rm -f "$ARTIFACTS_DIR/.stop-hook-blocks"
  # The wrap-up nudge (PostToolUse hook) is once-per-iteration by the same
  # freshness idiom; clearing its marker here keeps the two hook budgets on
  # one lifecycle. A healthy iteration (no sentinel) never creates it.
  rm -f "$ARTIFACTS_DIR/.wrapup-nudge-sent"
  # v0.18.0: the per-subagent markers and the repeat clocks, same lifecycle.
  rm -f "$ARTIFACTS_DIR"/logs/.wrapup-nudge-* 2>/dev/null || true
  # The wrap-up-in-progress marker (v0.13.1) is only ever written at session
  # end; one surviving into an iteration start belongs to a previous session
  # and would silently stand the deadline watchdog down for this whole one.
  rm -f "$ARTIFACTS_DIR/.wrapup-in-progress"
}

clear_scratch_at_iteration_start() {
  # v0.18.2 (row 1233 (5)): artifacts/scratch/ is the one sanctioned place
  # for a session's scratch files (or /tmp) — ignored (.git/info/exclude,
  # and unstaged by every commit path should a project's .gitignore
  # re-include it), never committed, and cleared when an ITERATION starts:
  # a new supervising iteration label (artifacts/iteration-mode.json), or
  # every session start when no supervisor names one. A second session of
  # the same iteration keeps it. phasekit cannot tell scratch from work
  # anywhere else in the tree — `git add -A` commits whatever is not
  # ignored — so the prompts send scratch here. Best-effort: never blocks.
  local d="$ARTIFACTS_DIR/scratch" mark="$ARTIFACTS_DIR/logs/.scratch-iteration" cur prev=""
  cur="$(supervising_iteration_json)"
  if [[ -f "$mark" ]]; then prev="$(head -c 200 "$mark" 2>/dev/null)" || prev=""; fi
  if [[ -d "$d" ]] && { [[ "$cur" == null ]] || [[ "$cur" != "$prev" ]]; }; then
    if [[ -n "$(ls -A "$d" 2>/dev/null | head -n1)" ]]; then
      rm -rf -- "$d" 2>/dev/null || true
      echo "run-until-done: cleared artifacts/scratch/ — a new iteration starts (scratch never outlives its iteration; v0.18.2)"
    fi
  fi
  mkdir -p "$d" "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  printf '%s' "$cur" > "$mark" 2>/dev/null || true
  return 0
}

print_json_summary() {
  local file="$1"
  jq -r '.' "$file"
}

# jq VARIABLE NAMES: never `$label` — `label` is a jq keyword and the runtime
# container ships jq 1.6, which rejects it as a variable ("unexpected label,
# expecting IDENT"); jq 1.7+ on a developer host accepts it, so the suite is
# blind. xmeo phase-144 patched its vendored copy by hand; v0.14.11 renames
# every occurrence to `$lbl` (the KEY `label:` is fine). Pinned by
# tests/test_boundary_state.py StructuralPins.
record_verify_failure() {
  # Single source of truth for the verify-failure capture. Both the contracts
  # gate and the project's verify script fail through here, so the next
  # iteration's recovery path, the attempts counter and the VERIFY_MAX_ATTEMPTS
  # breaker behave identically no matter which gate refused. (A second copy of
  # this logic is exactly how wrapup_commit silently lost the post-verify gates
  # before v0.6.6.)
  # $5 = "memo" (v0.14.5): the verdict came from the red verify memo for an
  # exact tree that already failed — re-surface the capture for the model
  # but spend NO breaker attempt and never trip the breaker from here.
  # $6 (v0.14.10) = the gate footprint as a JSON array of paths, or empty:
  # when present the run is red BY FOOTPRINT (exit_code may be 0 — the
  # command's own verdict is kept honest) and the artifact carries the paths,
  # the rule and the recipe.
  local cmd="$1" label="$2" exit_code="$3" log="$4" mode="${5:-}" footprint="${6:-}"
  [[ -n "$footprint" && "$footprint" != "null" ]] || footprint="null"
  # v0.18.0: `phasekit verify` is the MODEL's own check, not the commit gate
  # — its red spends no breaker attempt and writes no phase-verify-failed.json
  # (which tells the next iteration a COMMIT was refused); the output is the
  # message.
  if [[ "${VERIFY_INVOKER:-loop}" == model ]]; then
    if [[ "$footprint" != "null" ]]; then
      echo "  Verify FAILED — the gate WROTE TO THE TREE (gate footprint, NOT restored: a model's verify never rewrites the tree; command exit $exit_code): $(jq -r 'join(", ")' <<<"$footprint" 2>/dev/null || echo "$footprint")" >&2
      echo "  Rule: $GATE_FOOTPRINT_RULE. Fix: $GATE_FOOTPRINT_RECIPE. Then put the listed paths back (git checkout / rm) yourself." >&2
    else
      echo "  Verify FAILED ($label, exit $exit_code) — phasekit verify spends no breaker attempt." >&2
    fi
    echo "----- last 50 lines of verify output -----" >&2
    tail -n 50 "$log" >&2
    echo "------------------------------------------" >&2
    return 0
  fi

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
  # "memo": the red memo's replay; "settle" (v0.14.10): the footprint of a
  # gate the previous session died inside, restored at loop start. Neither
  # is a fresh run of the gate, so neither spends a breaker attempt — the
  # live re-run that follows does (in light mode the breaker is 2: a
  # settlement that counted would trip it before the model's first turn).
  [[ "$mode" == "memo" || "$mode" == "settle" ]] && attempts="$prior_attempts"
  local tail_output try_log wrote=0
  tail_output="$(tail -n 200 "$log")"
  # jq can choke on pathological log bytes: the second try drops the log
  # tail and keeps everything else (v0.14.10 review MINOR-3: a red by
  # footprint must never lose its paths to a bad log); only if that fails
  # too is a minimal valid capture written — never a zero-byte artifact.
  for try_log in "$tail_output" "(unavailable: capture failed)"; do
    if jq -n \
      --arg cmd "$cmd" \
      --arg lbl "$label" \
      --argjson exit_code "$exit_code" \
      --argjson attempts "$attempts" \
      --arg log "$try_log" \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --argjson footprint "$footprint" \
      --arg rule "$GATE_FOOTPRINT_RULE" \
      --arg recipe "$GATE_FOOTPRINT_RECIPE" \
      '{
        verify_failed: true,
        command: $cmd,
        label: $lbl,
        exit_code: $exit_code,
        attempts: $attempts,
        log_tail: $log,
        ts: $ts
      } + (if $footprint != null then
             {gate_footprint: $footprint, gate_footprint_rule: $rule, gate_footprint_recipe: $recipe}
           else {} end)' > "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null; then
      wrote=1; break
    fi
  done
  if [[ "$wrote" -eq 0 ]]; then
    printf '{"verify_failed": true, "label": "%s", "exit_code": %s, "attempts": %s, "log_tail": "(unavailable: capture failed)", "ts": "%s"}\n' \
      "$label" "$exit_code" "$attempts" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > "$ARTIFACTS_DIR/phase-verify-failed.json"
  fi

  if [[ "$footprint" != "null" ]]; then
    echo "  Verify FAILED — the gate WROTE TO THE TREE (gate footprint, restored; command exit $exit_code): $(jq -r 'join(", ")' <<<"$footprint" 2>/dev/null || echo "$footprint")" >&2
    echo "  Rule: $GATE_FOOTPRINT_RULE. Fix: $GATE_FOOTPRINT_RECIPE." >&2
  fi
  if [[ "$mode" == "memo" ]]; then
    echo "  Verify FAILED — known RED for this exact tree (verify memo); attempts unchanged at $attempts/$VERIFY_MAX_ATTEMPTS; see artifacts/phase-verify-failed.json" >&2
    return 0
  fi
  if [[ "$mode" == "settle" ]]; then
    echo "  Verify FAILED — settled from the previous session's kill inside the gate; no attempt spent (attempts $attempts/$VERIFY_MAX_ATTEMPTS); see artifacts/phase-verify-failed.json" >&2
    return 0
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
        next_step: "fix the failing verify: artifacts/phase-verify-failed.json names the command and its output. Only as a last resort, VERIFY_SKIP=1 bypasses the gate for this iteration."
      }' > "$ARTIFACTS_DIR/phase-blocked.json"
  fi
}

# --- verify-gate footprint (v0.14.10) ---------------------------------------
# THE RULE: a verify gate is read-only over tracked files and writes nothing
# untracked; gate output belongs under an ignored path. The loop proves it by
# observation, not by declaration: `git status` before and after the gate
# command, and the footprint is what appeared. Every xmeo landing from
# iteration 50 to 58 (runs 714, 756, 771) rested dirty because its gate
# re-measured three tracked evidence files AFTER the completion commit had
# staged them — the commit carried one content, the worktree another, and an
# operator committed the noise by hand at every landing; drill round 10
# scored that shape the landing seam's one FAIL. A formatter run with a write
# flag leaves the same shape. No opt-in and no declaration surface: a
# footprint is restored (tracked paths back to HEAD, untracked ones deleted)
# and the gate is RED even when the command returned 0, with the paths, the
# rule and the recipe in artifacts/phase-verify-failed.json — the existing
# red-gate machinery (CONTINUE_PROMPT step 2, the attempts breaker, the red
# memo) carries the fix to the next pass.
#
# Dirt present BEFORE the gate is the session's staged work and its staged
# bytes are never touched: the footprint is path-keyed (after minus before),
# so a session that edits ten files while the gate rewrites an eleventh
# yields a footprint of one. A gate that rewrites a file the session itself
# edited (a formatter with a write flag; a tracked evidence file the session
# re-measured in its turn and the gate re-measures again — the xmeo shape
# when the model runs the gate itself) is caught too: the commit sites stage
# everything before the gate, so an unstaged change that appears on a session
# path across the gate is the gate's, restored to the staged bytes and red
# (review MAJOR-2). A kill inside the gate — during the command
# or between its return and the restore — is settled at the next loop start,
# before any turn: the before-snapshot is kept in the transient boundary
# record (`gate_pending`) until the restore has run, so whatever appeared is
# restored and recorded red then (gate_settle_pending); the gate's dirt is
# never staged as the session's work (review MAJOR-1, 2026-09-15).
GATE_FOOTPRINT_RULE="a verify gate is read-only over tracked files and writes nothing untracked; gate output belongs under an ignored path"
GATE_FOOTPRINT_RECIPE="write the gate's output under artifacts/logs/ (ignored in every scaffolded project) or another ignored path; a measurement the project wants committed is produced in the phase's own work, not by the gate; a formatter runs in check mode here, never with a write flag"

# The credential shapes, ONE list (v0.18.2): the post-verify LEARNINGS scan
# refuses on them, and redact_credentials masks them in a refusal capture.
CREDENTIAL_TOKEN_RE='sk-ant-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}'
PRIVATE_KEY_RE='-----BEGIN [A-Z ]*PRIVATE KEY-----'

redact_credentials() {
  # stdin → stdout with credential-shaped text masked — the post-verify
  # scan's own patterns (one list): a capture a supervisor may surface never
  # carries a secret a git hook (e.g. an un-redacted scanner) printed.
  # A key on one line goes first; a multi-line one is cut from its BEGIN
  # line to its END line, the marker where it began (an unterminated one to
  # the end of the stream, never printed).
  sed -E "s/$CREDENTIAL_TOKEN_RE/[REDACTED]/g
    s/$PRIVATE_KEY_RE.*-----END [A-Z ]*PRIVATE KEY-----/[REDACTED PRIVATE KEY]/g
    /$PRIVATE_KEY_RE/,/-----END [A-Z ]*PRIVATE KEY-----/ {
      /$PRIVATE_KEY_RE/ {
        s/$PRIVATE_KEY_RE.*/[REDACTED PRIVATE KEY]/
        p
      }
      d
    }"
}

clear_commit_refusal() {
  # A loop commit that ran the gates green and HAPPENED clears a refusal the
  # commit path recorded (review round 4, m4: a wrap-up or a kept-out claim
  # landing is such a commit too). Only a commit refusal — the gate's own red
  # is the green gate's to clear.
  if jq -e '.commit_refusal == true' "$ARTIFACTS_DIR/phase-verify-failed.json" >/dev/null 2>&1; then
    rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
  fi
  return 0
}

record_commit_refusal() {
  # v0.18.2 (review round 3, MAJOR 2/3): a refusal INSIDE the loop's commit
  # path that is not the verify gate — the LEARNINGS credential scan, a
  # failed `git commit` (the project's own git hook, identity, signing), a
  # `git add` that could not stage — goes through the one channel a repair
  # turn reads: artifacts/phase-verify-failed.json (CONTINUE_PROMPT step 2),
  # with the VERIFY_MAX_ATTEMPTS breaker bounding it. Until v0.18.1 these
  # printed a line only the loop's log shows, and the next turn repaired
  # blind (or, with a committed completion record kept on disk, forever).
  # $1 = label, $2 = what the repair turn must read.
  local t out
  t="$(mktemp)" || return 0
  printf '%s\n' "$2" > "$t"
  record_verify_failure "$1" "$1" 1 "$t"
  rm -f "$t"
  # marked, so the next green gate does not clear it (_clear_verify_failed)
  out="$(jq '.commit_refusal = true' "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null)" \
    && printf '%s\n' "$out" > "$ARTIFACTS_DIR/phase-verify-failed.json"
  return 0
}

gate_status_snapshot() {
  # $1 = output file: one NUL-terminated "XY<TAB>path" record per entry of
  # `git status --porcelain -z`, untracked files listed one by one (never a
  # collapsed directory — except a nested repository, which stays `dir/`),
  # paths relative to the repo root whatever the cwd (porcelain ignores
  # status.relativePaths). A rename/copy entry carries its source as a
  # second NUL-terminated token (`R  new\0old\0`); the source is emitted as
  # its own record under the same letters so a restore puts BOTH ends back.
  # Returns non-zero (file empty) when git cannot report; the caller then
  # measures nothing this run rather than guessing.
  local rec xy path src
  git -C "$ROOT_DIR" status --porcelain -z --untracked-files=all 2>/dev/null \
    | {
        while IFS= read -r -d '' rec; do
          xy="${rec:0:2}"; path="${rec:3}"
          printf '%s\t%s\0' "$xy" "$path"
          case "$xy" in
            R?|C?|?R|?C)
              if IFS= read -r -d '' src; then printf '%s\t%s\0' "$xy" "$src"; fi ;;
          esac
        done
      } > "$1"
}

gate_footprint_diff() {
  # $1 = before, $2 = after, $3 = output. The footprint is (a) every
  # after-record whose PATH is absent from before — tracked paths newly
  # changed in any way, untracked paths newly present — and (b) a before-path
  # whose worktree column flipped from clean to changed across the gate
  # (review MAJOR-2): every commit site runs `git add -A` before the gate, so
  # a session path enters the gate with index == worktree (Y blank), and a Y
  # that is now `M`/`D` is unambiguously the gate's rewrite of the SESSION'S
  # file — restorable from the index byte-for-byte (recorded with X = "S";
  # the restore reads it). A path the session left untracked (`??`: a
  # transient signal, an unstaged add) cannot be judged and is left alone.
  # The loop's own transient signals (artifacts/<TRANSIENT_SIGNALS>) and the
  # baton slot (artifacts/session-handoff.json, promoted at loop start — the
  # moment a pending footprint is settled) are never a footprint. One record
  # per path, first wins (`git rm --cached` yields `D ` and `??` for one).
  local -A before=() seen=() skip=()
  local rec key xy sig
  for sig in "${TRANSIENT_SIGNALS[@]:-}"; do
    if [[ -n "$sig" ]]; then skip["artifacts/$sig"]=1; fi
  done
  skip["artifacts/session-handoff.json"]=1
  while IFS= read -r -d '' rec; do
    key="${rec#*$'\t'}"
    if [[ -z "${before[$key]:-}" ]]; then before["$key"]="${rec%%$'\t'*}"; fi
  done < "$1"
  : > "$3"
  while IFS= read -r -d '' rec; do
    key="${rec#*$'\t'}"; xy="${rec%%$'\t'*}"
    # The loop's own paths first, before any judgement (a healed tracked
    # transient is exactly `D ` + `??` on one path — heal_tracked_transients).
    # artifacts/logs/ is the loop's scratch (the record's lock and tmp files are
    # written INSIDE the gate window) and is excluded from status only
    # best-effort — skipped by name as well.
    if [[ -n "${skip[$key]:-}" || "$key" == artifacts/logs/* ]]; then continue; fi
    if [[ "$xy" == "??" && "${before[$key]:-}" == *D* ]]; then
      # A file the session deleted (staged or not) that the gate recreated:
      # the `??` is the gate's, whatever the first record for the path said
      # (review NEW-1: left alone, the next `git add -A` would silently undo
      # the session's deletion).
      printf '%s\0' "$rec" >> "$3"
      continue
    fi
    if [[ -n "${seen[$key]:-}" ]]; then continue; fi
    seen["$key"]=1
    if [[ -z "${before[$key]:-}" ]]; then
      printf '%s\0' "$rec" >> "$3"
    elif [[ "${before[$key]:1:1}" == " " && "${xy:1:1}" != " " && "${xy:1:1}" != "?" ]]; then
      case "$key" in
        artifacts/project-complete.json|artifacts/ready-to-deploy.json|artifacts/deferrals.json|artifacts/iterations/*/*.json)
          # The deadline watchdog's own keep-out (v0.18.0: `git reset` of
          # these two, which flips the worktree column while the worktree
          # keeps the session's bytes) runs in the last minute (review
          # NEW-3): never the gate's, never judged — a restore here would
          # rewrite the session's claim, the very thing v0.18.0 forbids. The
          # same unstage covers the loop's derived state (deferrals.json and
          # the phase-close evidence), never the gate's either.
          continue ;;
      esac
      printf 'S%s\t%s\0' "${xy:1:1}" "$key" >> "$3"
    fi
  done < "$2"
}

gate_footprint_json() {
  # $1 = footprint file → a JSON array of its paths, in order (jq --args
  # carries any bytes a path can hold; `--` so a leading `-` is a path, not
  # an option — review MINOR-1).
  local rec paths=()
  while IFS= read -r -d '' rec; do paths+=("${rec#*$'\t'}"); done < "$1"
  jq -cn '$ARGS.positional' --args -- "${paths[@]}"
}

_gate_git() {
  # Every restore names paths LITERALLY (review MAJOR-3): as a pathspec, a
  # name with `*`, `?`, `[` or a leading `:` would match OTHER files —
  # including the session's — and revert them without a word.
  git --literal-pathspecs -C "$ROOT_DIR" "$@"
}

gate_footprint_restore() {
  # $1 = footprint file. Records:
  #   "S?"  a session path the gate rewrote in the worktree → worktree back
  #         to the INDEX (the session's staged bytes); nothing else touched;
  #   "??"  untracked → deleted (a collapsed nested repository `dir/` whole);
  #   else  tracked → index back to HEAD, then worktree from the index — a
  #         path absent from the before-snapshot had index == HEAD ==
  #         worktree when the gate started, so HEAD is exactly what the
  #         index held; a path HEAD lacks (a file the gate created and
  #         staged, or added with intent) is unstaged and removed.
  # Untracked deletions run LAST, after the tracked restores and a
  # check-ignore pass: a gate that rewrote .gitignore exposed previously
  # ignored files as `??`; once .gitignore is back they are ignored again
  # and are kept (review MINOR-5). Best-effort per path: a path that cannot
  # be restored is named and left for git status to show.
  local rec xy path untracked=()
  while IFS= read -r -d '' rec; do
    xy="${rec%%$'\t'*}"; path="${rec#*$'\t'}"
    case "$xy" in
      "??")
        untracked+=("$path"); continue ;;
      S?)
        _gate_git checkout -q -- "$path" 2>/dev/null \
          || echo "  gate footprint: could not restore '$path' from the index — left as is (git status shows it)" >&2
        continue ;;
    esac
    if [[ "$(_gate_git ls-files -s -- "$path" 2>/dev/null | cut -c1-6)" == "160000" ]]; then
      # A submodule (gitlink): its worktree is another repository's; named,
      # never restored from here (review NEW-2).
      echo "  gate footprint: '$path' is a submodule — not restored (git status shows it)" >&2
      continue
    fi
    _gate_git reset -q -- "$path" >/dev/null 2>&1 || true
    if _gate_git cat-file -e "HEAD:$path" 2>/dev/null; then
      _gate_git checkout -q -- "$path" 2>/dev/null \
        || echo "  gate footprint: could not restore '$path' from HEAD — left as is (git status shows it)" >&2
    else
      rm -f -- "$ROOT_DIR/$path"
    fi
  done < "$1"
  for path in "${untracked[@]}"; do
    # check-ignore takes pathnames, not pathspecs (and refuses the literal
    # mode): a plain call is exact here.
    if git -C "$ROOT_DIR" check-ignore -q -- "$path" 2>/dev/null; then
      echo "  gate footprint: '$path' is ignored again now that the tree is restored — kept" >&2
      continue
    fi
    if [[ "$path" == */ ]]; then rm -rf -- "$ROOT_DIR/$path"; else rm -f -- "$ROOT_DIR/$path"; fi
  done
}

gate_pending_record() {
  # $1 = the before-snapshot file, $2 = command, $3 = label. Kept in the
  # (transient, hidden) boundary record until the restore has run, so a kill
  # anywhere between here and the restore is settled at the next loop start
  # (gate_settle_pending) instead of the gate's dirt being staged as the
  # session's work — and the deadline watchdog's last-resort commit, seeing
  # it, commits the index as it stands rather than `git add -A`. The whole
  # "XY<TAB>path" records are stored (letters included — the index survives
  # a kill, so a session path the gate rewrote is judged and restored from
  # the index at the settle exactly as on the live path; review re-check),
  # each as base64 of its raw bytes (review MINOR-2: jq would mangle a
  # non-UTF-8 name and the settle would then mistake the session's own file
  # for footprint). A record that cannot be written is said out loud (review
  # MINOR-6: ARG_MAX on a huge untracked set) — the kill window is unguarded
  # for that run.
  command -v _boundary_write >/dev/null 2>&1 || return 0
  local rec paths=() json
  while IFS= read -r -d '' rec; do
    paths+=("$(printf '%s' "$rec" | base64 -w0)")
  done < "$1"
  if ! json="$(jq -cn '$ARGS.positional' --args -- "${paths[@]}" 2>/dev/null)" \
     || ! _boundary_write '.gate_pending = {before: $before, command: $cmd, label: $lbl, at: $now}' \
          --argjson before "$json" --arg cmd "$2" --arg lbl "$3"; then
    echo "  (gate footprint: the pending record could not be written — a kill inside this gate run would not be settled at the next start)" >&2
  fi
}

gate_pending_clear() {
  command -v _boundary_write >/dev/null 2>&1 || return 0
  [[ -f "${BOUNDARY_STATE_FILE:-}" ]] || return 0
  _boundary_write 'del(.gate_pending)'
}

gate_pending_before_file() {
  # $1 = output file: the pending record's before-snapshot, the very
  # "XY<TAB>path" records the live path took (base64-decoded), so the
  # settle's diff judges exactly what the live diff would have.
  local line
  : > "$1"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    { base64 -d <<<"$line" 2>/dev/null || true; printf '\0'; } >> "$1"
  done < <(boundary_get '.gate_pending.before[]? | strings')
}

gate_pending_restore_now() {
  # The pending gate's footprint, measured and restored NOW: everything
  # that appeared since its before-snapshot — a new path, or a session path
  # whose worktree column flipped (the index survived the kill, so the
  # restore from it is exact). Prints the footprint as a JSON array of
  # paths, or nothing when nothing appeared or nothing could be measured.
  local before after paths json=""
  before="$(mktemp)"; after="$(mktemp)"; paths="$(mktemp)"
  gate_pending_before_file "$before"
  if gate_status_snapshot "$after"; then
    gate_footprint_diff "$before" "$after" "$paths"
    if [[ -s "$paths" ]]; then
      json="$(gate_footprint_json "$paths")" || json='["(unavailable)"]'
      gate_footprint_restore "$paths"
    fi
  else
    echo "  (git status unavailable — the pending gate footprint could not be measured; left as is)" >&2
  fi
  rm -f "$before" "$after" "$paths"
  printf '%s' "$json"
}

gate_settle_pending() {
  # Loop start, before any turn (and before the loop-start recovery stages
  # anything): the previous session died inside its verify gate — during
  # the command, or after it returned and before the restore — and the
  # record still carries that gate's before-snapshot. Everything dirty now
  # that was not dirty then is that gate's footprint: restore it and record
  # the red as the live path would (paths, rule, recipe) without spending a
  # breaker attempt (the live re-run does), then clear the record. No
  # command output survived the kill and the command never returned, so the
  # capture carries exit_code null and says so. Nothing but the loop may
  # touch a tree the loop left mid-gate: a hand edit made between the kill
  # and this start is dirt that was not there before the gate, and is
  # restored with the rest. Inert when no gate is pending.
  command -v boundary_get >/dev/null 2>&1 || return 0
  [[ -f "${BOUNDARY_STATE_FILE:-}" ]] || return 0
  local n
  n="$(boundary_get '.gate_pending.before | if type == "array" then length else -1 end')"
  [[ "$n" =~ ^[0-9]+$ ]] || return 0
  local at cmd label json log
  at="$(boundary_get '.gate_pending.at // "?"')"
  cmd="$(boundary_get '.gate_pending.command // "scripts/phasekit-verify.sh"')"
  label="$(boundary_get '.gate_pending.label // "scripts/phasekit-verify.sh"')"
  echo "Verify gate: the previous session was killed inside its gate ($label, at $at) — settling that gate's footprint before anything else runs (v0.14.10)." >&2
  json="$(gate_pending_restore_now)"
  if [[ -n "$json" ]]; then
    log="$(mktemp)"
    echo "(no command output: the previous session was killed inside this gate at $at; the footprint below is what that run left on disk)" > "$log"
    record_verify_failure "$cmd" "$label" null "$log" settle "$json"
    rm -f "$log"
  else
    echo "  (nothing appeared beyond that gate's before-snapshot — no footprint to settle)" >&2
  fi
  gate_pending_clear
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
  #    prerequisite.
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

_clear_verify_failed() {
  # A green gate clears the red capture — the LOOP's gate. A model's own
  # `phasekit verify` never does (v0.18.0, review round 1): the capture's
  # attempt count is the VERIFY_MAX_ATTEMPTS breaker's, and a model run is
  # not the commit gate. v0.18.1 (row 1193 (4)): every green path of the
  # loop's gate runs through here, so the last gate's red is cleared here
  # too — a green early return (VERIFY_SKIP, no gate configured, the memo)
  # once left LAST_GATE_RED standing for the wrap-up to read.
  if [[ "${VERIFY_INVOKER:-loop}" != model ]]; then
    # v0.18.2: a refusal the COMMIT path recorded (record_commit_refusal —
    # the gate was green then too) is not the gate's to clear: only a commit
    # that happens clears it, so its attempts count to VERIFY_MAX_ATTEMPTS.
    if ! jq -e '.commit_refusal == true' "$ARTIFACTS_DIR/phase-verify-failed.json" >/dev/null 2>&1; then
      rm -f "$ARTIFACTS_DIR/phase-verify-failed.json"
    fi
    LAST_GATE_RED=0
  fi
  return 0
}

verify_memo_reusable() {
  # May this run's verdict be memoised for the commit gate to reuse? The
  # loop's own runs, always. A model's `phasekit verify` only when its shell
  # environment is the loop's (review round 1: `SKIP_E2E=1 phasekit verify`
  # must never stand in for the loop's gate) — MODEL_ENV_REUSABLE is set
  # by phasekit_verify from the loop's hashed snapshot.
  [[ "${VERIFY_INVOKER:-loop}" != model ]] && return 0
  [[ "${MODEL_ENV_REUSABLE:-0}" == 1 ]]
}

verify_command_resolve() {
  # The project's gate, resolved ONE way (run_verify_gate and plain step 4's
  # memo check read it): VC_CMD, VC_LABEL, VC_INVOKE (empty when none).
  VC_CMD=""; VC_LABEL=""; VC_INVOKE=""
  if [[ -n "${PHASEKIT_VERIFY_CMD:-}" ]]; then
    VC_CMD="$PHASEKIT_VERIFY_CMD"; VC_LABEL="PHASEKIT_VERIFY_CMD"; VC_INVOKE="shell"
  elif [[ -f "$ROOT_DIR/scripts/phasekit-verify.sh" ]]; then
    VC_CMD="$ROOT_DIR/scripts/phasekit-verify.sh"; VC_LABEL="scripts/phasekit-verify.sh"; VC_INVOKE="bash"
  fi
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
    # v0.18.0: a contracts red is a loop gate's red like any other — the
    # wrap-up after a take-control must not re-gate it with no turn between.
    [[ "${VERIFY_INVOKER:-loop}" != model ]] && LAST_GATE_RED=1
    return 1
  fi

  # Escape hatch: VERIFY_SKIP=1 bypasses the gate entirely (sparingly — e.g.
  # docs-only phases or TDD phases that intentionally commit a red test).
  if [[ "${VERIFY_SKIP:-}" == "1" ]]; then
    echo "VERIFY_SKIP=1 — bypassing pre-commit verify gate."
    _clear_verify_failed
    return 0
  fi

  local cmd="" label="" invoke=""
  verify_command_resolve
  cmd="$VC_CMD"; label="$VC_LABEL"; invoke="$VC_INVOKE"

  if [[ -z "$cmd" ]]; then
    # Expected on the phasekit source repo itself: the verify script is rendered
    # into downstream projects but not committed here, so self-improvement loops
    # fail-open. Known gap — see docs/QUALITY_GATES.md "Self-hosting gap".
    echo "WARN: no verify configured (scripts/phasekit-verify.sh not present)" >&2
    echo "      see docs/QUALITY_GATES.md 'Pre-commit verification gate' to enable" >&2
    _clear_verify_failed
    return 0
  fi

  # Verify memo (v0.14.5): a green gate on this EXACT tree, same command,
  # same-or-stronger tier, is reused instead of re-run — the squash caught up
  # at a boundary and the completion commit after an approval commit were
  # each spending a whole session bound on a suite the tree already passed.
  # Never for a new tree. The contracts gate above always runs.
  local memo_tree="" memo_tier=fast
  if command -v verify_memo_hit >/dev/null 2>&1; then
    memo_tree="$(git write-tree 2>/dev/null)" || memo_tree=""
    memo_tier="$(verify_memo_tier)"
    if [[ -n "$memo_tree" ]] && verify_memo_hit "$memo_tree" "$memo_tier" "$label" "$cmd"; then
      echo "Pre-commit verify: $label — reusing the green verdict recorded for this exact tree (${memo_tree:0:12}, $(boundary_get '.verify_memo.tier // "?"') tier, passed $(boundary_get '.verify_memo.passed_at // "?"')); not re-run (v0.14.5 verify memo)."
      _clear_verify_failed
      return 0
    fi
    if [[ -n "$memo_tree" && "${BOUNDARY_WALK_CONTEXT:-}" == "stranded" ]] \
       && verify_memo_hit_red "$memo_tree" "$label" "$cmd"; then
      # Known red for this exact tree, and NO gate has run since it was
      # recorded (loop-start recovery only — round-2 F2/F3: honoured
      # in-loop, the memo would let a model that re-touches the artifact
      # without changing the tree dodge VERIFY_MAX_ATTEMPTS forever, and
      # would pin a red the model fixed outside the tree). Answer from the
      # memo, re-surface the capture for the model, spend NO breaker attempt.
      echo "Pre-commit verify: $label — this exact tree (${memo_tree:0:12}) is recorded RED (failed $(boundary_get '.verify_red.failed_at // "?"')); not re-run, no attempt spent (v0.14.5 verify memo). Fix the failure in artifacts/phase-verify-failed.json first." >&2
      local memo_log memo_code
      memo_log="$(mktemp)"
      boundary_get '.verify_red.log_tail // ""' > "$memo_log" 2>/dev/null || true
      memo_code="$(boundary_get '.verify_red.exit_code // 1')"; [[ "$memo_code" =~ ^[0-9]+$ ]] || memo_code=1
      # v0.14.10: a red BY FOOTPRINT (command rc may be 0) replays with its
      # paths, so the memo can never pass it off as a plain red or a green.
      local memo_fp
      memo_fp="$(boundary_get '.verify_red.gate_footprint // null')"
      # ONE capture path for every verify failure (the v0.6.6 lesson): the
      # shared writer, in its no-attempt mode.
      record_verify_failure "$cmd" "$label" "$memo_code" "$memo_log" memo "$memo_fp"
      rm -f "$memo_log"
      [[ "${VERIFY_INVOKER:-loop}" != model ]] && LAST_GATE_RED=1
      return 1
    fi
  fi

  echo "Pre-commit verify: $label"
  local log
  log="$(mktemp)"
  local verify_status=0
  local verify_start verify_elapsed
  # v0.14.10 gate footprint: what git status sees before the command, and
  # after. Measured, never inferred (see the rule block above). When git
  # cannot report, nothing is measured this run — said out loud, never
  # guessed.
  local fp_before fp_after fp_paths fp_measured=1 footprint_json=""
  fp_before="$(mktemp)"; fp_after="$(mktemp)"; fp_paths="$(mktemp)"
  if gate_status_snapshot "$fp_before"; then
    # The before-snapshot outlives this process until the restore has run
    # (gate_pending): a kill inside the gate is settled at the next loop
    # start, never staged as the session's work. v0.18.0: never for a gate
    # the MODEL runs (`phasekit verify`) — the model may still be editing
    # while it runs (a backgrounded or timed-out call), and a settle would
    # restore or delete the model's own work as "footprint" (review round 2).
    if [[ "${VERIFY_INVOKER:-loop}" != model ]]; then gate_pending_record "$fp_before" "$cmd" "$label"; fi
  else
    fp_measured=0
    echo "  (gate footprint: git status unavailable before the gate — not measured this run)" >&2
  fi
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
  if [[ "$fp_measured" -eq 1 ]]; then
    if gate_status_snapshot "$fp_after"; then
      gate_footprint_diff "$fp_before" "$fp_after" "$fp_paths"
      # Fault injection (tests only): the seam between the command's return
      # and the restore — PHASEKIT_BOUNDARY_KILL_PROBE="gate:footprint".
      _boundary_kill_probe gate footprint
      if [[ -s "$fp_paths" ]]; then
        footprint_json="$(gate_footprint_json "$fp_paths")" || footprint_json='["(unavailable)"]'
        # The loop restores; a model's run only REPORTS (nothing rewrites the
        # tree during a model's turn — the same reason as above).
        if [[ "${VERIFY_INVOKER:-loop}" != model ]]; then gate_footprint_restore "$fp_paths"; fi
      fi
    else
      echo "  (gate footprint: git status unavailable after the gate — not measured this run)" >&2
    fi
    gate_pending_clear
  fi
  rm -f "$fp_before" "$fp_after" "$fp_paths"

  # v0.18.0: the gate's wall time is a cost-ledger sample for its tier, and
  # part of the landing's time the W measure must not count twice.
  # v0.18.1 (row 1193 (2)) — which runs are samples. The measure feeds the
  # landing budget (T_y = G_full + W + 60 s): what a GREEN gate costs,
  # since only a green gate lands anything. A command that ran to the end
  # (exit 0 — a footprint red included) is a sample. A red command stopped
  # where it failed, so its time is a LOWER BOUND on the green run: it is
  # evidence only when it exceeds the current estimate (a gate that hangs
  # into its own timeout still raises the P90), never below it — a
  # fast-failing lint once pulled the full tier's P90 down, shrinking the
  # lead for the green run that follows. Pass and fail are still both
  # measured; a red can only raise the estimate, never lower it.
  VERIFY_SECONDS_ACC=$(( ${VERIFY_SECONDS_ACC:-0} + verify_elapsed ))
  if verify_memo_reusable; then
    if [[ "$verify_status" -eq 0 ]] || (( verify_elapsed > $(cost_value "g_$memo_tier") )); then
      cost_sample "g_$memo_tier" "$verify_elapsed"
    fi
  fi
  if [[ "${VERIFY_INVOKER:-loop}" != model ]]; then hermetic_tests_advisory; scaffold_reads_advisory; fi

  # Verify-budget advisory. v0.18.0: under a session bound it is MEASURED —
  # the full tier's P90 against 12.5% of the bound (the F4 threshold), once
  # per session; standalone (no bound) it keeps the v0.6.4 fixed budget.
  # Never blocks, never edits the project's gate.
  if [[ "${DEADLINE_SPAN:-0}" -gt 0 ]]; then
    local g90
    g90="$(cost_p90 g_full)"
    if [[ "$VERIFY_BUDGET_ADVISED" == 0 && -n "$g90" ]] && (( g90 * 1000 > DEADLINE_SPAN * 125 )); then
      VERIFY_BUDGET_ADVISED=1
      echo "ADVISORY: the full verify tier's measured P90 (${g90}s) is over 12.5% of this session's bound (${DEADLINE_SPAN}s) — the landing does not fit its session; see docs/QUALITY_GATES.md 'Verify budget' for the split by measured duration."
    fi
  fi
  if (( verify_elapsed > VERIFY_BUDGET_SECONDS )); then
    if [[ "${DEADLINE_SPAN:-0}" -eq 0 ]]; then
      VERIFY_OVER_BUDGET_RUNS=$((VERIFY_OVER_BUDGET_RUNS + 1))
      if (( VERIFY_OVER_BUDGET_RUNS >= 2 && VERIFY_BUDGET_ADVISED == 0 )); then
        VERIFY_BUDGET_ADVISED=1
        echo "ADVISORY: verify exceeded its budget (${verify_elapsed}s > ${VERIFY_BUDGET_SECONDS}s, ${VERIFY_OVER_BUDGET_RUNS} runs this session) — see docs/QUALITY_GATES.md 'Verify budget' for the fast/slow split."
      fi
    fi
  fi

  if [[ "${VERIFY_INVOKER:-loop}" != model ]]; then
    if [[ "$verify_status" -eq 0 && -z "$footprint_json" ]]; then LAST_GATE_RED=0; else LAST_GATE_RED=1; fi
  fi
  if [[ "$verify_status" -eq 0 && -z "$footprint_json" ]]; then
    echo "  Verify passed."
    rm -f "$log"; _clear_verify_failed
    if [[ -n "$memo_tree" ]] && verify_memo_exact_tree && verify_memo_reusable; then
      verify_memo_record "$memo_tree" "$memo_tier" "$label" "$cmd"
    fi
    return 0
  fi

  # Failure path — a red command, or a green command that wrote to the tree
  # (v0.14.10: the footprint alone makes the run red; exit_code stays the
  # command's own). Capture context so the next iteration can diagnose.
  record_verify_failure "$cmd" "$label" "$verify_status" "$log" "" "$footprint_json"
  if [[ -n "$memo_tree" ]] && command -v verify_memo_record_red >/dev/null 2>&1 && verify_memo_exact_tree && verify_memo_reusable; then
    verify_memo_record_red "$memo_tree" "$label" "$cmd" "$verify_status" "$log" "$footprint_json"
  fi
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
  if squash_mode; then
    # Branch-per-iteration: the work branch may be brand new (no upstream
    # yet), and the target moved locally at the last squash — push both.
    if git push -u origin HEAD 2>&1 && git push origin "$SQUASH_TARGET" 2>&1; then
      echo "  Pushed (work branch + $SQUASH_TARGET)."
    else
      echo "  WARN: git push failed (commits are local; continuing loop)" >&2
    fi
    return 0
  fi
  if git push 2>&1; then
    echo "  Pushed."
  else
    echo "  WARN: git push failed (commit is local; continuing loop)" >&2
  fi
}

# --- Branch-per-iteration + squash-to-target (v0.14.0) -----------------------
# Design: foundry-meta designs/DESIGN-branch-per-iteration.md (approved
# 2026-08-13, forks: squash per PHASE; branches kept; per-project pilot).
#
# The model. With PHASEKIT_SQUASH_TARGET=<branch> set, the loop works on a
# WORK BRANCH (PHASEKIT_WORK_BRANCH, or `iter/<utc-stamp>` created on the
# spot when the loop finds HEAD on the target) and commits there exactly as
# before: checkpoints, wrap-ups, strand commits, heals. The target only ever
# moves at an APPROVAL-CLASS commit (phase-approval.json /
# project-complete.json), and only through squash_to_target:
#
#   S = commit-tree(HEAD^{tree}, parent = target tip, message = the approval's
#       suggested_commit_message + a `phasekit-squash: <branch>@<sha>` trailer)
#   target := S                       (update-ref, old-value guarded: atomic)
#   M = commit-tree(HEAD^{tree}, parents = HEAD + S)   ("merge-back")
#   work   := M
#
# The merge-back is what makes the NEXT squash diff only the next phase: the
# target tip is now an ancestor of the work branch, so the branch's tree
# relative to the target is exactly the work since this approval. Nothing is
# ever rewritten or force-pushed — not the target, not the branch; checkpoint
# history survives on the branch for forensics (fork B keeps branches).
# Plumbing (commit-tree/update-ref) rather than checkout+merge --squash on
# purpose: the working tree and index are never touched, so a kill at any
# instant leaves at most a half-done squash (target advanced, merge-back
# missing) — which repair_half_squash completes idempotently at the next
# boundary. Hooks do not run for S; its tree is the branch commit's tree,
# which just passed the verify gate and the project's own hooks.
#
# Squash-integrity guard (field-scan steal-list #1, Gas Town's Refinery):
# the target may only move through this function, so its tip must be an
# ancestor of the work branch. Anything else — a hand commit, a hotfix, a
# hand-merge — fails the squash CLOSED: phase-blocked.json (blocker_kind
# branch-integrity), exit 2, target untouched, and the loop re-attempts at
# every later boundary without spending a token until an operator merges the
# target into the branch (or resets it). A best-effort fetch extends the same
# rule to origin/<target> when a remote is known. Under a supervisor that
# serializes sessions per project, the guard never fires on a healthy repo.
#
# Standalone users pay nothing for this: unset, no function here runs.

squash_mode() {
  [[ -n "$SQUASH_TARGET" ]]
}

current_branch() {
  git symbolic-ref -q --short HEAD 2>/dev/null || echo "HEAD"
}

write_branch_integrity_block() {
  local reason="$1" next_step="$2"
  jq -n \
    --arg reason "$reason" \
    --arg next "$next_step" \
    --arg target "$SQUASH_TARGET" \
    --arg branch "$(current_branch)" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      blocked: true,
      blocker_kind: "branch-integrity",
      reason: $reason,
      summary: ("branch-per-iteration: " + $reason),
      target: $target,
      branch: $branch,
      next_step: $next,
      ts: $ts
    }' > "$ARTIFACTS_DIR/phase-blocked.json"
  BRANCH_INTEGRITY_BLOCKED=1
  echo "run-until-done: BLOCKED (branch-integrity) — $reason. See artifacts/phase-blocked.json." >&2
}

write_landing_block() {
  # v0.18.2 (review round 1, M1): a FINAL boundary whose worktree still
  # differs from HEAD after the loop's own commit carried everything it can
  # stage — nothing any later commit could carry, so re-entering would spin
  # to MAX_ITERATIONS. Stop, named: the paths, and what to do.
  local paths="$1" why="${2:-the worktree differs from HEAD in paths no loop commit can carry}"
  jq -n --arg paths "$paths" --arg why "$why" --arg branch "$(current_branch)" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      blocked: true,
      blocker_kind: "landing",
      reason: ("the completion cannot land: " + $why + (if $paths == "" then "" else " (" + $paths + ")" end)),
      summary: ("landing: " + $why),
      branch: $branch,
      next_step: "make the listed paths match HEAD (they are changes no loop commit can carry — the loop never lands what it cannot verify), then re-run: the committed completion lands at the next start",
      ts: $ts
    }' > "$ARTIFACTS_DIR/phase-blocked.json"
  BRANCH_INTEGRITY_BLOCKED=1
  echo "run-until-done: BLOCKED (landing) — the completion cannot land: $why${paths:+ ($paths)}. See artifacts/phase-blocked.json." >&2
}

squash_applies_to() {
  # Only approval-class records move the target; checkpoints stay on the branch.
  squash_mode || return 1
  case "$(basename "$1")" in
    phase-approval.json|project-complete.json) return 0 ;;
  esac
  return 1
}

squash_pending() {
  # An approval-class record is committed on the work branch that the target
  # does not carry. Stateless and exact: the target only ever receives a tree
  # through squash_to_target, after which both sides hold identical blobs.
  # Checkpoint-only differences (phase-update commits) are NOT pending — they
  # belong to a phase that has not been approved yet.
  squash_mode || return 1
  local f hb tb
  for f in phase-approval.json project-complete.json; do
    hb="$(git rev-parse -q --verify "HEAD:artifacts/$f" 2>/dev/null)" || continue
    tb="$(git rev-parse -q --verify "refs/heads/$SQUASH_TARGET:artifacts/$f" 2>/dev/null)" || tb=""
    [[ "$hb" == "$tb" ]] || return 0
  done
  return 1
}

merge_back_from_target() {
  # Record the target's tip as a second parent of the work branch. Trees are
  # identical by construction, so this is history-only: index and working tree
  # are untouched and `git status` reads the same before and after.
  local tree m head
  head="$(git rev-parse HEAD)" || return 1
  tree="$(git rev-parse "$head^{tree}")" || return 1
  m="$(git commit-tree "$tree" -p "$head" -p "refs/heads/$SQUASH_TARGET" \
        -m "chore(workflow): merge-back $SQUASH_TARGET after squash (phasekit v0.14.0)" \
        -m "$(phasekit_trailers merge-back)")" || return 1
  git update-ref -m "phasekit merge-back" "refs/heads/$(current_branch)" "$m" "$head"
}

repair_half_squash() {
  # A kill between the two ref updates leaves the target at S with no
  # merge-back on the branch — which the ancestry guard would otherwise read
  # as an out-of-band move. Recognise our own half-done squash by S's own
  # trailer (`phasekit-squash: <branch>@<sha>`): the named commit must be an
  # ancestor of HEAD and S must carry exactly its tree. Commits the branch
  # gained since the kill (an intake, a strand) do not defeat the match
  # (v0.14.0 review, MINOR-1). Idempotent; a no-op on every other state.
  squash_mode || return 0
  git rev-parse -q --verify "refs/heads/$SQUASH_TARGET" >/dev/null 2>&1 || return 0
  git merge-base --is-ancestor "refs/heads/$SQUASH_TARGET" HEAD 2>/dev/null && return 0
  local from
  from="$(git log -1 --format=%B "refs/heads/$SQUASH_TARGET" 2>/dev/null \
          | sed -n 's/^phasekit-squash: [^@]*@\([0-9a-f]\{7,40\}\)$/\1/p' | tail -n1)" || from=""
  [[ -n "$from" ]] || return 0
  git rev-parse -q --verify "$from^{commit}" >/dev/null 2>&1 || return 0
  git merge-base --is-ancestor "$from" HEAD 2>/dev/null || return 0
  [[ "$(git rev-parse "refs/heads/$SQUASH_TARGET^{tree}")" == "$(git rev-parse "$from^{tree}")" ]] || return 0
  echo "Branch-per-iteration: completing an interrupted squash (merge-back was missing)."
  merge_back_from_target
}

ensure_work_branch() {
  # Loop start. Decide where HEAD should be and put it there, or block.
  squash_mode || return 0
  local cur want
  cur="$(current_branch)"
  if ! git rev-parse -q --verify "refs/heads/$SQUASH_TARGET" >/dev/null 2>&1; then
    write_branch_integrity_block \
      "PHASEKIT_SQUASH_TARGET='$SQUASH_TARGET' is not a local branch" \
      "create or fetch branch '$SQUASH_TARGET', or unset PHASEKIT_SQUASH_TARGET, then re-run"
    return 1
  fi
  if [[ "$cur" == "HEAD" ]]; then
    write_branch_integrity_block \
      "detached HEAD — the loop needs a work branch" \
      "check out '$SQUASH_TARGET' (the loop creates the work branch) or an existing work branch, then re-run"
    return 1
  fi
  want="${PHASEKIT_WORK_BRANCH:-}"
  if [[ "$cur" == "$SQUASH_TARGET" ]]; then
    [[ -n "$want" ]] || want="iter/$(date -u +%Y%m%dT%H%M%SZ)"
    if git rev-parse -q --verify "refs/heads/$want" >/dev/null 2>&1; then
      # Re-entering an existing work branch from the target is only safe when
      # it carries nothing the target lacks: its tree is one the target has
      # already held (the target may have advanced since — an intake commit
      # after a finished iteration; v0.14.0 review, MINOR-4).
      if ! git log -n 500 --format=%T "refs/heads/$SQUASH_TARGET" 2>/dev/null \
           | grep -qx "$(git rev-parse "refs/heads/$want^{tree}")"; then
        # v0.14.4: a kept branch that differs from the target ONLY by the
        # loop's own transient signals or batons (a kill-path wip commits
        # session-handoff.json with `add -A`; the squash never carries it) is
        # merged in every sense that matters — re-enter it. Anything else is
        # real content, and the block names it so the operator sees WHAT the
        # target lacks instead of guessing (2026-09-07 04:01 incident).
        # `--no-renames` (review finding): rename detection would fold a real
        # deletion into the baton path and admit it; `T...B` compares against
        # the merge base so an intake commit on the target is not counted as
        # branch content.
        # git prints repo-relative paths; compare against the artifacts dir
        # as git names it, not as the filesystem does.
        local _bd _bd_real=() _bd_path _art_rel
        _art_rel="$(realpath --relative-to="$ROOT_DIR" "$ARTIFACTS_DIR" 2>/dev/null || echo artifacts)"
        while IFS= read -r _bd_path; do
          [[ -n "$_bd_path" ]] || continue
          if [[ "$_bd_path" == "$_art_rel/session-handoff.json" || "$_bd_path" == "$_art_rel/session-interrupted.json" ]]; then continue; fi
          _bd=0
          for sig in "${TRANSIENT_SIGNALS[@]}"; do [[ "$_bd_path" == "$_art_rel/$sig" ]] && { _bd=1; break; }; done
          [[ "$_bd" -eq 1 ]] || _bd_real+=("$_bd_path")
        done < <(git diff --no-renames --name-only "refs/heads/$SQUASH_TARGET...refs/heads/$want" 2>/dev/null)
        if [[ ${#_bd_real[@]} -gt 0 ]]; then
          write_branch_integrity_block \
            "work branch '$want' already exists with content '$SQUASH_TARGET' does not carry (differs in: ${_bd_real[*]:0:5}), while HEAD is on '$SQUASH_TARGET'" \
            "check out '$want' and re-run (the loop squashes it at the next approval), or retire the branch by hand"
          return 1
        fi
        echo "Branch-per-iteration: re-entering '$want' — it differs from '$SQUASH_TARGET' only by transient signals or batons."
      fi
      git checkout -q "$want" || { write_branch_integrity_block "could not check out work branch '$want'" "resolve the checkout failure by hand, then re-run"; return 1; }
      # The target may have advanced since this branch finished (an intake
      # commit); bring it in — a clean merge by construction, the branch holds
      # nothing beyond a tree the target already had.
      if ! git merge-base --is-ancestor "refs/heads/$SQUASH_TARGET" HEAD 2>/dev/null; then
        git merge -q --no-edit "refs/heads/$SQUASH_TARGET" >/dev/null 2>&1 \
          || { git merge --abort >/dev/null 2>&1 || true; write_branch_integrity_block "could not bring '$SQUASH_TARGET' into re-entered work branch '$want'" "merge $SQUASH_TARGET into '$want' by hand, then re-run"; return 1; }
      fi
    else
      git checkout -q -b "$want" || { write_branch_integrity_block "could not create work branch '$want'" "resolve the checkout failure by hand, then re-run"; return 1; }
      # v0.18.0: the iteration's base when no supervisor names one (the
      # target tip this branch starts from) — iteration_base_sha's last
      # source; carried across passes by boundary_begin.
      _boundary_write '.work_base = {branch: $b, sha: $s}' --arg b "$want" --arg s "$(git rev-parse HEAD)"
    fi
    echo "Branch-per-iteration: work branch '$want' (squash target '$SQUASH_TARGET')."
  elif [[ -n "$want" && "$cur" != "$want" ]]; then
    write_branch_integrity_block \
      "HEAD is on '$cur' but PHASEKIT_WORK_BRANCH='$want'" \
      "check out '$want' (or '$SQUASH_TARGET', and the loop will create/enter '$want'), then re-run"
    return 1
  else
    echo "Branch-per-iteration: on work branch '$cur' (squash target '$SQUASH_TARGET')."
  fi
  repair_half_squash || true
  # The same guard the squash applies, applied before any token is spent: a
  # target that moved out-of-band will refuse every squash this session.
  if ! git merge-base --is-ancestor "refs/heads/$SQUASH_TARGET" HEAD 2>/dev/null; then
    write_branch_integrity_block \
      "'$SQUASH_TARGET' moved out-of-band (its tip is not an ancestor of the work branch)" \
      "git merge $SQUASH_TARGET into the work branch by hand (resolve conflicts, re-verify), then re-run"
    return 1
  fi
  return 0
}

squash_to_target() {
  # $1 = commit message for the squash commit
  # $2 = 1 when HEAD's tree just passed the verify gate (the commit path),
  #      0 to run the gate here first (a squash caught up at a boundary —
  #      the tree may have landed via a --no-verify strand commit).
  # $3 = the Phasekit-* trailer lines (v0.18.0), appended to the one trailer
  #      block after `phasekit-squash:` (which stays first and unchanged).
  # Returns 0 on success or nothing-to-do; 1 with phase-blocked.json written
  # (or phase-verify-failed.json, when the gate is what refused).
  local msg="$1" verified="${2:-1}" facts="${3:-}"
  squash_mode || return 0
  local work old_target head tree remote_target trailer s
  work="$(current_branch)"
  if [[ "$work" == "HEAD" || "$work" == "$SQUASH_TARGET" ]]; then
    write_branch_integrity_block "cannot squash: HEAD is on '$work', not a work branch" \
      "check out '$SQUASH_TARGET' and re-run (the loop creates the work branch)"
    return 1
  fi
  if ! git rev-parse -q --verify "refs/heads/$SQUASH_TARGET" >/dev/null 2>&1; then
    write_branch_integrity_block "PHASEKIT_SQUASH_TARGET='$SQUASH_TARGET' is not a local branch" \
      "create or fetch branch '$SQUASH_TARGET', then re-run"
    return 1
  fi
  repair_half_squash || true
  old_target="$(git rev-parse "refs/heads/$SQUASH_TARGET")"
  # One HEAD snapshot for tree, trailer and merge-back parent: a strand commit
  # landing between two reads must not produce a merge-back whose tree
  # silently omits it (v0.14.0 review, MINOR-2).
  head="$(git rev-parse HEAD)"
  tree="$(git rev-parse "$head^{tree}")"
  # Guard 1 (local ancestry) runs BEFORE the nothing-to-do short-circuit: an
  # unrelated target that happens to hold this tree is still an integrity
  # failure, not a finished squash (MINOR-5).
  if ! git merge-base --is-ancestor "$old_target" "$head" 2>/dev/null; then
    write_branch_integrity_block \
      "squash refused: '$SQUASH_TARGET' moved out-of-band (tip $(git rev-parse --short "$old_target") is not an ancestor of '$work')" \
      "git merge $SQUASH_TARGET into '$work' by hand (resolve conflicts, re-verify), then re-run — the squash retries at the next boundary"
    return 1
  fi
  if [[ "$tree" == "$(git rev-parse "$old_target^{tree}")" ]]; then
    echo "Branch-per-iteration: '$SQUASH_TARGET' already carries this tree — nothing to squash."
    return 0
  fi
  # Guard 2 (remote, best-effort): when origin/<target> is known, it must not
  # be ahead either — the push would be rejected anyway, and a target that
  # diverged upstream must never be papered over locally. Fetch may fail
  # (no credentials inside a container): the last-seen remote tip still
  # counts, and a fetch that hangs is bounded.
  if git rev-parse -q --verify "refs/remotes/origin/$SQUASH_TARGET" >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
      timeout 20 git fetch -q origin "$SQUASH_TARGET" >/dev/null 2>&1 \
        || echo "  (branch-per-iteration: remote fetch unavailable — last-seen origin/$SQUASH_TARGET used for the guard)"
    else
      git fetch -q origin "$SQUASH_TARGET" >/dev/null 2>&1 \
        || echo "  (branch-per-iteration: remote fetch unavailable — last-seen origin/$SQUASH_TARGET used for the guard)"
    fi
    remote_target="$(git rev-parse "refs/remotes/origin/$SQUASH_TARGET")"
    if ! git merge-base --is-ancestor "$remote_target" "$head" 2>/dev/null; then
      write_branch_integrity_block \
        "squash refused: origin/$SQUASH_TARGET ($(git rev-parse --short "$remote_target")) is ahead of the work branch" \
        "git fetch, then git merge origin/$SQUASH_TARGET into '$work' by hand (resolve conflicts, re-verify), then re-run"
      return 1
    fi
  fi
  if [[ "$verified" != "1" ]]; then
    # v0.18.2 (row 1233 shape 2): the gate judges EXACTLY the tree it
    # squashes. Until v0.18.1 it verified the worktree and squashed HEAD's
    # tree — a repair left uncommitted went green here while HEAD (red) was
    # squashed onto the target. Decided: commit first, then squash — at a
    # completion step 3 lands the rest through its verify-gated commit
    # before this runs; a worktree that still differs from HEAD here is
    # never judged in HEAD's place (the squash waits, named, for the next
    # verify-gated commit to carry the difference).
    local _sq_dirty
    _sq_dirty="$(_boundary_dirty_paths | sed -E 's/^.. //' | head -n 8 | paste -sd' ' -)"
    if [[ -n "$_sq_dirty" ]]; then
      if boundary_final; then
        write_landing_block "$_sq_dirty"
        return 1
      fi
      echo "Branch-per-iteration: squash deferred — the worktree differs from HEAD ($_sq_dirty), and the catch-up squash judges exactly the tree it lands; the next verify-gated commit carries the difference (v0.18.2)." >&2
      return 1
    fi
    echo "Branch-per-iteration: squash caught up at a boundary — running the verify gate on the branch tree first."
    if ! run_verify_gate; then
      echo "Branch-per-iteration: squash deferred — the verify gate is red (artifacts/phase-verify-failed.json); the next approval carries this work." >&2
      return 1
    fi
  fi
  trailer="phasekit-squash: $work@$(git rev-parse --short "$head")"
  if [[ -n "$facts" ]]; then trailer="$trailer"$'\n'"$facts"; fi
  if ! s="$(git commit-tree "$tree" -p "$old_target" -m "$msg" -m "$trailer")"; then
    write_branch_integrity_block "git commit-tree failed while squashing" "inspect the repository (git fsck), then re-run"
    return 1
  fi
  if ! git update-ref -m "phasekit squash ($work)" "refs/heads/$SQUASH_TARGET" "$s" "$old_target"; then
    write_branch_integrity_block "'$SQUASH_TARGET' moved while the squash was in progress" "re-run — the guard re-evaluates at the next boundary"
    return 1
  fi
  if ! merge_back_from_target; then
    write_branch_integrity_block "merge-back failed after squashing to $(git rev-parse --short "$s")" "re-run — repair_half_squash completes the merge-back at the next boundary"
    return 1
  fi
  echo "Branch-per-iteration: squashed '$work' onto '$SQUASH_TARGET' as $(git rev-parse --short "$s") (was $(git rev-parse --short "$old_target"))."
  return 0
}

clear_consumed_batons_at_completion() {
  # v0.14.4. An iteration that CONCLUDED explains its own tree; a baton left
  # on disk from a previous killed session (promoted into session-handoff.json
  # at this session's start) would lie to the next session — and, untracked,
  # it read as "uncommitted work" to a supervisor's completion-gap detector
  # (2026-09-07 04:01: a finished iteration was re-dispatched into its own
  # kept branch). Only UNTRACKED batons are removed: a tracked one is history
  # and its removal would dirty the tree the completion just cleaned.
  # Only the promoted/real baton: the provisional session-interrupted.json is
  # the exit trap's business (clear_provisional_handoff_on_exit, verdict rule).
  local f="$ARTIFACTS_DIR/session-handoff.json"
  [[ -f "$f" ]] || return 0
  if git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then return 0; fi
  rm -f "$f" && echo "run-until-done: removed consumed baton session-handoff.json — the iteration concluded, nothing is in flight"
  return 0
}

rest_on_target() {
  # Iteration complete and fully squashed: leave HEAD on the target so the
  # repo rests where the next intake (and a standalone user) expects it. The
  # work branch is kept, not deleted (fork B). Trees are identical, so the
  # checkout changes no file. Best-effort.
  squash_mode || return 0
  local work
  work="$(current_branch)"
  [[ "$work" != "$SQUASH_TARGET" && "$work" != "HEAD" ]] || return 0
  [[ "$(git rev-parse "HEAD^{tree}")" == "$(git rev-parse "refs/heads/$SQUASH_TARGET^{tree}")" ]] || return 0
  if git checkout -q "$SQUASH_TARGET" 2>/dev/null; then
    echo "Branch-per-iteration: iteration complete — resting on '$SQUASH_TARGET' (work branch '$work' kept)."
    clear_consumed_batons_at_completion
  else
    echo "  WARN: branch-per-iteration: could not check out '$SQUASH_TARGET' at completion — HEAD stays on '$work'." >&2
  fi
  return 0
}

catchup_squash() {
  # $1 = verified (see squash_to_target). An approval-class record the work
  # branch carries and the target lacks is squashed now, under the message
  # its own commit carried: composed from the HEAD copy of the record (the
  # completion record when the target lacks it, else the approval) exactly
  # as the branch commit's was (v0.18.0), with the same Phasekit-* facts.
  local verified="${1:-0}" d src="phase-approval.json" msg fallback="chore(workflow): approved phase (squash caught up at a boundary)" rc=0
  if git rev-parse -q --verify "HEAD:artifacts/project-complete.json" >/dev/null 2>&1 \
     && [[ "$(git rev-parse -q --verify "HEAD:artifacts/project-complete.json" 2>/dev/null)" != "$(git rev-parse -q --verify "refs/heads/$SQUASH_TARGET:artifacts/project-complete.json" 2>/dev/null)" ]]; then
    src="project-complete.json"; fallback="chore(workflow): project completion record (squash caught up at a boundary)"
  fi
  d="$(mktemp -d)"
  git show "HEAD:artifacts/$src" > "$d/$src" 2>/dev/null || : > "$d/$src"
  msg="$(jq -r '.suggested_commit_message // empty' "$d/$src" 2>/dev/null)" || msg=""
  [[ -n "$msg" ]] || msg="$(jq -r '.summary // empty | tostring' "$d/$src" 2>/dev/null | head -n1 | cut -c1-120)" || msg=""
  [[ -n "$msg" ]] || msg="$fallback"
  FACTS_ITERATION="$(normalize_iteration_label "$(jq -c '.iteration // null' "$d/$src" 2>/dev/null)")"
  msg="$(compose_commit_message "$msg" "$d/$src" 2>/dev/null)" || true
  [[ -n "$msg" ]] || msg="$fallback"
  echo "Branch-per-iteration: an approval-class commit on the work branch has not reached '$SQUASH_TARGET' — squashing now."
  local facts
  facts="$(phasekit_trailers squash "$d/$src")"
  FACTS_ITERATION=""
  squash_to_target "$msg" "$verified" "$facts" || rc=$?
  rm -rf "$d"
  return "$rc"
}

ensure_squashed_or_block() {
  # $1 = verified (see squash_to_target); $2 = "completion" to rest on the
  # target afterwards. Catches up any approval-class record the target does
  # not carry yet (a squash refused last session, or an approval that landed
  # through a wrap-up/strand commit instead of the commit path).
  local verified="${1:-0}" completion="${2:-}" msg
  squash_mode || return 0
  if squash_pending; then
    catchup_squash "$verified" || return 1
  fi
  if [[ "$completion" == "completion" ]]; then
    rest_on_target
  fi
  return 0
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
    if grep -nE "$CREDENTIAL_TOKEN_RE|$PRIVATE_KEY_RE" \
        "$ROOT_DIR/$learnings_file" >&2; then
      echo "run-until-done: REFUSED — $learnings_file matches a credential pattern (lines above). Remove the secret and retry." >&2
      return 1
    fi
  done < <(echo "$staged" | grep -E '^docs/LEARNINGS[^/]*\.md$')
  return 0
}

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

stage_all() {
  # `git add -A`, failing LOUDLY (v0.18.2, review rounds 3-6): git_add_all's
  # lock retry, then — git stages NOTHING when one path fails (an empty or
  # nested repository, an unreadable file) — a path-by-path add that names
  # what git refused. Sets STAGE_FAILED (the paths), STAGE_ERR (git's error),
  # STAGE_LOCKED (the lock that stayed). The landing (stage_landing_tree) and
  # the wrap-up both stage through here.
  STAGE_FAILED=""; STAGE_ERR=""; STAGE_LOCKED=""
  # Contention is not an unstageable path (review round 4, MAJOR): git_add_all
  # retries it and releases a dead writer's lock.
  if STAGE_ERR="$(git_add_all)"; then STAGE_ERR=""; fi
  if [[ "$STAGE_ERR" == *index.lock* ]]; then
    STAGE_LOCKED="$(git rev-parse --git-path index.lock 2>/dev/null || echo .git/index.lock)"
  elif [[ -n "$STAGE_ERR" ]]; then
    local _sp
    while IFS= read -r -d '' _sp; do
      [[ -n "$_sp" ]] || continue
      git add -A -- "$_sp" >/dev/null 2>&1 || STAGE_FAILED="${STAGE_FAILED}${_sp} "
    done < <(git ls-files -z --others --exclude-standard 2>/dev/null; git ls-files -z --modified --deleted 2>/dev/null)
    STAGE_FAILED="${STAGE_FAILED% }"
    if [[ -z "$STAGE_FAILED" ]]; then STAGE_ERR=""; fi
  fi
}

stage_landing_tree() {
  # $1 = an artifact to force-add first (it may be partially gitignored), or
  # empty; $2 = "landing" (default) when the commit lands an approval-class
  # record, anything else for a checkpoint (its derived state is reset). The loop's one staging step, shared by every commit site and by
  # `phasekit verify` (v0.18.0), so the tree a model verified IS the tree the
  # commit gate judges.
  if [[ -n "${1:-}" ]]; then git add -f "$1" 2>/dev/null || true; fi
  # v0.18.2 (review round 3, MAJOR 3): `git add -A` stages NOTHING when one
  # path fails (an empty or nested repository, an unreadable file) — and the
  # commit then carried only what was force-added. Add path by path instead
  # and NAME what git refused (STAGE_FAILED / STAGE_ERR, for the caller).
  stage_all
  # Never commit per-iteration logs. run-phase.sh rewrites artifacts/logs/*
  # every iteration (the iteration counter resets on each run), so committing
  # them floods history with churn AND lets a no-progress iteration look like
  # a real change. Keep them on disk for live tailing/forensics; just don't
  # stage them. (Autonomous-loop-only — logs only exist during loop runs.)
  git reset -q -- "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  git reset -q -- "$ARTIFACTS_DIR/scratch" 2>/dev/null || true   # v0.18.2: scratch is never committed
  git reset -q -- "${WRAPUP_SENTINEL:-$ARTIFACTS_DIR/wrapup-requested}" 2>/dev/null || true
  # Never commit transient signals either (v0.6.5) — fresh adds only; a staged
  # deletion of a legacy tracked copy rides so the untracking lands.
  unstage_transient_adds
  # A carried completion record that claims nothing stays out (v0.18.0).
  keep_unclaimed_completion_out
  if [[ "${2:-landing}" != landing ]]; then reset_derived_state; fi
}

# --- the loop's own landing is in flight (v0.18.0, review rounds 5-7) -------
# The deadline watchdog's last-resort commit STANDS DOWN while the loop is
# landing (v0.13.1's rule for the wrap-up, extended to every landing): the
# landing is verify-gated and strictly better, and a kill that arrives
# mid-landing leaves the verdicts and the index on disk, which the next
# start's recovery lands with zero model turns. Rounds 4-7 of the v0.18.0
# review each found a new defect in a wip racing a landing (a verdict
# unstaged into a running gate, a phase squash without its approval, a green
# completion carried away); no race is left to special-case when there is no
# second committer. The mark is depth-counted: commit_from_artifact runs
# inside land_boundary.
LANDING_DEPTH=0
landing_enter() {
  LANDING_DEPTH=$((LANDING_DEPTH + 1))
  if [[ "$LANDING_DEPTH" -eq 1 ]]; then
    mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
    touch "$ARTIFACTS_DIR/logs/.landing-in-flight" 2>/dev/null || true
  fi
}
landing_leave() {
  [[ "$LANDING_DEPTH" -gt 0 ]] && LANDING_DEPTH=$((LANDING_DEPTH - 1))
  if [[ "$LANDING_DEPTH" -eq 0 ]]; then rm -f "$ARTIFACTS_DIR/logs/.landing-in-flight" 2>/dev/null || true; fi
  return 0
}

commit_from_artifact() {
  local rc=0
  landing_enter
  _commit_from_artifact "$@" || rc=$?
  landing_leave
  return "$rc"
}

_commit_from_artifact() {
  local file="$1"
  local fallback_msg="$2"
  local body_note="${3:-}"   # v0.18.2: one paragraph appended to the body (the leftover commit says what it is)

  # Deferred-scope gate, machine side (v0.14.5), and the loop's iteration
  # facts (v0.18.0): every approval-class artifact this commit may sweep
  # (`git add -A` takes both when both are on disk — review finding 2)
  # leaves with stable deferral keys, the supervisor's iteration, the base,
  # and (a completion) the open set from the deferral ledger. Only an
  # artifact that is NOT yet landed is rewritten: touching a landed one
  # would turn it into a "stranded" artifact the moment this commit is
  # refused (the generated matrix caught exactly that).
  prepare_verdicts_for_landing

  local kind="update" msg
  case "$(basename "$file")" in
    phase-approval.json) kind="phase" ;;
    project-complete.json) kind="completion" ;;
  esac
  msg="$(jq -r '.suggested_commit_message // empty' "$file")"
  if [[ -z "$msg" && "$kind" != update ]]; then
    # No message: the record's own summary says more than a generic chore.
    msg="$(jq -r '.summary // empty | tostring' "$file" 2>/dev/null | head -n1 | cut -c1-120)" || msg=""
  fi
  if [[ -z "$msg" ]]; then
    msg="$fallback_msg"
  fi
  if [[ "$kind" != update ]]; then
    msg="$(compose_commit_message "$msg" "$file")"
  fi
  if [[ -n "$body_note" ]]; then msg="$msg"$'\n\n'"$body_note"; fi

  if [[ "$kind" == update ]]; then
    stage_landing_tree "$file" checkpoint
  else
    stage_landing_tree "$file" landing
  fi
  if [[ -n "${STAGE_LOCKED:-}" ]]; then
    echo "run-until-done: REFUSED — git's index is locked ($STAGE_LOCKED); nothing was staged" >&2
    record_commit_refusal "git add (the git index is locked)" "git's index is locked: $STAGE_LOCKED exists and is not stale yet, so nothing could be staged. If no git process is running, the loop removes a lock older than 10 s itself at the next attempt; never run git yourself — end your turn and the loop retries the commit."
    unstage_verdicts_after_refusal
    return 1
  fi
  if [[ -n "${STAGE_FAILED:-}" ]]; then
    echo "run-until-done: REFUSED — git could not stage: $STAGE_FAILED" >&2
    record_commit_refusal "git add (paths git cannot stage)" "git could not stage: $STAGE_FAILED
$(printf '%s' "$STAGE_ERR" | head -n 5)
Make them stageable (an empty or nested repository: remove its .git or move it outside the tree; an unreadable file: fix its permissions) or remove them — the loop commits everything else it finds (the loop owns every commit; never run git add yourself)."
    unstage_verdicts_after_refusal
    return 1
  fi
  if [[ "$kind" != update ]]; then
    # Only a record this commit LANDS writes its evidence: a landed approval
    # re-written byte-identical is no landing, and a rewritten evidence file
    # would defeat the no-churn gate below (review round 1).
    if artifact_never_landed "$file"; then write_phase_evidence "$file"; fi
  fi

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
  # next iteration can keep working from the same state — every change but
  # the verdict artifacts (v0.18.0) — and return non-zero so the caller does
  # not advance the iteration counter.
  if ! run_verify_gate; then
    unstage_verdicts_after_refusal
    return 1
  fi

  # Scope containment (v0.4.8, ADOPTIONS item 4 warn-first). Hard-refuse only
  # the security pair where a bad commit IS the damage; the remaining
  # post-verify gates are shared with the wrap-up path (v0.6.6).
  if staged_touches_security_pair; then
    # Explain the refusal where the NEXT session will find it, so staged-but-
    # uncommitted work is never a mystery state.
    printf '{"scope_refused": true, "reason": "staged changes touch committed .claude/settings.json or .github/workflows/ — security-critical, never committed by the loop (docs/QUALITY_GATES.md scope containment)", "action": "put those files back as HEAD has them (git show HEAD:<path> > <path>; delete a file HEAD does not have) — never git restore/checkout/reset, the loop owns the index — then re-write your signal artifact; the loop retries the commit", "ts": "%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ARTIFACTS_DIR/scope-refusal.json"
    echo "run-until-done: REFUSED — staged changes touch committed .claude/settings.json or .github/workflows/ (security-critical). See artifacts/scope-refusal.json." >&2
    unstage_verdicts_after_refusal
    return 1
  fi
  rm -f "$ARTIFACTS_DIR/scope-refusal.json"

  local pvrc=0 pv_err
  pv_err="$(mktemp)"
  post_verify_commit_gates iteration 2>"$pv_err" || pvrc=$?
  cat "$pv_err" >&2 2>/dev/null || true
  if [[ "$pvrc" -ne 0 ]]; then
    if [[ "$pvrc" -eq 1 ]]; then
      # Only the REFUSED line(s): the scan's matched lines hold the secret,
      # and a supervisor may surface log_tail.
      record_commit_refusal "commit gate (after verify)" "$(grep '^run-until-done: REFUSED —' "$pv_err" | head -n 5)
The commit gates that run after the verify gate refused the staged work. Fix what the line above names (e.g. remove the credential-shaped line from the named docs/LEARNINGS*.md file), then end your turn — the loop retries the commit."
    fi
    rm -f "$pv_err"
    unstage_verdicts_after_refusal
    return "$pvrc"
  fi
  rm -f "$pv_err"

  local pre_commit_head cm_err
  pre_commit_head="$(git rev-parse -q --verify HEAD 2>/dev/null)" || pre_commit_head=""
  cm_err="$(mktemp)"
  if ! git commit -m "$msg" -m "$(phasekit_trailers "$kind" "$file")" 2>"$cm_err"; then
    cat "$cm_err" >&2 2>/dev/null || true
    # v0.18.0 (review round 5): no commit, no squash — a squash of whatever
    # HEAD is (the watchdog's wip of this very index, committed inside the
    # gate window) would land it under this phase's subject.
    echo "run-until-done: the commit did not happen (HEAD ${pre_commit_head:0:12} → $(git rev-parse --short HEAD 2>/dev/null)); nothing squashed — the next boundary lands what stands." >&2
    # v0.18.2: git itself refused (HEAD did not move — a project's git hook,
    # identity, signing): the repair turn is told why.
    if [[ "$(git rev-parse -q --verify HEAD 2>/dev/null)" == "$pre_commit_head" ]]; then
      record_commit_refusal "git commit (git refused the loop's commit)" "$(redact_credentials < "$cm_err" | tail -n 30)
git refused the loop's commit of the staged work (a project git hook, the commit identity, signing). Fix what git names — never commit yourself: the loop retries the commit."
    fi
    rm -f "$cm_err"
    unstage_verdicts_after_refusal
    return 1
  fi
  cat "$cm_err" >&2 2>/dev/null || true
  rm -f "$cm_err"
  clear_commit_refusal   # the commit happened: nothing it recorded stands
  # v0.18.2: the landing's plan check (planned vs changed paths) is the
  # record's `plan_paths`; the boundary record carries the last one.
  if [[ "$kind" != update ]] && jq -e '(.plan_paths // null) | type == "object"' "$file" >/dev/null 2>&1; then
    _boundary_write '.plan_paths = $p' --argjson p "$(jq -c '.plan_paths' "$file" 2>/dev/null || echo null)"
  fi
  # Branch-per-iteration (v0.14.0): an approval-class commit also lands on the
  # target as one squash commit. A refused squash leaves the branch commit in
  # place and returns 1 with phase-blocked.json written — the caller's
  # blocked path stops the loop (exit 2) and the next boundary retries.
  if squash_applies_to "$file"; then
    if ! squash_to_target "$msg" 1 "$(phasekit_trailers squash "$file")"; then
      auto_push_if_enabled   # the branch commit is real work — keep it durable
      return 1
    fi
  fi
  auto_push_if_enabled
}

# v0.12.2: a phase approval that never landed must commit under its OWN
# message before any completion sweep. Twice now (xmeo iteration 28 phase-74,
# iteration 9 phase-25) a whole phase's substantive work shipped inside the
# generic completion chore commit — approval and completion written in the
# same iteration, and the completion branch runs first, so the approval's
# suggested_commit_message sat unused while its work rode an unlabeled sweep.
# rc semantics (v0.12.3): the phase commit sweeps the whole tree (completion
# record included — the boundary is NAMED, which is the property this buys;
# the resting predicate reads the committed record, not the message), so the
# completion commit that follows typically finds nothing (rc 2 = clean
# finish). The helper RETURNS commit_from_artifact's rc and gates nothing
# itself — each call site decides: the in-loop gate short-circuits its
# completion attempt on rc 1 (the tree is verify-red; a second full-tier run
# on the same red tree would double the spend AND double-count the
# VERIFY_MAX_ATTEMPTS breaker at exactly the boundary where verify is most
# expensive), while the stranded-at-start site ignores the rc (`|| true`)
# because its failure path already falls into the loop.
commit_pending_approval_first() {
  artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" || return 0
  echo "Unlanded phase approval detected before completion — committing the phase under its own message first."
  print_json_summary "$ARTIFACTS_DIR/phase-approval.json"
  local acrc=0
  commit_from_artifact \
    "$ARTIFACTS_DIR/phase-approval.json" \
    "chore(workflow): approve completed phase" || acrc=$?
  return "$acrc"
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

completion_record_carried() {
  # Is the completion record on disk one the loop CARRIED over a refused
  # landing? The marker artifacts/logs/.carried-completion is written with
  # the record's own mtime when the landing is refused; one file, read the
  # same way by the loop, the next session's recovery and `phasekit verify`
  # (review round 2: an in-memory flag let the next session land a carried
  # record as a completion nobody re-claimed).
  [[ -f "$ARTIFACTS_DIR/project-complete.json" && -f "$ARTIFACTS_DIR/logs/.carried-completion" ]]
}

completion_record_claims() {
  # v0.18.0 (§3.2): does artifacts/project-complete.json CLAIM completion
  # now? A record on disk does, unless the loop carried it over a refused
  # landing and the session has not re-written it since (newer than the
  # carry marker; the loop's own rewrites keep the record's mtime) — an
  # unchanged record is never a verdict (the rule has_verdict_artifact and
  # the Stop hook already apply). Until v0.17.0 cleanup_artifacts deleted
  # such a record, so "on disk" meant "claimed"; this keeps that meaning
  # while the bytes stay for the repair.
  local pc="$ARTIFACTS_DIR/project-complete.json"
  [[ -f "$pc" ]] || return 1
  completion_record_carried || return 0
  [[ "$pc" -nt "$ARTIFACTS_DIR/logs/.carried-completion" ]]
}

drop_unclaimed_carried_record() {
  # The carry lasts for the rest of THIS session (every retry and repair
  # turn can still edit and re-claim it — review round 2 found a per-turn
  # drop losing it to a CLI or verdict retry). A session that ends without
  # re-claiming it declined to: the record is removed at the exit, as v0.17.0
  # removed it at once, so no later session and no supervisor rescue can
  # resurrect it as a completion (review rounds 1-2). A final approval's
  # boundary also removes it first (its record is the approval's own).
  completion_record_carried || return 0
  completion_record_claims && return 0
  retire_completion_record
  rm -f "$ARTIFACTS_DIR/logs/.carried-completion"
  echo "run-until-done: the carried completion record was not re-written this session — removed at exit (it claims nothing; v0.18.0)." >&2
  return 0
}

completion_record_rides() {
  # May the completion record on disk ride the commit being made? When it
  # claims completion, or (not carried) when the boundary being landed is
  # final by its approval's own final_phase flag.
  completion_record_claims && return 0
  completion_record_carried && return 1
  [[ "$(boundary_get '.final // false' 2>/dev/null)" == "true" ]]
}

keep_unclaimed_completion_out() {
  # After a `git add -A`: a carried record that claims nothing never rides a
  # checkpoint, a wrap-up or a phase commit — it stays on disk, unstaged.
  local pc="$ARTIFACTS_DIR/project-complete.json"
  [[ -f "$pc" ]] || return 0
  completion_record_rides && return 0
  git reset -q -- "$pc" 2>/dev/null || true
}

unstage_verdicts_after_refusal() {
  # v0.18.0 (§3.2, link [F]): a refused commit leaves the tree staged so the
  # next iteration keeps working from it — but NEVER a verdict artifact. A
  # staged record that the next cleanup deletes from disk is the `AD` state
  # (index has it, disk does not; xmeo run 1002), and the repair turn then
  # rebuilt the record from an archive. Unstaged, the record shows as `??`
  # (or ` M` where HEAD tracks one) and is carried into the repair turn.
  git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
  # The loop's derived state (the ledger, the evidence) is the landing's:
  # a refused landing leaves none behind — the next attempt recomputes it.
  reset_derived_state
  # Only a record the SESSION wrote is carried; one the loop synthesized from
  # a final approval (step 3) is derived — re-synthesized byte-identical by
  # the next landing — and keeps claiming, as before v0.18.0 (review round 3:
  # a carried synthesized record made the wrap-up gate a different tree than
  # the landing that went red, so the red memo never matched).
  if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] && artifact_never_landed "$ARTIFACTS_DIR/project-complete.json" \
     && ! jq -e '(.recorded_by // "") | tostring | startswith("phasekit run-until-done.sh — boundary-state step 3")' \
          "$ARTIFACTS_DIR/project-complete.json" >/dev/null 2>&1; then
    carry_completion_record
  fi
  return 0
}

drop_unlanded_synthesized_record() {
  # At the loop's exit: a completion record the LOOP synthesized (from a
  # final approval) that never landed is derived state — the next landing
  # re-synthesizes it byte-identical from the approval. Left on disk at an
  # exit it reads to a supervisor as the session's completion (review round
  # 4); the approval stays, and the next start records it again.
  local pc="$ARTIFACTS_DIR/project-complete.json"
  [[ -f "$pc" ]] && artifact_never_landed "$pc" || return 0
  jq -e '(.recorded_by // "") | tostring | startswith("phasekit run-until-done.sh — boundary-state step 3")' "$pc" >/dev/null 2>&1 || return 0
  retire_completion_record
  echo "run-until-done: the completion record the loop synthesized did not land — removed at exit (the approval re-records it at the next start; v0.18.0)." >&2
  return 0
}

retire_completion_record() {
  # v0.18.2 (review rounds 3-4): the ONE way the loop takes a completion
  # record off the tree when it claims nothing (a new pass, a carry nobody
  # re-claimed, a synthesized record that did not land). A record that is
  # THIS boundary's committed completion, still landing (completion_in_flight),
  # is never deleted: a deletion on disk is exactly what the next checkpoint
  # commits, un-recording it (on the target itself in plain mode) — its
  # committed bytes are restored instead. Any other record goes, as before
  # v0.18.2 ("deleted until real"): an untracked one unstaged and removed; an
  # EARLIER completion HEAD still carries (a project resumed for new work)
  # removed from disk, its deletion landing with the new work. A deletion
  # already on disk (a human's, an intake's) is left as it is.
  local pc="$ARTIFACTS_DIR/project-complete.json"
  [[ -f "$pc" ]] || return 0
  # Only THIS boundary's completion, still landing, is kept (review round 4,
  # BLOCKER: an EARLIER completion kept at a new pass re-recorded a resumed
  # project complete over its half-done work — the v0.14.5 BLOCKER 1 class).
  if completion_in_flight; then
    if artifact_never_landed "$pc"; then
      git checkout -q HEAD -- artifacts/project-complete.json 2>/dev/null \
        && echo "run-until-done: the completion record on disk differed from the committed one — restored to HEAD's bytes, never deleted (the completion is committed; v0.18.2)." >&2
    fi
    return 0
  fi
  if ! git cat-file -e "HEAD:artifacts/project-complete.json" 2>/dev/null; then
    git reset -q -- "$pc" 2>/dev/null || true
  fi
  rm -f "$pc"
  return 0
}

carry_completion_record() {
  # Mark the record on disk as carried: it claims nothing from here until
  # the session re-writes it. Best-effort; without the marker the record
  # simply keeps claiming, as before v0.18.0.
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  if ! completion_record_carried; then
    echo "run-until-done: the completion record's landing was refused — kept on disk, unstaged (never deleted); the repair turn edits it and re-writes it once the gate is green (v0.18.0)." >&2
  fi
  # v0.18.1: the marker also names the carried bytes (their blob), so a
  # commit that sweeps the carried record in unchanged is known to claim
  # nothing (the completion guard) — the mtime still carries the rule.
  git hash-object -- "$ARTIFACTS_DIR/project-complete.json" > "$ARTIFACTS_DIR/logs/.carried-completion" 2>/dev/null || true
  touch -r "$ARTIFACTS_DIR/project-complete.json" "$ARTIFACTS_DIR/logs/.carried-completion" 2>/dev/null || true
  return 0
}

committed_record_claims() {
  # $1 = a completion record blob at HEAD (v0.18.1). Does that COMMITTED
  # record claim completion? Not when it is the carried record swept into a
  # commit unchanged (a model's checkpoint during its repair turn): the
  # carry marker names those bytes — unless the session re-claimed them (the
  # v0.18.0 rule: re-written, newer than the marker) and the tree's record
  # is exactly what HEAD carries. A marker without a blob (written before
  # v0.18.1) falls back to the on-disk rule.
  local carried pc="$ARTIFACTS_DIR/project-complete.json"
  [[ -f "$ARTIFACTS_DIR/logs/.carried-completion" ]] || return 0
  carried="$(head -c 64 "$ARTIFACTS_DIR/logs/.carried-completion" 2>/dev/null | tr -d '[:space:]')" || carried=""
  if [[ "$carried" =~ ^[0-9a-f]{40,64}$ ]]; then
    [[ "$carried" != "$1" ]] && return 0
    completion_record_claims || return 1
    [[ "$(git hash-object -- "$pc" 2>/dev/null)" == "$1" ]]
    return
  fi
  completion_record_claims
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

write_provisional_handoff() {
  # Dead-man baton (v0.10.1). The wrap-up baton above is written by the
  # wrap-up — and a session killed mid-iteration (deadline class (a)) or one
  # that exits silently dies BEFORE wrap-up runs, which is exactly the case
  # where the next session most needs orientation: it inherits a dirty tree
  # with no explanation, and the record shows inherited trees "confused the
  # next session. Three times." So the loop writes a PROVISIONAL baton at
  # every iteration start and removes it only when the iteration concludes
  # with a verdict (the EXIT trap below). A kill cannot cooperate, and does
  # not need to: the baton it leaves behind is accurate by construction.
  #
  # Its OWN file, not session-handoff.json, and the separation is
  # load-bearing: iteration 1's provisional is written BEFORE the model runs,
  # and the model's orientation reads session-handoff.json — writing there
  # would clobber the inbound baton with a false "you were killed" note about
  # the session that is only just starting. Instead the next session's loop
  # PROMOTES a surviving session-interrupted.json into the baton slot at
  # startup (see the promotion block beside the iteration marker), where the
  # existing read-then-delete orientation consumes it unchanged. Same six
  # keys as the real baton — the manifest pins one schema for both.
  local iter="$1"
  local phase="unknown"
  if [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    phase="$(jq -r '.phase // "unknown"' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)" || phase="unknown"
    [[ -n "$phase" ]] || phase="unknown"
  fi
  jq -n \
    --arg phase "$phase" \
    --arg iter "$iter" \
    --arg mode "$ITERATION_MODE" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      stopped_at_phase: $phase,
      in_flight: ("iteration " + $iter + " (" + $mode + " mode) was IN FLIGHT when this session ended; whatever `git status` shows now is that iteration'"'"'s unverified work-in-progress"),
      verified: false,
      next_step: "audit the dirty tree as IN-PROGRESS IMPLEMENTATION from an interrupted session: re-derive what is verified, keep it, finish the rest or edit it back (the loop owns every commit: never git checkout/reset/stash) — do not read the tree as intentional resting state",
      note: "dead-man baton: written at iteration start, removed when the iteration concludes with a verdict — you are reading it because the previous session was killed or exited without concluding (stopped_at_phase = last APPROVED phase). Ephemeral: delete after orienting.",
      ts: $ts
    }' > "$ARTIFACTS_DIR/session-interrupted.json"
}

clear_provisional_handoff_on_exit() {
  # The dead-man baton's other half: on any NORMAL exit, remove the
  # provisional iff this iteration produced a verdict — a concluded iteration
  # explains its own tree, and a leftover "you were killed" note would lie.
  # Fail directions, each deliberate:
  #   * no verdict this iteration (the silent exit-1 class) -> LEFT IN PLACE,
  #     because the baton is then telling the truth;
  #   * an exit before the loop ever started (preflight refusals) has no
  #     iteration marker; nothing was in flight, so a surviving provisional
  #     can only be a previous session's truthful one -> leave it;
  #   * a hard kill never runs this trap at all, which is the whole point.
  local f="$ARTIFACTS_DIR/session-interrupted.json"
  [[ -f "$f" ]] || return 0
  [[ -n "${ITER_START_MARKER:-}" && -f "${ITER_START_MARKER:-}" ]] || return 0
  local a
  for a in "${VERDICT_ARTIFACTS[@]}"; do
    if [[ -f "$ARTIFACTS_DIR/$a" && "$ARTIFACTS_DIR/$a" -nt "$ITER_START_MARKER" ]]; then
      rm -f "$f"
      return 0
    fi
  done
  echo "run-until-done: dead-man handoff left in place (no verdict this iteration) — the next session orients from it" >&2
}

# --- Boundary state (v0.14.5) ------------------------------------------------
# Four incidents in three days (2026-09-07/08), none a regression, each a SEAM
# between two correct mechanisms: a consumed baton nobody deleted; a final
# phase approved and squashed whose completion record was never written; a
# release credited only when the deploy came second; a light breaker tripped
# by pins a new field legitimately reddens. Root cause, stated once: "done"
# was a DERIVED state, inferred by eight accreted mechanisms from different
# subsets of five files/rows, and every pair of them disagreed at some kill
# point. Foundry-meta kickoffs/KICKOFF-phasekit-v0145-boundary-state.md.
#
# This block makes the landing sequence ONE record and ONE path:
#
#   artifacts/boundary-state.json   (transient for git: never committed; hidden)
#   steps, per boundary, in order:
#     0 idle          nothing approved this boundary
#     1 approved      an approval-class artifact is on disk (final_phase carried)
#     2 committed     the phase approval commit is on the work branch
#     3 recorded      project-complete.json is committed (final boundaries only)
#     4 squashed      the target carries the approval-class blobs (squash mode)
#     5 merged-back   the target tip is an ancestor of the work branch
#     6 armed         ready-to-deploy.json observed (presence + mtime; the loop
#                     never writes it — the mtime rule is the supervisor's)
#     7 rested        tree clean; on a final boundary HEAD is on the target and
#                     the consumed batons are gone
#
# land_boundary is the ONLY code path that advances the record. It walks the
# steps from the one its caller can prove: each step has a PROOF (git/disk
# alone answers "is it done?") and an ACTION (the existing mechanism —
# commit_pending_approval_first, commit_from_artifact, squash_to_target,
# repair_half_squash, rest_on_target, clear_consumed_batons_at_completion —
# now a callee, not a decider). A proven step is recorded and skipped; an
# unproven one gets its action, then must prove. So "recovery" is not a
# separate mechanism: it is land_boundary called at loop start, and a session
# killed at ANY instant leaves a tree the proofs describe exactly. The eight
# mechanisms of §1 become callers; the record is what a supervisor READS.
#
# The verify memo: a green gate records `git write-tree` + tier + command; a
# later gate on the SAME tree (the squash caught up at a boundary, the
# completion commit after an approval commit) reuses the verdict instead of
# spending a whole session bound on a suite the tree already passed (sessions
# 669/670, 2026-09-08). Never for a new tree; never across a changed command;
# a fast-tier memo never satisfies a full-tier gate.
#
# Nothing loosens: every gate and doctrine — the security pair, the LEARNINGS
# scan, the scope warning, the SPEC attestation, the transient vocabulary, the
# atomicity marker, the verify-budget doctrine, the branch-integrity refusals,
# the deploy-artifact disarm on the kill path — runs exactly where it did.
# The record is observability of the sequence, never a gate on it: a record
# that cannot be written prints once and the sequence continues.

BOUNDARY_STATE_FILE="$ARTIFACTS_DIR/boundary-state.json"
BOUNDARY_APPROVAL_RIDES_COMPLETION=0   # set by step 2 when a stale approval is left to the completion sweep (walk-local: reset at land_boundary entry)
BOUNDARY_STEP2_ATTEMPTED=0             # set by step 2's action (walk-local): a FRESH in-loop approval always drives its commit once
BOUNDARY_STEP3_ATTEMPTED=0             # v0.18.2: set by step 3's action (walk-local): the whole-tree check ran once
BOUNDARY_STEP4_VERIFIED=0              # v0.18.2: set by plain step 4's action (walk-local): HEAD's tree passed the gate
BOUNDARY_STEP_NAMES=(idle approved committed recorded squashed merged-back armed rested)
BOUNDARY_STEP_RESTED=7

boundary_step() {
  # The record's current step; 0 when absent or unreadable.
  local s
  [[ -f "$BOUNDARY_STATE_FILE" ]] || { echo 0; return 0; }
  s="$(jq -r '.step // 0' "$BOUNDARY_STATE_FILE" 2>/dev/null)" || s=0
  [[ "$s" =~ ^[0-7]$ ]] || s=0
  echo "$s"
}

boundary_get() {
  # $1 = jq expression; prints the raw value, empty when the record is absent.
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 0
  jq -r "$1" "$BOUNDARY_STATE_FILE" 2>/dev/null || true
}

_boundary_write() {
  # $1 = jq filter applied to the current record ({} when absent); the rest
  # are jq arguments. $now is always bound. Read-modify-write under a lock
  # (the deadline watchdog subshell writes killed_after while the loop may be
  # advancing a step — review finding 8), through a per-writer tmp file
  # under artifacts/logs/ (never in git status, never swept by add -A —
  # finding 7), then an atomic mv. Best-effort: never gates the sequence.
  local filter="$1"; shift
  local tmp="$ARTIFACTS_DIR/logs/.boundary-state.$BASHPID.tmp" lock="$ARTIFACTS_DIR/logs/.boundary-state.lock"
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  _boundary_write_locked() {
    local cur='{}'
    if [[ -f "$BOUNDARY_STATE_FILE" ]] && jq -e . "$BOUNDARY_STATE_FILE" >/dev/null 2>&1; then
      cur="$(cat "$BOUNDARY_STATE_FILE")"
    fi
    jq --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$@" "$filter" <<<"$cur" > "$tmp" 2>/dev/null \
      && mv -f "$tmp" "$BOUNDARY_STATE_FILE" 2>/dev/null
  }
  local ok=0
  if command -v flock >/dev/null 2>&1; then
    { flock -w 5 9 2>/dev/null || true; _boundary_write_locked "$@" && ok=1; } 9>>"$lock" 2>/dev/null || true
  else
    _boundary_write_locked "$@" && ok=1
  fi
  if [[ "$ok" -eq 1 ]]; then return 0; fi
  rm -f "$tmp" 2>/dev/null || true
  echo "boundary-state: WARN — could not write artifacts/boundary-state.json; the sequence continues, the record is stale" >&2
  return 0
}

supervising_iteration_json() {
  # v0.14.8: the supervising iteration's label, as JSON, for the record's
  # `iteration`. phasekit does not define iterations — the iter/<N>-<slug>
  # work branch is the supervisor's naming (PHASEKIT_WORK_BRANCH), not a
  # fact of phasekit's — so the only honest source is what the supervisor
  # declared: the `iteration` key of artifacts/iteration-mode.json (the
  # orchestrator writes that file inside the iteration commit; the MODE still
  # arrives as PHASEKIT_ITERATION_MODE, this reads nothing else from it).
  # Carried verbatim when it is a number or a string (a label is one of
  # those; an object, array or boolean is not a label and reads as null —
  # a consumer that requires an int treats any other value as "no label",
  # never as a corrupt record), never derived from the branch name; `null`
  # when the file is absent, unparseable, or names no iteration (a
  # standalone run).
  local f="$ARTIFACTS_DIR/iteration-mode.json" v=null
  [[ -f "$f" ]] || { echo null; return 0; }
  v="$(jq -cs 'if (.[0] | type) == "object" and ((.[0].iteration | type) == "number" or (.[0].iteration | type) == "string") then .[0].iteration else null end' "$f" 2>/dev/null)" || v=null
  [[ -n "$v" ]] && jq . <<<"$v" >/dev/null 2>&1 || v=null
  echo "$v"
}

boundary_begin() {
  # $1 = pass number: this session's index inside the MAX_ITERATIONS loop
  # (1, 2, …; 0 when loop-start recovery opened the record before the first
  # pass). Recorded as `pass` since v0.14.8 — until v0.14.7 it was
  # written as `iteration`, which every consumer read as the SUPERVISING
  # iteration, and every live record said 1. A NEW boundary: step 0, nothing
  # approved yet. The verify memo is carried forward (it is keyed by tree, so
  # a stale entry is inert); everything else belongs to the boundary that
  # just ended.
  local pass="$1" mode=plain
  squash_mode && mode=squash
  # `previous` keeps the boundary that just ended (its final step, phase,
  # shas, pass and iteration — and its own `schema`: a schema-1 record a
  # v0.14.7 session left is archived as it was, so a reader branches on the
  # schema of the block it reads) so a reader arriving between sessions can
  # see what last landed even after a later pass began a new, idle record.
  _boundary_write '{
      schema: 2,
      pass: ($pass | tonumber),
      iteration: $iteration,
      branch: $branch, target: $target, mode: $mode,
      phase: null, final: false,
      step: 0, step_name: "idle", step_at: $now, began_at: $now,
      sha_at_step: {}, deploy: null, killed_after: null, killed_mode: null, killed_at: null,
      verify_memo: (.verify_memo // null), verify_red: (.verify_red // null),
      work_base: (.work_base // null), scaffold_reads: (.scaffold_reads // null), scaffold_reads_at: (.scaffold_reads_at // null),
      previous: (if (.step // 0) > 0 then (del(.verify_memo) | del(.previous)) else (.previous // null) end)
    } + (if (.unlanded // null) != null and $ucc != "" and ((.unlanded.completion_commit // "") == $ucc)
            and (if $iteration != null and (.iteration // null) != null then .iteration == $iteration
                 else (.branch // "") == $branch end)
         then {unlanded: .unlanded} else {} end)' --arg pass "$pass" --argjson iteration "$(supervising_iteration_json)" \
       --arg ucc "$(git cat-file -e HEAD:artifacts/project-complete.json 2>/dev/null && git log -1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)" \
       --arg branch "$(current_branch)" --arg target "$SQUASH_TARGET" --arg mode "$mode"
}

boundary_advance() {
  # $1 = step 1..7, $2 = the sha that proves it (optional). Called ONLY from
  # land_boundary. Monotonic: a record never moves backwards inside a
  # boundary; a lower step is a no-op (the property pin in
  # tests/test_boundary_state.py).
  local step="$1" sha="${2:-}"
  [[ "$step" -ge "$(boundary_step)" ]] || return 0
  _boundary_write '.step = ($step | tonumber) | .step_name = $name | .step_at = $now
      | (if $sha != "" then .sha_at_step[$step] = $sha else . end)' \
    --arg step "$step" --arg name "${BOUNDARY_STEP_NAMES[$step]}" --arg sha "$sha"
}

boundary_mark_killed() {
  # $1 = "kill" (the deadline watchdog, seconds before the bound) or "wrapup"
  # (the v0.14.2 fall-through). Records the step observed at the kill — the
  # one field written outside land_boundary, and not a step advance: the
  # next session's recovery names it in its first line.
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 0
  _boundary_write '.killed_after = ($step | tonumber) | .killed_mode = $mode | .killed_at = $now' \
    --arg step "$(boundary_step)" --arg mode "${1:-kill}"
}

boundary_phase_rested() {
  # $1 = phase. Does the record (current or previous) show this phase rested?
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 1
  jq -e --arg p "$1" '
      ((.phase // "") == $p and (.step // 0) == 7)
      or (((.previous // {}).phase // "") == $p and ((.previous // {}).step // 0) == 7)' \
    "$BOUNDARY_STATE_FILE" >/dev/null 2>&1
}

approval_final_unrecorded() {
  # The 2026-09-08 06:22 shape, and ONLY that shape: the approval on disk is
  # landed, names the final phase, no completion record exists on disk or in
  # HEAD, AND no commit since the approval landed has ever touched
  # project-complete.json. A project that COMPLETED and was then resumed
  # carries a deletion (an intake rename, the "deleted until real" checkpoint)
  # after its approval — that is a resumed project, not an unrecorded one
  # (review BLOCKER 1: the loop once re-recorded "complete" over a resumed
  # project's half-done work, exit 0, zero model turns).
  local ap="$ARTIFACTS_DIR/phase-approval.json" ap_commit
  [[ -f "$ap" ]] || return 1
  [[ "$(jq -r '.final_phase // false' "$ap" 2>/dev/null)" == "true" ]] || return 1
  artifact_never_landed "$ap" && return 1
  [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] && return 1
  git cat-file -e "HEAD:artifacts/project-complete.json" 2>/dev/null && return 1
  # The range starts at the OLDEST commit anywhere in the path's history
  # that carries the CURRENT approval blob, not at the last commit that
  # touched the path: a later re-touch with identical content — a mode
  # change, a delete-and-restore, an edit-and-revert (operator hand-commits
  # after the completion's deletion; round-2 F6, round-3 finding 1) — must
  # not hide that deletion. A re-land with DIFFERENT content later is the
  # one genuinely ambiguous shape (an operator re-asserted final_phase on a
  # resumed project) and is an accepted limitation, stated in
  # docs/QUALITY_GATES.md.
  local blob c
  blob="$(git rev-parse -q --verify "HEAD:artifacts/phase-approval.json" 2>/dev/null)" || return 1
  ap_commit=""
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    [[ "$(git rev-parse -q --verify "$c:artifacts/phase-approval.json" 2>/dev/null)" == "$blob" ]] || continue
    ap_commit="$c"
  done < <(git log --format=%H -- artifacts/phase-approval.json 2>/dev/null)
  [[ -n "$ap_commit" ]] || return 1
  [[ -z "$(git rev-list "$ap_commit..HEAD" -- artifacts/project-complete.json 2>/dev/null)" ]]
}

boundary_final() {
  # Is this boundary the project's last? Read, not inferred: the record's
  # `final` (from the approval's final_phase at step 1), or a completion
  # record on disk (a completion-only boundary is final by definition).
  [[ "$(boundary_get '.final // false')" == "true" ]] && return 0
  completion_record_claims
}

boundary_complete() {
  # v0.14.9: the iteration's TERMINAL state — a final boundary whose
  # completion has LANDED: the record says final and its step is at least 6
  # (3 recorded, 4 squashed, 5 merged-back proven from git; 6 is trivial).
  # Step 7 (rested) is hygiene, not completion: a verify gate that rewrites
  # tracked files after the completion commit staged them (xmeo's acceptance
  # re-measurements — iteration 50 run 714, 2026-09-12; iteration 56 run 756,
  # 2026-09-13) leaves step 7 unprovable, and the loop read "could not be
  # proven" as "failed verify" and re-entered: a next pass that found no
  # next phase and wrote phase-blocked.json, a pacing wrap-up that committed
  # the noise straight onto the target. Read, never inferred from files.
  # Three reads, and the third is git's, not the record's (review m1: a
  # stale record left when boundary_begin's write failed must not stand the
  # watchdog down on an unrelated pass): final; step >= 6; the completion
  # record still on disk and landed at HEAD.
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 1
  [[ "$(boundary_get '.final // false')" == "true" ]] || return 1
  [[ "$(boundary_step)" -ge 6 ]] || return 1
  [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] || return 1
  ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"
}

boundary_complete_here() {
  # v0.14.9: is the record's complete boundary THIS session's iteration? The
  # record is transient and survives on disk across sessions, so a session
  # dispatched into a finished iteration (a landing session) must exit at
  # once — while the next iteration, which begins by deleting the completion
  # record "until real" and committing that deletion, must not be mistaken
  # for it. Two reads: complete (which includes the completion record still
  # landed at HEAD); and the same iteration — by the supervisor's label when the record
  # (schema 2) and the marker both carry one, else by the work branch (a
  # schema-1 record; a standalone run).
  boundary_complete || return 1
  boundary_same_iteration
}

boundary_same_iteration() {
  # Is the record's boundary THIS iteration's? By the supervisor's label when
  # the record (schema 2) and the marker both carry one, else by the work
  # branch (a schema-1 record; a standalone run). One test, two readers:
  # boundary_complete_here and completion_in_flight (v0.18.2). $1 = the
  # block to read: "" (the current boundary) or ".previous".
  local b="${1:-}" schema rec cur
  schema="$(boundary_get "${b}.schema // 1")"; [[ "$schema" =~ ^[0-9]+$ ]] || schema=1
  if [[ "$schema" -ge 2 ]]; then
    rec="$(boundary_get "${b}.iteration | tojson")"
    cur="$(supervising_iteration_json)"
    if [[ -n "$rec" && "$rec" != "null" && "$cur" != "null" ]]; then
      [[ "$rec" == "$cur" ]]; return
    fi
  fi
  [[ -n "$(boundary_get "${b}.branch // empty")" && "$(boundary_get "${b}.branch // empty")" == "$(current_branch)" ]]
}

completion_in_flight() {
  # v0.18.2 (review round 4, BLOCKER): is the completion record HEAD tracks
  # THIS boundary's completion, still landing? Either `unlanded` names the
  # commit that landed it, or the record says a final boundary of this very
  # iteration stopped before it completed (step 1..5). A record from an
  # EARLIER completion (a project resumed for new work without an intake
  # deleting it, a wiped boundary-state.json) is not — it goes at the next
  # pass, as it always did.
  git cat-file -e "HEAD:artifacts/project-complete.json" 2>/dev/null || return 1
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 1
  local rc b uc st base
  rc="$(git log -1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)" || rc=""
  [[ -n "$rc" ]] || return 1
  # The RECORD must be this iteration's (review round 7, BLOCKER — the root
  # the earlier rounds circled): the commit that landed it comes after this
  # iteration's base (the supervisor's intake, else the work base the loop
  # recorded). An earlier iteration's completion is an ancestor of the new
  # intake, so it is never "in flight" here, whatever a boundary block says.
  # No base at all (a standalone run on one branch) has no iteration
  # boundary to cross (declined with record).
  base="$(iteration_base_sha "$(supervising_iteration_label)")"
  if [[ -n "$base" ]]; then
    if [[ "$rc" == "$base" ]]; then
      # the commit that first carried the iteration's marker may itself land
      # the record (a marker nobody committed before it — review round 8); a
      # supervisor's intake never does (it only ever deletes the record)
      [[ "$(git log -1 --format='%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)' "$base" 2>/dev/null)" != intake ]] || return 1
    else
      git merge-base --is-ancestor "$base" "$rc" 2>/dev/null || return 1
    fi
  fi
  # The current boundary, else the one boundary_begin archived when a new
  # pass began — a session that ended between that begin and its walk (a CLI
  # failure, a kill) leaves the stopped completion only there (review round
  # 6, MAJOR). Always THIS iteration's (round 5, BLOCKER: an earlier
  # iteration's `unlanded` once kept its record alive across the next
  # iteration's intake).
  for b in "" ".previous"; do
    boundary_same_iteration "$b" || continue
    uc="$(boundary_get "${b}.unlanded.completion_commit // empty")"
    if [[ -n "$uc" && "$uc" == "$rc" ]]; then return 0; fi
    [[ "$(boundary_get "${b}.final // false")" == "true" ]] || continue
    st="$(boundary_get "${b}.step // 0")"; [[ "$st" =~ ^[0-7]$ ]] || st=0
    if [[ "$st" -ge 1 && "$st" -lt 6 ]]; then return 0; fi
  done
  return 1
}

finish_complete() {
  rm -f "$ARTIFACTS_DIR/logs/.carried-completion" 2>/dev/null || true
  # v0.14.9: the ONE exit for a complete iteration — every site that lands a
  # final boundary ends here, and so does a loop top that finds the
  # iteration already complete. Rest hygiene the walk could not prove is
  # NAMED (the dirty paths, HEAD off the target) and left exactly as it is:
  # neither a next pass nor a wrap-up commit is the iteration's work.
  # v0.18.1: a write made AFTER the completion commit never reaches here —
  # the walk restored it (completion_residue_settle, steps 3 and 7); what
  # can remain is dirt that predates the commit and that the commit did not
  # carry (a model's own partial commit), or a path git could not
  # restore. Nothing else runs.
  local why="${1:-}"
  if [[ -n "$why" ]]; then echo "$why"; fi
  # A zero-turn session re-entered the work branch only to look
  # (ensure_work_branch): put HEAD back on the target — step 7's own action,
  # a checkout of identical trees, no commit — so the project is left
  # exactly as it was found. Best-effort, like every rest.
  if squash_mode && [[ "$(current_branch)" != "$SQUASH_TARGET" ]]; then rest_on_target || true; fi
  if ! boundary_prove "$BOUNDARY_STEP_RESTED"; then
    echo "boundary-state: the completion landed (record final, step $(boundary_step)) but the tree did not rest (step 7 unproven: HEAD on '$(current_branch)'; changes the completion commit did not carry — older work, a deletion, or a later turn's own; a write made after it in the turn that committed it is restored since v0.18.1, the verify gate's footprint at the gate since v0.14.10) — left as is, nothing else runs (v0.14.9):" >&2
    git status --porcelain 2>/dev/null | sed 's/^/  /' >&2 || true
  fi
  echo "Run finished successfully."
  exit 0
}

# --- writes after the completion commit (v0.18.1, queue row 831) ------------
# foundry-orchestrator iteration 142 (run 829, 2026-09-16): the model
# committed the completion record itself, then its turn — and the light
# review's turn after it — kept editing tracked files. The loop landed and
# squashed the committed record, the tree could not rest ("changes after the
# completion commit"), the supervisor spent two landing dispatches on a tree
# only a human could settle, and a human did. The completion is terminal
# (v0.14.9), so nothing written after it is the iteration's work. Two halves,
# decided together (Aaron, 2026-09-30, fork (a)+(b)):
#
#   (a) the turn guard (run_once): while a model turn runs, the loop watches
#       HEAD; the moment a commit made during the turn lands a completion
#       record that claims AND carries the turn's work (no older work left
#       uncommitted — a record-only commit is not the end: the model may
#       commit the rest next), it ends the turn — v0.18.0's take-control
#       (SIGTERM to the pid run-phase.sh names) — and snapshots the tree at
#       that commit, so the model gets no further tool call in it. In
#       branch-per-iteration mode the light review, which precedes the final
#       commit, does not run over a completion the build turn committed with
#       the whole tree.
#   (b) the settle (this block): at the start of the landing walk, at step 3
#       (before the squash's gate judges the tree) and before the rest is
#       proven, every path that changed since the completion snapshot
#       (tracked: back to the committed bytes; untracked: removed) is
#       restored, NAMED (the record's `post_completion`, stderr) and KEPT
#       under artifacts/logs/post-completion/<stamp>/ (tracked.patch +
#       untracked/) — a path whose bytes cannot be kept is not restored. It
#       corrects, it never refuses: a landing is never blocked by it
#       (Aaron's no-new-stalls rule).
#
# What counts as "after" — by OBSERVATION, never by a file's time (review
# round 4: a timestamp cannot tell older work re-touched after the commit
# from work written after it). The loop snapshots the tree only at moments
# it knows the state: the guard, when it sees the completion committed AND
# the tree clean (the commit carried the turn's work, nothing written
# since) — then it ends the turn; the walk, right after its own commit
# (which staged the whole tree). Whatever appears after such a snapshot — a
# write racing the signal, a process the turn left running, a write after a
# kill between the commit and the rest — is after, and is restored. A turn
# the loop dispatches (a repair pass, the light review, a verdict request)
# drops the snapshot first: its writes are never residue. No snapshot, no
# judgement: a completion never seen clean (a record-only commit over older
# work, a write in the same shell command as the commit) keeps the v0.18.0
# rest — its dirt is named by finish_complete and left as is. Untracked
# files are restored too (removed, kept in the residue): an untracked `??`
# blocks the rest exactly like a tracked edit. The loop's own paths (logs,
# batons, transient signals) are never residue.

completion_blob_at_head() {
  git rev-parse -q --verify "HEAD:artifacts/project-complete.json" 2>/dev/null || true
}

completion_landed_at_head() {
  # The completion record on disk is the one HEAD carries (committed, and
  # unchanged since).
  [[ -f "$ARTIFACTS_DIR/project-complete.json" && -n "$(completion_blob_at_head)" ]] \
    && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"
}

_completion_commit_time() {
  # The committer time (epoch s) of the commit that put $1 (a blob) at
  # artifacts/project-complete.json in HEAD's history; empty when unknown.
  local c
  c="$(git log -1 --format='%H %ct' HEAD -- artifacts/project-complete.json 2>/dev/null)" || c=""
  echo "${c#* }"
}

_status_records_to_b64_file() {
  # $1 = a NUL-separated "XY<TAB>path" file (gate_status_snapshot), $2 = out:
  # one base64 record per line (raw bytes survive jq; a huge set never
  # passes through argv — the gate-pending record's ARG_MAX limit).
  local rec
  : > "$2"
  while IFS= read -r -d '' rec; do printf '%s' "$rec" | base64 -w0 >> "$2"; echo >> "$2"; done < "$1"
}

_boundary_b64_records_file() {
  # $1 = jq path of an array of base64 records in the record, $2 = out: the
  # NUL-separated "XY<TAB>path" file they encode.
  local line
  : > "$2"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    { base64 -d <<<"$line" 2>/dev/null || true; printf '\0'; } >> "$2"
  done < <(boundary_get "$1 | if type == \"array\" then .[] else empty end | strings")
}

completion_snapshot_take() {
  # $1 = source (turn-guard | landing), $2 = the completion blob at HEAD.
  # Records `completion_snapshot` {blob, commit, source, at, before}:
  # `before` is the tree's dirt at the moment the loop OBSERVED the
  # completion committed — the guard only when the tree was clean then, the
  # walk right after its own commit (which staged everything). Nothing is
  # judged by a timestamp (review round 4: a file's time cannot tell older
  # work re-touched after the commit from work written after it).
  local src="$1" blob="$2" commit snap b64
  [[ -n "$blob" ]] || return 0
  commit="$(git log -1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)" || commit=""
  snap="$(mktemp)" || return 0
  b64="$(mktemp)" || { rm -f "$snap"; return 0; }
  if ! GIT_OPTIONAL_LOCKS=0 gate_status_snapshot "$snap"; then
    rm -f "$snap" "$b64"
    echo "boundary-state: git status unavailable — no snapshot at the completion commit (a later write is left for the rest to name)" >&2
    return 0
  fi
  _status_records_to_b64_file "$snap" "$b64"
  _boundary_write '.completion_snapshot = {blob: $blob, commit: (if $commit == "" then null else $commit end),
        source: $src, at: $now, before: ($b | split("\n") | map(select(length > 0)))}' \
    --arg blob "$blob" --arg commit "$commit" --arg src "$src" --rawfile b "$b64"
  rm -f "$snap" "$b64"
  return 0
}

completion_snapshot_ensure() {
  # The walk, right after its OWN commit carried a (new) completion record:
  # keep the guard's snapshot of this very record, else take the walk's own —
  # the commit staged the whole tree, so what is dirty now predates nothing.
  # Never anything else (review round 2): a record the walk finds already
  # committed without a snapshot is not judged — its dirt is named at the
  # rest, as before.
  local blob
  completion_landed_at_head || return 0
  blob="$(completion_blob_at_head)"
  [[ "$(boundary_get '.completion_snapshot.blob // empty')" == "$blob" ]] && return 0
  completion_snapshot_take landing "$blob"
}

completion_snapshot_drop() {
  # A snapshot covers the window from its completion commit until the loop
  # next hands the tree to a model: whatever a dispatched turn writes (a
  # repair pass, the light review, a verdict request) is that turn's work,
  # never residue (review round 2). run_once drops it before every turn.
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 0
  [[ -n "$(boundary_get '.completion_snapshot // empty | tostring')" ]] || return 0
  _boundary_write 'del(.completion_snapshot)'
}

completion_residue_settle() {
  # (b): at a final boundary whose completion is committed, restore every
  # path that changed since the completion snapshot, keep the bytes, name
  # them. Best-effort per path, never refuses, never blocks. A path whose
  # bytes could not be kept is NOT restored (named, left as is): a restore
  # never discards what the residue did not keep.
  boundary_final || return 0
  local blob
  blob="$(completion_blob_at_head)"
  [[ -n "$blob" && "$(boundary_get '.completion_snapshot.blob // empty')" == "$blob" ]] || return 0
  local before after paths keep rec xy path dir stamp json ok left=()
  before="$(mktemp)"; after="$(mktemp)"; paths="$(mktemp)"; keep="$(mktemp)"
  _boundary_b64_records_file '.completion_snapshot.before' "$before"
  if ! gate_status_snapshot "$after"; then
    rm -f "$before" "$after" "$paths" "$keep"
    return 0
  fi
  gate_footprint_diff "$before" "$after" "$paths"
  if [[ ! -s "$paths" ]]; then rm -f "$before" "$after" "$paths" "$keep"; return 0; fi
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
  dir="$ARTIFACTS_DIR/logs/post-completion/$stamp"
  : > "$keep"
  if mkdir -p "$dir/untracked" 2>/dev/null; then
    # Keep every byte first, path by path; only a kept path is restored.
    while IFS= read -r -d '' rec; do
      xy="${rec%%$'\t'*}"; path="${rec#*$'\t'}"
      ok=0
      if [[ "$xy" == "??" ]]; then
        ( cd "$ROOT_DIR" && cp -a --parents -- "${path%/}" "$dir/untracked/" ) 2>/dev/null && ok=1
      else
        # A deletion, a modification, a staged add: the diff against HEAD
        # carries it (binary-safe); appended per path.
        _gate_git diff --binary HEAD -- "$path" >> "$dir/tracked.patch" 2>/dev/null && ok=1
      fi
      if [[ "$ok" -eq 1 ]]; then printf '%s\0' "$rec" >> "$keep"; else left+=("$path"); fi
    done < "$paths"
  else
    while IFS= read -r -d '' rec; do left+=("${rec#*$'\t'}"); done < "$paths"
  fi
  if [[ -s "$keep" ]]; then
    gate_footprint_restore "$keep"
    # Name only what the restore achieved (review round 5): a submodule, or a
    # checkout git refused, is still dirty exactly as it was — left, named.
    local -A was=() now=()
    local done_paths=() r2 k
    while IFS= read -r -d '' rec; do k="${rec#*$'\t'}"; was["$k"]="${rec%%$'\t'*}"; done < "$before"
    r2="$(mktemp)" || r2=""
    if [[ -n "$r2" ]] && gate_status_snapshot "$r2"; then
      while IFS= read -r -d '' rec; do k="${rec#*$'\t'}"; now["$k"]="${rec%%$'\t'*}"; done < "$r2"
    fi
    while IFS= read -r -d '' rec; do
      k="${rec#*$'\t'}"
      if [[ -n "${now[$k]:-}" && "${now[$k]}" != "${was[$k]:-}" ]]; then left+=("$k")
      elif [[ "${rec%%$'\t'*}" == "??" && ( -e "$ROOT_DIR/${k%/}" || -L "$ROOT_DIR/${k%/}" ) ]]; then left+=("$k")   # ignored again, kept on disk
      else done_paths+=("$k"); fi
    done < "$keep"
    [[ -n "$r2" ]] && rm -f "$r2"
    if [[ ${#done_paths[@]} -gt 0 ]]; then
      json="$(jq -cn '$ARGS.positional' --args -- "${done_paths[@]}")" || json='["(unavailable)"]'
      _boundary_write '.post_completion = {restored: (((.post_completion // {}).restored // []) + $p | unique),
            residue: $dir, commit: (.completion_snapshot.commit // null), at: $now}' \
        --argjson p "$json" --arg dir "${dir#"$ROOT_DIR"/}"
      echo "boundary-state: ${#done_paths[@]} path(s) were written AFTER the completion commit $(boundary_get '.completion_snapshot.commit // "?"' | cut -c1-12) — the iteration is terminal, so they are restored to the committed state and named, never landed (v0.18.1); the bytes are kept under ${dir#"$ROOT_DIR"/}/: $(jq -r 'join(", ")' <<<"$json" 2>/dev/null)" >&2
    fi
  fi
  if [[ ${#left[@]} -gt 0 ]]; then
    echo "boundary-state: WARN — ${#left[@]} path(s) written after the completion commit are NOT restored (their bytes could not be kept under artifacts/logs/post-completion/ — a restore never discards what the residue did not keep — or git could not restore them: a submodule, a refused checkout) — left as is: ${left[*]}" >&2
  fi
  rm -f "$before" "$after" "$paths" "$keep"
  return 0
}

_boundary_kill_probe() {
  # Fault injection for the generated kill-point test ONLY
  # (tests/test_boundary_state.py): PHASEKIT_BOUNDARY_KILL_PROBE="<step>:<pre|post>"
  # SIGKILLs this process right before (pre) or right after (post) the record
  # advances to <step> — a real kill, of the shipped code, at every seam. Never
  # set in production; inert when unset.
  [[ -n "${PHASEKIT_BOUNDARY_KILL_PROBE:-}" ]] || return 0
  [[ "${PHASEKIT_BOUNDARY_KILL_PROBE}" == "$1:$2" ]] || return 0
  echo "boundary-state: KILL PROBE $1:$2 — SIGKILL now (test fault injection)" >&2
  kill -KILL $$
  sleep 5
}

# --- verify memo -------------------------------------------------------------

verify_memo_tier() {
  # The tier the project's gate is about to run: the doctrine
  # (docs/QUALITY_GATES.md "Verify budget") makes a gate run FULL when
  # project-complete.json exists, fast otherwise. The loop cannot see inside
  # the project's script; presence of the record is the contract.
  if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]]; then echo full; else echo fast; fi
}

verify_memo_exact_tree() {
  # Only a tree the gate ran on EXACTLY may be memoised: no unstaged tracked
  # changes and no untracked files outside artifacts/. (Until v0.18.2 the
  # catch-up squash verified the working tree while landing HEAD's; it now
  # gates only a tree that is HEAD's — and a memo still never makes an
  # inexact tree durable.)
  git diff --quiet 2>/dev/null || return 1
  [[ -z "$(git ls-files --others --exclude-standard 2>/dev/null | grep -v '^artifacts/')" ]]
}

verify_memo_record() {
  # $1 tree $2 tier $3 label $4 command — after a GREEN gate on an exact tree.
  _boundary_write '.verify_memo = {tree_sha: $tree, tier: $tier, label: $lbl, command: $cmd, passed_at: $now} | .verify_red = null' \
    --arg tree "$1" --arg tier "$2" --arg lbl "$3" --arg cmd "$4"
}

_verify_memo_fresh() {
  # $1 = the memo's passed_at/failed_at. A memo older than
  # PHASEKIT_VERIFY_MEMO_TTL_SECONDS (default one day) is not honoured: the
  # gate's inputs a tree cannot see (a container image, ignored files, a
  # dependency resolved at build time) drift on that scale (review finding 10).
  local ts="$1" ttl="${PHASEKIT_VERIFY_MEMO_TTL_SECONDS:-86400}" then now
  [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=86400
  [[ "$ttl" -gt 0 ]] || return 0
  then="$(date -u -d "$ts" +%s 2>/dev/null)" || return 1
  now="$(date +%s)"
  [[ $((now - then)) -le "$ttl" ]]
}

verify_memo_hit() {
  # $1 tree $2 tier $3 label $4 command → 0 iff the recorded green verdict
  # covers this run: same tree, same command, a tier at least as strong, and
  # recent enough.
  local m
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 1
  m="$(boundary_get '.verify_memo.tree_sha // empty')"; [[ -n "$m" && "$m" == "$1" ]] || return 1
  m="$(boundary_get '.verify_memo.label // empty')";    [[ "$m" == "$3" ]] || return 1
  m="$(boundary_get '.verify_memo.command // empty')";  [[ "$m" == "$4" ]] || return 1
  _verify_memo_fresh "$(boundary_get '.verify_memo.passed_at // empty')" || return 1
  m="$(boundary_get '.verify_memo.tier // empty')"
  [[ "$m" == "full" || "$m" == "$2" ]]
}

verify_memo_record_red() {
  # $1 tree $2 label $3 command $4 exit code $5 log file — after a RED gate on
  # an exact tree. The next gate on the same tree (a loop-start recovery of
  # the approval a red wrap-up stranded) is answered from here at zero cost
  # and without spending a breaker attempt (review finding 6). $6 (v0.14.10)
  # = the gate footprint (JSON array of paths) or empty — a red by footprint
  # is memoised WITH its paths, so its replay is the same red, never a green
  # (the command's exit code may be 0) and never a plain red without them.
  local tail_output footprint="${6:-}"
  [[ -n "$footprint" && "$footprint" != "null" ]] || footprint="null"
  tail_output="$(tail -n 50 "$5" 2>/dev/null | tail -c 4000)" || tail_output=""
  _boundary_write '.verify_red = {tree_sha: $tree, label: $lbl, command: $cmd, exit_code: ($code | tonumber), log_tail: $log, gate_footprint: $footprint, failed_at: $now}' \
    --arg tree "$1" --arg lbl "$2" --arg cmd "$3" --arg code "$4" --arg log "$tail_output" --argjson footprint "$footprint"
}

verify_memo_hit_red() {
  # $1 tree $2 label $3 command → 0 iff the recorded RED verdict covers this run.
  local m
  [[ -f "$BOUNDARY_STATE_FILE" ]] || return 1
  m="$(boundary_get '.verify_red.tree_sha // empty')"; [[ -n "$m" && "$m" == "$1" ]] || return 1
  m="$(boundary_get '.verify_red.label // empty')";    [[ "$m" == "$2" ]] || return 1
  m="$(boundary_get '.verify_red.command // empty')";  [[ "$m" == "$3" ]] || return 1
  _verify_memo_fresh "$(boundary_get '.verify_red.failed_at // empty')"
}

# --- deferral keys (the deferred-scope gate, machine side) -------------------

_deferral_key_jq() {
  # The ONE key derivation (v0.14.5), as jq definitions: `keyed` maps one
  # deferral entry to itself with a stable `key` — explicit wins; else the
  # cited AC number (AC#n); else a slug of the item's first six words; NEVER
  # a hash. Shared by the artifact normalizer and (v0.18.0) the deferral
  # ledger's seed, so the two can never key one entry two ways.
  cat <<'JQ_DEFS'
    def trim: gsub("^\\s+|\\s+$"; "");
    def slug: ascii_downcase | gsub("[^a-z0-9]+"; "-") | gsub("^-+|-+$"; "") | .[0:48] | gsub("-+$"; "");
    def first_words: gsub("\\s+"; " ") | trim | split(" ") | .[0:6] | join(" ");
    def keyed:
      if type != "object" then .
      elif ((.key // "") | tostring | trim | length) > 0 then .key = ((.key | tostring) | trim)
      elif ((.item // "") | tostring | test("AC#[0-9]+"; "i")) then
        .key = ((.item | tostring | match("AC#[0-9]+"; "i").string) | ascii_upcase) | .key_derived = "ac"
      elif ((.item // "") | tostring | first_words | slug | length) > 0 then
        .key = (.item | tostring | first_words | slug) | .key_derived = "slug"
      else . end;
JQ_DEFS
}

_completion_key_order_jq() {
  # v0.18.5 (rider 3): the ONE key order of artifacts/project-complete.json —
  # `iteration` first, then the other scalar keys, then the arrays and
  # objects; keys and values unchanged, and nothing moves inside a value. The
  # record carries the full open deferral set (v0.18.0); on xmeo it reached
  # 382 KB with `iteration` appended after a ~218 KB `deferrals`, and the
  # orchestrator's landing reader, then capped at 256 KB, never found it
  # (2026-10-01: xmeo stalled ~9 h). A prefix reader now finds every scalar
  # at once. Applied by every loop writer of the record: the shared rewrite
  # below and the step-3 synthesizer.
  cat <<'JQ_DEFS'
    def completion_key_order:
      if type != "object" then .
      else to_entries as $e
        | ([$e[] | select(.key == "iteration")]
           + [$e[] | select(.key != "iteration" and ((.value | type) as $t | $t != "array" and $t != "object"))]
           + [$e[] | select(.key != "iteration" and ((.value | type) as $t | $t == "array" or $t == "object"))])
        | reduce .[] as $x ({}; .[$x.key] = $x.value)
      end;
JQ_DEFS
}

_rewrite_json_keeping_mtime() {
  # $1 = file, $2 = new content. Replaces the file atomically (tmp under
  # artifacts/logs/, never swept by `git add -A`) and KEEPS ITS MTIME: a
  # rewrite by the loop is not the session writing a verdict, so it must
  # never make a stale record read as fresh (artifact_written_this_iteration,
  # the Stop hook) — nor a fresh one as stale. No-op when unchanged.
  # v0.18.5: a completion record leaves here in its one key order.
  local file="$1" content="$2" tmp
  if [[ "$(basename "$file")" == project-complete.json ]]; then
    content="$(jq "$(_completion_key_order_jq) completion_key_order" <<<"$content" 2>/dev/null)" || content="$2"
    [[ -n "$content" ]] || content="$2"
  fi
  cmp -s <(printf '%s\n' "$content") "$file" && return 1
  tmp="$ARTIFACTS_DIR/logs/.rewrite.$BASHPID.tmp"
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  if printf '%s\n' "$content" > "$tmp" 2>/dev/null && touch -r "$file" "$tmp" 2>/dev/null \
     && mv -f "$tmp" "$file" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
  return 1
}

normalize_deferral_keys() {
  # $1 = an approval-class artifact about to be committed. Every `deferrals`
  # entry leaves here with a stable `key`: explicit wins; else the cited AC
  # number (AC#n); else a slug of the item's first six words. NEVER a hash —
  # iteration 115 (2026-09-08) minted item-<hash> keys downstream, which
  # defeat the severity floor's dedupe and the drain ritual. A `deferrals`
  # that is null counts as absent; one that is not an array of objects, or
  # an entry with neither key nor item text, is left as it is with a WARN —
  # the supervisor's reader drops what it cannot name and logs it. The
  # kickoff asked for a refusal; the review showed a refusal here is BLIND
  # (nothing on disk tells the model why its commit keeps failing — 50 turns
  # in standard mode), and a gate that strands a session over a malformed
  # note violates the gate-recovery principle. Always returns 0.
  local file="$1" out
  [[ -f "$file" ]] || return 0
  jq -e '(.deferrals // null) != null' "$file" >/dev/null 2>&1 || return 0
  if ! jq -e '.deferrals | type == "array" and all(.[]; type == "object")' "$file" >/dev/null 2>&1; then
    echo "run-until-done: WARN — $(basename "$file") carries a 'deferrals' field that is not an array of {item, reason, suggested_task, key} objects; left as written — the supervisor's reader will drop what it cannot name (deferred-scope gate)." >&2
    return 0
  fi
  out="$(jq "$(_deferral_key_jq) .deferrals |= map(keyed)" "$file" 2>/dev/null)" || {
    echo "run-until-done: WARN — $(basename "$file") could not be read as JSON by the deferred-scope gate; left as written." >&2
    return 0
  }
  local missing
  missing="$(jq -r '[.deferrals | to_entries[] | select(((.value.key // "") | tostring | length) == 0) | .key] | join(",")' <<<"$out" 2>/dev/null)" || missing=""
  if [[ -n "$missing" ]]; then
    echo "run-until-done: WARN — $(basename "$file") deferral entry index $missing has no key the loop can derive (no 'key', no AC number, no ASCII words in 'item'); it stays in this record as written but cannot enter the deferral ledger — give it an explicit 'key' (deferred-scope gate)." >&2
  fi
  # v0.14.12: `severity` is a contract word — one of exactly BLOCKER | MAJOR |
  # MINOR (case-insensitive, surrounding whitespace ignored — the consumer
  # reads it the same way; contracts/interface.json approval-deferrals
  # severity_enum). Any other word is not a grade: a consumer treats it as
  # absent (MINOR, below the queue-row floor) and files nothing, so the
  # operator ask reaches nobody (orchestrator iteration 134: BLOCKING, LOW).
  # WARN once per artifact naming key + word; never rewrite a session's grade
  # and never go red — this is a vocabulary, not a gate. An absent or null
  # severity is silent (absent is a valid grade: MINOR).
  local ungraded
  ungraded="$(jq -r '[.deferrals[] | select(.severity != null)
      | ((.severity | tostring | gsub("^\\s+|\\s+$"; "") | ascii_upcase) as $s
         | select(($s == "BLOCKER" or $s == "MAJOR" or $s == "MINOR") | not))
      | "\(.key // "?")=\(.severity | tostring)"] | join(", ")' <<<"$out" 2>/dev/null)" || ungraded=""
  if [[ -n "$ungraded" ]]; then
    echo "run-until-done: WARN — $(basename "$file") deferral severity outside BLOCKER | MAJOR | MINOR is not a grade (a consumer files nothing for it; deferred-scope gate): $ungraded" >&2
  fi
  if _rewrite_json_keeping_mtime "$file" "$out"; then
    jq -r '.deferrals[] | select(.key_derived != null) | "run-until-done: deferred-scope gate — derived key \(.key) (\(.key_derived)) for deferral: \(.item | tostring | .[0:80])"' "$file" 2>/dev/null || true
  fi
  return 0
}

# --- iteration facts (v0.18.0) ------------------------------------------------
# foundry-meta designs/DESIGN-session-efficiency.md §4, links [H]–[J]. Until
# v0.17.0 the facts about an iteration were the MODEL's: the commit subject
# was `suggested_commit_message` verbatim, the open deferrals were copied
# forward from the previous record by hand (xmeo: 38 entries), and "what did
# phase P change" was rebuilt by projects from `git rev-list --grep=<subject>`.
# Iteration 88's record was built by loading iteration 87's and overwriting a
# hand-picked field list; the stale message came along, master carries
# iteration 88's work under "iteration 87 phase 187" (494fb6f), and 55 xmeo
# suites that find commits by subject silently point at the wrong tree.
#
# The loop now owns what it can see:
#   * the iteration LABEL is the supervisor's (artifacts/iteration-mode.json,
#     read since v0.14.8); the iteration BASE is the commit that iteration
#     started from (the supervisor's intake, found by trailer or by the
#     commit that introduced its iteration-mode.json; standalone: the target
#     tip when the loop created the work branch);
#   * every loop-authored commit carries `Phasekit-Iteration:` /
#     `Phasekit-Phase:` / `Phasekit-Kind:` trailers (Gerrit Change-Id shape:
#     machine keys stable across rewording; subjects become for humans);
#   * an approval-class commit's subject PREFIX is generated from the record
#     (`iteration N phase P: <prose>`; standalone `phase P: <prose>`) — a
#     model prefix that disagrees is CORRECTED and its prose replaced
#     (commitizen, not commitlint: never refused);
#   * records are stamped (iteration, base, final_phase) and a completion
#     record's deferrals are COMPOSED from a tool-maintained ledger
#     (artifacts/deferrals.json, committed), so a session writes only this
#     iteration's new entries and the keys it `closes`;
#   * every approval-class landing writes artifacts/iterations/<N>/<P>.json
#     into its own commit: what that phase changed, with content hashes — the
#     evidence a hermetic test reads instead of git history.
# Fork F5 (Aaron, 2026-09-28): correct what can be derived; a fact that
# cannot be derived is left as written with a WARN and its trailer omitted —
# never a refusal (a refused landing is a stall on a fact the loop does not
# own).

normalize_iteration_label() {
  # $1 = a JSON value (as `jq -c` prints it). Prints the label a subject, a
  # trailer and an evidence path carry: a whole number as itself; a string
  # with a leading "iteration" word stripped (iteration-196 → 196,
  # "Iteration 12" → 12); nothing for anything else or a label a path or a
  # trailer cannot hold.
  local v
  v="$(jq -r 'if type == "number" then (if . == floor then (floor | tostring) else empty end)
              elif type == "string" then (gsub("^\\s+|\\s+$"; "") | sub("^iteration[-_ ]*"; ""; "i"))
              else empty end' <<<"$1" 2>/dev/null | head -n1)" || v=""
  [[ "$v" =~ ^[A-Za-z0-9._-]{1,64}$ ]] && printf '%s' "$v"
  return 0
}

supervising_iteration_label() {
  # FACTS_ITERATION (set only around a catch-up squash): the iteration the
  # record being squashed was STAMPED with — a squash caught up after a new
  # intake belongs to the record's iteration, not the marker's (review
  # round 1: the 494fb6f mislabel shape, again).
  if [[ -n "${FACTS_ITERATION:-}" ]]; then printf '%s' "$FACTS_ITERATION"; return 0; fi
  normalize_iteration_label "$(supervising_iteration_json)"
}

normalize_phase_id() {
  # $1 = a verdict's phase as written. "phase-184" → 184, "Phase 3a" → 3a,
  # "12" → 12, "M9.4" → M9.4. Nothing when unusable — including the loop's
  # own placeholders (unknown, project) and a boolean that wandered into a
  # phase field.
  local v="$1"
  v="$(printf '%s' "$v" | sed -E 's/^[[:space:]]+//; s/^[Pp][Hh][Aa][Ss][Ee][-_ ]*//')"
  v="${v%%[[:space:]:(,]*}"
  case "$v" in unknown|project|true|false|null|"") return 0 ;; esac
  [[ "$v" =~ ^[A-Za-z0-9._-]{1,64}$ ]] && printf '%s' "$v"
  return 0
}

this_iterations_approval_phase() {
  # The phase of the approval on disk IF it belongs to this iteration: never
  # landed (this session's), or stamped with this iteration's label. A light
  # iteration writes no approval; the one on disk is then an older
  # iteration's and names nothing about this one.
  local ap="$ARTIFACTS_DIR/phase-approval.json" n
  [[ -f "$ap" ]] || return 0
  n="$(supervising_iteration_label)"
  if artifact_never_landed "$ap" \
     || { [[ -n "$n" ]] && [[ "$(normalize_iteration_label "$(jq -c '.iteration // null' "$ap" 2>/dev/null)")" == "$n" ]]; }; then
    jq -r '.phase // empty | tostring' "$ap" 2>/dev/null || true
  fi
  return 0
}

verdict_phase_id() {
  # $1 = an artifact file (phase-approval.json, project-complete.json,
  # phase-update.json, or a copy of one). The phase it closes, normalized;
  # nothing when underivable.
  local f="$1" v=""
  [[ -f "$f" ]] || return 0
  case "$(basename "$f")" in
    *project-complete*)
      v="$(jq -r '[.final_phase, .phase] | map(select(type == "string" or type == "number")) | .[0] // empty | tostring' "$f" 2>/dev/null)" || v=""
      [[ -n "$(normalize_phase_id "$v")" ]] || v="$(this_iterations_approval_phase)" ;;
    *)
      v="$(jq -r '.phase // empty | tostring' "$f" 2>/dev/null)" || v="" ;;
  esac
  normalize_phase_id "$v"
}

iteration_base_sha() {
  # $1 = normalized iteration label ("" when unknown). Prints the commit the
  # iteration started from, or nothing. Read from git, in this order:
  #   1. the supervisor's intake commit by trailer (`Phasekit-Kind: intake`
  #      + `Phasekit-Iteration: <N>`);
  #   2. the commit that introduced the supervisor's artifacts/iteration-mode.json
  #      naming <N> (the oldest of the newest contiguous run of commits whose
  #      copy names <N> — a later commit that touched the file keeps <N>);
  #   3. the target tip the loop recorded when it created the work branch
  #      (standalone branch-per-iteration; `work_base` in boundary-state.json).
  local n="$1" c v run=""
  if [[ -n "$n" ]]; then
    c="$(git log -n 400 --format='%H%x09%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)%x09%(trailers:key=Phasekit-Iteration,valueonly,separator=%x2C)' HEAD 2>/dev/null \
         | awk -F'\t' -v n="$n" '$2 == "intake" && $3 == n { print $1; exit }')" || c=""
    if [[ -n "$c" ]]; then printf '%s' "$c"; return 0; fi
    while IFS= read -r c; do
      [[ -n "$c" ]] || continue
      v="$(git show "$c:artifacts/iteration-mode.json" 2>/dev/null | jq -c '.iteration // null' 2>/dev/null)" || v=""
      [[ -n "$v" && "$(normalize_iteration_label "$v")" == "$n" ]] || break
      run="$c"
    done < <(git log -n 200 --format=%H HEAD -- artifacts/iteration-mode.json 2>/dev/null)
    if [[ -n "$run" ]]; then printf '%s' "$run"; return 0; fi
  fi
  c="$(boundary_get '.work_base.sha // empty')"
  if [[ -n "$c" && "$(boundary_get '.work_base.branch // empty')" == "$(current_branch)" ]] \
     && git cat-file -e "$c^{commit}" 2>/dev/null; then
    printf '%s' "$c"
  fi
  return 0
}

phasekit_trailers() {
  # $1 = kind (phase | completion | update | squash | merge-back | wrapup |
  # wip | heal | claim), $2 = the artifact the commit carries (optional; its phase).
  # Prints the trailer lines, one per fact the loop can derive; a fact it
  # cannot derive has no trailer (F5: its absence is honest).
  local kind="$1" f="${2:-}" n p=""
  n="$(supervising_iteration_label)"
  if [[ -n "$f" ]]; then p="$(verdict_phase_id "$f")"; fi
  if [[ -n "$n" ]]; then echo "Phasekit-Iteration: $n"; fi
  if [[ -n "$p" ]]; then echo "Phasekit-Phase: $p"; fi
  echo "Phasekit-Kind: $kind"
}

# --- the plan in docs/PHASES.md (v0.18.2, row 1233 (4) and (6)) -------------
# The PLANNED phase is the loop's to read, never the model's to restate: the
# commit subject after the `iteration N phase P:` prefix is the phase's title
# as docs/PHASES.md plans it (the session's `suggested_commit_message` goes in
# the body — iteration 88's squash carried iteration 87's words because the
# subject was the model's prose), and a phase MAY declare the paths it
# expects to change:
#
#   ## Phase 12 — Lobby admits three matches
#   Planned paths: src/lobby/**, tests/lobby/*.test.ts, docs/SPEC.md
#
# One `Planned paths:` line (or several — they add up) anywhere in the
# phase's section (its heading to the next heading of the same or a higher
# level; not inside a code fence); globs relative to the repository root,
# separated by commas or spaces, backticks optional: `*` stays inside one
# path segment, `**` crosses them, `?` is one character, a trailing `/` or a
# glob-free name also covers everything under it; `Planned paths: none`
# declares that the phase changes nothing outside the implicit set. Always
# planned (the loop's and the workflow's own): artifacts/**, docs/PHASES.md,
# docs/LEARNINGS*.md. A phase is found by its heading: `Phase <P>` (or the
# bare id when it is not purely numeric; `Meta Phase <P>` too) followed by a
# separator (— – - : or ". "); of those the SHALLOWEST level, then the last
# (a plan that restarts its numbering names the current phase last); never a
# progress or status record (`### Phase 109 — progress record …`), and
# `## Phase 193 continuation — …` is not a heading of phase 193.
_phase_plan_py() {
  # $1 = title | check, $2 = phase id, $3 = since (check only). Reads the plan
  # from docs/PHASES.md; check reads the changed
  # paths from `git diff --cached` since $3. Prints the title (or nothing),
  # or the plan_paths JSON object.
  python3 - "$@" <<'PLAN_PY'
import json, os, re, subprocess, sys

mode, pid = sys.argv[1], sys.argv[2]
since = sys.argv[3] if len(sys.argv) > 3 else ""
IMPLICIT = ["artifacts/**", "docs/PHASES.md", "docs/LEARNINGS*.md"]
CAP = 100

def plan_file():
    return "docs/PHASES.md" if os.path.isfile("docs/PHASES.md") else None

# a record ABOUT the phase, not its plan: "progress record …", "status
# update", or the bare word — never a title like "Status page" (round 3)
PROGRESS = re.compile(r"^(progress|status)(\s+(record|records|report|update|updates|log|note|notes)\b|\s*$|\s*[(:\u2014\u2013-])", re.I)

def section(lines, pid):
    """(title, body lines) of the heading that PLANS phase pid: `Phase <pid>`
    (or `Meta Phase <pid>`; a bare id only when it is not purely numeric,
    `## M9.4 — …`) followed by a separator. Of those: never a progress or
    status record (`### Phase 109 — progress record …`), then the SHALLOWEST
    level, then the last (a plan that restarts its numbering names the
    current phase last)."""
    if not pid:
        return None
    lead = r"(?:(?:meta[\s_-]*)?phase[\s_-]*)" + ("?" if re.search(r"[A-Za-z]", pid) else "")
    head = re.compile(r"^(#{1,6})\s+(?:\*\*|__)?\s*" + lead + re.escape(pid)
                      + r"(?![A-Za-z0-9._-])\s*(?:\*\*|__)?\s*(?:[—–:\-]|\.\s)\s*(.*?)\s*#*\s*$", re.I)
    fence = False
    found = []
    heads = []
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"^(#{1,6})\s", ln)
        if m:
            heads.append((i, len(m.group(1))))
        hm = head.match(ln)
        if hm and not PROGRESS.match(re.sub(r"(\*\*|__)", "", hm.group(2)).strip()):
            found.append((i, len(hm.group(1)), hm.group(2)))
    if not found:
        return None
    top = min(f[1] for f in found)
    i, lvl, title = [f for f in found if f[1] == top][-1]
    end = len(lines)
    for j, l2 in heads:
        if j > i and l2 <= lvl:
            end = j
            break
    return title, lines[i + 1:end]

def clean_title(t):
    t = re.sub(r"(\*\*|__)", "", t).strip()
    t = re.sub(r"\s*[✅✔☑]\s*$", "", t)
    t = re.sub(r"\s*\[(x|done)\]\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*[—–-]+\s*(DONE|APPROVED|COMPLETE|COMPLETED)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*\((done|approved|complete|completed)\)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -—–:")
    if len(t) > CAP and re.search(r"\s\([^()]*\)$", t):
        t = re.sub(r"\s\([^()]*\)$", "", t)
    if len(t) > CAP:
        cut = t[:CAP].rsplit(" ", 1)[0].rstrip(" ,;:-—–")
        t = (cut or t[:CAP]) + "…"
    return t

def glob_re(g):
    g = g.strip().strip("`'\"")
    while g.startswith("./"):
        g = g[2:]
    g = g.lstrip("/")
    if not g:
        return None
    dirlike = g.endswith("/") or not re.search(r"[*?\[]", g)
    g = g.rstrip("/")
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif g.startswith("**", i):
            out += ".*"; i += 2
        elif g[i] == "*":
            out += "[^/]*"; i += 1
        elif g[i] == "?":
            out += "[^/]"; i += 1
        elif g[i] == "[" and "]" in g[i + 2:]:
            j = g.index("]", i + 2)
            body = g[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out += "[" + body.replace("\\", "\\\\") + "]"; i = j + 1
        else:
            out += re.escape(g[i]); i += 1
    return re.compile("^" + out + ("(?:/.*)?$" if dirlike else "$"))

def declared_globs(body):
    fence, globs, declared = False, [], False
    for ln in body:
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?planned[ _-]paths(?:\*\*|__)?\s*:(?:\*\*|__)?\s*(.*)$", ln, re.I)
        if not m:
            continue
        declared = True
        rest = m.group(1).strip()
        if re.fullmatch(r"\(?\s*none\s*\)?\.?", rest, re.I) or rest in ("", "-", "—"):
            continue
        for tok in re.split(r"[,\s]+", rest):
            tok = tok.strip().strip("`'\"").rstrip(".;")
            if tok:
                globs.append(tok)
    return declared, globs

rel = plan_file()
lines = open(rel, encoding="utf-8", errors="replace").read().splitlines() if rel else []
sec = section(lines, pid)
if mode == "title":
    if sec:
        t = clean_title(sec[0])
        if t:
            print(t)
    sys.exit(0)

res = {"schema": 1, "phase": pid or None, "source": rel, "declared": False, "globs": [],
       "status": "underivable", "changed_count": None, "unplanned": [], "unplanned_count": None}
if not pid or not since:
    res["note"] = "no phase id" if not pid else "no base to compare against"
    print(json.dumps(res, sort_keys=True)); sys.exit(0)
try:
    raw = subprocess.run(["git", "diff", "--cached", "--no-renames", "--name-only", "-z", since],
                         capture_output=True, check=True).stdout.split(b"\0")
except Exception:
    res["note"] = "git could not list the changed paths"
    print(json.dumps(res, sort_keys=True)); sys.exit(0)
implicit = [glob_re(g) for g in IMPLICIT]
changed = sorted({p.decode("utf-8", "replace") for p in raw if p})
changed = [p for p in changed if not any(r.match(p) for r in implicit)]
res["changed_count"] = len(changed)
declared, globs = declared_globs(sec[1]) if sec else (False, [])
res["declared"], res["globs"] = declared, globs
if not declared:
    res["status"] = "no-plan-declared"
    res["note"] = ("docs/PHASES.md has no heading for phase %s" % pid) if not sec else "the phase declares no `Planned paths:`"
    print(json.dumps(res, sort_keys=True)); sys.exit(0)
pats = [r for r in (glob_re(g) for g in globs) if r]
unplanned = [p for p in changed if not any(r.match(p) for r in pats)]
res["unplanned_count"] = len(unplanned)
res["unplanned"] = unplanned[:200]
res["status"] = "outside" if unplanned else "inside"
print(json.dumps(res, sort_keys=True))
PLAN_PY
}

planned_phase_title() {
  # $1 = normalized phase id. The phase's planned title, or nothing.
  [[ -n "${1:-}" ]] || return 0
  (cd "$ROOT_DIR" && _phase_plan_py title "$1" 2>/dev/null | head -n1) || true
  return 0
}

phase_plan_check() {
  # $1 = normalized phase id (may be empty), $2 = since. Prints the
  # `plan_paths` JSON (never empty: a failure is `underivable`) and ONE
  # stderr line saying what it found — warn only, never a refusal.
  local p="${1:-}" since="${2:-}" j st
  j="$(cd "$ROOT_DIR" && _phase_plan_py check "$p" "$since" 2>/dev/null)" || j=""
  if [[ -z "$j" ]] || ! jq -e 'type == "object"' <<<"$j" >/dev/null 2>&1; then
    j="$(jq -cn --arg p "$p" '{schema: 1, phase: (if $p == "" then null else $p end), status: "underivable", declared: false, globs: [], changed_count: null, unplanned: [], unplanned_count: null, note: "the plan could not be read (python3 unavailable?)"}')"
  fi
  st="$(jq -r '.status' <<<"$j")"
  case "$st" in
    inside) echo "plan: phase ${p:-?} — all $(jq -r '.changed_count' <<<"$j") changed path(s) are inside its planned paths (docs/PHASES.md)." >&2 ;;
    outside) echo "plan: phase ${p:-?} — $(jq -r '.unplanned_count' <<<"$j") changed path(s) are OUTSIDE its planned paths (warn only; recorded as plan_paths): $(jq -r '.unplanned[:10] | join(", ")' <<<"$j")$( [[ "$(jq -r '.unplanned_count' <<<"$j")" -gt 10 ]] && echo ", …")" >&2 ;;
    no-plan-declared) echo "plan: phase ${p:-?} — no plan declared ($(jq -r '.note // ""' <<<"$j")); $(jq -r '.changed_count' <<<"$j") changed path(s), none compared." >&2 ;;
    *) echo "plan: phase ${p:-?} — the plan check is underivable ($(jq -r '.note // ""' <<<"$j"))." >&2 ;;
  esac
  printf '%s\n' "$(jq -c . <<<"$j")"
}

compose_commit_message() {
  # $1 = the message as the verdict wrote it (or the loop's fallback), $2 =
  # the approval-class artifact it lands. Prints the message the loop
  # commits: the FIRST line's facts prefix is generated from the record —
  #   supervised:  iteration <N> phase <P>: <prose>
  #   standalone:  phase <P>: <prose>
  # — where <prose> is the written first line minus any facts prefix the
  # model put there (text before the first ": " that starts with
  # "iteration"/"phase" and a number). When that prefix DISAGREES with the
  # record, the prose is stale too (494fb6f: iteration 88's work under
  # iteration 87's subject): it is replaced by the record's iteration_label
  # or summary, and the correction is printed. Later lines are kept. When P
  # cannot be derived the message stays as written (WARN when supervised) —
  # except that a disagreeing iteration is still corrected (F5).
  local msg="$1" f="$2" n p first rest head tail mn="" mp="" prose lower repl=""
  n="$(supervising_iteration_label)"
  p="$(verdict_phase_id "$f")"
  first="${msg%%$'\n'*}"
  rest=""; if [[ "$msg" == *$'\n'* ]]; then rest="${msg#*$'\n'}"; fi
  prose="$first"
  # A facts prefix starts with "iteration"/"phase" and a NUMBER ("phase-out
  # of legacy:" and "iteration planning:" are prose — review round 1); a
  # subject that is nothing but a prefix has no prose of its own.
  local pl="${p,,}" re_colon re_bare
  re_colon='^[[:space:]]*([^:]{1,80}):([[:space:]]+(.*))?$'
  re_bare='^[[:space:]]*((iteration[-_ ]?[0-9][0-9a-z._-]*[, ]*)?phase[-_ ]?[0-9a-z._-]+)[[:space:]]*$'
  head=""; tail=""
  if [[ "$first" =~ $re_colon ]]; then
    head="${BASH_REMATCH[1]}"; tail="${BASH_REMATCH[3]:-}"
  elif [[ "${first,,}" =~ $re_bare ]]; then
    head="${BASH_REMATCH[1]}"; tail=""
  fi
  local sepnorm_head="" sepnorm_n="" sepnorm_p=""
  if [[ -n "$head" ]]; then
    sepnorm_head="$(printf '%s' "${head,,}" | tr -- '-_' '  ' | tr -s ' ' | sed -E 's/^ //; s/ $//')"
    sepnorm_n="$(printf '%s' "${n,,}" | tr -- '-_' '  ' | tr -s ' ')"
    sepnorm_p="$(printf '%s' "${p,,}" | tr -- '-_' '  ' | tr -s ' ')"
  fi
  if [[ -n "$head" && -n "$sepnorm_p" ]] && { [[ -n "$sepnorm_n" && "$sepnorm_head" == "iteration $sepnorm_n phase $sepnorm_p" ]] \
       || [[ -n "$sepnorm_n" && "$sepnorm_head" == "iteration $sepnorm_n, phase $sepnorm_p" ]] \
       || [[ "$sepnorm_head" == "phase $sepnorm_p" ]]; }; then
    # the record's own labels, written its way (hyphens and all): agreed
    [[ "$sepnorm_head" == iteration* ]] && mn="${n,,}"
    mp="${p,,}"; prose="$tail"
  elif [[ -n "$head" ]]; then
    lower="${head,,}"
    local nl="${n,,}"
    if [[ -n "$nl" && ! "$nl" =~ ^[0-9] && "$lower" =~ ^[[:space:]]*iteration[-_\ ]?([0-9a-z._]+) && "${BASH_REMATCH[1]}" == "$nl" ]]; then
      # the supervisor's own non-numeric label ("iteration A7 phase 5: …")
      mn="$nl"
      if [[ "$lower" =~ phase[-_\ ]?([0-9a-z][0-9a-z._]*) ]]; then mp="${BASH_REMATCH[1]}"; fi
      prose="$tail"
    elif [[ -n "$pl" && "$lower" =~ ^[[:space:]]*([0-9a-z._-]+)[[:space:]]*$ && "${BASH_REMATCH[1]}" == "$pl" ]]; then
      # the record's own phase id written bare ("M9.4: …")
      mp="$pl"; prose="$tail"
    elif [[ "$lower" =~ ^[[:space:]]*(iteration|phase)[-_\ ]?[0-9] ]] \
       || { [[ -n "$pl" ]] && [[ "$lower" =~ ^[[:space:]]*phase[-_\ ]?([0-9a-z._-]+)[[:space:]]*$ ]] && [[ "${BASH_REMATCH[1]}" == "$pl" ]]; }; then
      if [[ "$lower" =~ iteration[-_\ ]?([0-9][0-9a-z._]*) ]]; then mn="${BASH_REMATCH[1]}"; fi
      if [[ "$lower" =~ phase[-_\ ]?([0-9a-z][0-9a-z._]*) ]]; then mp="${BASH_REMATCH[1]}"; fi
      prose="$tail"
    fi
  fi
  local disagree=0
  if [[ -n "$mn" && -n "$n" && "$mn" != "${n,,}" ]]; then disagree=1; fi
  if [[ -n "$mp" && -n "$p" && "$mp" != "${p,,}" ]]; then disagree=1; fi
  if [[ "$disagree" -eq 1 || -z "${prose//[[:space:]]/}" ]]; then
    repl="$(jq -r '[.iteration_label, .summary] | map(select(type == "string" and length > 0)) | .[0] // empty' "$f" 2>/dev/null | head -n1)" || repl=""
    repl="$(printf '%s' "$repl" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | cut -c1-160)"
    [[ -n "$repl" ]] && prose="$repl"
  fi
  if [[ -z "${prose//[[:space:]]/}" ]]; then
    prose="$(jq -r '.phase // "approved phase" | tostring' "$f" 2>/dev/null)" || prose="approved phase"
  fi
  # v0.18.2 (row 1233 (4)): the subject after the prefix is the PLANNED
  # phase title (docs/PHASES.md); the session's prose goes in the body. No
  # plan title: the record's prose, and the loop says so.
  local out title="" sprose="$prose" body=""
  if [[ -n "$p" ]]; then title="$(planned_phase_title "$p")"; fi
  if [[ -n "$title" ]]; then
    sprose="$title"; body="$prose"
  elif [[ -n "$p" ]]; then
    echo "commit subject: docs/PHASES.md plans no title for phase $p — the record's prose is the subject (v0.18.2)" >&2
  fi
  if [[ -n "$p" && -n "$n" ]]; then
    out="iteration $n phase $p: $sprose"
  elif [[ -n "$p" ]]; then
    out="phase $p: $sprose"
  elif [[ -n "$n" && -n "$mn" && "$mn" != "${n,,}" ]]; then
    out="iteration $n: $prose"
    echo "run-until-done: WARN — the phase of $(basename "$f") cannot be derived; the stale iteration in its subject was corrected, the phase left out (F5)." >&2
  else
    out="$first"
    if [[ -n "$n" ]]; then
      echo "run-until-done: WARN — the phase of $(basename "$f") cannot be derived; subject kept as written, no Phasekit-Phase trailer (F5)." >&2
    fi
  fi
  if [[ "$disagree" -eq 1 ]]; then
    echo "commit subject corrected: '${head}' → '${out%%: *}' (the record says so)" >&2
  fi
  if [[ -n "$body" && "$body" != "$title" ]]; then out="$out"$'\n\n'"$body"; fi
  if [[ -n "$rest" ]]; then printf '%s\n%s' "$out" "$rest"; else printf '%s' "$out"; fi
}

stamp_verdict_facts() {
  # $1 = an approval-class artifact that has not landed and will ride the
  # commit being made. Stamps what the loop owns: `iteration` (the
  # supervisor's value, written when absent and CORRECTED when the record
  # names another iteration), `base`, and on a completion record
  # `final_phase` and `recorded_by` when absent. Keeps the file's mtime.
  local f="$1" raw n cur base kind="approval" p="" out msg=""
  jq -e 'type == "object"' "$f" >/dev/null 2>&1 || return 0
  [[ "$(basename "$f")" == project-complete.json ]] && kind="completion"
  raw="$(supervising_iteration_json)"; n="$(normalize_iteration_label "$raw")"
  cur="$(jq -c '.iteration // null' "$f" 2>/dev/null)" || cur=null
  if [[ -n "$n" && "$cur" != "null" && "$(normalize_iteration_label "$cur")" != "$n" ]]; then
    msg="run-until-done: record corrected: $(basename "$f") named iteration $cur; the supervisor's is $raw (the record says so from now on)"
  fi
  base="$(iteration_base_sha "$n")"
  if [[ "$kind" == completion ]]; then p="$(this_iterations_approval_phase)"; fi
  out="$(jq --argjson raw "${raw:-null}" --arg n "$n" --arg base "$base" --arg kind "$kind" --arg p "$p" '
      def itlabel: if type == "number" then (if . == floor then (floor | tostring) else "" end)
                 elif type == "string" then (gsub("^\\s+|\\s+$"; "") | sub("^iteration[-_ ]*"; ""; "i"))
                 else "" end;
      (if $n == "" then .
       elif (.iteration // null) == null then .iteration = $raw
       elif (.iteration | itlabel) != $n then .iteration = $raw
       else . end)
      | (if $base != "" then .base = $base else . end)
      | (if $kind == "completion" and ((.final_phase // null) == null) and $p != "" then .final_phase = $p else . end)
      | (if $kind == "completion" and ((.recorded_by // null) == null) then
           .recorded_by = "the session; stamped at landing by phasekit run-until-done.sh (v0.18.0): iteration, base and final_phase from the loop, deferrals = the open set in artifacts/deferrals.json"
         else . end)' "$f" 2>/dev/null)" || return 0
  if _rewrite_json_keeping_mtime "$f" "$out"; then
    if [[ -n "$msg" ]]; then echo "$msg" >&2; fi
  fi
  return 0
}

_last_committed_completion_record() {
  # The newest completion record git has on HEAD's history — the previous
  # iteration's full open set when records were copied forward (the ledger's
  # one-time seed). Its content at the newest commit that touched the path,
  # or just before it when that commit deleted it (an intake's rename).
  local c
  c="$(git log -n 1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)" || c=""
  [[ -n "$c" ]] || return 0
  git show "$c:artifacts/project-complete.json" 2>/dev/null \
    || git show "$c^:artifacts/project-complete.json" 2>/dev/null || true
}

_ledger_base_to() {
  # $1 = output file. The open set BEFORE this landing, as JSON
  # {schema, open, closed}: HEAD's committed ledger — it moves only with an
  # approval-class landing, so a draft a `phasekit verify` or a refused
  # landing left on disk never becomes the base — else (the first
  # composition) the deferrals of the last committed completion record,
  # keyed. Everything travels through FILES: xmeo's open set alone is
  # 218 KB, over a single argument's limit (review round 1, BLOCKER).
  local out="$1" seed
  if git show HEAD:artifacts/deferrals.json > "$out" 2>/dev/null \
     && jq -e 'type == "object" and (.open | type == "array")' "$out" >/dev/null 2>&1; then
    jq '{schema: 1, open: .open, closed: (.closed // [] | map(tostring))}' "$out" > "$out.n" 2>/dev/null && mv -f "$out.n" "$out"
    return 0
  fi
  seed="$out.seed"
  _last_committed_completion_record > "$seed" 2>/dev/null || : > "$seed"
  if [[ -s "$seed" ]] && jq "$(_deferral_key_jq) {schema: 1, closed: [],
        open: [(.deferrals // [])[]? | select(type == \"object\") | keyed | select(((.key // \"\") | tostring | length) > 0)]}" \
        "$seed" > "$out" 2>/dev/null; then
    echo "run-until-done: deferral ledger artifacts/deferrals.json started from the last committed completion record ($(jq '.open | length' "$out") open) — v0.18.0" >&2
  else
    printf '{"schema": 1, "open": [], "closed": []}\n' > "$out"
  fi
  rm -f "$seed"
  return 0
}

compose_deferrals_ledger() {
  # The open set, recomputed at every preparation from HEAD's ledger plus the
  # approval-class records that ride this landing (the changesets shape):
  # each record's keyed `deferrals` are upserted (an entry copied forward out
  # of habit is "unchanged", never new), its `closes` keys removed and
  # remembered as CLOSED (a later copy-forward never reopens one — a WARN
  # names it). Recomputed, never accumulated: a draft verdict edited or
  # abandoned after a `phasekit verify` leaves nothing behind (review round
  # 1). A COMPLETION record's `deferrals` is then written back as the full
  # open set plus its own entries that carry no key (they cannot enter the
  # ledger and are never dropped). Deterministic, idempotent, never refuses.
  local ledger="$ARTIFACTS_DIR/deferrals.json" d f files=() pc="" next
  for f in phase-approval.json project-complete.json; do
    artifact_never_landed "$ARTIFACTS_DIR/$f" || continue
    if [[ "$f" == project-complete.json ]]; then completion_record_rides || continue; pc="$ARTIFACTS_DIR/$f"; fi
    if ! jq -e 'type == "object" and ((.deferrals // []) | type == "array") and ((.closes // []) | type == "array")' "$ARTIFACTS_DIR/$f" >/dev/null 2>&1; then
      if jq -e 'type == "object"' "$ARTIFACTS_DIR/$f" >/dev/null 2>&1; then
        echo "run-until-done: WARN — $f: 'deferrals'/'closes' are not arrays; left out of the deferral ledger (v0.18.0)." >&2
      fi
      [[ "$f" == project-complete.json ]] && pc=""
      continue
    fi
    files+=("$ARTIFACTS_DIR/$f")
  done
  [[ ${#files[@]} -gt 0 ]] || return 0
  d="$(mktemp -d)" || return 0
  _ledger_base_to "$d/base.json"
  jq -s '.' "${files[@]}" > "$d/recs.json" 2>/dev/null || { rm -rf "$d"; return 0; }
  if ! jq -n --slurpfile base "$d/base.json" --slurpfile recs "$d/recs.json" '
      reduce ($recs[0][]) as $r ({open: $base[0].open, closed: $base[0].closed, reopened: []};
        ([$r.deferrals[]? | select(type == "object" and ((.key // "") | tostring | length) > 0)]) as $new
        | ([$r.closes[]? | tostring]) as $cl
        | reduce $new[] as $e (.;
            if (.closed | index($e.key | tostring)) != null then .reopened += [$e.key | tostring]
            else (.open | map(.key | tostring) | index($e.key | tostring)) as $i
                 | if $i == null then .open += [$e] else .open[$i] = $e end end)
        | .open |= map(select((.key | tostring) as $k | ($cl | index($k)) == null))
        | .closed = ((.closed + $cl) | unique))
      | {schema: 1,
         note: "the OPEN deferrals, maintained by phasekit run-until-done.sh at every approval-class landing (v0.18.0): a record adds entries in `deferrals` and removes keys in `closes` (remembered in `closed`); never hand-edited",
         open, closed, reopened: (.reopened | unique)}' > "$d/next.json" 2>/dev/null; then
    rm -rf "$d"; return 0
  fi
  if [[ "$(jq '.reopened | length' "$d/next.json")" != 0 ]]; then
    echo "run-until-done: WARN — deferral key(s) $(jq -r '.reopened | join(", ")' "$d/next.json") were closed earlier; a record re-listing them does not reopen them (use a new key) — v0.18.0 deferral ledger." >&2
  fi
  jq 'del(.reopened)' "$d/next.json" > "$d/ledger.json"
  if ! cmp -s "$d/ledger.json" "$ledger"; then cp -f "$d/ledger.json" "$ledger" 2>/dev/null || true; fi
  if [[ -n "$pc" ]]; then
    next="$(jq --slurpfile l "$d/ledger.json" \
             '.deferrals = ($l[0].open + [(.deferrals // [])[] | select(type != "object" or ((.key // "") | tostring | length) == 0)])' \
             "$pc" 2>/dev/null)" || next=""
    if [[ -n "$next" ]]; then _rewrite_json_keeping_mtime "$pc" "$next" || true; fi
  fi
  rm -rf "$d"
  return 0
}

reset_derived_state() {
  # A commit that does not LAND an approval-class record (a checkpoint, a
  # wrap-up, the watchdog's wip) never carries the loop's derived state: the
  # deferral ledger goes back to HEAD's copy and a phase-close evidence file
  # no landing committed (a `phasekit verify` draft) is removed — both are
  # recomputed by the landing that owns them (review round 1). $1 = "unstage"
  # keeps them out of the index only (the files stay as they are).
  local mode="${1:-restore}" rel p
  rel="artifacts/deferrals.json"
  if [[ "$mode" == unstage ]]; then
    git reset -q -- "$ARTIFACTS_DIR/deferrals.json" 2>/dev/null || true
  elif git cat-file -e "HEAD:$rel" 2>/dev/null; then
    git reset -q -- "$ARTIFACTS_DIR/deferrals.json" 2>/dev/null || true
    git checkout -q HEAD -- "$rel" 2>/dev/null || true
  else
    git reset -q -- "$ARTIFACTS_DIR/deferrals.json" 2>/dev/null || true
    rm -f "$ARTIFACTS_DIR/deferrals.json"
  fi
  while IFS= read -r -d '' p; do
    [[ "$p" == artifacts/iterations/*/*.json ]] || continue
    git reset -q -- "$ROOT_DIR/$p" 2>/dev/null || true
    [[ "$mode" == unstage ]] || rm -f -- "$ROOT_DIR/$p"
  done < <(git -C "$ROOT_DIR" ls-files -z --others --exclude-standard -- artifacts/iterations 2>/dev/null; \
           git -C "$ROOT_DIR" diff --cached --name-only -z --diff-filter=A -- artifacts/iterations 2>/dev/null)
  return 0
}

prepare_verdicts_for_landing() {
  # Every approval-class artifact the commit may sweep (`git add -A` takes
  # both when both are on disk), before it is staged: deferral keys (the
  # deferred-scope gate, v0.14.5), then the loop's facts (v0.18.0). Only an
  # artifact that has NOT landed is touched (touching a landed one would
  # turn it "stranded" the moment the commit is refused), and a carried
  # completion record only when it rides this commit. Idempotent and
  # deterministic, so `phasekit verify` and the commit gate prepare the SAME
  # tree and the gate's verdict for it is reused (the verify memo).
  local _art
  for _art in phase-approval.json project-complete.json; do
    artifact_never_landed "$ARTIFACTS_DIR/$_art" || continue
    if [[ "$_art" == project-complete.json ]] && ! completion_record_rides; then continue; fi
    normalize_deferral_keys "$ARTIFACTS_DIR/$_art"
    stamp_verdict_facts "$ARTIFACTS_DIR/$_art"
  done
  compose_deferrals_ledger
  return 0
}

evidence_driver() {
  # Which artifact a landing of the current tree is driven by: an unlanded
  # approval first (step 2 commits it under its own message), else an
  # unlanded completion record that rides (step 3). Prints its path.
  if artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
    echo "$ARTIFACTS_DIR/phase-approval.json"
  elif [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] && artifact_never_landed "$ARTIFACTS_DIR/project-complete.json" \
       && completion_record_rides; then
    echo "$ARTIFACTS_DIR/project-complete.json"
  fi
}

write_phase_evidence() {
  # $1 = the artifact driving this approval-class commit; call AFTER the tree
  # is staged. Writes artifacts/iterations/<N>/<P>.json (complete.json for a
  # completion commit) INTO the commit: iteration, phase, base, `since` (the
  # previous phase close: the target tip in branch-per-iteration, else this
  # iteration's last phase/completion commit by trailer, else the base) and
  # `changed` — path, status and sha256 of the staged content, per path. A
  # test that wants "phase 177 left demo/ unmoved" compares the tree's bytes
  # against this file: a golden read from the tree, hermetic. Supervised
  # iterations only (a standalone run has no iteration to file it under);
  # best-effort — a failure is a WARN, never a refused commit.
  local f="$1" n p name since base rel c plan
  n="$(supervising_iteration_label)"
  case "$(basename "$f")" in
    phase-approval.json) p="$(verdict_phase_id "$f")"; name="$p" ;;
    project-complete.json) p="$(verdict_phase_id "$f")"; name="complete" ;;
    *) return 0 ;;
  esac
  base="$(iteration_base_sha "$n")"
  since=""
  if squash_mode; then
    since="$(git rev-parse -q --verify "refs/heads/$SQUASH_TARGET" 2>/dev/null)" || since=""
  else
    since="$(git log -n 400 --format='%H%x09%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)%x09%(trailers:key=Phasekit-Iteration,valueonly,separator=%x2C)' HEAD 2>/dev/null \
             | awk -F'\t' -v n="$n" '($2 == "phase" || $2 == "completion") && $3 == n { print $1; exit }')" || since=""
  fi
  [[ -n "$since" ]] || since="$base"
  [[ -n "$since" ]] || since="$(git rev-parse -q --verify HEAD 2>/dev/null)" || since=""
  # v0.18.2 (row 1233 (6)): the phase's changed paths against the paths its
  # plan declares — every approval-class landing, supervised or not; warn
  # only. The record carries it as `plan_paths` (stamped into the staged
  # record, mtime kept), and so does the evidence file below.
  plan="$(phase_plan_check "$p" "$since")"
  _stamp_plan_paths "$f" "$plan"
  [[ -n "$n" ]] || return 0
  if [[ -z "$p" && "$name" != complete ]]; then echo "run-until-done: WARN — the approval names no usable phase; no phase-close evidence written (F5)." >&2; return 0; fi
  [[ -n "$since" ]] || return 0
  rel="artifacts/iterations/$n/$name.json"
  mkdir -p "$ROOT_DIR/artifacts/iterations/$n" 2>/dev/null || return 0
  # A draft no landing committed (an earlier `phasekit verify` of a phase
  # since renamed) is not evidence of anything: removed, never swept.
  local stale
  while IFS= read -r -d '' stale; do
    [[ "$stale" == "$rel" ]] && continue
    git reset -q -- "$ROOT_DIR/$stale" 2>/dev/null || true
    rm -f -- "$ROOT_DIR/$stale"
  done < <(git -C "$ROOT_DIR" ls-files -z --others --exclude-standard -- "artifacts/iterations/$n" 2>/dev/null; \
           git -C "$ROOT_DIR" diff --cached --name-only -z --diff-filter=A -- "artifacts/iterations/$n" 2>/dev/null)
  if ! (cd "$ROOT_DIR" && PK_PLAN_PATHS="$plan" python3 - "$since" "$rel" "$n" "$p" "$base" <<'EVIDENCE_PY'
import hashlib, json, os, subprocess, sys
since, rel, n, p, base = sys.argv[1:6]
raw = subprocess.run(["git", "diff", "--cached", "--no-renames", "--name-status", "-z", since],
                     capture_output=True, check=True).stdout.split(b"\0")
entries = []
i = 0
while i + 1 < len(raw):
    status, path = raw[i].decode("ascii", "replace"), raw[i + 1]
    i += 2
    if path.decode("utf-8", "replace") != rel:
        entries.append((path, status[:1]))
staged = {}
live = [pth for pth, st in entries if st != "D"]
if live:
    wanted = set(live)
    out = subprocess.run(["git", "ls-files", "-s", "-z"], capture_output=True, check=True).stdout.split(b"\0")
    for rec in out:
        if not rec:
            continue
        meta, _, pth = rec.partition(b"\t")
        mode, sha, _ = meta.split(b" ", 2)
        if pth in wanted and mode != b"160000":
            staged[pth] = sha.decode()
digests = {}
if staged:
    proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for pth, sha in staged.items():
        proc.stdin.write(sha.encode() + b"\n"); proc.stdin.flush()
        header = proc.stdout.readline().split()
        size = int(header[2]) if len(header) == 3 else 0
        h, left = hashlib.sha256(), size
        while left:
            chunk = proc.stdout.read(min(left, 1 << 20)); h.update(chunk); left -= len(chunk)
        proc.stdout.read(1)
        digests[pth] = h.hexdigest()
    proc.stdin.close(); proc.wait()
record = {
    "schema": 1,
    "iteration": n,
    "phase": p or None,
    "base": base or None,
    "since": since,
    "changed": [{"path": pth.decode("utf-8", "replace"), "status": st, "sha256": digests.get(pth)}
                for pth, st in sorted(entries)],
    "note": "written by phasekit run-until-done.sh at this phase's close (v0.18.0): what the phase changed since `since`; read it from the tree instead of git history (docs/QUALITY_GATES.md 'Hermetic tests')",
    }
try:
    plan = json.loads(os.environ.get("PK_PLAN_PATHS") or "null")
except ValueError:
    plan = None
if isinstance(plan, dict):
    record["plan_paths"] = plan
with open(rel, "w", encoding="utf-8") as fh:
    json.dump(record, fh, indent=1, sort_keys=True)
    fh.write("\n")
EVIDENCE_PY
  ); then
    echo "run-until-done: WARN — phase-close evidence $rel could not be written; the commit proceeds without it." >&2
    return 0
  fi
  git add -f -- "$rel" 2>/dev/null || true
  return 0
}

_stamp_plan_paths() {
  # $1 = the approval-class record being landed, $2 = the plan_paths JSON.
  # Written into the record (mtime kept: the loop's rewrite is never the
  # session's verdict) and re-staged. Best-effort.
  local f="$1" j="$2" out
  [[ -f "$f" && -n "$j" ]] || return 0
  jq -e 'type == "object"' "$f" >/dev/null 2>&1 || return 0
  out="$(jq --argjson p "$j" '.plan_paths = $p' "$f" 2>/dev/null)" || return 0
  _rewrite_json_keeping_mtime "$f" "$out" || true
  git add -f -- "$f" 2>/dev/null || true
  return 0
}

phasekit_scope() {
  # `phasekit scope [--iteration N] [--phase P] [--json]` (v0.18.0, design
  # §4.5): what did this iteration — or one of its phases — change? Answered
  # from the record's base and HEAD (and the phase-close evidence the loop
  # writes into every approval-class commit), never by grepping subjects.
  # The model-facing tool: it may read git; a TEST may not (docs/
  # QUALITY_GATES.md "Hermetic tests" — a test reads the evidence file).
  local n="" p="" json=0 base head src="" pc="" rel
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --iteration) n="${2:-}"; shift; [[ $# -gt 0 ]] && shift ;;
      --iteration=*) n="${1#--iteration=}"; shift ;;
      --phase) p="${2:-}"; shift; [[ $# -gt 0 ]] && shift ;;
      --phase=*) p="${1#--phase=}"; shift ;;
      --json) json=1; shift ;;
      -h|--help) echo "usage: phasekit scope [--iteration N] [--phase P] [--json]"; return 0 ;;
      *) echo "phasekit scope: unknown argument '$1' (usage: phasekit scope [--iteration N] [--phase P] [--json])" >&2; return 2 ;;
    esac
  done
  if [[ -n "$n" ]]; then n="$(normalize_iteration_label "$(jq -cn --arg v "$n" '$v')")"; else n="$(supervising_iteration_label)"; fi
  if [[ -z "$n" ]]; then
    echo "phasekit scope: no iteration — artifacts/iteration-mode.json names none; pass --iteration N" >&2
    return 2
  fi
  if [[ -n "$p" ]]; then p="$(normalize_phase_id "$p")"; fi
  base="$(iteration_base_sha "$n")"
  head="$(git rev-parse -q --verify HEAD 2>/dev/null)" || head=""
  local commits
  commits="$(git log -n 400 --format='%H%x09%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)%x09%(trailers:key=Phasekit-Iteration,valueonly,separator=%x2C)%x09%(trailers:key=Phasekit-Phase,valueonly,separator=%x2C)' HEAD 2>/dev/null \
            | awk -F'\t' -v n="$n" '$3 == n && ($2 == "phase" || $2 == "completion" || $2 == "squash") { print $1 "\t" $2 "\t" tolower($4) }')" || commits=""
  local changed="[]"
  if [[ -n "$p" ]]; then
    rel="artifacts/iterations/$n/$p.json"
    if [[ ! -f "$ROOT_DIR/$rel" && -d "$ROOT_DIR/artifacts/iterations/$n" ]]; then
      # phase ids compare case-insensitively (M9.4 = m9.4)
      local cand
      for cand in "$ROOT_DIR/artifacts/iterations/$n"/*.json; do
        [[ -f "$cand" ]] || continue
        if [[ "$(basename "$cand" .json | tr '[:upper:]' '[:lower:]')" == "${p,,}" ]]; then
          rel="artifacts/iterations/$n/$(basename "$cand")"; p="$(basename "$cand" .json)"; break
        fi
      done
    fi
    if [[ -f "$ROOT_DIR/$rel" ]] && jq -e '.changed | type == "array"' "$ROOT_DIR/$rel" >/dev/null 2>&1; then
      changed="$(jq -c '[.changed[] | {path, status}]' "$ROOT_DIR/$rel")"; src="evidence $rel"
      pc="$(awk -F'\t' -v p="${p,,}" '$3 == p { print $1; exit }' <<<"$commits")"
    else
      # No evidence file (a phase that closed before v0.18.0): the newest
      # commit whose trailers name the phase — the squash first (on the
      # target it is exactly the phase), else the phase commit.
      pc="$(awk -F'\t' -v p="${p,,}" '$3 == p && $2 == "squash" { print $1; exit }' <<<"$commits")"
      [[ -n "$pc" ]] || pc="$(awk -F'\t' -v p="${p,,}" '$3 == p { print $1; exit }' <<<"$commits")"
      if [[ -z "$pc" ]]; then
        echo "phasekit scope: iteration $n has no evidence file for phase $p ($rel) and no commit carries 'Phasekit-Phase: $p'" >&2
        return 1
      fi
      changed="$(_name_status_json "$pc^" "$pc")"; src="commit $pc (by trailer)"
    fi
  else
    if [[ -z "$base" ]]; then
      echo "phasekit scope: the base of iteration $n cannot be derived (no intake trailer, no artifacts/iteration-mode.json naming it, no recorded work base)" >&2
      return 1
    fi
    changed="$(_name_status_json "$base" "HEAD")"; src="$base..HEAD"
  fi
  local phases
  phases="$(awk -F'\t' 'NF { print $1 "\t" $2 "\t" $3 }' <<<"$commits" \
            | jq -R -s -c 'split("\n") | map(select(length > 0) | split("\t") | {sha: .[0], kind: .[1], phase: (.[2] // "" | if . == "" then null else . end)}) | reverse')"
  if [[ "$json" -eq 1 ]]; then
    local d; d="$(mktemp -d)"
    printf '%s\n' "$changed" > "$d/c.json"; printf '%s\n' "$phases" > "$d/p.json"
    jq -cn --arg n "$n" --arg p "$p" --arg base "$base" --arg head "$head" --arg src "$src" --arg pc "$pc" \
      --slurpfile changed "$d/c.json" --slurpfile phases "$d/p.json" \
      '{iteration: $n, phase: (if $p == "" then null else $p end), base: (if $base == "" then null else $base end),
        head: $head, phase_commit: (if $pc == "" then null else $pc end), source: $src,
        phase_commits: $phases[0], changed: $changed[0]}'
    rm -rf "$d"
    return 0
  fi
  echo "iteration $n — base ${base:-(underivable)}, HEAD ${head:0:12}"
  if [[ -n "$p" ]]; then echo "phase $p — ${pc:+commit ${pc:0:12}, }from $src"; else echo "changed $src"; fi
  jq -r '.[] | "  \(.kind)\t\(.sha[0:12])\tphase \(.phase // "?")"' <<<"$phases" | sed '1s/^/phase commits (oldest first):\n/'
  jq -r 'if length == 0 then "  (no changed paths)" else .[] | "\(.status)\t\(.path)" end' <<<"$changed"
}

_name_status_json() {
  # $1..$2 = a revision range. The changed paths as [{path, status}].
  # NUL-separated records straight into jq (a big iteration's paths exceed
  # one argument list — review round 1).
  git diff --no-renames --name-status -z "$1" "$2" 2>/dev/null \
    | jq -Rsc 'split("\u0000") | map(select(length > 0)) | [range(0; length - 1; 2) as $i | {path: .[$i + 1], status: .[$i][0:1]}]'

}

phasekit_verify() {
  # `phasekit verify [--tier fast|full]` (v0.18.0, design §2.3): run the
  # project's gate EXACTLY as the loop's commit gate will — the same verdict
  # preparation (deferral keys, stamps, the ledger, the phase-close
  # evidence), the same staging, the contracts gate, the footprint snapshot
  # and restore, the gate-pending record — then time it into the cost ledger
  # and record the verify memo for the exact tree. If the turn then ends
  # with nothing changed, the commit gate REUSES the green verdict instead
  # of running the full tier a second (light mode: a third) time — the
  # merge-queue rule: the tested tree is the landed tree.
  # A red run spends no breaker attempt and writes no phase-verify-failed.json
  # (the model's own check is not the commit gate); the output is printed.
  local req="" tier drv
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tier) req="${2:-}"; shift; [[ $# -gt 0 ]] && shift ;;
      --tier=*) req="${1#--tier=}"; shift ;;
      -h|--help) echo "usage: phasekit verify [--tier fast|full]"; return 0 ;;
      *) echo "phasekit verify: unknown argument '$1' (usage: phasekit verify [--tier fast|full])" >&2; return 2 ;;
    esac
  done
  if [[ -n "$req" && "$req" != fast && "$req" != full ]]; then
    echo "phasekit verify: --tier is fast or full, not '$req'" >&2
    return 2
  fi
  VERIFY_INVOKER=model
  VERIFY_MAX_ATTEMPTS="${VERIFY_MAX_ATTEMPTS:-3}"
  local envdiff
  envdiff="$(model_env_differences)"
  MODEL_ENV_REUSABLE=0
  if [[ -z "$envdiff" ]]; then MODEL_ENV_REUSABLE=1; fi
  tier="$(verify_memo_tier)"
  if [[ -n "$req" && "$req" != "$tier" ]]; then
    if [[ "$req" == full ]]; then
      echo "phasekit verify: the gate runs the FULL tier exactly when artifacts/project-complete.json exists (docs/QUALITY_GATES.md \"Verify budget\") — write the completion record first, then run this again." >&2
    else
      echo "phasekit verify: artifacts/project-complete.json exists, so the gate runs the FULL tier (docs/QUALITY_GATES.md \"Verify budget\") — the fast tier is the one before the record is written." >&2
    fi
    return 2
  fi
  local p90
  p90="$(cost_p90 "g_$tier")"
  if [[ -n "$p90" && "$p90" -gt 540 ]]; then
    echo "phasekit verify: skipped — the $tier tier's measured P90 is ${p90}s, longer than a tool call may run (600 s). End your turn; the loop's commit gate runs the gate itself."
    return 3
  fi
  # v0.18.1 (row 1193 (1)): a git index.lock stops `git add` with only git's
  # own "fatal: Unable to create …" and no word from this tool. Name it and
  # what to do; never remove it (a model's own git may be running — the
  # loop's commit gate stages the tree itself later).
  local lock lock_age
  lock="$(git rev-parse --git-path index.lock 2>/dev/null)" || lock=""
  if [[ -n "$lock" ]]; then
    [[ "$lock" = /* ]] || lock="$ROOT_DIR/$lock"
    if [[ -e "$lock" ]]; then
      lock_age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || date +%s) ))
      echo "phasekit verify: not run — git's index is locked: $lock exists (${lock_age}s old). Another git command is running in this repository, or one died and left the lock behind. If no git process is running (\`pgrep -a git\` shows none), remove it — rm -f '$lock' — and run phasekit verify again; otherwise wait for it to finish. Nothing was staged or run." >&2
      return 2
    fi
  fi
  prepare_verdicts_for_landing
  drv="$(evidence_driver)"
  stage_landing_tree "$drv" landing
  if [[ -n "${STAGE_LOCKED:-}" ]]; then
    git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
    echo "phasekit verify: not run — git's index is locked ($STAGE_LOCKED) and nothing could be staged; the gate would judge a stale index. If no git process is running (\`pgrep -a git\` shows none), remove it — rm -f '$STAGE_LOCKED' — and run phasekit verify again." >&2
    return 2
  fi
  if [[ -n "${STAGE_FAILED:-}" ]]; then
    git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
    echo "phasekit verify: RED — git cannot stage: $STAGE_FAILED ($(printf '%s' "$STAGE_ERR" | head -n 2 | tr '\n' ' ')). Make them stageable (an empty or nested repository: remove its .git or move it outside the tree; an unreadable file: fix its permissions) or remove them; the loop's commit stages everything else it finds." >&2
    return 1
  fi
  if [[ -n "$drv" ]]; then write_phase_evidence "$drv"; fi
  echo "phasekit verify: the tree is staged exactly as the commit will stage it; running the gate ($tier tier)."
  if [[ "$MODEL_ENV_REUSABLE" != 1 ]]; then
    echo "phasekit verify: this shell's environment is not the loop's ($envdiff) — the verdict is shown, not memoised; the commit gate runs its own."
  fi
  local vrc=0
  run_verify_gate || vrc=$?
  # The verdict artifacts go back to unstaged (the memo already holds the
  # tree; the commit re-stages the same bytes): a record the model deletes
  # after this call can never be left `AD` (review round 2).
  git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
  if [[ "$vrc" -eq 0 ]]; then
    if [[ "$MODEL_ENV_REUSABLE" == 1 ]]; then
      echo "phasekit verify: GREEN ($tier tier). End your turn now and change nothing — the loop's commit gate reuses this verdict for this exact tree."
    else
      echo "phasekit verify: GREEN ($tier tier), not reusable (see above) — end your turn; the commit gate runs the gate itself."
    fi
    return 0
  fi
  echo "phasekit verify: RED ($tier tier) — fix what the output above names, then run it again." >&2
  return 1
}

write_loop_env_snapshot() {
  # The loop's environment as NAME<TAB>sha256(value)[:16] — never a value —
  # so a model's `phasekit verify` can tell whether its gate ran in the
  # loop's environment before its verdict is memoised for the commit gate.
  # Best-effort: no snapshot = no reuse (the gate simply runs again).
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || return 0
  # Keyed by a per-session secret that lives only in the processes'
  # environment, never in the file: a snapshot a hard kill leaves behind
  # cannot be dictionary-attacked for a low-entropy value (review round 2).
  PHASEKIT_ENV_KEY="$(head -c 32 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  export PHASEKIT_ENV_KEY
  python3 - "$ARTIFACTS_DIR/logs/.loop-env" <<'ENV_PY' 2>/dev/null || rm -f "$ARTIFACTS_DIR/logs/.loop-env"
import hmac, os, sys
key = os.environb.get(b"PHASEKIT_ENV_KEY", b"")
with open(sys.argv[1], "w") as f:
    for k in sorted(os.environb):
        f.write(k.decode("utf-8", "replace") + "\t" + hmac.new(key, os.environb[k], "sha256").hexdigest()[:16] + "\n")
ENV_PY
  LOOP_ENV_WRITTEN=1
  return 0
}

model_env_differences() {
  # Names (never values) of the variables in which this shell's environment
  # differs from the loop's snapshot, minus what the harness itself sets on
  # a Bash tool call (probed in scaffold-runner, claude 2.1.282: CLAUDECODE,
  # CLAUDE_CODE_*, CLAUDE_PID, AI_AGENT, GIT_EDITOR, COREPACK_ENABLE_AUTO_PIN,
  # NoDefaultCurrentDirectoryInExePath added; TZ removed; SHLVL changed) and
  # what the loop sets per invocation. Prints "(no loop snapshot)" when there
  # is nothing to compare with.
  local snap="$ARTIFACTS_DIR/logs/.loop-env"
  if [[ ! -s "$snap" ]]; then echo "(no loop snapshot)"; return 0; fi
  python3 - "$snap" <<'ENV_PY' 2>/dev/null || echo "(comparison unavailable)"
import hmac, os, sys
IGNORE = {"PWD", "OLDPWD", "SHLVL", "_", "TZ", "CLAUDE_MODE", "PHASEKIT_ITER", "PHASEKIT_RETRY_ATTEMPT",
          "ANTHROPIC_MODEL", "AI_AGENT", "CLAUDECODE", "CLAUDE_PID", "COREPACK_ENABLE_AUTO_PIN",
          "GIT_EDITOR", "NoDefaultCurrentDirectoryInExePath"}
def ignored(k):
    return k in IGNORE or k.startswith("CLAUDE_CODE_")
loop = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    k, _, h = line.rstrip("\n").partition("\t")
    loop[k] = h
key = os.environb.get(b"PHASEKIT_ENV_KEY", b"")
mine = {k.decode("utf-8", "replace"): hmac.new(key, v, "sha256").hexdigest()[:16] for k, v in os.environb.items()}
diff = sorted(k for k in set(loop) | set(mine) if not ignored(k) and loop.get(k) != mine.get(k))
print(" ".join(diff))
ENV_PY
}

took_control_this_iteration() {
  # Did the deadline watchdog end a turn during this iteration (v0.18.0)?
  local y="$ARTIFACTS_DIR/logs/.deadline-yield"
  [[ -f "$y" && -n "${ITER_START_MARKER:-}" && -f "${ITER_START_MARKER:-}" && "$y" -nt "$ITER_START_MARKER" ]]
}

took_control_inferred() {
  # v0.18.1 (row 1193 (3), v0.18.0 review round 11): the watchdog could not
  # write its marker (a full disk, an unwritable logs/) and ended the turn
  # anyway; left to the marker alone, the SIGTERM's exit code read as a CLI
  # failure and was retried. The loop knows its own plan: a turn that
  # started before a take-control instant, ended after it, and ended
  # non-zero was taken — read as taken, and said so (loudly: the marker's
  # absence is a fault worth seeing). $1 = the turn's start (epoch s),
  # $2 = build | review (the light build turn has its own earlier instant),
  # $3 = the turn's exit code. A turn that yielded (0) is never inferred.
  local started="$1" role="$2" rc="${3:-0}" now t inst
  [[ "$rc" != 0 && -n "${SESSION_DEADLINE:-}" && "$started" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  for t in "$( [[ "$role" == build ]] && echo "${DEADLINE_BUILDER_TAKE_CONTROL:-0}" || echo 0)" "${DEADLINE_TAKE_CONTROL:-0}"; do
    [[ "$t" =~ ^[0-9]+$ && "$t" -gt 0 ]] || continue
    inst=$((SESSION_DEADLINE - t))
    if [[ "$started" -lt "$inst" && "$now" -ge "$inst" ]]; then
      echo "deadline watchdog: took control (inferred — the take-control marker artifacts/logs/.deadline-yield is missing; the turn ended with exit $rc after the T-${t}s take-control point, so the watchdog ended it; see artifacts/logs/deadline-watchdog.log) — not a CLI failure, not retried (v0.18.1)." >&2
      return 0
    fi
  done
  return 1
}

release_stale_index_lock() {
  # Right after a turn the watchdog ended: the claude process and its tool
  # children are gone (probed in scaffold-runner, 2026-09-28) and the loop
  # runs no git of its own in this window, so an index.lock now is a dead
  # writer's — left, it refuses every commit that follows (a stale lock
  # blocked round-clock's upgrade commits for eight days). Named, removed.
  local lock
  lock="$(git rev-parse --git-path index.lock 2>/dev/null)" || return 0
  [[ "$lock" = /* ]] || lock="$ROOT_DIR/$lock"
  # Only a lock older than 10 s: a process that outlived the turn (a
  # backgrounded call) holds git's lock for moments at a time, never that long.
  local age
  [[ -f "$lock" ]] || return 0
  age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || date +%s) ))
  [[ "$age" -ge 10 ]] || return 0
  if rm -f "$lock" 2>/dev/null; then
    echo "${1:-deadline watchdog}: removed $lock (${age}s old) — its writer is gone" >&2
  fi
  return 0
}

git_add_all() {
  # `git add -A` for the loop's own commits (the landing's stage_landing_tree,
  # the wrap-up), ONE retry rule (v0.18.2, review round 5): the loop runs no
  # other git while it stages, so an index.lock that stays is a dead
  # writer's — retried for ~14 s, long enough for a lock to age past
  # release_stale_index_lock's 10 s and be removed. Prints git's error of the
  # last attempt; returns git's status.
  local _t err=""
  for _t in 1 2 3 4 5 6 7 8; do
    if err="$(git add -A 2>&1)"; then return 0; fi
    [[ "$err" == *index.lock* ]] || { printf '%s' "$err"; return 1; }
    # Never inside a live model turn (`phasekit verify`): its own git may
    # hold the lock (review round 6) — there the lock is the model's to judge.
    [[ "${VERIFY_INVOKER:-loop}" == model ]] && { printf '%s' "$err"; return 1; }
    release_stale_index_lock "run-until-done (staging)"
    [[ "$_t" -lt 8 ]] && sleep 2
  done
  printf '%s' "$err"
  return 1
}

record_close_out_sample() {
  # The model's close-out span (the cost model's M): the main agent was told
  # to wrap up (the nudge hook's marker, fresh this iteration) and its turn
  # has now ended on its own. Sampled only from turns that yielded.
  local mk="$ARTIFACTS_DIR/.wrapup-nudge-sent" t
  [[ -f "$mk" && -n "${ITER_START_MARKER:-}" && -f "${ITER_START_MARKER:-}" && "$mk" -nt "$ITER_START_MARKER" ]] || return 0
  t="$(stat -c %Y "$mk" 2>/dev/null)" || return 0
  [[ "$t" =~ ^[0-9]+$ ]] || return 0
  cost_sample m "$(( $(date +%s) - t ))"
}

hermetic_tests_advisory() {
  # v0.18.0 (design §5): a test reads the tree and declared fixtures, never
  # git history — a fact about the past is an evidence file. The ADOPTED
  # rule, not Bazel's enforcement: enforcement would refuse, and refusing is
  # a stall. So after the gate the loop names, ONCE per session, the test
  # files that appear to read the project's history (Danger's warn(), never
  # fail(): no red, no row). Tracked files under a test path; a history
  # read = a rev-list/log with --grep or HEAD, or a `show <rev>:<path>`.
  [[ "${HERMETIC_ADVISED:-0}" == 1 ]] && return 0
  HERMETIC_ADVISED=1
  local re hits n first
  # A history read, in a test's own words: rev-list / log with --grep or a
  # HEAD ref; `show <rev>:<path>` (a shell form, or a quoted argument
  # holding "rev:path"); blame; `diff <rev>`. HEAD must stand alone
  # (HEAD_COUNT is a name, not a ref — review round 1). Declared as
  # contracts/interface.json facts.hermetic_tests (v0.18.3).
  re=$'(rev-list|git log|[\'"]log[\'"]).{0,120}(--grep|HEAD([^A-Za-z0-9_]|$))|git show [^ -][^ ]*:|[\'"]show[\'"],[[:space:]]*[`\'"][^`\'"]*:|git blame|[\'"]blame[\'"]|git diff [A-Za-z0-9$~^{][^ ]*|[\'"]diff[\'"],[[:space:]]*[`\'"][^-`\'"]'
  hits="$(cd "$ROOT_DIR" && git ls-files 2>/dev/null \
          | grep -E '(^|/)(tests?|spec|__tests__)/|\.(test|spec)\.[A-Za-z]+$|_test\.[A-Za-z]+$|(^|/)test_[^/]*\.py$' \
          | grep -v -E '(^|/)node_modules/' \
          | xargs -r -d '\n' grep -l -E "$re" -- 2>/dev/null)" || true
  [[ -n "$hits" ]] || return 0
  n="$(printf '%s\n' "$hits" | wc -l | tr -d ' ')"
  first="$(printf '%s\n' "$hits" | head -n 5 | paste -sd, - | sed 's/,/, /g')"
  echo "ADVISORY: $n test files appear to read git history ($first$( [[ "$n" -gt 5 ]] && echo ", …")) — a test reads the tree and declared fixtures, never the project's history; a fact about the past is an evidence file (artifacts/iterations/<N>/<phase>.json). See docs/QUALITY_GATES.md \"Hermetic tests\" (a test over a scratch repository it builds itself is fine). Advisory only: the gate is unchanged."
  return 0
}

scaffold_reads_advisory() {
  # v0.18.3 (queue row 1194; docs/QUALITY_GATES.md "Tests read the declared
  # surface"): a test reads the project's tree and phasekit's DECLARED
  # surface, never scaffold-owned files. Adopted, not enforced — like the
  # hermetic advisory above: once per session, after the gate, the loop names
  # the test files that read a scaffold-owned file and RECORDS them
  # (boundary-state.json `scaffold_reads`, [] when none) so a supervisor can
  # act on and count them. Never a red gate, never a refusal; any failure of
  # the scan is silent (nothing recorded).
  [[ "${SCAFFOLD_READS_ADVISED:-0}" == 1 ]] && return 0
  SCAFFOLD_READS_ADVISED=1
  local tool="$ROOT_DIR/scripts/phasekit-surface.py" j line
  [[ -f "$tool" ]] || return 0
  # scaffold_reads_at says when; a scan that failed records null (never a
  # stale list mistaken for a current one)
  j="$(cd "$ROOT_DIR" && timeout 60 python3 "$tool" scaffold-reads --json . 2>/dev/null)" || j=""
  if ! jq -e '(.scaffold_reads | type) == "array"' <<<"$j" >/dev/null 2>&1; then
    _boundary_write '.scaffold_reads = null | .scaffold_reads_at = $now'
    return 0
  fi
  _boundary_write '.scaffold_reads = $r | .scaffold_reads_at = $now' --argjson r "$(jq -c '.scaffold_reads' <<<"$j")"
  line="$(jq -r '.line // ""' <<<"$j" 2>/dev/null)" || line=""
  if [[ -n "$line" ]]; then echo "$line"; fi
  return 0
}

# --- the landing sequence -----------------------------------------------------

_boundary_tree_clean() {
  # The tree is clean apart from the loop's own transients, logs and batons.
  [[ -z "$(_boundary_dirty_paths | head -n1)" ]]
}

_boundary_dirty_paths() {
  # One line per path that keeps the tree from being clean ("XY path", as
  # `git status --porcelain` prints it), the loop's own transients, logs,
  # batons, scratch and derived state excepted — the ONE list the rest, the
  # completion's whole-tree check (v0.18.2) and the squash's exactness check
  # read.
  # Content INSIDE a submodule is not this repository's tree — no commit of
  # this repository can carry it — so `--ignore-submodules=dirty` (a moved
  # submodule commit still counts). A custom wrap-up sentinel inside the tree
  # is the loop's own file (review round 1, M1).
  local line path sig sentinel_rel=""
  if [[ -n "${WRAPUP_SENTINEL:-}" ]]; then
    sentinel_rel="$(realpath -m --relative-to="$ROOT_DIR" "$WRAPUP_SENTINEL" 2>/dev/null)" || sentinel_rel=""
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    path="${line:3}"; path="${path#\"}"; path="${path%\"}"
    [[ -n "$sentinel_rel" && "$path" == "$sentinel_rel" ]] && continue
    case "$path" in
      artifacts/logs/*|artifacts/scratch/*|artifacts/session-handoff.json|artifacts/session-interrupted.json|artifacts/wrapup-requested) continue ;;
      artifacts/project-complete.json)
        # v0.18.0: a carried record that claims nothing is deliberately on
        # disk (the repair turn's to edit), not unlanded work of this boundary.
        completion_record_rides || continue ;;
      artifacts/deferrals.json|artifacts/iterations/*/*.json|artifacts/iterations/*/|artifacts/iterations/)
        # v0.18.0: derived state a draft left; the next landing recomputes it.
        [[ "${line:0:2}" == "??" || "${line:0:2}" == " M" ]] && continue ;;
    esac
    for sig in "${TRANSIENT_SIGNALS[@]}"; do [[ "$path" == "artifacts/$sig" ]] && continue 2; done
    printf '%s\n' "$line"
  done < <(git status --porcelain --ignore-submodules=dirty 2>/dev/null)
  return 0
}

_boundary_synthesize_completion() {
  # Step 3's action when the approval said final_phase but the session never
  # wrote project-complete.json (2026-09-08 06:22: approved, squashed, killed;
  # the next session blocked "no next phase"). Derived from the landed
  # approval — summary and deferrals carried, provenance named.
  local ap="$ARTIFACTS_DIR/phase-approval.json"
  [[ -f "$ap" ]] || return 1
  # Deterministic on purpose (no wall-clock field): a re-synthesis after a
  # disarm or a cleanup yields the same bytes, so the verify memo can match
  # the tree it already judged (round-2 F4). The approval's `iteration` rides
  # verbatim (v0.14.7, orchestrator #655): a record that names no iteration
  # claims nothing to a supervisor — completed_at stayed null for every
  # loop-synthesized completion and the consumer's own commit gate refused
  # the commit this step was landing. `null` only when the approval has none.
  # v0.18.5: written in the record's one key order (_completion_key_order_jq).
  jq "$(_completion_key_order_jq)"' {
      done: true,
      summary: ("Project complete — final phase " + ((.phase // "unknown") | tostring) + " approved: " + ((.summary // "") | tostring)),
      suggested_commit_message: ("chore(workflow): project completion record (final phase " + ((.phase // "unknown") | tostring) + " approved)"),
      deferrals: (.deferrals // []),
      final_phase: (.phase // "unknown"),
      iteration: (.iteration // null),
      recorded_by: "phasekit run-until-done.sh — boundary-state step 3 (the approval carried final_phase: true and no completion record existed)"
    } | completion_key_order' "$ap" > "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || return 1
  echo "boundary-state: the approval carries final_phase: true and no completion record exists — recorded artifacts/project-complete.json from it (step 3)."
  return 0
}

_boundary_derive() {
  # What disk says this boundary is: "<phase> <final>" — the approval's phase
  # and final_phase flag, a completion record on disk making it final.
  local ap="$ARTIFACTS_DIR/phase-approval.json" phase="" final=false
  if [[ -f "$ap" ]]; then
    phase="$(jq -r '.phase // empty' "$ap" 2>/dev/null)" || phase=""
    if [[ "$(jq -r '.final_phase // false' "$ap" 2>/dev/null)" == "true" ]]; then
      # A fresh (never-landed) final approval is final. A LANDED one is final
      # only while nothing since it has recorded a completion — a resumed
      # project's approval still says final_phase: true, and a walk that
      # trusted the flag alone re-recorded "complete" over new work (round-1
      # BLOCKER 1; round-2 F1 with the record absent).
      if artifact_never_landed "$ap" || approval_final_unrecorded; then final=true; fi
    fi
  fi
  if completion_record_claims; then final=true; fi
  if [[ -z "$phase" ]]; then
    if [[ "$final" == true ]]; then phase=project; else phase=unknown; fi
  fi
  # final first: `read -r final phase` keeps a phase name with spaces whole.
  echo "$final $phase"
}

boundary_prove() {
  # $1 = step. 0 iff git/disk prove the step done. Pure: writes nothing.
  local step="$1"
  case "$step" in
    1) # an approval-class artifact is on disk AND the record has read it
       # (phase + final) — presence alone proved nothing, and a proof that is
       # always true would skip the derivation (the generated test's first
       # catch, 2026-09-08).
       [[ -f "$ARTIFACTS_DIR/phase-approval.json" || -f "$ARTIFACTS_DIR/project-complete.json" ]] || return 1
       local d_phase d_final
       read -r d_final d_phase <<<"$(_boundary_derive)"
       [[ "$(boundary_get '.phase // empty')" == "$d_phase" && "$(boundary_get '.final // false')" == "$d_final" ]] ;;
    2) # the phase approval landed under its own message — or step 2's action
       # declined a STALE one (v0.12.3 freshness rule) and the completion
       # commit of step 3 carries it instead. At the in-loop approval site a
       # FRESH artifact is a verdict even when byte-identical to HEAD (the
       # v0.6.0 mtime rule): the action runs once before cleanliness counts
       # as proof, else new work under an unchanged approval is stranded
       # (review finding 4).
       if [[ "${BOUNDARY_WALK_CONTEXT:-}" == "iteration" && "${BOUNDARY_STEP2_ATTEMPTED:-0}" != 1 ]]; then return 1; fi
       ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" \
         || { [[ "${BOUNDARY_APPROVAL_RIDES_COMPLETION:-0}" == 1 ]] && boundary_final; } ;;
    3) if boundary_final; then
         [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] \
           && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json" \
           && ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" || return 1
         # v0.18.2 (row 1233): the WHOLE tree, whoever committed the record —
         # a committed record over uncommitted work is not a recorded
         # completion (a record-only commit over older work; a review that
         # fixed files and left the record's bytes as they were). The action
         # runs once per walk; after it, what it could not stage is the
         # rest's to name.
         [[ "${BOUNDARY_STEP3_ATTEMPTED:-0}" == 1 ]] && return 0
         _boundary_tree_clean
       else return 0; fi ;;
    4) # Squash mode: the target carries the approval-class record. Plain
       # mode (v0.18.2): the branch IS the target, so step 4 is "HEAD's tree
       # passed the verify gate" — a loop-gated commit by its trailer, or the
       # gate run by this step's action (a model's own commit is gated
       # before the boundary counts; row 1233's plain shape).
       if squash_mode; then ! squash_pending; else plain_head_verified; fi ;;
    5) if squash_mode; then git merge-base --is-ancestor "refs/heads/$SQUASH_TARGET" HEAD 2>/dev/null; else return 0; fi ;;
    6) return 0 ;;
    7) _boundary_tree_clean || return 1
       [[ -f "$ARTIFACTS_DIR/session-interrupted.json" ]] && return 1
       if boundary_final; then
         if squash_mode; then [[ "$(current_branch)" == "$SQUASH_TARGET" ]] || return 1; fi
         [[ -f "$ARTIFACTS_DIR/session-handoff.json" ]] && ! git ls-files --error-unmatch -- "$ARTIFACTS_DIR/session-handoff.json" >/dev/null 2>&1 && return 1
       fi
       return 0 ;;
    *) return 1 ;;
  esac
}

_boundary_sha_for() {
  case "$1" in
    2) # proven by exception (a stale approval rides the completion) names no commit
       if artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then :; else git rev-parse HEAD 2>/dev/null || true; fi ;;
    3|5|7) git rev-parse HEAD 2>/dev/null || true ;;
    4) if squash_mode; then git rev-parse "refs/heads/$SQUASH_TARGET" 2>/dev/null || true; fi ;;
    *) : ;;
  esac
}

boundary_do() {
  # $1 = step, $2 = context, $3 = any_age (1: an uncommitted approval of any
  # age commits under its own message — the stranded-at-start pairing; 0: only
  # a fresh one does, and a stale one rides the completion sweep).
  # Returns the action's rc: 0 done, 2 nothing to commit, 1 refused/red,
  # 4 light-mode escalation.
  local step="$1" context="$2" any_age="$3" rc=0 msg
  case "$step" in
    1)
      # v0.18.0: a final approval's boundary owns its record — a carried
      # record nobody re-claimed is not it.
      if [[ "$(jq -r '.final_phase // false' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)" == "true" ]] \
         && artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
        drop_unclaimed_carried_record
      fi
      local phase final
      read -r final phase <<<"$(_boundary_derive)"
      _boundary_write '.phase = $phase | .final = ($final == "true") | .context = $ctx' \
        --arg phase "$phase" --arg final "$final" --arg ctx "$context"
      if [[ "$final" == true && ! -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
        _boundary_synthesize_completion || return 1
      fi
      return 0 ;;
    2)
      if [[ "$any_age" != 1 ]] \
         && ! artifact_written_this_iteration "$ARTIFACTS_DIR/phase-approval.json" \
         && [[ "${PENDING_COMMIT_RETRY:-}" != "phase-approval" ]]; then
        echo "boundary-state: the uncommitted phase-approval.json predates this iteration — it rides the completion commit (v0.12.3 freshness rule), not its own."
        BOUNDARY_APPROVAL_RIDES_COMPLETION=1
        return 0
      fi
      BOUNDARY_APPROVAL_RIDES_COMPLETION=0
      BOUNDARY_STEP2_ATTEMPTED=1
      # A final boundary's completion record rides THIS commit (one commit,
      # one squash, one verify) — synthesized here when the record on disk
      # was disarmed or never written.
      if boundary_final && [[ ! -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
        _boundary_synthesize_completion || return 1
      fi
      if [[ "$context" == "iteration" ]]; then
        # The site proved freshness (written this iteration, or the retry
        # marker): the artifact drives the commit whatever its bytes — the
        # gate inside returns 2 when nothing substantive is staged.
        echo "Phase approval artifact drives the commit (fresh this iteration)."
        commit_from_artifact "$ARTIFACTS_DIR/phase-approval.json" "chore(workflow): approve completed phase" || rc=$?
        return "$rc"
      fi
      commit_pending_approval_first || rc=$?
      return "$rc" ;;
    3)
      if [[ ! -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
        # The record says final but the completion is gone (an iteration
        # cleanup after a red verify, or a kill before it was written): the
        # approval on disk must be the boundary's own phase, else this record
        # is stale and nothing is synthesized from it.
        local rp ap_phase
        rp="$(boundary_get '.phase // empty')"
        ap_phase="$(jq -r '.phase // empty' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)" || ap_phase=""
        if [[ -n "$rp" && "$rp" != "$ap_phase" ]]; then
          echo "boundary-state: record names phase '$rp' as final but the approval on disk is '$ap_phase' — not synthesizing a completion record from a stale record" >&2
          return 1
        fi
        if ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" && ! approval_final_unrecorded; then
          echo "boundary-state: a commit since this approval landed already touched project-complete.json — a resumed project, not an unrecorded completion; not synthesizing" >&2
          return 1
        fi
        _boundary_synthesize_completion || return 1
      fi
      BOUNDARY_STEP3_ATTEMPTED=1
      if completion_landed_at_head; then
        # v0.18.2 (row 1233): the record is committed — by a model's own
        # commit, a hand, a killed session — and the tree is not. The rest
        # lands through the loop's own verify-gated completion commit (the
        # record is unchanged, so the commit carries exactly what the first
        # one did not); if the gate refuses it, it is NAMED (boundary-state
        # `unlanded`, stderr) and the walk stops here as for any red
        # completion — the next turn repairs it; the work is never discarded
        # and a red tree never reaches the target.
        land_completion_leftovers || rc=$?
        return "$rc"
      fi
      commit_from_artifact \
        "$ARTIFACTS_DIR/project-complete.json" \
        "chore(workflow): final session work + project completion record" || rc=$?
      return "$rc" ;;
    4)
      if ! squash_mode; then
        plain_verify_head || rc=$?
        return "$rc"
      fi
      # verified=0: the memo decides whether the gate re-runs for this tree.
      catchup_squash 0 || rc=$?
      return "$rc" ;;
    5)
      repair_half_squash || true
      if ! git merge-base --is-ancestor "refs/heads/$SQUASH_TARGET" HEAD 2>/dev/null; then
        write_branch_integrity_block \
          "'$SQUASH_TARGET' moved out-of-band after the squash (its tip is not an ancestor of the work branch)" \
          "git merge $SQUASH_TARGET into the work branch by hand (resolve conflicts, re-verify), then re-run"
        return 1
      fi
      return 0 ;;
    6)
      local rd="$ARTIFACTS_DIR/ready-to-deploy.json" present=false mtime=""
      if [[ -f "$rd" ]]; then present=true; mtime="$(date -u -r "$rd" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"; fi
      _boundary_write '.deploy = {ready_to_deploy_present: ($present == "true"), ready_to_deploy_mtime: (if $mtime == "" then null else $mtime end), observed_at: $now}' \
        --arg present "$present" --arg mtime "$mtime"
      return 0 ;;
    7)
      # Nothing is in flight once a boundary rests: this iteration's dead-man
      # baton comes down here (a kill between here and the next iteration
      # start then leaves NO baton — which is the truth). The EXIT trap's own
      # clear stays for checkpoint iterations, which never reach this step.
      rm -f "$ARTIFACTS_DIR/session-interrupted.json" 2>/dev/null || true
      if boundary_final; then
        clear_consumed_batons_at_completion
        rest_on_target
        # v0.18.2: a rested final boundary carries nothing unlanded
        if [[ -n "$(boundary_get '.unlanded // empty | tostring')" ]]; then _boundary_write 'del(.unlanded)'; fi
      fi
      return 0 ;;
  esac
  return 1
}

land_completion_leftovers() {
  # Step 3's action when the completion record is already committed and the
  # tree is not clean (v0.18.2, row 1233 shape 1). Returns commit_from_artifact's rc.
  local paths rc=0 cc head0 t0
  paths="$(_boundary_dirty_paths | sed -E 's/^.. //; s/^"//; s/"$//' | head -n 50 | paste -sd, - | sed 's/,/, /g')"
  head0="$(git rev-parse -q --verify HEAD 2>/dev/null)" || head0=""
  t0="$(date +%s)"
  cc="$(git log -1 --format=%h HEAD -- artifacts/project-complete.json 2>/dev/null)" || cc=""
  echo "boundary-state: the completion record is committed (${cc:-?}) but the tree is not — the rest of the iteration's work lands through the loop's verify-gated completion commit (the loop owns every commit; v0.18.2): ${paths:-(unnamed)}"
  commit_from_artifact \
    "$ARTIFACTS_DIR/project-complete.json" \
    "chore(workflow): the rest of the iteration's work" \
    "The completion record was already committed (${cc:-?}); this commit lands what that commit did not carry (phasekit v0.18.2: at a completion the loop checks the whole tree, whoever committed)." || rc=$?
  # The rest landed (the commit happened) even when a later step refused —
  # a squash refused for branch integrity writes its own blocker: nothing
  # is unlanded then (review round 1, m4).
  # "Nothing substantive staged" while the tree is still dirty and HEAD did
  # not move is NOT landed (review round 4, MAJOR: nothing could be staged).
  if [[ "$rc" -eq 2 && "$(git rev-parse -q --verify HEAD 2>/dev/null)" == "$head0" \
        && -n "$(_boundary_dirty_paths | head -n1)" ]]; then
    rc=1
  fi
  if [[ "$rc" -eq 0 || "$rc" -eq 2 || "$(git rev-parse -q --verify HEAD 2>/dev/null)" != "$head0" ]]; then
    _boundary_write 'del(.unlanded)'
    return "$rc"
  fi
  case "$rc" in
    *)
      local why="a commit gate refused it"
      if [[ "$rc" -eq 4 ]]; then why="light mode: the change touches scaffold-class files (escalated)"
      elif [[ -f "$ARTIFACTS_DIR/scope-refusal.json" ]]; then why="a commit gate refused it (artifacts/scope-refusal.json)"
      elif [[ -f "$ARTIFACTS_DIR/phase-verify-failed.json" && "$(stat -c %Y "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null || echo 0)" -ge "$t0" ]]; then
        why="refused by $(jq -r '.label // "the verify gate"' "$ARTIFACTS_DIR/phase-verify-failed.json" 2>/dev/null | cut -c1-80) — artifacts/phase-verify-failed.json says why"
      fi
      local json
      json="$(_boundary_dirty_paths | sed -E 's/^.. //; s/^"//; s/"$//' | jq -Rsc 'split("\n") | map(select(length > 0))')" || json='[]'
      _boundary_write '.unlanded = {paths: $p, reason: $why, completion_commit: (if $cc == "" then null else $cc end), at: $now}' \
        --argjson p "${json:-[]}" --arg why "$why" --arg cc "$(git log -1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)"
      echo "boundary-state: NOT landed — the work the completion commit ${cc:-?} did not carry could not be verified ($why); it stays in the tree, named in boundary-state.json \`unlanded\`, and the walk stops at step 3 (never a red tree on the target; the next turn repairs it): ${paths:-(unnamed)}" >&2 ;;
  esac
  return "$rc"
}

plain_head_verified() {
  # Plain mode's step 4 (v0.18.2): HEAD's tree passed the verify gate —
  # proven by the loop's own green verdict for EXACTLY that tree (the verify
  # memo: every loop commit's gate records the tree it commits), never by a
  # trailer a copied or amended message could carry (review round 1, m3).
  # Anything else — a model's own commit, a hand commit, the watchdog's wip,
  # an intake, a verdict older than the memo's TTL — is gated by the
  # action, once per walk.
  [[ "${BOUNDARY_STEP4_VERIFIED:-0}" == 1 ]] && return 0
  git rev-parse -q --verify HEAD >/dev/null 2>&1 || return 0
  # (Tree and freshness only, never the gate's command: a tree the loop's
  # gate passed stays passed when a later session's PHASEKIT_VERIFY_CMD
  # differs — the next approval-class commit is gated with that command.)
  local tree memo
  tree="$(git rev-parse "HEAD^{tree}" 2>/dev/null)" || return 1
  memo="$(boundary_get '.verify_memo.tree_sha // empty')"
  [[ -n "$memo" && "$memo" == "$tree" ]] || return 1
  _verify_memo_fresh "$(boundary_get '.verify_memo.passed_at // empty')"
}

plain_verify_head() {
  # Plain step 4's action: run the gate on HEAD's tree — only when the tree
  # IS HEAD's (the gate judges exactly what the target carries); otherwise
  # stop, named: the next verify-gated commit carries the difference.
  local dirty
  dirty="$(_boundary_dirty_paths | sed -E 's/^.. //' | head -n 8 | paste -sd' ' -)"
  if [[ -n "$dirty" ]]; then
    if boundary_final; then
      write_landing_block "$dirty"
      return 1
    fi
    echo "boundary-state: HEAD ($(git rev-parse --short HEAD 2>/dev/null)) was not made by the loop's verify-gated commit, and the worktree differs from it ($dirty) — its tree cannot be judged exactly here; the boundary waits for the next verify-gated commit (v0.18.2)." >&2
    return 1
  fi
  echo "boundary-state: HEAD ($(git rev-parse --short HEAD 2>/dev/null)) was not made by the loop's verify-gated commit — running the verify gate on its tree before the boundary counts (v0.18.2)."
  run_verify_gate || return 1
  BOUNDARY_STEP4_VERIFIED=1
  return 0
}

kept_out_claim_only() {
  # v0.18.0 (§3.1): HEAD is an unverified commit (`Phasekit-Kind: wip` — the
  # watchdog's last-resort commit or the wrap-up fall-through) that kept the
  # deploy claim OUT, and the claim is the tree's only change since: the
  # killed session's coherent tree is HEAD plus that claim.
  [[ "$(git log -1 --format='%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)' HEAD 2>/dev/null)" == wip ]] || return 1
  local line path sig found=0
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    path="${line:3}"; path="${path#\"}"; path="${path%\"}"
    case "$path" in
      artifacts/logs/*|artifacts/scratch/*|artifacts/session-handoff.json|artifacts/session-interrupted.json|artifacts/wrapup-requested) continue ;;
      artifacts/ready-to-deploy.json) found=1; continue ;;
      # the wip keeps these out too (review round 8); they land beside the
      # claim, behind the post-verify gates' credential scan
      docs/LEARNINGS*.md) continue ;;
    esac
    for sig in "${TRANSIENT_SIGNALS[@]}"; do [[ "$path" == "artifacts/$sig" ]] && continue 2; done
    # the loop's own derived state is recomputed by the landing, never work
    [[ "$path" == artifacts/deferrals.json || "$path" == artifacts/iterations/*/*.json ]] && continue
    return 1
  done < <(git status --porcelain --untracked-files=all 2>/dev/null)
  [[ "$found" -eq 1 ]]
}

land_kept_out_claim() {
  # Loop start, before the walk: the claim the last unverified commit kept
  # out lands WITH its tree, behind the verify gate (the gate judges the
  # worktree, which is exactly HEAD plus the claim) — so a catch-up squash
  # never lands HEAD's older claim beside the session's new evidence, and
  # the tree can rest. Red: the claim is unstaged again, the walk stops.
  echo "boundary-state: the last unverified commit kept the deploy claim out — landing it with its tree, verify-gated (v0.18.0)."
  git add -f -- "$ARTIFACTS_DIR/ready-to-deploy.json" 2>/dev/null || return 1
  git add -- "$ROOT_DIR/docs/LEARNINGS"*.md 2>/dev/null || true
  local _kc_err _kc_rc=0
  _kc_err="$(mktemp)"
  if run_verify_gate; then post_verify_commit_gates wrapup 2>"$_kc_err" || _kc_rc=$?; else _kc_rc=1; : > "$_kc_err"; fi
  cat "$_kc_err" >&2 2>/dev/null || true
  if [[ "$_kc_rc" -ne 0 ]]; then
    if grep -q '^run-until-done: REFUSED —' "$_kc_err" 2>/dev/null; then
      record_commit_refusal "commit gate (after verify)" "$(grep '^run-until-done: REFUSED —' "$_kc_err" | head -n 5)
The commit gates that run after the verify gate refused the deploy claim's landing. Fix what the line above names, then end your turn — the loop retries."
    fi
    rm -f "$_kc_err"
    git reset -q -- "$ARTIFACTS_DIR/ready-to-deploy.json" "$ROOT_DIR/docs/LEARNINGS"*.md 2>/dev/null || true
    return 1
  fi
  : > "$_kc_err"
  if ! git commit -q -m "chore(workflow): land the deploy claim the last unverified commit kept out" \
       -m "$(phasekit_trailers claim)" 2>"$_kc_err"; then
    record_commit_refusal "git commit (git refused the loop's commit)" "$(redact_credentials < "$_kc_err" | tail -n 30)
git refused the loop's commit of the kept-out deploy claim (a project git hook, the commit identity, signing). Fix what git names — never commit yourself: the loop retries."
    rm -f "$_kc_err"
    return 1
  fi
  rm -f "$_kc_err"
  clear_commit_refusal
  return 0
}

land_boundary() {
  local rc=0
  landing_enter
  _land_boundary "$@" || rc=$?
  landing_leave
  return "$rc"
}

_land_boundary() {
  # $1 = the step the caller can prove (1..7), $2 = context (iteration,
  # completion, stranded, wrapup), $3 = any_age (see boundary_do 2).
  # Returns 0 = reached `rested` with new work landed; 2 = reached `rested`
  # with nothing new to commit; 1 = stopped (verify red, a gate refused, or
  # the squash was refused — the record stays at the last proven step and
  # the refusing gate has written its own artifact); 4 = light escalation.
  local from="${1:-1}" context="${2:-iteration}" any_age="${3:-1}"
  local step rc did=0 recorded name sha walk_start walk_gate
  walk_start="$(date +%s)"; walk_gate="${VERIFY_SECONDS_ACC:-0}"
  # Walk-local state (review finding 3: a sticky flag from an earlier walk
  # once let a FRESH approval skip its own commit).
  BOUNDARY_APPROVAL_RIDES_COMPLETION=0
  BOUNDARY_STEP2_ATTEMPTED=0
  BOUNDARY_STEP3_ATTEMPTED=0   # v0.18.2
  BOUNDARY_STEP4_VERIFIED=0    # v0.18.2 (plain mode)
  BOUNDARY_WALK_CONTEXT="$context"
  # The caller's `from` is a claim the walk verifies, not a shortcut past
  # step 1: every walk derives phase/final from disk first (the catch-up
  # entry once started at 2 and never learned the phase — generated test).
  [[ -f "$BOUNDARY_STATE_FILE" ]] || boundary_begin 0
  recorded="$(boundary_step)"
  echo "boundary-state: landing from step $from (${BOUNDARY_STEP_NAMES[$from]}) — context $context; record was at step $recorded (${BOUNDARY_STEP_NAMES[$recorded]})$( [[ "$(boundary_get '.killed_after // empty')" != "" ]] && echo ", killed after step $(boundary_get '.killed_after')")."
  [[ "$context" == "stranded" ]] && _boundary_write '.recovered_at = $now'
  # Only when the walk itself makes no commit that would stage the claim
  # (a stranded verdict artifact or an unrecorded final approval is landed
  # by its own `git add -A`, the claim with it, in one gate run).
  # v0.18.0 (review round 6): without a squash target nothing re-verifies an
  # approval that rode an unverified wip (the loop's in-flight landing,
  # committed whole at the kill) — the catch-up squash does that in branch-
  # per-iteration. Plain mode: the gate runs here before the boundary counts.
  if [[ "$context" == "stranded" ]] && ! squash_mode \
     && ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" \
     && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json" \
     && { [[ "$(git log -1 --format='%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)' HEAD -- artifacts/phase-approval.json 2>/dev/null)" == wip ]] \
          || [[ "$(git log -1 --format=%s HEAD -- artifacts/phase-approval.json 2>/dev/null)" == "wip: last-resort deadline commit (phasekit deadline watchdog) — "* ]]; }; then
    echo "boundary-state: the approval rode an unverified wip — running the verify gate before its boundary counts (v0.18.0)."
    if ! run_verify_gate; then
      BOUNDARY_WALK_CONTEXT=""
      echo "boundary-state: stopped at step 0 (before the walk) — the approval that rode the wip is red; record stays at step $(boundary_step)." >&2
      return 1
    fi
  fi
  if [[ "$context" == "stranded" ]] && kept_out_claim_only \
     && ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" \
     && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json" \
     && ! approval_final_unrecorded; then
    if ! land_kept_out_claim; then
      BOUNDARY_WALK_CONTEXT=""
      echo "boundary-state: stopped at step 0 (before the walk) — the kept-out deploy claim did not pass the verify gate; it stays on disk, unstaged; record stays at step $(boundary_step)." >&2
      return 1
    fi
    did=1
  fi
  # A completion commit never carries the promoted baton (v0.14.4), and
  # neither does a loop-start recovery commit (v0.14.5): the baton is what
  # the model is about to orient from, and a phase commit that swept it would
  # put a "you were killed" note in the phase's history, to be deleted by a
  # later commit. In-loop phase commits keep today's behavior (the model has
  # already read and deleted its baton by then).
  local prior_ccip="${COMPLETION_COMMIT_IN_PROGRESS:-0}"
  if boundary_final || [[ "$context" == "stranded" ]]; then COMPLETION_COMMIT_IN_PROGRESS=1; fi
  # v0.18.1 (row 831 (b)): a completion already committed when the walk
  # begins (the model's own commit, a killed session's) — restore what was
  # written after it BEFORE any step stages the tree (an approval commit's
  # `git add -A` would otherwise land it).
  if boundary_final; then completion_residue_settle || true; fi
  local act_blob
  for (( step=1; step<=BOUNDARY_STEP_RESTED; step++ )); do
    name="${BOUNDARY_STEP_NAMES[$step]}"
    # v0.18.1: at the rest nothing written after the completion commit
    # survives in the tree — restored, named, kept under logs/ — whether or
    # not the rest would otherwise prove (a derived-state edit it tolerates
    # is still residue).
    if [[ "$step" -eq "$BOUNDARY_STEP_RESTED" ]] && boundary_final; then completion_residue_settle || true; fi
    if ! boundary_prove "$step"; then
      rc=0
      act_blob="$(completion_blob_at_head)"
      boundary_do "$step" "$context" "$any_age" || rc=$?
      case "$rc" in
        0) did=1
           # v0.18.1: the walk's own commit carried the completion record
           # (step 3, or step 2 when the approval commit swept it): the
           # tree right after it is the snapshot.
           if [[ "$step" -eq 2 || "$step" -eq 3 ]] && boundary_final \
              && [[ -n "$(completion_blob_at_head)" && "$(completion_blob_at_head)" != "$act_blob" ]]; then
             completion_snapshot_ensure || true
           fi ;;
        2) : ;;
        *) COMPLETION_COMMIT_IN_PROGRESS="$prior_ccip"; BOUNDARY_WALK_CONTEXT=""
           echo "boundary-state: stopped at step $step ($name) — rc $rc; record stays at step $(boundary_step) ($(boundary_get '.step_name // "idle"'))." >&2
           return "$rc" ;;
      esac
      # Re-evaluate `final` after step 1's action (it may have synthesized).
      [[ "$step" -eq 1 ]] && boundary_final && COMPLETION_COMMIT_IN_PROGRESS=1
      if ! boundary_prove "$step"; then
        COMPLETION_COMMIT_IN_PROGRESS="$prior_ccip"; BOUNDARY_WALK_CONTEXT=""
        echo "boundary-state: step $step ($name) could not be proven after its action — stopping; record stays at step $(boundary_step)." >&2
        return 1
      fi
    fi
    _boundary_kill_probe "$step" pre
    sha="$(_boundary_sha_for "$step")"
    boundary_advance "$step" "$sha"
    _boundary_kill_probe "$step" post
    # v0.18.1 (row 831 (b)): the completion is committed — snapshot the tree
    # at it (or keep the turn guard's), and restore anything written since
    # BEFORE the squash's gate judges the tree; step 7 settles again.
    if [[ "$step" -eq 3 ]] && boundary_final; then completion_residue_settle || true; fi
  done
  COMPLETION_COMMIT_IN_PROGRESS="$prior_ccip"; BOUNDARY_WALK_CONTEXT=""
  # v0.18.2 (review round 2, m1): a final boundary that rests carries
  # nothing unlanded, however the rest landed (step 7 proven, not acted on).
  if boundary_final && [[ -n "$(boundary_get '.unlanded // empty | tostring')" ]]; then _boundary_write 'del(.unlanded)'; fi
  echo "boundary-state: rested (step $BOUNDARY_STEP_RESTED) — $(boundary_final && echo "final boundary" || echo "phase boundary") for phase $(boundary_get '.phase // "unknown"')$( [[ "$did" -eq 1 ]] || echo " (nothing new to commit)")."
  if [[ "$did" -eq 1 ]]; then
    # v0.18.0: the landing's own cost (commit, squash, merge-back, rest) —
    # the cost model's W — is the walk's wall time minus its gate runs.
    local w_s=$(( $(date +%s) - walk_start - (${VERIFY_SECONDS_ACC:-0} - walk_gate) ))
    [[ "$w_s" -lt 0 ]] && w_s=0
    cost_sample w "$w_s"
    return 0
  fi
  return 2
}

# --- cost model (v0.18.0) -----------------------------------------------------
# foundry-meta designs/DESIGN-session-efficiency.md §2, links [A]–[D]. Every
# timeout since 2026-09-14 (31/31) logged "sentinel never observed", and at
# least 24 were killed mid close-out: the wrap-up lead was a fixed 15% of the
# session (and the supervisor computed a second, different one), while what
# close-out actually costs — the project's own gate, the landing, the
# model's own close-out — was never measured. phasekit is the one component
# that sees gate runs, turn ends and landings as they happen, so it keeps
# the numbers: artifacts/logs/cost-ledger.json (under the loop's own logs/:
# persists in the tree across sessions, never in git status, never
# committed — deliberately NOT a new member of TRANSIENT_SIGNALS, which a
# supervisor pins member for member; a fresh clone falls back to the priors),
# holding the last 20 samples per measure:
#
#   g_full, g_fast  wall time of the gate per tier (the loop's and `phasekit
#                   verify`'s)                          prior 300 s / 60 s
#   w               the loop's landing cost minus its gate (land_boundary)
#                                                       prior 60 s
#   m               the model's close-out: nudge delivered → turn yielded
#                                                       prior g_full + 120 s
#   r               light mode's final-review invocation prior g_full + 300 s
#   pass            one pass of the loop (pacing seeds from it)
#   sessions        bound, lead, take-control, exit (yielded | took-control |
#                   killed), passes — the facts a supervisor's "too heavy"
#                   threshold (fork F4) reads from the `phasekit-cost:` line.
#
# All figures are P90 (nearest rank). The one formula, in one home:
#   take-control  T_y = G_full + W + 60 s
#   lead          L   = T_y + M (+ R in light mode), clamped to
#                       [300 s, 25% of the bound] (fork F1: above the cap the
#                       project gets push-back, never a wider lead)
#   last-resort       = 60 s (unchanged, keep-fixed)
# Fork F2: the loop never changes the bound (max_minutes) — only the lead.

_cost_write() {
  # $1 = jq filter over the ledger ({} when absent); the rest are jq args.
  # Locked read-modify-write through a tmp under artifacts/logs/ (the loop,
  # `phasekit verify` and the watchdog each write it). Best-effort: a
  # ledger that cannot be written is said once and the loop goes on.
  local filter="$1"; shift
  local file="$ARTIFACTS_DIR/logs/cost-ledger.json" tmp="$ARTIFACTS_DIR/logs/.cost-ledger.$BASHPID.tmp" lock="$ARTIFACTS_DIR/logs/.cost-ledger.lock"
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  _cost_write_locked() {
    local cur='{}'
    if [[ -f "$file" ]] && jq -e 'type == "object"' "$file" >/dev/null 2>&1; then cur="$(cat "$file")"; fi
    jq "$@" "$filter" <<<"$cur" > "$tmp" 2>/dev/null && mv -f "$tmp" "$file" 2>/dev/null
  }
  local ok=0
  if command -v flock >/dev/null 2>&1; then
    { flock -w 5 9 2>/dev/null || true; _cost_write_locked "$@" && ok=1; } 9>>"$lock" 2>/dev/null || true
  else
    _cost_write_locked "$@" && ok=1
  fi
  if [[ "$ok" -eq 1 ]]; then return 0; fi
  rm -f "$tmp" 2>/dev/null || true
  if [[ "${COST_WRITE_WARNED:-0}" != 1 ]]; then
    COST_WRITE_WARNED=1
    echo "phasekit-cost: WARN — artifacts/logs/cost-ledger.json could not be written; this session's samples are lost (priors stand in)" >&2
  fi
  return 0
}

cost_sample() {
  # $1 = measure, $2 = whole seconds. Keeps the newest 20.
  [[ "${2:-}" =~ ^[0-9]+$ ]] || return 0
  _cost_write '.schema = 1 | .samples[$m] = (((.samples[$m] // []) + [($v | tonumber)]) | .[-20:])' \
    --arg m "$1" --arg v "$2"
}

cost_count() {
  local file="$ARTIFACTS_DIR/logs/cost-ledger.json" n
  n="$(jq -r --arg m "$1" '(.samples[$m] // []) | length' "$file" 2>/dev/null)" || n=0
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  echo "$n"
}

cost_p90() {
  # $1 = measure. Nearest-rank P90 of its samples; nothing when it has none.
  local file="$ARTIFACTS_DIR/logs/cost-ledger.json" v
  [[ -f "$file" ]] || return 0
  v="$(jq -r --arg m "$1" '(.samples[$m] // []) | map(select(type == "number")) | sort
        | if length == 0 then empty else .[(((length * 9) + 9) / 10 | floor) - 1] | floor end' "$file" 2>/dev/null)" || v=""
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v"
  return 0
}

cost_value() {
  # $1 = measure. Its P90, or the prior when the ledger has no sample.
  local v
  v="$(cost_p90 "$1")"
  if [[ -n "$v" ]]; then echo "$v"; return 0; fi
  case "$1" in
    g_full) echo 300 ;;
    g_fast) echo 60 ;;
    w) echo 60 ;;
    m) echo $(( $(cost_value g_full) + 120 )) ;;
    r) echo $(( $(cost_value g_full) + 300 )) ;;
    *) echo 0 ;;
  esac
}

cost_session_record() {
  # $1 = session id, $2 = a JSON object. Upserts the session by id (the
  # watchdog records "killed" seconds before the bound; a loop that then
  # exits on its own overwrites it with what really happened). Newest 20.
  _cost_write '.schema = 1 | .sessions = (((.sessions // []) | map(select(.id != $id))) + [($rec + {id: $id})] | .[-20:])' \
    --arg id "$1" --argjson rec "$2"
}

cost_session_update() {
  # $1 = session id, $2 = a JSON object merged into that session's sample.
  _cost_write '.sessions = ((.sessions // []) | map(if .id == $id then . + $rec else . end))' \
    --arg id "$1" --argjson rec "$2"
}

heavy_sessions_recent() {
  # Of the last 4 recorded sessions, how many were too heavy for their bound
  # (the F4 facts; the supervisor applies the "2 of the last 5" window with
  # the current session's own `heavy`).
  local n
  n="$(jq -r '[(.sessions // [])[-4:][] | select(.heavy == true)] | length' "$ARTIFACTS_DIR/logs/cost-ledger.json" 2>/dev/null)" || n=0
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  echo "$n"
}

# --- Deadline watchdog (v0.13.0) --------------------------------------------
# The 2026-08-26/27 strand run: five heavy first sessions in a row were killed
# at their bound (exit 124) with a full session of coherent work uncommitted —
# every one needed an out-of-band hand (operator or orchestrator) to commit the
# strand and lift a pause. Two gaps owned HERE, not by the supervisor:
#
#   (1) the wrap-up sentinel is armed by the SUPERVISOR's timer, so a
#       supervisor that arms late, arms with too short a lead for this repo's
#       landing cost, or never arms at all (deadline classes (b) and (c))
#       leaves the loop blind until the kill;
#   (2) nothing at all runs INSIDE the final minute, so the kill always
#       strands whatever the wrap-up did not land.
#
# One background watchdog closes both, armed only when the supervisor already
# tells us the kill time (PHASEKIT_SESSION_DEADLINE — no deadline, no
# watchdog, behavior unchanged):
#
#   phase 1 (self-armed lead): at deadline − LEAD, touch the same wrap-up
#     sentinel the supervisor would. Idempotent with the supervisor's own
#     touch; the nudge hook and the loop's boundary check consume it
#     unchanged. LEAD scales with the session (15% of span, clamped 300–900s)
#     instead of being a fixed number chosen for a smaller repo — the
#     post-mortem's "scale the LEAD, not just arm it".
#
#   phase 2 (last-resort strand commit): at deadline − LASTRESORT (default
#     60s), if the loop is still alive and the tree is dirty, commit the tree
#     AS A STRAND: --no-verify, transients unstaged, and — the 2026-08-27
#     04:09 incident's lesson — the deploy-arming artifacts kept OUT of the
#     commit, so an unverified mid-build ready-to-deploy.json/
#     project-complete.json can never ride a strand into a tree that looks
#     like a verified release to a deploy seam. v0.18.0: kept out means
#     unstaged, never rewritten, never deleted (_disarm_deploy_artifact): the
#     session's copies stay on disk for the next loop start's verify-gated
#     landing, which stages them with the rest of their coherent tree.
#     The dead-man baton is refreshed by
#     this independent process, so a kill path that eats the loop's own EXIT
#     trap can no longer lose it.
#
# The watchdog is a subshell: no signal-delivery assumptions (a TERM the shell
# defers while waiting on the model, a SIGKILL that runs nothing — neither
# matters; the commit already happened before the kill). Fail-open throughout.

compute_wrapup_lead() {
  # $1 = the session's span (seconds from arm to the bound). Prints four
  # numbers: "L T_y L_uncapped cap" — the sentinel lead in force, the
  # take-control lead, the lead the measurements ask for, and the cap
  # (v0.18.0, the cost model above; foundry-meta
  # designs/DESIGN-session-efficiency.md §2.2, forks F1/F2):
  #   T_y = G_full + W + 60 s              (the loop's own landing, P90)
  #   L   = T_y + M (+ R in light mode)    (the model's close-out, P90)
  #   L   clamped to [300 s, 25% of the span]; where 25% is under the 300 s
  #       floor (a session shorter than 20 minutes) half the span stands in
  #   T_y never exceeds L (control is never taken before the model was told),
  #       and a T_y at or inside the last-resort lead is dropped (0): the
  #       last-resort commit is then the next stage anyway.
  # PHASEKIT_WRAPUP_LEAD_SECONDS (a published knob for standalone use;
  # nothing in Foundry sets it) replaces L verbatim — 0 disables both the
  # self-armed sentinel and the take-control.
  local span="$1" g w m r ty lu cap l floor=300 lastresort
  g="$(cost_value g_full)"; w="$(cost_value w)"; m="$(cost_value m)"; r="$(cost_value r)"
  ty=$((g + w + 60))
  lu=$((ty + m))
  if [[ "${ITERATION_MODE:-standard}" == "light" ]]; then lu=$((lu + r)); fi
  cap=$((span * 25 / 100))
  if [[ "$cap" -lt "$floor" ]]; then
    l="$floor"; [[ "$l" -gt $((span / 2)) ]] && l=$((span / 2))
  else
    l="$lu"; [[ "$l" -lt "$floor" ]] && l="$floor"; [[ "$l" -gt "$cap" ]] && l="$cap"
  fi
  local override="${PHASEKIT_WRAPUP_LEAD_SECONDS:-}"
  if [[ -n "$override" && "$override" =~ ^[0-9]+$ ]]; then l="$override"; fi
  # Control is never taken before the model had 60 s with the nudge (a third
  # of a lead under 3 minutes; review round 2: at short bounds the capped
  # lead met T_y exactly).
  local window=60
  [[ "$l" -lt 180 ]] && window=$((l / 3))
  [[ "$ty" -gt $((l - window)) ]] && ty=$((l - window))
  lastresort="${PHASEKIT_LASTRESORT_LEAD_SECONDS:-60}"; [[ "$lastresort" =~ ^[0-9]+$ ]] || lastresort=60
  [[ "$ty" -le "$lastresort" ]] && ty=0
  echo "$l $ty $lu $cap"
}

deadline_lastresort_commit() {
  # Phase 2 body. Runs in the watchdog subshell ~LASTRESORT seconds before the
  # kill, possibly while the model still holds the tree. Every step is
  # best-effort: a failure here must only ever mean "no better off than
  # before the watchdog existed".
  #
  #   $1 = mode: "kill" (default — the watchdog, seconds before the bound) or
  #        "wrapup" (v0.14.2 — wrapup_commit falls through here when its verify
  #        gate is RED but every other commit gate passed; the session is
  #        ending by pacing or soft stop, and a labeled unverified wip on the
  #        work branch beats a dirty tree, a stall row and a resolver session).
  #        Same commit, same disarm, same baton — only the wording differs.
  local mode="${1:-kill}" why in_flight note_tail
  case "$mode" in
    wrapup)
      why="the session wrapped up (pacing or soft stop) with a RED verify gate (phasekit wrap-up fall-through)"
      in_flight="the verify gate was red when the session wrapped up; the standing work was preserved as an UNVERIFIED wip commit (v0.14.2 fall-through)"
      note_tail=" A last-resort wip commit preserved this tree at wrap-up because the verify gate was red; the next session's gates judge it." ;;
    *)
      why="the session was about to be killed at its bound"
      in_flight="an iteration was IN FLIGHT when the session was killed at its deadline; the last-resort watchdog committed the tree seconds beforehand"
      note_tail=" A last-resort wip commit preserved this tree seconds before the kill; only the final moments of work can be missing." ;;
  esac
  cd "$ROOT_DIR" 2>/dev/null || return 0
  if [[ "$mode" == kill ]] && [[ -f "$ARTIFACTS_DIR/logs/.landing-in-flight" || -f "$ARTIFACTS_DIR/.wrapup-in-progress" ]]; then
    echo "deadline watchdog: the loop's own landing or wrap-up is in flight — standing down (nothing written)"
    return 0
  fi
  [[ -n "$(git status --porcelain 2>/dev/null)" ]] || return 0
  # v0.14.9: a complete iteration has nothing to preserve — the dirt is the
  # gate's re-measurement after the completion commit, not in-flight work,
  # and a wip commit of it would be a second commit on a landed completion.
  if boundary_complete; then
    echo "deadline watchdog: the iteration is complete (record final, step $(boundary_step)) — standing down; nothing to preserve (v0.14.9)"
    return 0
  fi

  # Keep the deploy seam out BEFORE the tree can go clean: an artifact the
  # dead session wrote mid-build is unverified by definition, and a wip
  # commit that carries it re-creates the 2026-08-27 04:09 incident
  # (unverified code self-deployed off a strand commit). Re-applied inside
  # every commit attempt below — the session is still alive and can re-arm
  # between the unstage and an add. v0.18.0: unstaged, never rewritten,
  # never deleted — both files stay on disk exactly as the session wrote
  # them (_disarm_deploy_artifact).
  _disarm_deploy_artifact ready-to-deploy.json
  _disarm_deploy_artifact project-complete.json

  # Refresh the dead-man baton from OUTSIDE the loop process (the v0.10.1
  # baton was observed missing after one real kill — run 404, 2026-08-22 —
  # and this write does not depend on any trap running). Six-key schema
  # preserved; jq-update where one exists, else the minimal truthful one.
  local baton="$ARTIFACTS_DIR/session-interrupted.json" now_iso
  now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ -f "$baton" ]] && jq -e . "$baton" >/dev/null 2>&1; then
    jq --arg ts "$now_iso" --arg tail "$note_tail" \
       '.note += $tail | .ts = $ts' \
       "$baton" > "$baton.tmp" 2>/dev/null && mv "$baton.tmp" "$baton" 2>/dev/null || rm -f "$baton.tmp" 2>/dev/null || true
  else
    local next_step="audit the last wip commit as IN-PROGRESS IMPLEMENTATION from a killed session: re-derive what is verified, keep it, finish the rest or edit it back (never git checkout/reset/stash — the loop owns every commit); an uncommitted artifacts/phase-approval.json, project-complete.json or ready-to-deploy.json on disk is the session's own (the wip kept them out, unstaged) — the loop lands them verify-gated with the rest of the tree, do not delete or rebuild them (a completion whose landing was refused and never re-claimed is removed at the next start)"
    local note="dead-man baton written by the deadline watchdog (v0.13.0): the loop was killed before it could conclude. Ephemeral: delete after orienting."
    if [[ "$mode" == "wrapup" ]]; then
      next_step="audit the last wip commit as UNVERIFIED work from a session whose verify gate was red at wrap-up; the approval artifact (if any) was left on disk uncommitted for the stranded-artifact recovery to re-verify"
      note="baton written by the wrap-up fall-through (v0.14.2): the session ended by pacing or soft stop with a red verify gate. Ephemeral: delete after orienting."
    fi
    jq -n --arg ts "$now_iso" --arg in_flight "$in_flight" --arg next_step "$next_step" --arg note "$note" '{
      stopped_at_phase: "unknown",
      in_flight: $in_flight,
      verified: false,
      next_step: $next_step,
      note: $note,
      ts: $ts
    }' > "$baton" 2>/dev/null || true
  fi

  # v0.14.5: name the step the kill (or the fall-through) interrupted, so the
  # next session's first recovery line says where the sequence stood; and
  # key the deferrals of any approval-class artifact this wip may sweep, so
  # a keyless record never reaches a supervisor through the kill path.
  if command -v boundary_mark_killed >/dev/null 2>&1; then boundary_mark_killed "$mode"; fi
  if command -v normalize_deferral_keys >/dev/null 2>&1; then
    local _art
    for _art in phase-approval.json project-complete.json; do
      if artifact_never_landed "$ARTIFACTS_DIR/$_art"; then normalize_deferral_keys "$ARTIFACTS_DIR/$_art" || true; fi
    done
  fi

  # The commit. The model may hold index.lock mid-operation — bounded retry,
  # then give up open. --no-verify: this is corpse preservation, not a gated
  # release; the next session's gates judge the content.
  #
  # The disarm is re-applied INSIDE every attempt (v0.13.1, review finding 2):
  # the model is alive by hypothesis until the kill, so a single-shot restore
  # races a session that re-writes the artifact between the restore and a
  # retried add — and a COMMITTED armed artifact survives git transport,
  # which the mtime aging alone cannot protect against. The staged-clean
  # check on exactly those two paths is what makes the disarm a gate rather
  # than a hope.
  # v0.14.10 (review MAJOR-1, re-check): a verify gate may be running right
  # now — its before-snapshot is in the record. Every gate site ran
  # `git add -A` before its gate and no model holds the tree during one, so
  # the INDEX is the session's complete work and is immune to the gate's
  # worktree writes: commit it as it stands, no `git add -A` — a restore
  # here would race the still-running gate (verified: a second write after
  # a single restore rode the wip). The gate's worktree residue is the next
  # start's settle (gate_settle_pending), which restores it from this same
  # index.
  local attempt _gp_pending _gp_said=0
  for attempt in 1 2 3 4 5; do
    # Re-read per attempt: a gate that starts between attempts must not be
    # swept by the next one (review, final pass).
    _gp_pending=0
    if [[ "$mode" == kill ]] && [[ -f "$ARTIFACTS_DIR/logs/.landing-in-flight" || -f "$ARTIFACTS_DIR/.wrapup-in-progress" ]]; then
      # a landing or the wrap-up (both verify-gated) began since the
      # watchdog woke: it owns the tree now (review round 8)
      echo "deadline watchdog: the loop's own landing or wrap-up began — standing down"
      return 0
    fi
    if [[ -n "$(boundary_get '.gate_pending.before // empty' 2>/dev/null)" ]]; then
      _gp_pending=1
      if [[ "$_gp_said" -eq 0 ]]; then
        _gp_said=1
        echo "deadline watchdog: a verify gate is running — committing the index as it stands (its worktree writes are the next start's settle)"
      fi
    fi
    if [[ "$_gp_pending" -eq 0 ]]; then
      git add -A 2>/dev/null || { sleep 2; continue; }
    fi
    _disarm_deploy_artifact ready-to-deploy.json
    _disarm_deploy_artifact project-complete.json
    # v0.18.0 (review round 4): nor the kill path's — an approval that rode a
    # --no-verify wip landed UNVERIFIED and never reached the deferral ledger
    # or its phase-close evidence; stranded, the next start lands it
    # verify-gated with both, like the wrap-up case below. (A gate pending
    # outside a landing — none remains since the watchdog stands down for
    # landings — would have its index committed whole: unstaging one member
    # would reach the running gate as a rewrite.)
    if [[ "$_gp_pending" -eq 0 ]]; then
      git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null || true
    fi
    if [[ "$mode" == "wrapup" ]]; then
      # The approval artifact never rides the wrap-up wip (v0.14.2, review
      # finding 1+2): committed, it makes the wip an approval-class record —
      # in squash mode the NEXT session's loop-start catch-up squash then runs
      # the verify gate with no model work and burns a breaker attempt; without
      # a squash target it reads as "landed" and the phase's named commit is
      # lost. Unstaged, it stays on disk as a STRANDED artifact, which the
      # existing verify-gated stranded-artifact recovery already handles at the
      # first boundary. The deploy artifacts above are kept out exactly as
      # on the kill path (v0.18.0: unstaged, left on disk as the session
      # wrote them): an unverified deploy claim never rides an unverified
      # commit, and the next verify-gated landing re-judges it with the rest
      # of the tree.
      git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null || true
      # Nor the batons (re-review finding 1): with an approval stranded on
      # disk, a LATER stop that did no work would otherwise sweep the previous
      # session's handoff into a wip of its own — HEAD moving with zero work,
      # which reads as progress to the orchestrator and hides a real stall.
      # With approval and batons unstaged, a no-work stop stages nothing, the
      # staged-clean check below returns without a commit, and the caller
      # writes the ordinary "leaving work uncommitted" handoff.
      git reset -q -- "$ARTIFACTS_DIR/session-handoff.json" "$ARTIFACTS_DIR/session-interrupted.json" 2>/dev/null || true
    fi
    unstage_transient_adds
    # v0.18.0: the loop's derived state (the deferral ledger, evidence) is
    # the landing's, never an unverified commit's.
    if [[ "$_gp_pending" -eq 0 ]] && command -v reset_derived_state >/dev/null 2>&1; then reset_derived_state; fi
    # Nor, ever, the security pair (review round 7; open since v0.13.0): the
    # loop never commits it on any path, and a --no-verify wip is a path. Nor
    # the agent-appended LEARNINGS files, which only a VERIFIED commit may
    # carry — its post-verify gates scan them for credentials (one copy of
    # that scan, v0.6.6). Both kept on disk, unstaged, for the next landing.
    if [[ "$_gp_pending" -eq 0 ]]; then
      git reset -q -- "$ROOT_DIR/.claude/settings.json" "$ROOT_DIR/.github/workflows" 2>/dev/null || true
      git reset -q -- "$ROOT_DIR/docs/LEARNINGS"*.md 2>/dev/null || true
    fi
    # Belt-and-braces the wrap-up commit already wears (review finding 5):
    # never sweep session logs or an in-repo custom sentinel into history on
    # the strand path either.
    git reset -q -- "$ARTIFACTS_DIR/logs" 2>/dev/null || true
    git reset -q -- "$ARTIFACTS_DIR/scratch" 2>/dev/null || true
    git reset -q -- "$WRAPUP_SENTINEL" 2>/dev/null || true
    if ! git diff --cached --quiet -- \
        "$ARTIFACTS_DIR/ready-to-deploy.json" \
        "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null; then
      # The disarm did not hold (the session re-wrote an artifact mid-race).
      # Never commit an armed artifact — retry the whole attempt.
      sleep 1; continue
    fi
    if git diff --cached --quiet 2>/dev/null; then
      return 0
    fi
    local kept="deploy artifacts and verdicts kept out (unstaged: this commit carries HEAD's ready-to-deploy.json, project-complete.json and phase-approval.json, the session's copies stay on disk for the verify-gated landing)"
    if git commit -q --no-verify \
      -m "wip: last-resort deadline commit (phasekit deadline watchdog) — ${why}; unverified in-progress work preserved, ${kept}" \
      -m "$(phasekit_trailers wip "$(_wip_phase_source)")" 2>/dev/null; then
      if [[ "$mode" == "wrapup" ]]; then
        echo "wrap-up fall-through: last-resort commit landed $(git rev-parse --short HEAD 2>/dev/null) — unverified work preserved on the branch instead of a dirty tree" >&2
      else
        echo "deadline watchdog: last-resort commit landed $(git rev-parse --short HEAD 2>/dev/null) — the kill strands nothing but the final seconds" >&2
      fi
      return 0
    fi
    sleep 2
  done
  echo "deadline watchdog: last-resort commit could not land (index contention?) — tree left as-is" >&2
  return 0
}

_wip_phase_source() {
  # The artifact whose phase a wip names: an approval this session has not
  # landed (the phase in flight), else none — a wip claims no phase it
  # cannot see.
  if [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]] && artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
    echo "$ARTIFACTS_DIR/phase-approval.json"
  fi
}

_disarm_deploy_artifact() {
  # v0.18.0 — ONE rule for both deploy-arming artifacts on every UNVERIFIED
  # commit path (the watchdog's kill, the wrap-up fall-through, the
  # gate-pending index commit): UNSTAGE, never rewrite, never delete. The
  # wip commit carries HEAD's version (or none, where HEAD has none); the
  # worktree keeps the session's. `git reset -q -- <f>` is the whole rule.
  #
  # Why the rule changed (foundry-meta designs/DESIGN-session-efficiency.md
  # §3.1, link [E]): until v0.17.0 the deploy CLAIM was restored to HEAD
  # while every evidence file the session re-armed beside it stayed at the
  # new iteration — a restore rewrote ONE half of a coherent pair, the
  # committed tree contradicted the project's own consistency checks (xmeo
  # run 999: 40 × "expected <HEAD digest> to be <session digest>"), and the
  # next start's stranded landing went red and needed a model to hand-edit
  # digests (34b035a). Kept on disk, the session's claim is re-staged with
  # the rest of its coherent tree by the next start's verify-gated landing
  # (`git add -A`), which is green where the session's tree was coherent.
  #
  # Why it is still safe (the v0.13.0 incident was a claim COMMITTED on a
  # strand and then deployed): an unverified commit never carries a changed
  # claim (the staged-clean re-check in every attempt below stays), and an
  # on-disk claim HEAD does not carry byte-for-byte cannot deploy — the
  # supervisor publishes only a claim HEAD carries with the worktree
  # matching it, and only from a project at rest (Foundry: the provenance
  # gate AC#126 and the resting gate; a killed iteration is not at rest).
  # The mtime aging the restore needed is gone with the restore.
  #
  # A torn file (zero bytes, or not a JSON object — a writer the kill
  # interrupted) is nobody's claim: untracked, it is deleted (v0.14.7 review
  # MAJOR-1: kept, a torn record would land at the next start as a silent
  # false completion); tracked, HEAD's bytes come back (deleting a tracked
  # claim would stage its REMOVAL at the next verified landing) and are aged
  # to HEAD's commit time, so a restored claim never reads as a fresh arm.
  local f="$1" p="$ARTIFACTS_DIR/$1" ts
  if [[ -e "$p" ]] && ! jq -e 'type == "object"' "$p" >/dev/null 2>&1; then
    if git cat-file -e "HEAD:artifacts/$f" 2>/dev/null; then
      if ! git diff --quiet HEAD -- "artifacts/$f" 2>/dev/null; then
        git checkout -q HEAD -- "artifacts/$f" 2>/dev/null || true
      fi
      ts="$(git log -1 --format=%cI HEAD -- "artifacts/$f" 2>/dev/null)" || ts=""
      [[ -n "$ts" ]] && touch -d "$ts" "$p" 2>/dev/null || true
    else
      rm -f "$p" 2>/dev/null || true
    fi
  fi
  git reset -q -- "$p" 2>/dev/null || true
  return 0
}

DEADLINE_WATCHDOG_PID=""
DEADLINE_LEAD=0
DEADLINE_TAKE_CONTROL=0
DEADLINE_BUILDER_TAKE_CONTROL=0
DEADLINE_SPAN=0
COST_SESSION_ID=""
COST_SESSION_HEAVY=false

claude_turn_pid() {
  # The model process of the turn in flight, from run-phase.sh's pidfile
  # ("<pid> <role>", role = PHASEKIT_ITER: a pass number, or light-review):
  # prints "<pid> <role>" when that process is alive AND is a claude
  # process (a recycled pid is never signalled); nothing otherwise.
  local f="$ARTIFACTS_DIR/logs/claude.pid" pid role
  [[ -f "$f" ]] || return 0
  read -r pid role < "$f" 2>/dev/null || return 0
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  # The process must BE claude: its program, or the script an interpreter
  # (node, bash) runs, is named `claude` — a recycled pid running `tee
  # …/claude-iter-1.jsonl` is not (review round 2).
  if [[ -r "/proc/$pid/cmdline" ]]; then
    local a0 a1
    { IFS= read -r -d '' a0; IFS= read -r -d '' a1; } < "/proc/$pid/cmdline" 2>/dev/null || true
    if [[ "${a0##*/}" != claude ]]; then
      case "${a0##*/}" in node|bash|sh) [[ "${a1##*/}" == claude ]] || return 0 ;; *) return 0 ;; esac
    fi
  fi
  echo "$pid ${role:-?}"
}

deadline_take_control() {
  # The take-control stage (v0.18.0, fork F3 — probed first in
  # scaffold-runner: SIGTERM to the claude child ends the turn in under a
  # second, kills its foreground tool child with it, leaves every completed
  # write intact, no index.lock, and the tree commits). Runs in the watchdog
  # at T-T_y (and, in light mode, at T-(T_y+R) for the BUILD turn, so the
  # default-model review still fits). When a model turn is still in flight
  # it is ended — the SIGTERM-then-SIGKILL shape, the last-resort commit at
  # T-60 s staying the SIGKILL stage — after a `deadline-yield` marker the
  # loop reads as a wrap-up at an iteration boundary: no CLI retry, no
  # verdict retry, straight to its verify-gated landing / wrap-up. The model
  # no longer has to hear the nudge for the loop to get control back
  # (link [C]: 31 of 31 timeouts since 09-14 never observed the sentinel).
  #   $1 = "builder" (light mode: leave the review turn alone) or "any"
  #   $2 = the T-minus seconds, for the record
  local which="$1" tminus="$2" turn pid role waited=0
  turn="$(claude_turn_pid)"; [[ -n "$turn" ]] || return 0
  read -r pid role <<<"$turn"
  if [[ "$which" == builder && "$role" == light-review ]]; then return 0; fi
  if boundary_complete; then return 0; fi
  if ! jq -n --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg t "$tminus" --arg pid "$pid" --arg role "$role" \
       '{took_control: true, at: $at, t_minus_s: ($t | tonumber), pid: ($pid | tonumber), turn: $role}' \
       > "$ARTIFACTS_DIR/logs/.deadline-yield" 2>/dev/null; then
    # v0.18.1 (row 1193 (3)): degrade loudly, never into a CLI retry — the
    # loop infers the take-control from its own plan (took_control_inferred).
    echo "deadline watchdog: WARN — could not write artifacts/logs/.deadline-yield; ending the turn anyway (the loop reads the take-control from its own plan and says so)"
  fi
  if [[ ! -f "$WRAPUP_SENTINEL" ]]; then touch "$WRAPUP_SENTINEL" 2>/dev/null || true; fi
  kill -TERM "$pid" 2>/dev/null || true
  echo "deadline watchdog: took control at T-${tminus}s — the model had not yielded; its turn was ended (SIGTERM to claude pid $pid, turn $role); the loop lands what is verified"
  while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 30 ]]; do sleep 0.5; waited=$((waited + 1)); done
  if kill -0 "$pid" 2>/dev/null; then
    echo "deadline watchdog: claude pid $pid is still alive 15s after SIGTERM — the last-resort commit remains the backstop"
  fi
  return 0
}

arm_deadline_watchdog() {
  # $1 = deadline (epoch seconds). Computes the plan (compute_wrapup_lead),
  # prints it — the `armed` line a supervisor reads the lead from, and the
  # one-line `phasekit-cost:` facts — and spawns the watchdog: sentinel at
  # T-L, take-control at T-T_y (light: the build turn at T-(T_y+R) first),
  # last-resort commit at T-LASTRESORT. The EXIT trap kills it on any
  # normal conclusion.
  local deadline="$1" now span lead ty lu cap lastresort arm_at tcb=0 r plan
  now="$(date +%s)"
  span=$((deadline - now))
  [[ "$span" -gt 0 ]] || return 0
  plan="$(compute_wrapup_lead "$span")"
  read -r lead ty lu cap <<<"$plan"
  lastresort="${PHASEKIT_LASTRESORT_LEAD_SECONDS:-60}"
  [[ "$lastresort" =~ ^[0-9]+$ ]] || lastresort=60
  if [[ "${ITERATION_MODE:-standard}" == "light" && "$ty" -gt 0 ]]; then
    local bwin=60
    [[ "$lead" -lt 180 ]] && bwin=$((lead / 3))
    r="$(cost_value r)"; tcb=$((ty + r)); [[ "$tcb" -gt $((lead - bwin)) ]] && tcb=$((lead - bwin))
    [[ "$tcb" -gt "$ty" ]] || tcb=0
  fi
  DEADLINE_LEAD="$lead"; DEADLINE_TAKE_CONTROL="$ty"; DEADLINE_BUILDER_TAKE_CONTROL="$tcb"; DEADLINE_SPAN="$span"
  arm_at=$((deadline - lead))
  echo "deadline watchdog: armed — sentinel at T-${lead}s, last-resort commit at T-${lastresort}s (span ${span}s), take control at T-${ty}s$( [[ "$tcb" -gt 0 ]] && echo " (build turn T-${tcb}s)")"
  cost_session_begin "$span" "$lead" "$ty" "$lu" "$cap"
  # The subshell's stdio is DETACHED (log file, /dev/null stdin): it must not
  # inherit the loop's pipes, or any harness reading the loop's output blocks
  # on EOF until the watchdog's sleeps finish — long after the loop exited.
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  (
    # Sleep in short slices with an is-the-loop-alive check, so a watchdog
    # orphaned by any kill path (even one the EXIT trap never saw) dies
    # within seconds instead of holding on for the whole span.
    _sleep_until() {
      local target="$1" now left
      while :; do
        now="$(date +%s)"; left=$((target - now))
        [[ "$left" -le 0 ]] && return 0
        kill -0 "$$" 2>/dev/null || exit 0
        sleep $(( left < 15 ? left : 15 ))
      done
    }
    # Phase 1: self-armed wrap-up lead.
    if [[ "$lead" -gt 0 ]]; then
      _sleep_until "$arm_at"
      kill -0 "$$" 2>/dev/null || exit 0
      if [[ ! -f "$WRAPUP_SENTINEL" ]]; then
        touch "$WRAPUP_SENTINEL" 2>/dev/null \
          && echo "deadline watchdog: wrap-up sentinel self-armed at T-${lead}s" || true
      fi
    fi
    # Phase 2 (v0.18.0): take control of a turn that has not yielded.
    if [[ "$tcb" -gt 0 ]]; then
      _sleep_until "$((deadline - tcb))"
      kill -0 "$$" 2>/dev/null || exit 0
      deadline_take_control builder "$tcb"
    fi
    if [[ "$ty" -gt 0 ]]; then
      _sleep_until "$((deadline - ty))"
      kill -0 "$$" 2>/dev/null || exit 0
      deadline_take_control any "$ty"
    fi
    # Phase 3: last-resort strand commit. Stands down when the loop's own
    # wrap-up is already landing the tree (v0.13.1, review finding 1): two
    # concurrent committers in the same final minute can strand each other —
    # the wrap-up is verify-gated and strictly better, so it wins.
    [[ "$lastresort" -gt 0 ]] || exit 0
    _sleep_until "$((deadline - lastresort))"
    kill -0 "$$" 2>/dev/null || exit 0
    if [[ -n "$COST_SESSION_ID" ]]; then
      cost_session_update "$COST_SESSION_ID" '{"exit": "killed"}'
    fi
    if [[ -f "$ARTIFACTS_DIR/.wrapup-in-progress" ]]; then
      echo "deadline watchdog: wrap-up commit in progress — standing down"
      exit 0
    fi
    if [[ -f "$ARTIFACTS_DIR/logs/.landing-in-flight" ]]; then
      echo "deadline watchdog: the loop's own landing is in flight — standing down (a kill now leaves its verdicts on disk for the next start's verify-gated landing)"
      exit 0
    fi
    deadline_lastresort_commit
  ) >>"$ARTIFACTS_DIR/logs/deadline-watchdog.log" 2>&1 </dev/null &
  DEADLINE_WATCHDOG_PID=$!
}

cost_session_begin() {
  # $1 span $2 lead $3 take-control $4 uncapped lead $5 cap. Opens this
  # session's sample and prints the `phasekit-cost:` line (the orchestrator
  # reads it from the session log it already captures, and never computes
  # these numbers a second time). `heavy` is the F4 fact for this session —
  # the full tier's measured P90 over 12.5% of the bound, or the uncapped
  # lead over the cap — only once the full tier HAS a measurement (a prior
  # is never evidence that a suite is too heavy).
  local span="$1" lead="$2" ty="$3" lu="$4" cap="$5" g at_cap=false heavy=false recent line
  [[ "$lu" -gt "$cap" ]] && at_cap=true
  g="$(cost_p90 g_full)"
  if [[ -n "$g" ]] && { [[ "$lu" -gt "$cap" ]] || [[ $((g * 1000)) -gt $((span * 125)) ]]; }; then heavy=true; fi
  COST_SESSION_HEAVY="$heavy"
  recent="$(heavy_sessions_recent)"
  COST_SESSION_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  line="$(jq -cn --arg mode "${ITERATION_MODE:-standard}" --argjson span "$span" --argjson lead "$lead" \
      --argjson ty "$ty" --argjson lu "$lu" --argjson cap "$cap" --argjson at_cap "$at_cap" \
      --argjson heavy "$heavy" --argjson recent "$recent" \
      --arg gf "$(cost_p90 g_full)" --arg gs "$(cost_p90 g_fast)" --arg w "$(cost_p90 w)" \
      --arg m "$(cost_p90 m)" --arg r "$(cost_p90 r)" --arg pass "$(cost_p90 pass)" \
      --argjson n "$(jq -c '(.samples // {}) | map_values(length)' "$ARTIFACTS_DIR/logs/cost-ledger.json" 2>/dev/null || echo '{}')" \
      'def num: if . == "" then null else tonumber end;
       {schema: 1, mode: $mode, bound_s: $span, lead_s: $lead, take_control_s: $ty,
        lead_uncapped_s: $lu, lead_cap_s: $cap, at_cap: $at_cap, heavy: $heavy,
        heavy_in_previous_4: $recent,
        p90_s: {g_full: ($gf | num), g_fast: ($gs | num), w: ($w | num), m: ($m | num), r: ($r | num), pass: ($pass | num)},
        samples: $n}' 2>/dev/null)" || line=""
  if [[ -n "$line" ]]; then
    echo "phasekit-cost: $line"
    if [[ "$at_cap" == true ]]; then
      echo "phasekit-cost: the lead this project's close-out needs (${lu}s) is over the cap (${cap}s = 25% of the bound) — the lead stays at the cap (fork F1); the full tier needs to be split by measured duration (docs/QUALITY_GATES.md \"Verify budget\")"
    fi
    cost_session_record "$COST_SESSION_ID" "$(jq -c '{started_at: (now | todate), mode, bound_s, lead_s, take_control_s, lead_uncapped_s, lead_cap_s, at_cap, heavy, exit: "running", passes: 0}' <<<"$line")"
  fi
}

run_until_done_exit_trap() {
  # Kill AND reap (v0.13.1, review finding 4): kill alone is asynchronous — a
  # watchdog already inside phase 2 could re-write the baton after the clear
  # below removed it, leaving a lying "you were killed" note on a session
  # that concluded. wait makes the ordering real.
  completion_guard_stop   # v0.18.1: a turn's guard never outlives the loop
  if [[ -n "$DEADLINE_WATCHDOG_PID" ]]; then
    kill "$DEADLINE_WATCHDOG_PID" 2>/dev/null || true
    wait "$DEADLINE_WATCHDOG_PID" 2>/dev/null || true
  fi
  clear_provisional_handoff_on_exit
  if [[ "${LOOP_ENV_WRITTEN:-0}" == 1 ]]; then
    rm -f "$ARTIFACTS_DIR/logs/.loop-env"
    drop_unclaimed_carried_record || true
    drop_unlanded_synthesized_record || true
  fi
  # v0.18.0: this session's cost sample says how it ended (after the reap,
  # so the watchdog's "killed" cannot overwrite a loop that concluded).
  if [[ -n "${COST_SESSION_ID:-}" ]]; then
    local _exit=yielded
    [[ "${TOOK_CONTROL:-0}" == 1 ]] && _exit=took-control
    cost_session_update "$COST_SESSION_ID" "$(jq -cn --arg e "$_exit" --arg p "${passes_done:-0}" '{exit: $e, passes: ($p | tonumber)}')" || true
  fi
  # v0.18.5: last, after every reap above — the loop's temporaries go with it
  # (only from the shell that made them; a subshell never removes them).
  if [[ -n "${PK_LOOP_TMP:-}" && "$BASHPID" == "${PK_LOOP_TMP_OWNER:-}" ]]; then
    rm -rf -- "$PK_LOOP_TMP" 2>/dev/null || true
  fi
}
trap run_until_done_exit_trap EXIT

# v0.18.5 (rider 2): the loop's temporaries live in ONE private directory
# that the EXIT trap above removes. Every bare `mktemp` below — about twenty
# sites, and any added later — goes through this function into it; a child
# process (the model, the gate, a hook) keeps the TMPDIR it was given. In a
# container /tmp dies with the run, but the suite drives this loop hundreds
# of times on a host, and each run used to leave its marker and scratch files
# in $TMPDIR (2026-10-01: ~1,226 per suite run, 122k in one host's /tmp).
# A hard kill skips every trap; the directory it leaves is one, not a flood.
PK_LOOP_TMP="$(command mktemp -d "${TMPDIR:-/tmp}/phasekit-loop.XXXXXXXX" 2>/dev/null)" || PK_LOOP_TMP=""
# absolute, so the loop's own `cd`s cannot strand it (a relative TMPDIR; review MINOR 5)
if [[ -n "$PK_LOOP_TMP" ]]; then PK_LOOP_TMP="$(cd "$PK_LOOP_TMP" 2>/dev/null && pwd)" || PK_LOOP_TMP=""; fi
PK_LOOP_TMP_OWNER="$BASHPID"
mktemp() {
  # ${…:-}: a test that extracts the loop's functions runs this without the setup above
  if [[ -n "${PK_LOOP_TMP:-}" && -d "${PK_LOOP_TMP:-}" ]]; then
    TMPDIR="$PK_LOOP_TMP" command mktemp "$@"
  else
    command mktemp "$@"
  fi
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
  #
  # The marker tells the deadline watchdog's phase 2 to stand down (v0.13.1):
  # both committers wake in the same final minute, and an unguarded add/commit
  # here under set -e once meant a lock collision could kill the loop
  # mid-wrap-up. Never removed on the happy path — every wrap-up exit ends
  # the session; the loop clears a stale one at startup beside the nudge
  # marker, and the transient vocabulary keeps it uncommittable.
  # v0.14.9: nothing to wrap up once the iteration is complete — a wrap-up
  # here re-ran the verify gate over a landed completion and committed its
  # re-measurement noise straight onto the target (iteration 50, 1591dd4).
  if boundary_complete; then
    echo "Wrap-up: the iteration is complete (record final, step $(boundary_step)) — nothing to wrap up; nothing else runs (v0.14.9)."
    return 0
  fi
  touch "$ARTIFACTS_DIR/.wrapup-in-progress" 2>/dev/null || true
  # v0.14.5: an approval-class artifact this sweep may carry (a CLI retry
  # ended the iteration before the boundary saw it — round-2 F5) leaves
  # with keyed deferrals like every other commit path.
  prepare_verdicts_for_landing || true
  # The loop's one staging retry rule (git_add_all): a lock left by a dead
  # writer — an ended turn's, after a take-control — is released once it is
  # old enough to be nobody's (review round 2; one rule since v0.18.2).
  stage_all
  if [[ -n "${STAGE_LOCKED:-}" ]]; then
    echo "Wrap-up: git's index is locked ($STAGE_LOCKED) — committing what is staged" >&2
  elif [[ -n "${STAGE_FAILED:-}" ]]; then
    # The gate would judge a worktree the commit does not carry: never a
    # LANDING then (review round 7) — the verdicts stay out and the rest is a
    # checkpoint; the next landing judges the whole tree.
    echo "Wrap-up: git could not stage: $STAGE_FAILED — committing the rest as a checkpoint (verdicts kept out); the baton names them" >&2
    git reset -q -- "$ARTIFACTS_DIR/phase-approval.json" "$ARTIFACTS_DIR/project-complete.json" 2>/dev/null || true
  fi
  git reset -q -- "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  git reset -q -- "$ARTIFACTS_DIR/scratch" 2>/dev/null || true
  git reset -q -- "$WRAPUP_SENTINEL" 2>/dev/null || true
  unstage_transient_adds
  keep_unclaimed_completion_out
  # A wrap-up that sweeps an unlanded approval-class record IS its landing:
  # the same derived state (ledger, evidence) as the boundary commit that
  # would have landed it, so the two gates judge one tree and a red verdict
  # memoised by either answers the next start's recovery (review round 2).
  # Otherwise it is a checkpoint and carries none.
  local _wdrv
  _wdrv="$(evidence_driver)"
  [[ -z "${STAGE_FAILED:-}" ]] || _wdrv=""
  if [[ -n "$_wdrv" ]]; then
    if artifact_never_landed "$_wdrv"; then write_phase_evidence "$_wdrv"; fi
  else
    reset_derived_state
  fi
  if git diff --cached --quiet -- ':/' \
       ":(exclude)$ARTIFACTS_DIR/phase-blocked.json" \
       ":(exclude)$ARTIFACTS_DIR/phase-verify-failed.json"; then
    echo "Wrap-up: tree already clean — nothing substantive to commit."
    return 0
  fi
  if staged_touches_security_pair; then
    write_session_handoff false "put the changed .claude/settings.json / .github/workflows/ files back as HEAD has them (edit them back: git show HEAD:<path> > <path>) — the loop never commits them — then redo the phase work without touching them"
    echo "Wrap-up: staged changes touch committed .claude/settings.json or .github/workflows/ — leaving work uncommitted (security-critical, never committed by the loop). Handoff note written." >&2
    return 0
  fi
  # v0.18.0 (review round 9): after a take-control there is no model turn
  # between the landing the gate just refused and this wrap-up — the same
  # work is not gated again (a second full-tier run does not fit T_y, and in
  # light mode it trips the 2-attempt breaker with zero repair turns).
  local _wgate=0
  if [[ "${TOOK_CONTROL:-0}" == 1 && "${LAST_GATE_RED:-0}" == 1 ]]; then
    echo "Wrap-up: the gate refused this work moments ago and no turn has run since (take-control) — not re-running it; the red stands."
    _wgate=1
  else
    run_verify_gate || _wgate=$?
  fi
  if [[ "$_wgate" -ne 0 ]]; then
    # Fall-through (v0.14.2, Aaron 2026-09-07, foundry-meta #517). A red
    # verify at wrap-up used to leave the tree dirty under a "wrapped up
    # cleanly" banner, and the watchdog's last-resort commit never fired
    # because the loop was already gone — every such stop became a stall row,
    # a 20-minute pause and a resolver session (three on 2026-09-06). The
    # hard-kill path already trusts a LABELED unverified wip commit, and with
    # branch-per-iteration that commit lands on the work branch and folds into
    # the next phase's squash, never on the target. So: when the ONLY thing
    # wrong is the verify gate — the security pair untouched (checked above)
    # and the post-verify gates (secret scan, scope, SPEC attestation) still
    # passing on the staged tree — make the same last-resort commit the
    # watchdog would have made. Any other refusal keeps refusing: this never
    # commits what a gate other than verify would refuse.
    if post_verify_commit_gates wrapup; then
      local _head_before
      _head_before="$(git rev-parse HEAD 2>/dev/null || true)"
      deadline_lastresort_commit wrapup || true
      if [[ "$(git rev-parse HEAD 2>/dev/null || true)" != "$_head_before" ]]; then
        # Written only once the commit is real (review finding 5): a baton
        # that says "preserved as a wip commit" over a commit that never
        # landed would misdirect the next session.
        write_session_handoff false "the verify gate was RED at wrap-up and the standing work was preserved as an UNVERIFIED wip commit (fall-through); the approval artifact, if any, is on disk uncommitted for the stranded-artifact recovery: audit the wip, fix the verify failure recorded in artifacts/phase-verify-failed.json, then land the work properly"
        echo "Wrap-up: verify failed — standing work preserved as an UNVERIFIED wip commit $(git rev-parse --short HEAD 2>/dev/null) (fall-through); the next session's gates judge it. phase-verify-failed.json + session-handoff.json record the state." >&2
        WRAPUP_UNVERIFIED=1
        return 0
      fi
    fi
    write_session_handoff false "fix the verify failure recorded in artifacts/phase-verify-failed.json, then write your verdict (phase-update.json at least) — the loop commits the standing work"
    echo "Wrap-up: verify failed — leaving work uncommitted (phase-verify-failed.json + session-handoff.json record the state for the next session)." >&2
    return 0
  fi
  if ! post_verify_commit_gates wrapup; then
    write_session_handoff false "the post-verify commit gates refused the staged work (see the REFUSED line in the session log — e.g. remove a credential-shaped line from docs/LEARNINGS.md), then write your verdict (phase-update.json at least) — the loop commits the standing work"
    echo "Wrap-up: post-verify gates refused the staged work — leaving it uncommitted (session-handoff.json records the state)." >&2
    return 0
  fi
  if [[ -n "${STAGE_FAILED:-}" ]]; then
    write_session_handoff true "standing work was committed at wrap-up EXCEPT paths git cannot stage: $STAGE_FAILED — make them stageable (an empty or nested repository: remove its .git or move it outside the tree; an unreadable file: fix its permissions) or remove them, then continue from the next unapproved phase"
  else
    write_session_handoff true "standing work was committed at wrap-up; re-orient and continue from the next unapproved phase"
  fi
  git add -f "$ARTIFACTS_DIR/session-handoff.json" 2>/dev/null || true
  # Tolerant commit (v0.13.1): under set -e a bare failure here killed the
  # loop. "Nothing to commit" means a concurrent last-resort commit already
  # landed the staged work (mislabeled wip, but landed — the next session's
  # gates judge it); any other failure leaves the gated, staged work for the
  # next session / the scheduler's complete-but-dirty backstop. Neither is
  # worth dying over at session end.
  if ! git commit -m "chore(workflow): session wrap-up — soft stop before session end" -m "$(phasekit_trailers wrapup)" 2>/dev/null; then
    if git diff --cached --quiet 2>/dev/null; then
      echo "Wrap-up: staged work was already landed by a concurrent last-resort commit — nothing left to commit."
    else
      echo "Wrap-up: commit failed (index contention?) — gated, staged work left for the next session." >&2
    fi
    return 0
  fi
  # a refusal the wrap-up could not stage past still stands (review round 6)
  [[ -n "${STAGE_FAILED:-}" ]] || clear_commit_refusal
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
- The pre-commit verify gate is unchanged and mandatory. The default-model
  final review that follows your turn runs the FULL tier; you run the FAST
  tier: `bash scripts/phasekit.sh verify` BEFORE you write the completion
  record (the gate runs full exactly when artifacts/project-complete.json
  exists), make it pass, then write the record and end your turn.
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
- The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree, push) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
- Scratch files go in artifacts/scratch/ (ignored, never committed, cleared when an iteration starts) or /tmp — never elsewhere in the tree: the loop commits everything else it finds.
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
    if [[ "$(jq -r '.blocker_kind // empty' "$ARTIFACTS_DIR/phase-blocked.json" 2>/dev/null)" == "branch-integrity" ]]; then
      write_light_escalation "branch_integrity" "the squash onto $SQUASH_TARGET was refused (see phase-blocked.json)"
      exit 2
    fi
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
3. If the work is fundamentally unsound or clearly outgrew a light task,
   delete artifacts/project-complete.json, write artifacts/phase-blocked.json
   explaining why, and stop.
4. Otherwise re-write artifacts/project-complete.json — keep its shape,
   update the summary if you changed anything — so the wrapper can commit.
5. LAST, run `bash scripts/phasekit.sh verify` (the FULL tier: the record
   exists). It runs the gate exactly as the wrapper's commit will, and a
   green verdict is REUSED by that commit instead of run again — so change
   nothing after it. If it is red, fix and run it again.

The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree, push) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
Scratch files go in artifacts/scratch/ (ignored, never committed, cleared when an iteration starts) or /tmp — never elsewhere in the tree: the loop commits everything else it finds.
REVIEW_EOF
  echo "Light mode: final review pass on the default model before the final commit."
  local rrc=0 rstart
  rstart="$(date +%s)"
  # Only a take-control during THIS invocation counts (review round 2: the
  # build turn's own marker would read as the review's).
  touch "${ITER_START_MARKER}.review" 2>/dev/null || true
  (
    ANTHROPIC_MODEL=""
    export ANTHROPIC_MODEL
    run_once "$review_prompt" "new" "light-review" 0
  ) || rrc=$?
  rm -f "$review_prompt"
  REVIEW_ENDED=0
  if [[ -f "$ARTIFACTS_DIR/logs/.deadline-yield" && "$ARTIFACTS_DIR/logs/.deadline-yield" -nt "${ITER_START_MARKER}.review" ]] \
     || { ! completion_guard_ended_turn "${ITER_START_MARKER}.review" "$rrc" && took_control_inferred "$rstart" review "$rrc"; }; then
    TOOK_CONTROL=1
    REVIEW_ENDED=1
    release_stale_index_lock
    # The review did not finish: the completion is never landed unreviewed.
    # The record is CARRIED (claims nothing, contributes no derived state,
    # removed at the session's exit — review round 3: a record left on disk
    # at exit 0 reads as a completion to a supervisor), and the wrap-up lands
    # the build verify-gated; the next session re-claims the completion and
    # its review runs to the end.
    if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]]; then carry_completion_record; fi
    echo "deadline watchdog: took control — the light review's turn was ended before it finished; the completion is not landed this session (the build is wrapped up; the next session re-claims it)."
  elif completion_guard_ended_turn "${ITER_START_MARKER}.review" "$rrc"; then
    echo "completion guard: the review committed the completion record itself — its turn was ended there (v0.18.1); landing it."
    release_stale_index_lock
  else
    cost_sample r "$(( $(date +%s) - rstart ))"
  fi
  rm -f "${ITER_START_MARKER}.review" 2>/dev/null || true
  if [[ "$rrc" -ne 0 ]]; then
    echo "WARN: light final-review pass exited $rrc — proceeding to the verify-gated final commit anyway." >&2
  fi
  return 0
}

# --- the completion turn guard (v0.18.1, row 831 (a)) -------------------------
# See "writes after the completion commit" in the boundary block. One
# background child per model turn, started and reaped by run_once: it polls
# HEAD every half second; when a commit made during the turn lands a
# completion record that CLAIMS (completion_record_claims — a carried record
# a checkpoint swept in unchanged claims nothing), it snapshots the tree at
# that commit (the commit's instant from HEAD's reflog), writes
# artifacts/logs/.completion-yield and ends the turn — SIGTERM to the claude
# pid run-phase.sh names, the take-control mechanism v0.18.0 probed (the turn
# ends in ~0.5 s, completed writes intact, no index.lock). A write racing the
# signal is the settle's business. When the turn ended on its own before a
# poll saw the commit, run_once makes the same observation once, in the
# foreground, without a signal. Stdio detached (the v0.13.0 pipe-holder
# lesson); it exits within half a second of the loop, and is reaped when the
# turn ends. Its git reads take no optional locks (never an index.lock
# against the model's own git).
COMPLETION_GUARD_PID=""
COMPLETION_GUARD_BASE=""
COMPLETION_GUARD_STARTED=""
COMPLETION_GUARD_STASH=""

completion_guard_observe() {
  # $1 = the completion blob at HEAD when the turn started, $2 = the turn's
  # start (epoch s), $3 = 1 to end the turn. 0 when a completion commit of
  # this turn is observed (and recorded); 1 otherwise.
  local base="$1" started="$2" end_turn="${3:-0}" b ct turn pid="" role="" waited=0
  b="$(completion_blob_at_head)"
  [[ -n "$b" && "$b" != "$base" ]] || return 1
  ct="$(_completion_commit_time)"
  [[ "$ct" =~ ^[0-9]+$ && "$ct" -ge "$started" ]] || return 1
  git cat-file -p "$b" 2>/dev/null | jq -e 'type == "object"' >/dev/null 2>&1 || return 1
  committed_record_claims "$b" || return 1
  # A rebase, cherry-pick, merge or am in progress re-commits as it goes:
  # never end a turn in the middle of one (review round 2) — the next poll,
  # or the observation after the turn, sees where it lands.
  local gd
  gd="$(git rev-parse --git-dir 2>/dev/null)" || gd=""
  if [[ -n "$gd" ]]; then
    [[ "$gd" = /* ]] || gd="$ROOT_DIR/$gd"
    if [[ -d "$gd/rebase-merge" || -d "$gd/rebase-apply" || -f "$gd/CHERRY_PICK_HEAD" \
          || -f "$gd/MERGE_HEAD" || -f "$gd/REVERT_HEAD" || -d "$gd/sequencer" ]]; then
      return 1
    fi
  fi
  # Git's own commit may still hold the index lock (it is released after
  # HEAD and the reflog move): never end a turn inside that window (review
  # round 4).
  if [[ -n "$gd" && -e "$gd/index.lock" ]]; then return 1; fi
  # A stash made during the turn hides work from the tree (review round 5:
  # `git stash -u` to look at the committed tree, the pop never came): a
  # clean tree then proves nothing — no snapshot, no signal.
  if [[ "$(git rev-parse -q --verify refs/stash 2>/dev/null || true)" != "${COMPLETION_GUARD_STASH:-}" ]]; then return 1; fi
  # The completion stands only on a clean tree: the commit carried the
  # turn's work (review round 3: a record-only commit over older work is not
  # the end — the model may commit the rest next) and nothing has been
  # written since. Seen clean, the snapshot is exact: whatever appears after
  # this moment — a write racing the signal included — is after the
  # commit. Never seen clean: no snapshot, no judgement (the v0.18.0 rest).
  _boundary_tree_clean || return 1
  completion_snapshot_take turn-guard "$b" || true
  # Still clean AFTER the snapshot, so nothing written between the two reads
  # hides in its before-set (review round 6); else no snapshot, no signal.
  if ! _boundary_tree_clean; then completion_snapshot_drop || true; return 1; fi
  if [[ "$end_turn" == 1 ]]; then
    turn="$(claude_turn_pid)"
    if [[ -n "$turn" ]]; then read -r pid role <<<"$turn"; fi
    if [[ -n "$pid" ]]; then kill -TERM "$pid" 2>/dev/null || true; fi
  fi
  if ! jq -n --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg blob "$b" \
       --arg commit "$(git log -1 --format=%H HEAD -- artifacts/project-complete.json 2>/dev/null)" \
       --arg pid "$pid" --arg role "$role" \
       '{completion_committed: true, at: $at, blob: $blob, commit: $commit,
         pid: (if $pid == "" then null else ($pid | tonumber) end),
         turn: (if $role == "" then null else $role end)}' \
       > "$ARTIFACTS_DIR/logs/.completion-yield" 2>/dev/null; then
    echo "completion guard: WARN — could not write artifacts/logs/.completion-yield; the turn is still ended, the loop reads the commit from git"
  fi
  if [[ -n "$pid" ]]; then
    echo "completion guard: the completion record was committed during the turn — the turn was ended (SIGTERM to claude pid $pid, turn $role)"
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 30 ]]; do sleep 0.5; waited=$((waited + 1)); done
  elif [[ "$end_turn" == 1 ]]; then
    echo "completion guard: the completion record was committed during the turn — no live turn to end"
  fi
  return 0
}

completion_guard_watch() {
  # The background poll: $1 base, $2 start (see completion_guard_observe).
  local base="$1" started="$2"
  export GIT_OPTIONAL_LOCKS=0
  while :; do
    kill -0 "$$" 2>/dev/null || exit 0
    if completion_guard_observe "$base" "$started" 1; then exit 0; fi
    # (The base never advances: a record older than the turn never fires —
    # the commit-time check — and a completion the guard held back for (a
    # rebase in progress, older work not yet committed) must fire the
    # moment it stands, review round 3.)
    sleep 0.5
  done
}

completion_guard_start() {
  completion_guard_stop
  mkdir -p "$ARTIFACTS_DIR/logs" 2>/dev/null || true
  COMPLETION_GUARD_BASE="$(completion_blob_at_head)"
  COMPLETION_GUARD_STARTED="$(date +%s)"
  COMPLETION_GUARD_STASH="$(git rev-parse -q --verify refs/stash 2>/dev/null || true)"
  touch "$ARTIFACTS_DIR/logs/.completion-guard-start" 2>/dev/null || true
  ( completion_guard_watch "$COMPLETION_GUARD_BASE" "$COMPLETION_GUARD_STARTED" ) \
    >>"$ARTIFACTS_DIR/logs/completion-guard.log" 2>&1 </dev/null &
  COMPLETION_GUARD_PID=$!
}

completion_guard_stop() {
  if [[ -n "${COMPLETION_GUARD_PID:-}" ]]; then
    kill "$COMPLETION_GUARD_PID" 2>/dev/null || true
    wait "$COMPLETION_GUARD_PID" 2>/dev/null || true
    COMPLETION_GUARD_PID=""
  fi
  return 0
}

completion_guard_ended_turn() {
  # $1 = the turn's start marker file, $2 = the turn's exit code. Was the
  # completion committed during that turn (and the turn ended there)? The
  # guard's marker; else — a turn that ended NON-zero only (a SIGTERM whose
  # marker could not be written) — git's own evidence: the claiming record
  # HEAD carries was committed at or after the marker (the loop commits no
  # completion while a turn runs). A turn that ended 0 without a marker
  # yielded on its own: nothing to say.
  local mk="${1:-}" rc="${2:-0}" y="$ARTIFACTS_DIR/logs/.completion-yield" ct mt
  [[ -n "$mk" && -f "$mk" ]] || return 1
  if [[ -f "$y" && "$y" -nt "$mk" ]]; then return 0; fi
  [[ "$rc" != 0 ]] || return 1
  completion_landed_at_head && committed_record_claims "$(completion_blob_at_head)" || return 1
  # Only a turn the guard acted on: its snapshot of this very record (taken
  # before the signal) — a CLI failure after a record-only commit the guard
  # held back for is a CLI failure (review round 5).
  [[ "$(boundary_get '.completion_snapshot.source // empty')" == turn-guard \
     && "$(boundary_get '.completion_snapshot.blob // empty')" == "$(completion_blob_at_head)" ]] || return 1
  ct="$(_completion_commit_time)"; mt="$(stat -c %Y "$mk" 2>/dev/null)" || mt=""
  [[ "$ct" =~ ^[0-9]+$ && "$mt" =~ ^[0-9]+$ && "$ct" -ge "$mt" ]]
}

run_once() {
  local prompt_file="$1"
  local mode="$2"
  local iter_num="$3"
  local retry_attempt="${4:-0}"
  local rc=0

  # v0.18.1: every model turn runs under the completion guard, and a turn's
  # own writes are never residue of an earlier completion commit.
  completion_snapshot_drop || true
  completion_guard_start
  if [[ "$mode" == "continue" ]]; then
    CLAUDE_MODE=continue \
      PHASEKIT_ITER="$iter_num" \
      PHASEKIT_RETRY_ATTEMPT="$retry_attempt" \
      "$RUN_PHASE_SCRIPT" "$prompt_file" || rc=$?
  else
    CLAUDE_MODE=new \
      PHASEKIT_ITER="$iter_num" \
      PHASEKIT_RETRY_ATTEMPT="$retry_attempt" \
      "$RUN_PHASE_SCRIPT" "$prompt_file" || rc=$?
  fi
  completion_guard_stop
  # The turn ended before a poll saw its completion commit: the same
  # observation once, now, so the walk judges "after" by the commit's own
  # instant (no signal — the turn is over).
  if [[ ! -f "$ARTIFACTS_DIR/logs/.completion-yield" ]] \
     || ! [[ "$ARTIFACTS_DIR/logs/.completion-yield" -nt "$ARTIFACTS_DIR/logs/.completion-guard-start" ]]; then
    GIT_OPTIONAL_LOCKS=0 completion_guard_observe "$COMPLETION_GUARD_BASE" "$COMPLETION_GUARD_STARTED" 0 >/dev/null 2>&1 || true
  fi
  return "$rc"
}

# --- model-facing subcommands (v0.18.0) --------------------------------------
# `phasekit verify` and `phasekit scope` (scripts/phasekit.sh forwards both to
# the PROJECT's own copy of this script, so they run this project's loop
# code): every function above is defined, nothing below has run — no
# iteration, no watchdog, no recovery, no branch move.
if [[ "${1:-}" == "verify" || "${1:-}" == "scope" ]]; then
  _sub="$1"; shift
  cd "$ROOT_DIR"
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "phasekit $_sub: $ROOT_DIR is not a git working tree" >&2
    exit 2
  fi
  ITER_START_MARKER=""
  WRAPUP_SENTINEL="${PHASEKIT_WRAPUP_SENTINEL:-$ARTIFACTS_DIR/wrapup-requested}"
  if [[ "$_sub" == scope ]]; then
    phasekit_scope "$@"
    exit $?
  fi
  phasekit_verify "$@"
  exit $?
fi

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

# Dead-man promotion (v0.10.1): a session-interrupted.json that survived to
# THIS session's start is the previous session's kill telling its story.
# Promote it into the baton slot the orientation already reads-then-deletes —
# unless a real wrap-up baton exists, which knows strictly more (the wrap-up
# ran after the last provisional was written) and wins.
if [[ -f "$ARTIFACTS_DIR/session-interrupted.json" ]]; then
  if [[ -f "$ARTIFACTS_DIR/session-handoff.json" ]]; then
    rm -f "$ARTIFACTS_DIR/session-interrupted.json"
  else
    mv -f "$ARTIFACTS_DIR/session-interrupted.json" "$ARTIFACTS_DIR/session-handoff.json"
    echo "run-until-done: promoted the previous session's dead-man baton to session-handoff.json (that session ended without concluding its iteration)" >&2
  fi
fi
PENDING_COMMIT_RETRY=""

# Soft wrap-up sentinel: an outer supervisor (e.g. the orchestrator's
# run-session.sh) touches this file at T-minus-N minutes before its hard kill.
# Between iterations the loop honors it: commit what stands (verify-gated) and
# exit 0 instead of starting an iteration the guillotine would truncate.
WRAPUP_SENTINEL="${PHASEKIT_WRAPUP_SENTINEL:-$ARTIFACTS_DIR/wrapup-requested}"
WRAPUP_UNVERIFIED=0   # set by wrapup_commit's verify-red fall-through (v0.14.2)
COMPLETION_COMMIT_IN_PROGRESS=0   # v0.14.4: a completion commit never carries a baton
# Project env the supervisor forwarded (v0.14.3): names only, so a session log
# answers "did the build see its keys?" without a docker inspect on the host.
if [[ -n "${PHASEKIT_FORWARD_ENV:-}" ]]; then
  echo "Project env forwarded by the supervisor: ${PHASEKIT_FORWARD_ENV//$'\n'/,}"
fi
if [[ -f "$WRAPUP_SENTINEL" ]]; then
  echo "Clearing stale wrap-up sentinel from a prior run: $WRAPUP_SENTINEL"
  rm -f "$WRAPUP_SENTINEL"
fi
# v0.18.0: the take-control marker belongs to the session that wrote it.
rm -f "$ARTIFACTS_DIR/logs/.deadline-yield" 2>/dev/null || true
rm -f "$ARTIFACTS_DIR/logs/.completion-yield" 2>/dev/null || true   # v0.18.1: so does the completion guard's
TOOK_CONTROL=0          # set when the deadline watchdog ended a turn (v0.18.0)
rm -f "$ARTIFACTS_DIR/logs/claude.pid"   # v0.18.0: a dead session's turn is nobody's to end
rm -f "$ARTIFACTS_DIR/logs/.landing-in-flight"
# v0.18.0: a carry never crosses a session — a session killed before its exit
# could drop an unclaimed carried record leaves it here; it goes now, as the
# exit would have removed it (review round 3).
drop_unclaimed_carried_record || true

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
# Deadline watchdog (v0.13.0): self-armed wrap-up lead + last-resort strand
# commit. Armed only when the deadline is known; see the block above
# wrapup_commit for the full argument.
if [[ -n "$SESSION_DEADLINE" ]]; then
  arm_deadline_watchdog "$SESSION_DEADLINE"
fi
pass_elapsed_total=0
passes_done=0
last_pass_start=""
# v0.18.0: after every export the loop makes, before any turn — what a model's
# `phasekit verify` compares its environment with.
write_loop_env_snapshot || true

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
# v0.18.2: the scratch space belongs to one iteration.
clear_scratch_at_iteration_start || true

# Branch-per-iteration (v0.14.0): put HEAD on the work branch before any
# loop-made commit can land — the heal commit just below and the recovery
# commits after it must ride the branch, never the target. A refusal here is
# a blocked verdict at zero token cost.
if ! ensure_work_branch; then
  echo "Stopping: branch-per-iteration preconditions not met (see artifacts/phase-blocked.json)." >&2
  exit 2
fi
heal_tracked_transients || true
# v0.14.10: a verify gate the previous session died inside is settled here —
# its footprint restored and recorded red — before the recovery below stages
# anything and before any turn. Inert when no gate is pending.
gate_settle_pending || true

# --- Boundary recovery at loop start (v0.14.5) --------------------------------
# One question, answered before any model turn: is a boundary open? Git and
# disk answer first (a never-landed verdict artifact; an approval-class commit
# the target lacks; a landed approval that names the final phase with no
# completion record), then the record (step < 7 for the last boundary, trusted
# only where its shas are reachable). Whichever says "open", land_boundary
# walks the sequence from the lowest step in question — every step proves
# itself, so starting low costs nothing and never guesses. A record whose shas
# git cannot find is named and discarded, and only git's evidence counts.
# Stranded artifacts (v0.6.3) and the catch-up squash (v0.14.0) are the same
# call now: their witness lines are kept.
recover_from=0
recover_why=""
if completion_record_claims && artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
  recover_from=1
  recover_why="Stranded project-complete.json from a prior session detected — attempting its final commit before starting."
elif artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
  recover_from=1
  recover_why="Stranded phase-approval.json from a prior session detected — landing it before starting (v0.14.5: at loop start, before any orientation; verify-gated as always)."
elif squash_pending; then
  recover_from=2
  recover_why="Branch-per-iteration: an approval-class commit on the work branch has not reached '$SQUASH_TARGET' — catching up before starting."
elif approval_final_unrecorded; then
  recover_from=1
  recover_why="The landed phase-approval.json names the final phase (final_phase: true) and no commit since it has recorded a completion — recording it before starting."
elif completion_in_flight && ! boundary_complete; then
  # v0.18.2 (review round 6): this iteration's committed completion stopped
  # mid-landing and a later pass began before its walk (the record's current
  # boundary is idle) — plain mode has no squash_pending to witness it.
  recover_from=1
  recover_why="The committed completion record is this iteration's and its landing never finished — continuing it before starting (v0.18.2)."
elif kept_out_claim_only; then
  recover_from=2
  recover_why="The last commit is an unverified wip that kept the deploy claim out — landing the claim with its tree and proving the boundary before starting (v0.18.0)."
elif [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]] \
     && ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json" \
     && [[ ! -f "$ARTIFACTS_DIR/project-complete.json" ]] \
     && ! boundary_phase_rested "$(jq -r '.phase // "unknown"' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)" \
     && _boundary_tree_clean; then
  # An approval that landed outside the sequence — a hand commit, a wip the
  # watchdog made seconds before a kill — has no rested boundary on record.
  # The walk proves it (trivially, in plain mode) so the record says what git
  # says; the first run after the upgrade does this once per project.
  recover_from=2
  recover_why="The landed phase-approval.json (phase $(jq -r '.phase // "unknown"' "$ARTIFACTS_DIR/phase-approval.json" 2>/dev/null)) has no rested boundary on record — proving its sequence before starting."
fi
boundary_recorded_step="$(boundary_step)"
if [[ "$boundary_recorded_step" -ge 1 && "$boundary_recorded_step" -lt "$BOUNDARY_STEP_RESTED" ]]; then
  boundary_shas_ok=1
  boundary_recorded_branch="$(boundary_get '.branch // empty')"
  while IFS= read -r _bsha; do
    [[ -n "$_bsha" ]] || continue
    if git merge-base --is-ancestor "$_bsha" HEAD 2>/dev/null; then continue; fi
    if squash_mode && git merge-base --is-ancestor "$_bsha" "refs/heads/$SQUASH_TARGET" 2>/dev/null; then continue; fi
    # After a final rest HEAD is the target; the branch commits that proved
    # steps 2/3/5 live on the recorded work branch.
    if [[ -n "$boundary_recorded_branch" ]] && git rev-parse -q --verify "refs/heads/$boundary_recorded_branch" >/dev/null 2>&1 \
       && git merge-base --is-ancestor "$_bsha" "refs/heads/$boundary_recorded_branch" 2>/dev/null; then continue; fi
    boundary_shas_ok=0
    break
  done < <(boundary_get '.sha_at_step // {} | to_entries[] | .value')
  if [[ "$boundary_shas_ok" -eq 1 ]]; then
    if [[ -z "$recover_why" ]]; then
      recover_from=1
      recover_why="boundary-state.json records step $boundary_recorded_step (${BOUNDARY_STEP_NAMES[$boundary_recorded_step]}) for phase $(boundary_get '.phase // "unknown"') — the previous session ended mid-sequence; continuing it before starting."
    fi
  else
    echo "boundary-state: the record's sha_at_step names commits neither HEAD, the target nor the recorded work branch reaches (an operator moved HEAD?) — discarding the record; recovery uses git's own evidence only." >&2
    _boundary_write '.step = 0 | .step_name = "idle" | .discarded_at = $now | .discarded_reason = "sha_at_step unreachable from HEAD, the target or the recorded work branch"'
  fi
fi
if [[ -n "$recover_why" ]]; then
  echo "$recover_why"
  if [[ -f "$ARTIFACTS_DIR/project-complete.json" ]]; then
    print_json_summary "$ARTIFACTS_DIR/project-complete.json" || true
  elif [[ -f "$ARTIFACTS_DIR/phase-approval.json" ]]; then
    print_json_summary "$ARTIFACTS_DIR/phase-approval.json" || true
  fi
  # ANY-age approval is deliberate at THIS site (v0.12.3 review): both
  # artifacts stranded together came from one dead session, so pairing them
  # is the likeliest truth — the wrong-phase risk the in-loop site guards
  # against does not apply to a tree no new iteration has touched.
  crc=0
  land_boundary "$recover_from" stranded 1 || crc=$?
  # v0.14.9: complete is terminal — whatever the walk's rc (a step-7 rest it
  # could not prove included), a landed completion ends the session here:
  # entering the loop would delete project-complete.json and spend a session
  # on a complete project (v0.14.0 review, MAJOR-2; run 756).
  if boundary_complete; then
    finish_complete "boundary-state: the recovered boundary was the project's last — complete; no model turn (v0.14.9)."
  fi
  if [[ "$crc" -eq 0 || "$crc" -eq 2 ]]; then
    if boundary_final && [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] \
       && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
      echo "Run finished successfully."
      exit 0
    fi
  elif [[ "$BRANCH_INTEGRITY_BLOCKED" -eq 1 ]]; then
    echo "Stopping: the landing cannot proceed — the work branch cannot be squashed onto ${SQUASH_TARGET:-its target}, or the completion's tree cannot be judged (see artifacts/phase-blocked.json)." >&2
    print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
    exit 2
  else
    if artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
      # Keep the retry marker so the first boundary commits it under its own
      # message even if the model never re-touches the artifact.
      PENDING_COMMIT_RETRY="phase-approval"
      echo "Stranded phase-approval.json did not pass the commit gates — its commit will be retried at the first iteration boundary." >&2
    fi
    if artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
      echo "Stranded completion did not pass the commit gates — entering the loop to fix and re-complete; the record stays on disk, unstaged, for the repair turn to edit (v0.18.0)." >&2
    elif ! artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"; then
      echo "boundary-state: the sequence could not finish (see above) — entering the loop; the next boundary retries it." >&2
    fi
  fi
fi

while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do
  # v0.14.9: complete is terminal. Checked FIRST — before pacing, before the
  # wrap-up sentinel, before a pass: a session that starts (or continues)
  # on an iteration whose completion already landed does nothing else. No
  # next pass looking for a phase that does not exist (run 756's
  # phase-blocked.json), no wrap-up re-running the gate over a landed
  # completion (iteration 50's commit on the target), no baton.
  if boundary_complete_here; then
    finish_complete "boundary-state: this iteration is already complete (record final, step $(boundary_step), iteration $(boundary_get '.iteration // "unlabelled"'), branch $(boundary_get '.branch // "?"')) — no pass, no wrap-up (v0.14.9). To resume work on a complete project, remove artifacts/project-complete.json (the completion record) and re-run — a supervisor's next-iteration intake does exactly that."
  fi
  # Pass-duration bookkeeping (v0.6.1): each trip through the loop top closes
  # the previous pass. Retried attempts count as passes too — that keeps the
  # average conservative, which is the right direction for pacing.
  now_ts="$(date +%s)"
  if [[ -n "$last_pass_start" ]]; then
    pass_elapsed_total=$((pass_elapsed_total + now_ts - last_pass_start))
    passes_done=$((passes_done + 1))
    cost_sample pass "$((now_ts - last_pass_start))"
  fi
  last_pass_start="$now_ts"

  # Soft wrap-up check (v0.6.0): honored between iterations, never mid-flight.
  # v0.18.0: a turn the deadline watchdog ended is a wrap-up at this boundary
  # even if the sentinel was consumed.
  if [[ -f "$WRAPUP_SENTINEL" || "$TOOK_CONTROL" == 1 ]]; then
    if [[ "$TOOK_CONTROL" == 1 ]]; then
      echo "=== Wrap-up requested (the deadline watchdog took control) — not starting iteration $iteration ==="
    else
      echo "=== Wrap-up requested (sentinel present) — not starting iteration $iteration ==="
    fi
    rm -f "$WRAPUP_SENTINEL"
    wrapup_commit
    if [[ "$WRAPUP_UNVERIFIED" == 1 ]]; then
      echo "Run wrapped up with UNVERIFIED work committed (soft stop) — see the wip commit and session-handoff.json."
    else
      echo "Run wrapped up cleanly (soft stop)."
    fi
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
      # v0.18.0: seeded from the ledger's pass P90 as well — a session whose
      # first pass was short still knows how long a pass of this project
      # takes. Never before the first pass: a session is dispatched to work.
      pacing_p90="$(cost_p90 pass)"
      if [[ -n "$pacing_p90" && "$pacing_p90" -gt "$pacing_threshold" ]]; then pacing_threshold="$pacing_p90"; fi
    fi
    if [[ "$remaining" -lt "$pacing_threshold" ]]; then
      echo "deadline pacing: not starting iteration $iteration (${remaining}s remain, threshold ${pacing_threshold}s from $passes_done completed passes)"
      wrapup_commit
      if [[ "$WRAPUP_UNVERIFIED" == 1 ]]; then
        echo "Run wrapped up with UNVERIFIED work committed (deadline pacing) — see the wip commit and session-handoff.json."
      else
        echo "Run wrapped up cleanly (deadline pacing)."
      fi
      exit 0
    fi
  fi

  echo "=== Iteration $iteration ==="
  # One verdict retry per iteration (see the backstop below).
  verdict_retry_used=0
  LAST_GATE_RED=0   # v0.18.0: only this iteration's gates speak for its wrap-up
  cleanup_artifacts
  boundary_begin "$iteration"
  touch "$ITER_START_MARKER"
  # Dead-man baton: overwritten here every iteration, removed by the EXIT
  # trap when the iteration concludes with a verdict, and left behind by a
  # kill — see write_provisional_handoff.
  write_provisional_handoff "$iteration"

  # First attempt of iteration 1 in `new` mode uses fresh-session semantics;
  # retries (and every later iteration) use `continue` so they resume the
  # session that was just established rather than starting a new one — BY ID
  # (v0.18.8: run-phase.sh records the first turn's session id and resumes
  # exactly that conversation; never `claude -c`, whose "most recent
  # conversation" could be another project's).
  rc=0
  turn_started_at="$(date +%s)"
  if [[ "$iteration" -eq 1 && "$CLAUDE_MODE" == "new" && "$retries_used" -eq 0 ]]; then
    run_once "$PROMPT_FILE" "new" "$iteration" "$retries_used" || rc=$?
  else
    run_once "$PROMPT_FILE" "continue" "$iteration" "$retries_used" || rc=$?
  fi

  # v0.18.0: did the deadline watchdog end this turn (take-control)? Then
  # the exit code is the SIGTERM's, not a transient CLI failure: no CLI
  # retry, no verdict retry — the loop lands what the turn left (a verdict
  # through its own verify-gated path) and wraps up at the next boundary.
  # v0.18.1 (row 1193 (3)): a marker the watchdog could not write no longer
  # turns that into a CLI retry — a turn that ended non-zero across its
  # take-control instant is read as taken, and said so.
  # The guard's own SIGTERM is never read as a take-control (review round 1).
  if took_control_this_iteration \
     || { ! completion_guard_ended_turn "$ITER_START_MARKER" "$rc" && took_control_inferred "$turn_started_at" build "$rc"; }; then
    TOOK_CONTROL=1
    echo "deadline watchdog: took control — the model's turn was ended at the take-control point (claude exited $rc); landing what stands, then wrapping up (no CLI retry, no verdict retry)."
    # (The loop's landing and wrap-up mark themselves in flight; the
    # last-resort commit stands down for both — no marker needed here.)
    release_stale_index_lock
    rc=0
  elif completion_guard_ended_turn "$ITER_START_MARKER" "$rc"; then
    # v0.18.1 (row 831 (a)): the turn committed the completion record — the
    # iteration is terminal. Ended there by the guard (its exit code is the
    # SIGTERM's): no CLI retry, no verdict retry — the loop lands it.
    if [[ "$rc" != 0 || -n "$(jq -r '.pid // empty' "$ARTIFACTS_DIR/logs/.completion-yield" 2>/dev/null)" ]]; then
      echo "completion guard: the completion record was committed during the turn ($(git log -1 --format=%h HEAD -- artifacts/project-complete.json 2>/dev/null)) — the iteration is terminal, so the turn was ended there (claude exited $rc; v0.18.1); landing it — a write made after that commit is restored at the rest."
      release_stale_index_lock
      rc=0
    else
      echo "completion guard: the turn committed the completion record itself ($(git log -1 --format=%h HEAD -- artifacts/project-complete.json 2>/dev/null)) and ended on its own — landing it; a write made after that commit is restored at the rest (v0.18.1)."
      record_close_out_sample
    fi
  else
    record_close_out_sample
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
  # v0.18.2: nor while a COMMITTED completion record claims — the completion
  # site below lands the rest of the tree whatever the turn wrote (review
  # round 3, MAJOR 2: a retry every pass asked for a record that exists).
  if [[ "$verdict_retry_used" -eq 0 ]] \
     && [[ "$TOOK_CONTROL" != 1 ]] \
     && [[ -z "$PENDING_COMMIT_RETRY" ]] \
     && ! { completion_record_claims && completion_landed_at_head; } \
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
start new work.
The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree, push) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
VERDICT_RETRY_EOF
    vrc=0
    turn_started_at="$(date +%s)"
    run_once "$verdict_prompt" "continue" "$iteration" 0 || vrc=$?
    rm -f "$verdict_prompt"
    if took_control_this_iteration \
       || { ! completion_guard_ended_turn "$ITER_START_MARKER" "$vrc" && took_control_inferred "$turn_started_at" build "$vrc"; }; then
      TOOK_CONTROL=1
      echo "deadline watchdog: took control — the verdict request's turn was ended at the take-control point; wrapping up what stands."
      release_stale_index_lock
    fi
    if [[ "$vrc" -ne 0 ]]; then
      echo "  Verdict request exited $vrc — continuing to the loop's own handling." >&2
    fi
  fi

  if completion_record_claims; then
    echo "Project complete artifact detected:"
    print_json_summary "$ARTIFACTS_DIR/project-complete.json"
    # Light mode: one review pass on the default model BEFORE the final commit
    # (decided fork A). The reviewer may fix defects in place, or withdraw the
    # completion by swapping the artifact for phase-blocked.json.
    light_review_skip=0
    if [[ "$ITERATION_MODE" == "light" && "$light_review_done" -eq 0 ]] \
       && [[ -n "$(completion_blob_at_head)" && "$(boundary_get '.completion_snapshot.blob // empty')" == "$(completion_blob_at_head)" ]]; then
      # v0.18.1 (row 831): the review precedes the final commit — and the
      # build turn committed the completion itself (the guard saw it). What
      # the turn wrote after that commit is restored first. If the commit
      # then carries the whole tree, the iteration is terminal and the review
      # does not run: it could only write what the rest restores (iteration
      # 142's review edited tests/test_record_run.py after the commit). If
      # older work is still uncommitted (a record-only commit), the review
      # runs exactly as in v0.18.0 — a record it re-writes with new bytes
      # lands that work through the verify-gated completion commit — and,
      # like every turn the
      # loop dispatches, its writes are never residue (run_once drops the
      # snapshot).
      completion_residue_settle || true
      # Squash mode only (review round 2): there the catch-up squash still
      # runs the verify gate before the tree reaches the target. In plain
      # mode nothing after a model's own commit gates it, so the review
      # keeps its v0.18.0 role (its re-written record lands through the
      # verify-gated completion commit).
      if squash_mode && _boundary_tree_clean; then
        # Not marked done (review round 3): if this landing goes red, the
        # repaired completion still gets its review.
        light_review_skip=1
        echo "Light mode: the completion record is already committed ($(git log -1 --format=%h HEAD -- artifacts/project-complete.json 2>/dev/null)) and carries the whole tree — the iteration is terminal, so the final review, which precedes the final commit, does not run (v0.18.1)."
      fi
    fi
    if [[ "$ITERATION_MODE" == "light" && "$light_review_done" -eq 0 && "$light_review_skip" -eq 0 ]]; then
      light_review_done=1
      run_light_final_review
      if [[ "${REVIEW_ENDED:-0}" == 1 ]]; then
        wrapup_commit
        echo "Run wrapped up (take-control during the light review) — the completion is re-claimed by the next session."
        exit 0
      fi
      if ! completion_record_claims; then
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
    #
    # FRESH approvals only at this site (v0.12.3, review MAJOR): a STALE
    # uncommitted approval reaching this gate would sweep THIS iteration's
    # completion work under the old phase's message — the mislabeling class
    # the stranded-at-start site's any-age pairing is exempt from. Fresh =
    # written this iteration, or the pending-retry marker names it (the
    # staged work belongs to that phase, so its message is right — the same
    # two conditions the phase-commit branch below trusts). v0.14.5: the
    # sequence itself is land_boundary's — the phase commit under its own
    # message first (commit_pending_approval_first, step 2; a red one stops
    # the sequence there, so the same red tree is never verified twice), then
    # the completion commit (step 3), the squash, the merge-back, the rest.
    approval_fresh=0
    if artifact_written_this_iteration "$ARTIFACTS_DIR/phase-approval.json" \
       || [[ "$PENDING_COMMIT_RETRY" == "phase-approval" ]]; then
      approval_fresh=1
    fi
    crc=0
    land_boundary 1 completion "$approval_fresh" || crc=$?
    # v0.14.9: complete is terminal, whatever the walk's rc (see finish_complete).
    if boundary_complete; then finish_complete; fi
    if [[ "$crc" -eq 0 || "$crc" -eq 2 ]]; then
      # 0 = final work committed; 2 = nothing substantive left (already
      # committed) — both a clean finish, the target carrying it and HEAD at
      # rest, or the sequence would not have reached step 7.
      echo "Run finished successfully."
      exit 0
    fi
    maybe_escalate_light_commit "$crc"
    # Verify gate failed on the final commit: the completion claim is not
    # backed by passing checks. Re-enter the loop so the next iteration sees
    # phase-verify-failed.json and fixes it (v0.18.0: the record is carried
    # on disk, unstaged — the model edits and re-writes it once green; until
    # then it claims nothing). The VERIFY_MAX_ATTEMPTS circuit breaker still
    # bounds this via phase-blocked.json.
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
    # v0.14.5: the whole landing sequence is land_boundary's — the commit
    # under the approval's own message, the squash, the merge-back, the
    # record. any_age=1: this site established freshness above.
    crc=0
    land_boundary 1 iteration 1 || crc=$?
    # v0.14.9: an approval that carried final_phase: true and whose
    # completion landed is terminal, whatever the walk's rc.
    if boundary_complete; then PENDING_COMMIT_RETRY=""; finish_complete; fi
    if [[ "$crc" -eq 0 || "$crc" -eq 2 ]]; then
      PENDING_COMMIT_RETRY=""
      if boundary_final && [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] \
         && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
        # The approval carried final_phase: true — the completion record was
        # written, committed and squashed inside this landing (step 3), so
        # the project is done here with no further model turn
        # (2026-09-08 06:22: the turn that never came).
        echo "Run finished successfully."
        exit 0
      fi
      if [[ "$crc" -eq 2 && -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
        # Nothing substantive to commit AND the session declared itself
        # blocked — stop cleanly rather than spin (unchanged from v0.6.x).
        echo "Phase blocked; no substantive change to commit:"
        print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
        exit 2
      fi
      iteration=$((iteration + 1))
      continue
    fi
    maybe_escalate_light_commit "$crc"
    # No commit was made: either the verify gate failed (rc 1 — mark the
    # approval for a commit retry next iteration, even if the model forgets to
    # re-touch it after fixing), or a gate refused it. If phase-blocked.json
    # is present the iteration is genuinely blocked — stop cleanly rather
    # than spinning to MAX_ITERATIONS or committing churn. Otherwise re-enter
    # so Claude can make progress (or fix a verify failure) on the next
    # iteration.
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

  # v0.14.5: the session wrote nothing because there IS no next phase — the
  # landed approval names the final one and no completion record exists
  # (2026-09-08 06:22, in-session shape). Record the completion, on a CLEAN
  # tree only: unclaimed work is the no-verdict backstop's business above,
  # never a completion sweep's.
  if approval_final_unrecorded && [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "boundary-state: the landed approval names the final phase and the session wrote no artifact (no next phase) — recording the completion."
    crc=0
    land_boundary 1 completion 1 || crc=$?
    if boundary_complete; then finish_complete; fi
    if [[ "$crc" -eq 0 || "$crc" -eq 2 ]] && [[ -f "$ARTIFACTS_DIR/project-complete.json" ]] \
       && ! artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then
      echo "Run finished successfully."
      exit 0
    fi
    maybe_escalate_light_commit "$crc"
    if [[ -f "$ARTIFACTS_DIR/phase-blocked.json" ]]; then
      echo "Final commit blocked; completion not committed:"
      print_json_summary "$ARTIFACTS_DIR/phase-blocked.json"
      exit 2
    fi
    echo "Final commit failed verify — re-entering loop to fix before completing." >&2
    iteration=$((iteration + 1))
    continue
  fi

  if [[ "$TOOK_CONTROL" == 1 ]]; then
    # The turn was ended before it wrote a verdict: the wrap-up lands what
    # stands (verify-gated; red falls through to the labelled wip).
    echo "deadline watchdog: took control before the turn wrote a verdict — wrapping up what stands."
    wrapup_commit
    if [[ "$WRAPUP_UNVERIFIED" == 1 ]]; then
      echo "Run wrapped up with UNVERIFIED work committed (take-control) — see the wip commit and session-handoff.json."
    else
      echo "Run wrapped up cleanly (take-control)."
    fi
    exit 0
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