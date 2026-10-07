#!/usr/bin/env bash
set -euo pipefail
# PHASEKIT_TRACE=1 turns on bash xtrace so every wrapper command is visible.
# Loud but useful for debugging the autonomous loop. See docs/EXECUTION_MODES.md.
[[ "${PHASEKIT_TRACE:-}" == "1" ]] && set -x

# v0.19.0: the engine (this checkout) vs the project (PHASEKIT_PROJECT_DIR;
# the same directory in a vendored project — see run-until-done.sh).
ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${PHASEKIT_PROJECT_DIR:-}" ]]; then
  ROOT_DIR="$(cd "$PHASEKIT_PROJECT_DIR" && pwd)"
else
  ROOT_DIR="$ENGINE_DIR"
fi
PINNED=0
if [[ "$ROOT_DIR" != "$ENGINE_DIR" ]]; then PINNED=1; fi
PLUGIN_DIR="$ENGINE_DIR/plugin"
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
FORMATTER="$ENGINE_DIR/scripts/phasekit-log-fmt.sh"
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
if [[ "$PINNED" == 1 ]]; then
  CLAUDE_FLAGS+=(--plugin-dir "$PLUGIN_DIR")
fi
# Per-project model choice (v0.4.5): ANTHROPIC_MODEL carries an alias
# (fable/opus/sonnet/haiku) or a full claude-* id, set by the orchestrator
# via build_env → run-session → container-setup. Passed explicitly as
# --model rather than relying on env-var honoring. Empty/unset = CLI default.
if [[ -n "${ANTHROPIC_MODEL:-}" ]]; then
  CLAUDE_FLAGS+=(--model "$ANTHROPIC_MODEL")
fi

# The model process's pid, for the loop's deadline watchdog (v0.18.0): at the
# take-control point it ends a turn that has not yielded (SIGTERM to exactly
# this process — `exec` makes the subshell's pid claude's). "<pid> <role>",
# role = PHASEKIT_ITER (a pass number, or light-review). Removed when the
# turn ends; the watchdog also checks the pid is alive and is claude.
PIDFILE="$LOG_DIR/claude.pid"

# --- a pinned run: the engine outside the tree (v0.19.0) ----------------------
# A vendored project wires its hooks and agents in its own .claude/ — nothing
# here applies to it. A pinned project carries none: they come from the
# engine's plugin, passed with --plugin-dir, so the read-only engine is the
# only copy a session can reach (it cannot edit its own guard). The prompt
# names the engine's docs by their real path (a `docs/<NAME>.md` this tree
# lacks is the engine's), and every turn first proves the plugin's guard is
# live (guard_probe): a turn whose hooks did not load is REFUSED, never run
# unguarded.
# The model's own tool calls (a project test that runs a phasekit fixture)
# must never inherit the engine's pointer at this tree.
export -n PHASEKIT_PROJECT_DIR 2>/dev/null || true
GUARD_REFUSED_RC=7

# Rewrite `docs/<NAME>.md` to the engine's copy for every engine doc this
# project does not have (its own docs keep their paths), then say where the
# engine is. Prints the rendered prompt.
# The engine's PROCESS docs — the scaffold-class docs a vendored project
# carries (tests/test_engine_outside.py pins this list to the manifest). Its
# seed templates (SPEC, PHASES, …) and internal docs are never offered.
ENGINE_PROCESS_DOCS="ADR_TEMPLATE CONTAINERIZATION CONTRACTS EXECUTION_MODES INSTALL_LIFECYCLE MUTATION_TESTING QUALITY_GATES REASONING_PROFILES USAGE_PATTERNS"
render_pinned_prompt() {
  local text="$1" name ver repl
  # `&`, `\` and `#` would be sed syntax in the replacement
  repl="$(printf '%s' "$ENGINE_DIR" | sed -e 's/[\\&#]/\\&/g')"
  for name in $ENGINE_PROCESS_DOCS; do
    [[ -f "$ENGINE_DIR/docs/$name.md" ]] || continue
    [[ -e "$ROOT_DIR/docs/$name.md" ]] && continue
    text="$(printf '%s' "$text" | sed -E "s#(^|[^/A-Za-z0-9_.-])docs/$name\\.md#\\1$repl/docs/$name.md#g")"
  done
  ver="$(cat "$ENGINE_DIR/.engine-version" 2>/dev/null || git -C "$ENGINE_DIR" describe --tags --always 2>/dev/null || echo unknown)"
  cat <<ENGINE_EOF
=== PHASEKIT ENGINE (this session) ===
phasekit ${ver} runs this project from OUTSIDE the repository: the engine is
read-only at ${ENGINE_DIR} and is not project content.
- Its process docs are there, not in this tree: ${ENGINE_DIR}/docs/
  (QUALITY_GATES.md, USAGE_PATTERNS.md, …; also \$PHASEKIT_ENGINE_DOCS). A
  docs/<NAME>.md this repository lacks is one of them — read it there.
- Run phasekit as \`phasekit <verb>\` (verify, scope, facts, contracts check): it
  is on PATH and runs this engine against this project.
- Its subagents are namespaced: phasekit:project-lead, phasekit:code-reviewer,
  phasekit:strategy-planner, phasekit:architecture-red-team, …
- Never copy engine files into this repository.
=== END ENGINE ===

ENGINE_EOF
  printf '%s' "$text"
}

# The fail-closed self-check: a throwaway, model-free `claude -p` whose prompt
# the plugin's guard-probe hook (UserPromptSubmit) blocks after proving the
# guard refuses. Returns 0 only when that hook wrote this probe's token.
guard_probe() {
  local probe token got=""
  if [[ ! -f "$PLUGIN_DIR/hooks/hooks.json" || ! -f "$PLUGIN_DIR/.claude-plugin/plugin.json" ]]; then
    echo "phasekit: REFUSING the turn — the engine's plugin is missing at $PLUGIN_DIR (hooks.json / plugin.json), so this session would run without its command guard and stop hook" >&2
    return 1
  fi
  probe="$LOG_DIR/.guard-probe.$$"
  token="$(new_session_id)"; [[ -n "$token" ]] || token="probe-$$-$(date +%s)"
  rm -f "$probe" 2>/dev/null || true
  PHASEKIT_GUARD_PROBE="$probe" PHASEKIT_GUARD_PROBE_TOKEN="$token" \
    timeout "${PHASEKIT_GUARD_PROBE_TIMEOUT:-120}" \
    claude --plugin-dir "$PLUGIN_DIR" --no-session-persistence --permission-mode bypassPermissions \
      -p "phasekit guard probe (blocked by the plugin before any model call)" >/dev/null 2>&1 || true
  got=""
  if [[ -r "$probe" ]]; then IFS= read -r got < "$probe" || got=""; fi
  rm -f "$probe" 2>/dev/null || true
  if [[ "$got" == "$token ok" ]]; then
    return 0
  fi
  echo "phasekit: REFUSING the turn — the guard self-check failed (${got:-the guard-probe hook of the plugin never ran, so the plugin did not load}). A pinned run never starts a model turn without its command guard and stop hook (plugin: $PLUGIN_DIR)." >&2
  return 1
}
trap 'rm -f "$PIDFILE" "${TURN_MARK:-}" 2>/dev/null' EXIT

# --- session continuity (v0.18.8) --------------------------------------------
# A run is ONE conversation: its first turn starts it, and every later turn —
# the next iteration, a CLI retry, the verdict request — resumes it BY ID.
# Never `claude -c`: that resumes "the most recent conversation in this
# directory", and every fleet session works in /workspace with one shared
# config volume, so the most recent conversation could be another project's
# (2026-10-06: 15 shared transcripts held two projects' turns, three of them
# xmeo-v3 turns that resumed a foundry-orchestrator session — another
# project's history driving edits in this tree). container-setup.sh also gives
# each project its own transcript directory; the id is the half that holds
# wherever the transcripts live.
#
# The id lives in artifacts/logs/claude-session-id (the loop's own logs: never
# committed, never in git status, kept across sessions). A NEW turn chooses its
# id up front (--session-id) and records it BEFORE the model starts, so a turn
# killed at any instant still names its conversation; after the turn, the id
# the CLI reports in its `system`/`init` event is recorded (it wins if the two
# ever differ). A turn that CONTINUES resumes the recorded id. Fail safe, never
# another conversation: no usable id, or a resume the CLI refused because it
# has no such conversation (an id from elsewhere, a transcript that is gone;
# see resume_not_honoured — nothing else), starts a NEW session whose prompt
# first says so and points it back at the tree. The loop's light review is a side conversation: a new
# session that records nothing, so the run's own conversation stays the one
# later turns resume. CLAUDE_MODE=continue at a run's start (an operator's
# entry point) resumes the last run's recorded conversation the same way.
SESSION_ID_FILE="$LOG_DIR/claude-session-id"
SESSION_ID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
SIDE_TURN=0
if [[ "${PHASEKIT_ITER:-}" == "light-review" ]]; then SIDE_TURN=1; fi

# The recorded id, or nothing (absent, unreadable, or not a session id).
recorded_session_id() {
  local sid=""
  if [[ -r "$SESSION_ID_FILE" ]]; then
    IFS= read -r sid < "$SESSION_ID_FILE" || true
  fi
  if [[ "$sid" =~ $SESSION_ID_RE ]]; then
    printf '%s' "$sid"
  fi
  return 0
}

# Atomic and never fatal: a turn's exit code is never a bookkeeping failure's.
record_session_id() {
  local tmp="$SESSION_ID_FILE.tmp.$$"
  if [[ -d "$SESSION_ID_FILE" ]]; then
    echo "WARN: $SESSION_ID_FILE is a directory — session id $1 not recorded (later turns of this run start new sessions)" >&2
    return 0
  fi
  { printf '%s\n' "$1" > "$tmp" && mv -f "$tmp" "$SESSION_ID_FILE"; } 2>/dev/null \
    || { rm -f "$tmp" 2>/dev/null || true; echo "WARN: could not record session id $1" >&2; }
  return 0
}

# A fresh id for a new session (Linux, then the usual tools); nothing if none.
new_session_id() {
  local sid=""
  sid="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)" \
    || sid="$(uuidgen 2>/dev/null)" \
    || sid="$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null)" \
    || sid=""
  sid="$(printf '%s' "$sid" | tr 'A-F' 'a-f')"
  if [[ "$sid" =~ $SESSION_ID_RE ]]; then
    printf '%s' "$sid"
  fi
  return 0
}

# The id the CLI reports in its first `system`/`init` event in $1 (a turn's raw
# stream-json, stderr mixed in), or nothing — the CLI starts no session it
# cannot resume, and then emits no init.
reported_session_id() {
  local sid=""
  sid="$(grep -m1 -F '"subtype":"init"' "$1" 2>/dev/null \
         | jq -r 'select(.type == "system") | .session_id // empty' 2>/dev/null)" || sid=""
  if [[ "$sid" =~ $SESSION_ID_RE ]]; then
    printf '%s' "$sid"
  fi
  return 0
}

# True when a resume ($1 = its raw log, $2 = its exit code) was refused because
# the CLI has no such conversation — and nothing else: exit 1 (a turn ended by
# a signal exits 128+N and is never read as this), no `init` (the CLI started
# no session), and the CLI's own words for it (probed on 2.1.289; with or
# without its zero-turn `result`). An auth or credit failure is NOT this: the
# run's conversation is kept for the retry. Never once the loop is taking the
# session back (the wrap-up sentinel, a take-control or completion yield since
# this turn began): a turn the loop ended stays ended.
resume_not_honoured() {
  [[ "$2" == 1 ]] || return 1
  [[ -z "$(reported_session_id "$1")" ]] || return 1
  grep -qF 'No conversation found with session ID' "$1" 2>/dev/null || return 1
  local m
  for m in "$ROOT_DIR/artifacts/wrapup-requested" "$LOG_DIR/.deadline-yield" "$LOG_DIR/.completion-yield"; do
    if [[ -e "$m" && ( "$m" == */wrapup-requested || -z "$TURN_MARK" || "$m" -nt "$TURN_MARK" ) ]]; then
      return 1
    fi
  done
  return 0
}

REANCHOR_NOTE='NOTE FROM THE LOOP: this turn was meant to continue this run'"'"'s earlier
conversation, but that conversation could not be resumed (REASON). You are
starting a NEW session with no memory of earlier turns. Re-anchor from the
repository before acting: read artifacts/session-handoff.json if it exists,
docs/PHASES.md and the artifacts/ records, then inspect `git status`,
`git diff HEAD` and `git log --oneline -10` — the tree holds the work done so
far. Then do what the prompt below asks.

'

# One claude invocation. $1 = tee mode ("" or -a), the rest = extra flags.
run_claude() {
  local tee_mode="$1"; shift
  ( echo "$BASHPID ${PHASEKIT_ITER:-manual}" > "$PIDFILE"
    exec claude "${CLAUDE_FLAGS[@]}" "$@" -p "$PROMPT_CONTENT" ) 2>&1 \
    | tee $tee_mode "$RAW_LOG" \
    | "${FORMAT_CMD[@]}" \
    | tee $tee_mode "$LOG_FILE"
}

# A NEW session: its id chosen and recorded before the model starts.
# $1 = tee mode.
start_new_session() {
  local sid
  sid="$(new_session_id)"
  if [[ "$SIDE_TURN" == 1 ]]; then
    run_claude "$1"
  elif [[ -n "$sid" ]]; then
    record_session_id "$sid"
    run_claude "$1" --session-id "$sid"
  else
    rm -f "$SESSION_ID_FILE" 2>/dev/null || true
    run_claude "$1"
  fi
}

if [[ "$PINNED" == 1 ]]; then
  PROMPT_CONTENT="$(render_pinned_prompt "$PROMPT_CONTENT")"
  if ! guard_probe; then
    exit "$GUARD_REFUSED_RC"
  fi
fi

rc=0
# This turn's start, for "a yield since this turn began" (resume_not_honoured).
TURN_MARK="$LOG_DIR/.turn-start.$$"
: > "$TURN_MARK" 2>/dev/null || TURN_MARK=""   # then any yield marker counts
if [[ "$CLAUDE_MODE" == "continue" ]]; then
  RESUME_ID="$(recorded_session_id)"
  if [[ -n "$RESUME_ID" ]]; then
    echo "Resuming this run's conversation: $RESUME_ID"
    run_claude "" --resume "$RESUME_ID" || rc=$?
    # The refused resume's pid is dead: never leave it for the watchdog.
    rm -f "$PIDFILE" 2>/dev/null || true
    if [[ "$rc" -ne 0 ]] && resume_not_honoured "$RAW_LOG" "$rc"; then
      echo "WARN: the CLI could not resume $RESUME_ID (exit $rc, no session started) — starting a NEW session with a re-anchoring prompt instead (never another conversation)." >&2
      PROMPT_CONTENT="${REANCHOR_NOTE/REASON/the CLI could not resume session $RESUME_ID}${PROMPT_CONTENT}"
      rc=0
      start_new_session -a || rc=$?
    fi
  else
    echo "WARN: no recorded session id for this run (artifacts/logs/claude-session-id absent or unreadable) — starting a NEW session with a re-anchoring prompt (never 'the most recent conversation')." >&2
    PROMPT_CONTENT="${REANCHOR_NOTE/REASON/no session id was recorded}${PROMPT_CONTENT}"
    start_new_session "" || rc=$?
  fi
else
  start_new_session "" || rc=$?
fi
# The id the CLI actually ran under wins (a CLI whose resume forks, a
# --session-id it did not take). A side turn records nothing.
if [[ "$SIDE_TURN" != 1 ]]; then
  REPORTED_ID="$(reported_session_id "$RAW_LOG")"
  if [[ -n "$REPORTED_ID" && "$REPORTED_ID" != "$(recorded_session_id)" ]]; then
    record_session_id "$REPORTED_ID"
  fi
fi
exit "$rc"