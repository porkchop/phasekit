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
#   (b) ONCE PER ITERATION, PER AGENT (v0.18.0). The first nudge writes a
#       marker; while the marker is fresh (newer than the loop's own
#       iteration marker — the `-nt` freshness idiom require-verdict.sh
#       uses) a PreToolUse call is not refused again. The marker is keyed on
#       the AGENT: the harness puts `agent_id` in the payload of a subagent's
#       tool calls and nothing in the main agent's (probed in scaffold-runner,
#       claude 2.1.282, 2026-09-28). Until v0.17.0 one marker served every
#       agent, so a subagent could spend the main agent's nudge — xmeo run
#       999: a code-review subagent received it, called it "a stale hook
#       artifact", and the main agent never heard it. The main agent's
#       marker is artifacts/.wrapup-nudge-sent (the loop reads its mtime as
#       "nudge delivered"); a subagent's lives under artifacts/logs/. The
#       loop clears all of them at iteration start beside the stop-hook
#       counter.
#
#   (b') POSTTOOLUSE REPEATS, AT MOST ONCE PER 60 s PER AGENT. Feedback after
#       a completed call costs no work, and a model deep in a long turn
#       loses a single line; the refusal (PreToolUse) stays once. The
#       loop's take-control (v0.18.0) ends a turn that still has not yielded,
#       so the repeats are bounded by the deadline itself.
#
#   (c) BLOCKS AT MOST ONE TOOL CALL PER AGENT. On PostToolUse, exit 2
#       cannot un-run the tool — pure feedback. On PreToolUse it refuses the
#       one about-to-launch call; the marker is already written, so
#       re-issuing the same call sails through. The message names the
#       close-out order (verdict → `phasekit verify` → end the turn), so the
#       model has a productive response, not a scolding.
#
# INERT unless the phasekit loop is driving the session (same rule and same
# variables as require-verdict.sh): an interactive session has no deadline
# and must not be nagged.
#
# Fail-open everywhere: a broken hook must never be able to wedge a session.

set -u

# Drain the JSON payload; the sentinel question is answered from disk, the
# agent's identity (v0.18.0) from the payload — read only when a nudge is due.
_payload="$(cat 2>/dev/null)" || _payload=""

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

# --- who is asking ------------------------------------------------------------
# A subagent's calls carry agent_id; the main agent's carry none. Anything
# unreadable is the main agent (the conservative reading: it is the one the
# nudge exists for). The id becomes a file name only through a strict filter.
_agent=""
_event=""
if command -v jq >/dev/null 2>&1 && [[ -n "$_payload" ]]; then
  _agent="$(jq -r '.agent_id // empty' <<<"$_payload" 2>/dev/null | tr -cd 'A-Za-z0-9_.-' | cut -c1-64)" || _agent=""
  _event="$(jq -r '.hook_event_name // empty' <<<"$_payload" 2>/dev/null)" || _event=""
fi
if [[ -z "$_agent" ]]; then
  _marker="$PHASEKIT_ARTIFACTS_DIR/.wrapup-nudge-sent"
  _last="$PHASEKIT_ARTIFACTS_DIR/logs/.wrapup-nudge-last.main"
else
  _marker="$PHASEKIT_ARTIFACTS_DIR/logs/.wrapup-nudge-sent.$_agent"
  _last="$PHASEKIT_ARTIFACTS_DIR/logs/.wrapup-nudge-last.$_agent"
fi

# --- once per iteration per agent; PostToolUse repeats at most every 60 s ---
# Fresh marker (newer than the iteration start) => this agent was already
# nudged this iteration. A stale marker from an earlier iteration does not
# silence us — the `-nt` test, not bare existence, is what decides
# (require-verdict.sh property (c), same reasoning).
if [[ -f "$_marker" && "$_marker" -nt "$PHASEKIT_ITER_MARKER" ]]; then
  [[ "$_event" == "PostToolUse" ]] || exit 0
  _now="$(date +%s)"
  _then="$(stat -c %Y "$_last" 2>/dev/null || echo "$_now")"
  [[ "$_then" =~ ^[0-9]+$ ]] || exit 0
  (( _now - _then >= 60 )) || exit 0
  touch "$_last" 2>/dev/null || exit 0
else
  # Cannot record the nudge => cannot bound it => stay silent rather than
  # nag on every tool call from here to the kill.
  mkdir -p "$(dirname "$_last")" 2>/dev/null || true
  touch "$_marker" 2>/dev/null || exit 0
  touch "$_last" 2>/dev/null || true
fi

cat >&2 <<'WRAPUP_EOF'
[wrapup-nudge] The supervisor has requested WRAP-UP: this session is inside
its final minutes. At the take-control point the loop ENDS this turn itself,
and at the deadline it is killed — every uncommitted edit and background task
goes with it.

Stop expanding scope NOW. No new work, no new reviews, no memory writes.

Close out in this order, then end your turn:
  1. Write your verdict: artifacts/phase-update.json (the phase is not done —
     almost always right here), phase-approval.json, or project-complete.json.
  2. Run `bash scripts/phasekit.sh verify` ONCE — the gate exactly as the
     commit runs it; a green verdict is reused by the commit, not repeated.
  3. End your turn without changing anything after a green verify.

Partial, committed progress is the designed outcome here; a dead tree is not.
If you are a subagent: stop and return what you have to the main agent now.
WRAPUP_EOF
exit 2
