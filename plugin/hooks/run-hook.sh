#!/usr/bin/env bash
#
# phasekit plugin hook dispatcher (v0.19.0).
#
# The plugin (plugin/hooks/hooks.json) wires phasekit's four hooks through this
# one script: `run-hook.sh <name>` runs .claude/hooks/<name>.sh from the SAME
# engine (plugin/hooks/scripts is a link to it; a plugin cache copy carries the
# files themselves), so the hooks have one source and the read-only engine
# mount is the only copy a session can reach.
#
# Two duties besides running the hook:
#
#   * A VENDORED project wires the same hooks itself, in its own
#     .claude/settings.json, to its own copies. When that wiring is present
#     (the settings name .claude/hooks/<name>.sh AND the file exists) the
#     plugin steps aside, so a vendored project behaves exactly as before
#     whether or not the plugin is installed — no hook fires twice (the Stop
#     hook's per-iteration block counter would halve). A stale wiring whose
#     file is gone does NOT make it step aside: the guard never goes missing
#     because a project kept a dead path.
#
#   * `guard-probe` (UserPromptSubmit) is the loop's fail-closed self-check.
#     Inert unless PHASEKIT_GUARD_PROBE names a file. scripts/run-phase.sh
#     starts a throwaway `claude -p` with it set before every turn of a
#     pinned run; this hook then proves the guard works — every hook present
#     and executable, the command guard refusing a destructive command (and,
#     under the loop, a commit) — writes "<token> ok" to that file, and BLOCKS
#     the prompt (exit 2), so the probe never reaches the model. No file, or
#     no token in it, and run-phase.sh refuses the turn: a plugin that did not
#     load is a session without its guard.
#
# Fail-open for the hooks themselves (each hook's own contract); the probe is
# the one place a missing piece fails, and it fails CLOSED, by design.

set -u

name="${1:-}"
# Pure bash (no dirname): a hook runs under whatever PATH the session has.
here="${BASH_SOURCE[0]%/*}"
[[ "$here" == "${BASH_SOURCE[0]}" ]] && here="."
here="$(cd "$here" 2>/dev/null && pwd)" || here=""
scripts="$here/scripts"
HOOKS="deny-dangerous-commands require-verdict wrapup-nudge compact-reanchor"

probe_fail() {
  printf 'FAIL %s\n' "$1" > "$PHASEKIT_GUARD_PROBE" 2>/dev/null || true
  echo "phasekit guard probe: FAILED — $1" >&2
  exit 2
}

guard_probe() {
  cat >/dev/null 2>&1 || true
  [[ -n "${PHASEKIT_GUARD_PROBE:-}" ]] || exit 0
  local h rc payload
  for h in $HOOKS; do
    [[ -x "$scripts/$h.sh" ]] || probe_fail "hook $h is missing or not executable at $scripts"
  done
  command -v jq >/dev/null 2>&1 || probe_fail "jq is not on PATH (the hooks need it)"
  payload="$(jq -cn --arg c "git reset --hard HEAD" --arg d "$PWD" \
               '{tool_name: "Bash", tool_input: {command: $c}, cwd: $d}')" \
    || probe_fail "could not build the probe payload"
  # The wiring the session will run: every hook this plugin owns is declared
  # in hooks.json on its event, through this dispatcher.
  local hj="$here/hooks.json" want
  for want in "PreToolUse deny-dangerous-commands" "PreToolUse wrapup-nudge" "PostToolUse wrapup-nudge" \
              "Stop require-verdict" "SessionStart compact-reanchor"; do
    jq -e --arg ev "${want%% *}" --arg h "${want#* }" \
      '[.hooks[$ev][]?.hooks[]?.command // empty] | any(test("run-hook\\.sh\"? " + $h + "$"))' \
      "$hj" >/dev/null 2>&1 || probe_fail "hooks.json does not wire ${want#* } on ${want%% *}"
  done
  # Through THIS dispatcher, for this project — exactly the path a tool call
  # takes, so a project that could make the dispatcher step aside fails here.
  rc=0
  printf '%s' "$payload" | PHASEKIT_ENGINE_DOCS="${PHASEKIT_ENGINE_DOCS:-$here}" CLAUDE_PROJECT_DIR="$PWD" \
      bash "$here/run-hook.sh" deny-dangerous-commands \
    >/dev/null 2>&1 || rc=$?
  [[ "$rc" == 2 ]] || probe_fail "the command guard did not refuse 'git reset --hard' (exit $rc)"
  if [[ -n "${PHASEKIT_ARTIFACTS_DIR:-}" && -n "${PHASEKIT_ITER_MARKER:-}" ]]; then
    payload="$(jq -cn --arg c "git commit -m probe" --arg d "$PWD" \
                 '{tool_name: "Bash", tool_input: {command: $c}, cwd: $d}')"
    rc=0
    printf '%s' "$payload" | PHASEKIT_ENGINE_DOCS="${PHASEKIT_ENGINE_DOCS:-$here}" CLAUDE_PROJECT_DIR="$PWD" \
      bash "$here/run-hook.sh" deny-dangerous-commands \
      >/dev/null 2>&1 || rc=$?
    [[ "$rc" == 2 ]] || probe_fail "the command guard did not refuse 'git commit' under the loop (exit $rc)"
  fi
  printf '%s ok\n' "${PHASEKIT_GUARD_PROBE_TOKEN:-}" > "$PHASEKIT_GUARD_PROBE" 2>/dev/null \
    || probe_fail "could not write $PHASEKIT_GUARD_PROBE"
  echo "phasekit guard probe: ok — the plugin's hooks are loaded and the guard refuses (this probe prompt is blocked; no model call)" >&2
  exit 2
}

case "$name" in
  guard-probe) guard_probe ;;
  deny-dangerous-commands|require-verdict|wrapup-nudge|compact-reanchor) ;;
  *) cat >/dev/null 2>&1 || true; exit 0 ;;
esac

project="${CLAUDE_PROJECT_DIR:-$PWD}"
top="$(git -C "$project" rev-parse --show-toplevel 2>/dev/null)" || top=""
[[ -n "$top" ]] && project="$top"

# Only a phasekit project's sessions (and the loop's) are the plugin's
# business: installed once per machine, it must not change a session in any
# other repository.
if [[ -z "${PHASEKIT_ARTIFACTS_DIR:-}" && -z "${PHASEKIT_ENGINE_DOCS:-}" && ! -f "$project/.phasekit-version" ]]; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

# A vendored project's own wiring wins (see above) — and ONLY a vendored
# project's: never in a pinned project (.phasekit-version) and never under a
# pinned loop (PHASEKIT_ENGINE_DOCS, fixed when the session started), so a
# session cannot plant `.claude/hooks/<name>.sh` plus a settings line and make
# its own guard step aside. The wiring must be a hook COMMAND in settings.json
# (not any mention of the path), and its file must exist.
if [[ -z "${PHASEKIT_ENGINE_DOCS:-}" && ! -e "$project/.phasekit-version" \
      && -f "$project/.claude/hooks/$name.sh" && -f "$project/.claude/settings.json" ]] \
   && jq -e --arg p ".claude/hooks/$name.sh" \
        '[.hooks // {} | .[]? | .[]? | .hooks[]? | .command? // empty | strings] | any(contains($p))' \
        "$project/.claude/settings.json" >/dev/null 2>&1; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

if [[ -x "$scripts/$name.sh" ]]; then
  exec "$scripts/$name.sh"
fi
# Fail-open like the hooks themselves; the loop's guard probe is what refuses
# a turn whose hooks are missing.
echo "phasekit plugin: hook $name not found at $scripts" >&2
cat >/dev/null 2>&1 || true
exit 0
