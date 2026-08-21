#!/usr/bin/env bash
#
# PreToolUse + PostToolUse hook: deliver the wrap-up request MID-ITERATION.
#
# THE INCIDENT (xmeo-v3 runs 388/390, 2026-08-21; class (a) of the deadline
# post-mortem). The supervisor's wrap-up timer fired on schedule and touched
# the sentinel at T-minus-300s — and the session never saw it. The loop polls
# the sentinel BETWEEN iterations, an iteration is one whole model turn, and a
# turn that spans the deadline is killed mid-sentence with real, uncommitted
# work in the tree. The wrapper's own diagnostic names the gap: "the wrap-up
# sentinel was never observed: one iteration spanned the deadline."
#
# A Stop hook cannot close this gap — Stop fires at turn end, the same
# boundary the loop already checks. The seams that reach a model mid-turn
# are the tool boundaries, and this hook sits on BOTH: PostToolUse (exit 2
# feeds stderr back as feedback after a completed call) and PreToolUse
# (exit 2 refuses the ABOUT-TO-LAUNCH call with the same message — the
# model must not START work that will not fit inside the deadline; the
# once-marker below means the refusal costs exactly one retried call).
# Aaron's straddling-tool case (08-21) is why the pre-boundary matters: a
# long tool launched just before the sentinel returns only after the kill,
# so the post-boundary alone can arrive too late.
#
# So: after any tool call, if the sentinel exists, say so — once. The model
# then lands what is verified via artifacts/phase-update.json and ends the
# turn; the loop observes the sentinel at the boundary it already owns and
# runs the wrap-up commit it already has. Nothing else changes hands.
#
# Load-bearing properties, in the require-verdict.sh mold:
#
#   (a) CHEAP ON THE HAPPY PATH. Two env tests and one -f test per tool call,
#       nothing else, no file created. The sentinel exists for at most the
#       last few minutes of a session.
#
#   (b) ONCE PER ITERATION. The first nudge writes a marker file; while the
#       marker is fresh (newer than the loop's own iteration marker — the
#       same `-nt` freshness idiom require-verdict.sh uses) the hook stays
#       silent. A nag on every tool call would drown the work it is trying
#       to land. The loop clears the marker at iteration start beside the
#       stop-hook counter.
#
#   (c) BLOCKS AT MOST ONE TOOL CALL, EVER. On PostToolUse, exit 2 cannot
#       un-run the tool — pure feedback. On PreToolUse it refuses the one
#       about-to-launch call; the marker is already written, so re-issuing
#       the same call sails through. The message names the one
#       state-changing move (land verified work via phase-update.json), so
#       the model has a productive response, not a scolding.
#
# INERT unless the phasekit loop is driving the session (same rule and same
# variables as require-verdict.sh): an interactive session has no deadline
# and must not be nagged.
#
# Fail-open everywhere: a broken hook must never be able to wedge a session.

set -u

# Drain the JSON payload; the sentinel question is answered from disk.
cat >/dev/null 2>&1 || true

# --- inert outside the loop -------------------------------------------------
[[ -n "${PHASEKIT_ARTIFACTS_DIR:-}" ]] || exit 0
[[ -d "$PHASEKIT_ARTIFACTS_DIR"     ]] || exit 0
[[ -n "${PHASEKIT_ITER_MARKER:-}"   ]] || exit 0
[[ -f "$PHASEKIT_ITER_MARKER"       ]] || exit 0

# The sentinel path is the loop's own default, overridable by the same
# variable the loop honours (contracts/interface.json: PHASEKIT_WRAPUP_SENTINEL).
_sentinel="${PHASEKIT_WRAPUP_SENTINEL:-$PHASEKIT_ARTIFACTS_DIR/wrapup-requested}"

# --- the happy path: no wrap-up requested -----------------------------------
[[ -f "$_sentinel" ]] || exit 0

# --- once per iteration ------------------------------------------------------
# Fresh marker (newer than the iteration start) => already nudged this
# iteration. A stale marker from an earlier iteration does not silence us —
# the `-nt` test, not bare existence, is what decides (require-verdict.sh
# property (c), same reasoning).
_marker="$PHASEKIT_ARTIFACTS_DIR/.wrapup-nudge-sent"
if [[ -f "$_marker" && "$_marker" -nt "$PHASEKIT_ITER_MARKER" ]]; then
  exit 0
fi
# Cannot record the nudge => cannot bound it => stay silent rather than nag
# on every tool call from here to the kill.
touch "$_marker" 2>/dev/null || exit 0

cat >&2 <<'WRAPUP_EOF'
[wrapup-nudge] The supervisor has requested WRAP-UP: this session is inside
its final minutes and will be killed at the deadline, taking every
uncommitted edit and background task with it.

Stop expanding scope NOW. Do not start new work, new reviews, or new
verification beyond what the commit gate itself requires.

Land what is already verified: write artifacts/phase-update.json describing
the state of the phase, then end your turn. The loop will commit your work
and hand the remainder to the next session — partial, committed progress is
the designed outcome here; a dead tree is not.
WRAPUP_EOF
exit 2
