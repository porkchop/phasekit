#!/usr/bin/env bash
#
# Stop hook: a session may not end without a verdict.
#
# THE INCIDENT (foundry-orchestrator runs 288/289/290, 2026-08-16). Three
# consecutive sessions did real, green work and committed nothing. None timed
# out — they ran 22.6, 15.8 and 23.5 minutes against a 70-minute deadline. Each
# ended its own turn while its own background work was still running; the last
# one's final message was literally "Mutation run is still in its control
# phase. Waiting for it before the final verify and the re-review."
#
# run-phase.sh invokes `claude -p`, so when the agent emits a final response
# the process exits and every background task it spawned dies with it. The loop
# then found no artifact, exited 1 with no retry, and left a dirty tree that
# confused the next session. Three times.
#
# The missing invariant: SILENCE IS NOT AN ALLOWED OUTCOME.
#
# ---------------------------------------------------------------------------
# Contract: Stop fires when Claude finishes responding. Exit 2 BLOCKS the stop;
# the hook's STDERR is what Claude is shown. There is no documented
# `stop_hook_active` field and no built-in loop breaker — preventing an
# infinite block/stop cycle is the hook author's job, which is what the
# counter below is for.
#
# Three load-bearing properties:
#
#   (a) BOUNDED. At most PHASEKIT_STOP_BLOCK_LIMIT (default 2) blocks per
#       iteration, counted in a file the loop clears at iteration start. Then
#       it steps aside and lets the loop's own `exit 1` catch the session.
#       This is a prompt with teeth, never a cage.
#
#   (b) EVERY BLOCK NAMES STATE-CHANGING ACTIONS. A hook that only says "you
#       may not stop" leaves the agent no move except stopping again — burning
#       the budget and ending in the same silent failure at 3x the cost. See
#       the message below: each branch is something the agent can DO.
#
#   (c) NOT SATISFIED BY A STALE ARTIFACT. The loop runs several iterations per
#       session and phase-approval.json persists across them as the durable
#       record of the last approved phase. A hook fooled by that would be inert
#       exactly when it is needed. The freshness test is `-nt` against
#       PHASEKIT_ITER_MARKER — the same ITER_START_MARKER the loop itself uses
#       in artifact_written_this_iteration(), not a second mechanism.
#
# INERT unless the phasekit loop is driving the session (the loop exports the
# three PHASEKIT_* variables below). An interactive `claude` session has no
# iteration and no verdict to give, and blocking one would violate phasekit's
# standing rule that ordinary direct work must not be made cumbersome.
#
# Fail-open everywhere except the deliberate block: a broken hook must never be
# able to wedge a session.

set -u

# Drain the JSON payload so the caller never sees SIGPIPE. We deliberately do
# not parse it: the verdict question is answered from disk, and the fields this
# hook would want are not part of the documented contract.
cat >/dev/null 2>&1 || true

# --- inert outside the loop -------------------------------------------------
[[ -n "${PHASEKIT_VERDICT_ARTIFACTS:-}" ]] || exit 0
[[ -n "${PHASEKIT_ARTIFACTS_DIR:-}"     ]] || exit 0
[[ -n "${PHASEKIT_ITER_MARKER:-}"       ]] || exit 0
[[ -d "$PHASEKIT_ARTIFACTS_DIR"         ]] || exit 0
# No marker means the loop never started an iteration around this invocation.
[[ -f "$PHASEKIT_ITER_MARKER"           ]] || exit 0

# --- the happy path ---------------------------------------------------------
# One existence-and-freshness test per verdict kind, and nothing else. On a
# healthy session this is the entire cost of the hook and the counter file
# below is never created.
#
# The vocabulary is supplied BY THE LOOP (PHASEKIT_VERDICT_ARTIFACTS) rather
# than restated here, so the hook cannot disagree with the dispatcher about
# what counts as an ending.
for _artifact in $PHASEKIT_VERDICT_ARTIFACTS; do
  _path="$PHASEKIT_ARTIFACTS_DIR/$_artifact"
  if [[ -f "$_path" && "$_path" -nt "$PHASEKIT_ITER_MARKER" ]]; then
    exit 0
  fi
done

# --- bounded blocking -------------------------------------------------------
_limit="${PHASEKIT_STOP_BLOCK_LIMIT:-2}"
[[ "$_limit" =~ ^[0-9]+$ ]] || _limit=2
_counter="$PHASEKIT_ARTIFACTS_DIR/.stop-hook-blocks"
_blocks="$(cat "$_counter" 2>/dev/null || echo 0)"
[[ "$_blocks" =~ ^[0-9]+$ ]] || _blocks=0

if (( _blocks >= _limit )); then
  # Budget spent. Step aside: the loop's own no-artifact path takes it from
  # here, and (from this release) gets one retry of its own. Print to stderr so
  # the transcript records that the hook tried and gave up, but exit 0 so the
  # session is allowed to end.
  echo "[require-verdict] no verdict after ${_blocks} block(s) — releasing the session to the loop." >&2
  exit 0
fi

if ! echo "$(( _blocks + 1 ))" > "$_counter" 2>/dev/null; then
  # Cannot count => cannot bound => must not block.
  exit 0
fi

cat >&2 <<'VERDICT_EOF'
[require-verdict] You are ending your turn without writing a verdict artifact
for this iteration, so this iteration would end in silence. Silence is not an
allowed outcome: the loop cannot commit your work, and everything still running
in the background dies with this process.

Do ONE of these before you stop. Each one changes the situation; stopping again
unchanged does not.

1. WAITING ON BACKGROUND WORK? It will be killed the moment this turn ends —
   `claude -p` exits when you emit a final response, and every task you
   spawned goes with it. Waiting by stopping does not work and never has.
   Re-run it in the FOREGROUND, in chunks that fit inside the tool timeout.
   If you do not actually need its results, abandon it and land what you have.

2. HAVE WORK IN THE TREE? Write artifacts/phase-update.json. It commits your
   work AND the loop continues to the next iteration — you do not lose the
   thread and you do not have to claim the phase is done. This is almost
   always the right move.

3. GENUINELY STUCK? Write artifacts/phase-blocked.json saying what you need.

Write the artifact, then end your turn.
VERDICT_EOF
exit 2
