#!/usr/bin/env bash
# phasekit — CLI wrapper around enrich-project.py.
#
# Verbs (operate on the current directory):
#   phasekit adopt [profile]      enrich an existing repo (no overwrite)
#   phasekit bootstrap [profile]  enrich a greenfield project
#   phasekit upgrade [flags...]   re-provision against the current scaffold
#   phasekit check                detect file drift vs the recorded manifest; (v0.26.0) also
#                                 checks the project rules register (exit 8 on findings)
#   phasekit check-version        is a newer scaffold release available?
#   phasekit status               current phase state (derived from artifacts)
#   phasekit channel [name]       show or set the self-update channel (stable|edge|<ref>)
#   phasekit self-update          move this phasekit clone along its channel
#   phasekit roadmap next|done|init  the OPTIONAL docs/ROADMAP.md (see scripts/phasekit-roadmap.py)
#   phasekit rules check|init     (v0.26.0) the project rules register docs/project/RULES.md
#                                 (see scripts/phasekit-rules.py)
#   phasekit tests [report] [--json] [--failures FILE] [--session-bound S] [--measure FILE]
#                                 (v0.27.0) the test-suite hygiene measure: per-tier time vs
#                                 budget, waiting and slowest files, never-failed files,
#                                 criterion-named suites, evidence weight. A map, never a
#                                 gate: a report exits 0 over budget or not
#   phasekit tests measure [--timeout S] [--out FILE]
#                                 run each test file the project's gate declares ALONE and
#                                 record wall and CPU time (a supervisor's nightly run;
#                                 see scripts/phasekit-tests.py)
#   phasekit verify [--tier fast|full]  run THIS project's gate exactly as the loop's
#                                 commit gate will; a green verdict is reused by it
#   phasekit verify --in-runner [--tier auto|fast|full] [--json] [--timeout S]
#                                 (v0.20.0) MEASURE this project's gate in the runner
#                                 image, composed exactly as the upgrade gate composes
#                                 it (pinned engine at /opt/phasekit, PHASEKIT_CONTRACT,
#                                 contracts, identity, runner user); never a verdict
#                                 the loop reuses, so any tier on any tree. Served by
#                                 this CLI (scripts/phasekit-pin.py verify-runner)
#   phasekit land --in-runner [--json] [--timeout S] [--push] [--squash-target B] [--work-branch B]
#                                 (v0.21.0) LAND a stranded verdict artifact: the loop's
#                                 recovery-only pass (MAX_ITERATIONS=0) in the runner image,
#                                 composed by the same harness as verify --in-runner; it
#                                 commits, so it refuses while a container works on the
#                                 tree. Served by this CLI (scripts/phasekit-pin.py land-runner)
#   phasekit launch-argv --role R --name N [--mount SRC:DST[:ro]] [--credentials none|ro|rw]
#                        [--env NAME] [--set NAME=VALUE] [--add-host H] [--workdir D]
#                        [--entrypoint E] [--json] -- CMD...
#                                 v0.22.0: print (never run) the `docker run` argv of a
#                                 runner-image container for ROLE (session, runner, oneshot,
#                                 repair): the one composer of user, environment, credentials,
#                                 image and bounds (scripts/phasekit-launch.py)
#   phasekit ui-check [--json] [--update [--phase P]]   (v0.23.0) the deterministic UI checks
#                                 (scripts/phasekit-ui-check.mjs) over docs/project/ui-pages.json,
#                                 in the runner image: axe, tap targets, fonts, visual baselines.
#                                 --update --phase P rewrites the baselines that phase declares
#                                 under `Visual changes:` (and captures missing ones)
#   phasekit ui-check init        seed docs/project/DESIGN_BRIEF.md, DESIGN_SYSTEM.md and
#                                 ui-pages.json (never overwrites)
#   phasekit scope [--iteration N] [--phase P] [--json]
#                                 what this iteration (or one phase) changed
#   phasekit facts [--json]       the facts phasekit DECLARES for downstream tests
#                                 (contracts/interface.json `facts`; read-only)
#   phasekit scaffold-reads [--json]  the `scaffold-reads` advisory: test files that
#                                 read scaffold-owned files instead, or test only
#                                 phasekit; plus `process-reads` (warn-only)
#
# The engine outside the repository (v0.19.0). A PINNED project tracks
# .phasekit-version and no engine files; the verbs below run the pinned engine
# (from the engine store, fetched on first use) against it. In a VENDORED
# project (scripts/run-until-done.sh in its own tree) every verb behaves as
# before. See docs/INSTALL_LIFECYCLE.md.
#   phasekit init [profile]       a new pinned project (pin + project docs + gate), one commit
#   phasekit migrate              a vendored project -> pinned (exact, gated, one commit)
#   phasekit loop                 the phase loop on the HOST against this project
#   phasekit run                  the phase loop in the CONTAINER (scripts/container-setup.sh run)
#   phasekit container <cmd>      container-setup.sh build|setup|run|shell for this project
#   phasekit hook <name>          run one of the engine's hooks (stdin passed through)
#   phasekit contracts <args>     the cross-project contracts checker against this project
#   phasekit engines install|list|path [tag]   the engine store
#   phasekit plugin install|status   the Claude Code plugin for interactive sessions
#   phasekit docs                 print the engine's docs directory
#
# Anything else is forwarded verbatim to the engine, so the raw flag form
# still works for any flag enrich-project.py supports:
#   phasekit --check .            phasekit --upgrade --yes .
#   phasekit --reconcile .        phasekit --uninstall --include-once --yes .
#
# The engine runs under ${PHASEKIT_PYTHON:-python3} so a global install can
# point at an isolated venv; downstream-vendored copies default to system python3.
#
# For frequent use, alias it (or use the installed `phasekit` shim on PATH):
#   alias phasekit='bash /path/to/phasekit/scripts/phasekit.sh'

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAFFOLD_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGINE=("${PHASEKIT_PYTHON:-python3}" "$SCRIPT_DIR/enrich-project.py")
PIN=("${PHASEKIT_PYTHON:-python3}" "$SCRIPT_DIR/phasekit-pin.py")

# --- the project around the working directory, and the engine that runs it ---
pk_project_root() {
  git rev-parse --show-toplevel 2>/dev/null || true
}

# pinned | vendored | none. The pin wins: a project that carries
# .phasekit-version is run by its pinned engine even if a stray
# scripts/run-until-done.sh is in its tree (a session could otherwise commit
# one and have it run as the loop; `phasekit check` reports it as a leftover).
pk_layout() {
  if [[ -f "$1/.phasekit-version" ]]; then echo pinned
  elif [[ -f "$1/scripts/run-until-done.sh" ]]; then echo vendored
  else echo none
  fi
}

require_pin_tool() {
  if [[ ! -f "$SCRIPT_DIR/phasekit-pin.py" ]]; then
    echo "phasekit $1: this copy of phasekit ($SCAFFOLD_ROOT) cannot run a pinned project — use the installed phasekit (install.sh)" >&2
    exit 2
  fi
}

# The engine directory for project $1: its own tree when vendored; when
# pinned, the engine its pin names (a running loop's own engine first:
# PHASEKIT_ENGINE_DIR, set by the loop's shim — a pin edited mid-run never
# switches engines). Exits with the resolver's code when it cannot.
pk_engine_for() {
  local root="$1" lay engine rc=0
  lay="$(pk_layout "$root")"
  case "$lay" in
    vendored) printf '%s\n' "$root"; return 0 ;;
    pinned)
      if [[ -n "${PHASEKIT_ENGINE_DIR:-}" && -f "${PHASEKIT_ENGINE_DIR}/scripts/run-until-done.sh" ]]; then
        printf '%s\n' "$PHASEKIT_ENGINE_DIR"; return 0
      fi
      require_pin_tool "engine"
      engine="$( cd "$root" && "${PIN[@]}" resolve )" || rc=$?
      [[ "$rc" -eq 0 && -n "$engine" ]] || exit "${rc:-1}"
      printf '%s\n' "$engine" ;;
    *) return 2 ;;
  esac
}

require_project() {
  local verb="$1" root
  root="$(pk_project_root)"
  if [[ -z "$root" || "$(pk_layout "$root")" == none ]]; then
    echo "phasekit $verb: run it inside a phasekit project (no scripts/run-until-done.sh or .phasekit-version at ${root:-this directory})" >&2
    exit 2
  fi
  printf '%s\n' "$root"
}

# Refuse to operate on anything but a real phasekit checkout — never a
# downstream project's git. Used by self-update and the channel verb, both of
# which only make sense on the canonical install (e.g. ~/.local/share/phasekit).
require_canonical_clone() {
  local repo="$1" verb="$2"
  if [[ ! -d "$repo/.git" || ! -f "$repo/capabilities/project-capabilities.yaml" \
        || ! -f "$repo/scripts/enrich-project.py" ]]; then
    echo "phasekit $verb: $repo is not a phasekit checkout; refusing." >&2
    echo "  ($verb only works on the canonical install, e.g. ~/.local/share/phasekit)" >&2
    return 1
  fi
}

# self-update: move the phasekit clone this wrapper lives in along its channel
# (stable = latest release tag, edge = origin/master tip, or an explicit pin).
# See docs/adr/ADR-0002-self-update-channels.md.
self_update() {
  local repo="$SCAFFOLD_ROOT"
  require_canonical_clone "$repo" "self-update" || return 1
  # shellcheck source=scripts/phasekit-channel.sh
  source "$SCRIPT_DIR/phasekit-channel.sh"

  local channel before after
  channel="$(pk_channel_read "$repo")"
  before="$(git -C "$repo" describe --tags --always --dirty 2>/dev/null || echo unknown)"
  echo "phasekit self-update: channel '$channel'; fetching…"
  git -C "$repo" fetch --tags --quiet
  if ! pk_channel_checkout "$repo" "$channel"; then
    echo "  No ref resolved for channel '$channel'; nothing to update to." >&2
    return 0
  fi
  after="$(git -C "$repo" describe --tags --always 2>/dev/null || echo '?')"

  if pk_channel_is_edge "$channel"; then
    echo "phasekit self-update: tracking UNRELEASED phasekit ($after) on channel '$channel';" >&2
    echo "  downstream 'phasekit upgrade' may provision pre-release scaffold." >&2
  fi

  # Refresh venv deps if a venv is present (idempotent; quiet).
  if [[ -x "$repo/.venv/bin/pip" ]]; then
    "$repo/.venv/bin/pip" install --quiet --upgrade pyyaml || true
  fi
  echo "phasekit self-update: ${before} → ${after}"
  # v0.19.0: a release this clone now sits on goes into the engine store too,
  # so pinned projects can bump to it (and pins to it resolve offline).
  if [[ "$after" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && -f "$SCRIPT_DIR/phasekit-pin.py" ]]; then
    "${PIN[@]}" engines install "$after" || echo "phasekit self-update: could not install $after into the engine store (pinned projects fetch it on first use)" >&2
  fi
}

# channel: show the current self-update channel, or set it. Persisted in the
# canonical clone; takes effect on the next self-update.
channel_cmd() {
  local repo="$SCAFFOLD_ROOT"
  require_canonical_clone "$repo" "channel" || return 1
  # shellcheck source=scripts/phasekit-channel.sh
  source "$SCRIPT_DIR/phasekit-channel.sh"

  if [[ -n "${1:-}" ]]; then
    pk_channel_write "$repo" "$1"
    echo "phasekit channel: set to '$1' (effective on next self-update)"
    if pk_channel_is_edge "$1"; then
      echo "  note: '$1' tracks unreleased phasekit; downstream upgrades may get pre-release scaffold." >&2
    fi
  else
    echo "$(pk_channel_read "$repo")"
  fi
}

# The scripts directory whose phasekit-rules.py judges project $1: a pinned
# project's engine (an engine older than v0.26.0 has none), else this CLI's —
# as `roadmap` resolves its tool.
rules_tool_dir() {
  local root="$1" engine
  if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then
    engine="$(pk_engine_for "$root")" || return 1
    printf '%s\n' "$engine/scripts"
  else
    printf '%s\n' "$SCRIPT_DIR"
  fi
}

# `phasekit check`'s second half (v0.26.0): the project rules register,
# docs/project/RULES.md. $1 = project root (may be empty), $2 = the layout
# check's exit code. Exits with $2 when it is non-zero (findings are still
# printed), else 8 on findings (scripts/phasekit-rules.py exit 3), else 0. A
# missing register is clean; a checker that is missing or cannot complete is
# a note, never a code. A pin that is not installed (6) is never resolved
# here: check does not fetch.
rules_check_verb() {
  local root="$1" crc="$2" dir rrc=0
  if [[ -z "$root" || "$crc" -eq 6 || ! -e "$root/docs/project/RULES.md" ]]; then
    exit "$crc"
  fi
  dir="$(rules_tool_dir "$root" 2>/dev/null)" || dir=""
  if [[ -z "$dir" || ! -f "$dir/phasekit-rules.py" ]]; then
    echo "phasekit check: note — docs/project/RULES.md was not checked (this engine has no rules checker)" >&2
    exit "$crc"
  fi
  "${PHASEKIT_PYTHON:-python3}" "$dir/phasekit-rules.py" check --repo "$root" || rrc=$?
  if [[ "$rrc" -eq 3 ]]; then
    echo "RULES: docs/project/RULES.md has findings (above); docs/QUALITY_GATES.md \"Project rules register\"" >&2
    [[ "$crc" -ne 0 ]] || exit 8
  elif [[ "$rrc" -ne 0 ]]; then
    echo "phasekit check: note — the rules checker could not complete (exit $rrc)" >&2
  fi
  exit "$crc"
}

verb="${1:-}"
case "$verb" in
  adopt|bootstrap)
    shift
    profile="${1:-default}"
    exec "${ENGINE[@]}" "$PWD" --profile "$profile"
    ;;
  upgrade)
    shift
    root="$(pk_project_root)"
    if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then
      require_pin_tool upgrade
      exec "${PIN[@]}" upgrade "$@"
    fi
    exec "${ENGINE[@]}" --upgrade "$PWD" "$@"
    ;;
  check)
    # v0.26.0: after the layout's own check, the project rules register
    # (rules_check_verb). The layout's non-zero code wins; findings in an
    # otherwise clean check are exit 8.
    shift
    root="$(pk_project_root)"
    crc=0
    if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then
      require_pin_tool check
      "${PIN[@]}" check "$@" || crc=$?
    else
      "${ENGINE[@]}" --check "$PWD" "$@" || crc=$?
    fi
    rules_check_verb "$root" "$crc"
    ;;
  init|migrate|engines|plugin|docs|resolve)
    require_pin_tool "$verb"
    exec "${PIN[@]}" "$@"
    ;;
  loop)
    shift
    root="$(require_project loop)"
    engine="$(pk_engine_for "$root")"
    if [[ "$engine" == "$root" ]]; then
      exec bash "$root/scripts/run-until-done.sh" "$@"
    fi
    PHASEKIT_PROJECT_DIR="$root" exec bash "$engine/scripts/run-until-done.sh" "$@"
    ;;
  run|container)
    [[ "$verb" == run ]] && set -- container run "${@:2}"
    shift
    root="$(require_project "$verb")"
    engine="$(pk_engine_for "$root")"
    if [[ "$engine" == "$root" ]]; then
      exec bash "$root/scripts/container-setup.sh" "$@"
    fi
    PHASEKIT_PROJECT_DIR="$root" exec bash "$engine/scripts/container-setup.sh" "$@"
    ;;
  hook)
    shift
    name="${1:-}"
    if [[ ! "$name" =~ ^[a-z][a-z-]*$ ]]; then
      echo "phasekit hook: usage: phasekit hook <name> (deny-dangerous-commands, require-verdict, wrapup-nudge, compact-reanchor)" >&2
      exit 2
    fi
    root="$(pk_project_root)"
    engine="$SCAFFOLD_ROOT"
    if [[ -n "$root" && "$(pk_layout "$root")" != none ]]; then engine="$(pk_engine_for "$root")"; fi
    if [[ ! -f "$engine/.claude/hooks/$name.sh" ]]; then
      echo "phasekit hook: no hook '$name' in $engine/.claude/hooks" >&2
      exit 2
    fi
    exec "$engine/.claude/hooks/$name.sh"
    ;;
  contracts)
    shift
    root="$(require_project contracts)"
    engine="$(pk_engine_for "$root")"
    exec "${PHASEKIT_PYTHON:-python3}" "$engine/scripts/phasekit-contracts.py" --repo "$root" "$@"
    ;;
  check-version)
    shift
    exec "${ENGINE[@]}" --check-version "$PWD"
    ;;
  status)
    shift
    exec "${ENGINE[@]}" --status "$PWD"
    ;;
  channel)
    shift
    channel_cmd "${1:-}"
    ;;
  self-update)
    self_update
    ;;
  launch-argv)
    # v0.22.0: the one launcher (O5). Needs no project: it composes, and runs nothing.
    shift
    exec "${PHASEKIT_PYTHON:-python3}" "$SCRIPT_DIR/phasekit-launch.py" argv "$@"
    ;;
  land)
    # v0.21.0: the landing pass is offered only in the runner — the spelling
    # names where it runs, as `verify --in-runner` does.
    for arg in "${@:2}"; do
      if [[ "$arg" == --in-runner ]]; then
        require_pin_tool "land --in-runner"
        exec "${PIN[@]}" land-runner "${@:2}"
      fi
    done
    echo "phasekit land: only \`phasekit land --in-runner\` is offered (the loop's recovery-only pass in the runner image)" >&2
    exit 2
    ;;
  verify|scope)
    # v0.20.0: `verify --in-runner` is not the loop's verify — it is THE gate
    # harness (scripts/enrich-project.py run_project_gate) run by this CLI in
    # the runner image, in either layout; the loop's verify stays as below.
    if [[ "$verb" == verify ]]; then
      for arg in "${@:2}"; do
        if [[ "$arg" == --in-runner ]]; then
          require_pin_tool "verify --in-runner"
          exec "${PIN[@]}" verify-runner "${@:2}"
        fi
      done
    fi
    # Model-facing (v0.18.0): run by the PROJECT's own loop code — the copy of
    # scripts/run-until-done.sh in the repository the command is run from —
    # so the gate, the staging and the facts are that project's, exactly as
    # its loop runs them. A pinned project (v0.19.0): its pinned engine's
    # loop, pointed at the project.
    shift
    project_root="$(require_project "$verb")"
    engine="$(pk_engine_for "$project_root")"
    if [[ "$engine" == "$project_root" ]]; then
      exec bash "$project_root/scripts/run-until-done.sh" "$verb" "$@"
    fi
    PHASEKIT_PROJECT_DIR="$project_root" exec bash "$engine/scripts/run-until-done.sh" "$verb" "$@"
    ;;
  ui-check)
    # v0.23.0: run from the project's engine (pinned: its pin's; vendored: its
    # own scripts/), like roadmap. For --update --phase P the declaration is
    # read by the loop's own plan parser (run-until-done.sh visual-changes),
    # the one parser of a phase's docs/PHASES.md section.
    shift
    project_root="$(require_project ui-check)"
    engine="$(pk_engine_for "$project_root")"
    phase="" update=0 pass=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --phase) phase="${2:-}"; shift 2 || { echo "phasekit ui-check: --phase needs a phase id" >&2; exit 2; } ;;
        --update) update=1; pass+=("$1"); shift ;;
        --declared-json)
          # Internal: only the phase's own declaration may let a baseline change.
          echo "phasekit ui-check: --declared-json is internal; declare the pages under \`Visual changes:\` and pass --phase P" >&2; exit 2 ;;
        *) pass+=("$1"); shift ;;
      esac
    done
    if [[ -n "$phase" && "$update" -eq 0 ]]; then
      echo "phasekit ui-check: --phase is only meaningful with --update" >&2; exit 2
    fi
    command -v node >/dev/null 2>&1 || {
      echo "phasekit ui-check: node is not on PATH (ui-check runs in the runner image)" >&2; exit 3; }
    if [[ "$update" -eq 1 && -n "$phase" ]]; then
      declared="$(PHASEKIT_PROJECT_DIR="$project_root" bash "$engine/scripts/run-until-done.sh" visual-changes "$phase")" || {
        echo "phasekit ui-check: could not read phase $phase's \`Visual changes:\` from docs/PHASES.md" >&2; exit 2; }
      # Refuse a phase the plan does not have, or a declaration with anything but
      # page ids on it: the declaration is what lets a baseline change.
      problem="$(printf '%s' "$declared" | python3 -c 'import json,sys
d = json.load(sys.stdin)
if not d.get("found"):
    print("docs/PHASES.md has no heading for phase %s" % d.get("phase"))
elif d.get("invalid"):
    print("its `Visual changes:` line has words that are not page ids (id or id@viewport): %s" % ", ".join(d["invalid"]))')" || problem="its declaration could not be read"
      if [[ -n "$problem" ]]; then
        echo "phasekit ui-check: phase $phase: $problem" >&2; exit 2
      fi
      pass+=(--declared-json "$declared")
    fi
    PHASEKIT_SCAFFOLD_ROOT="$SCAFFOLD_ROOT" exec node "$engine/scripts/phasekit-ui-check.mjs" --project "$project_root" "${pass[@]}"
    ;;
  rules)
    shift
    root="$(pk_project_root)"
    tool_dir="$(rules_tool_dir "$root")"
    # act on the project, not the working directory, unless the caller named one
    if (( $# > 0 )) && [[ -n "$root" && " $* " != *" --repo"* ]]; then set -- "$@" --repo "$root"; fi
    exec "${PHASEKIT_PYTHON:-python3}" "$tool_dir/phasekit-rules.py" "$@"
    ;;
  tests)
    # v0.27.0: the test-suite hygiene measure. Its own verb, not a `check`
    # flag: `check` is a pass/fail drift check whose codes a supervisor reads
    # as health, and a measure never fails. Run from the project's engine
    # (pinned: its pin's, as `rules`; an engine older than v0.27.0 has no
    # measure, so this CLI's own copy serves — it reads the project, never
    # the engine), against the project root, not the working directory.
    shift
    root="$(require_project tests)"
    tool_dir="$(rules_tool_dir "$root")"
    if [[ ! -f "$tool_dir/phasekit-tests.py" ]]; then
      echo "phasekit tests: note — this project's engine has no test-suite measure; using $SCRIPT_DIR/phasekit-tests.py" >&2
      tool_dir="$SCRIPT_DIR"
    fi
    if [[ " $* " != *" --repo"* ]]; then set -- "$@" --repo "$root"; fi
    exec "${PHASEKIT_PYTHON:-python3}" "$tool_dir/phasekit-tests.py" "$@"
    ;;
  roadmap)
    shift
    tool_dir="$SCRIPT_DIR"
    root="$(pk_project_root)"
    if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then tool_dir="$(pk_engine_for "$root")/scripts"; fi
    exec "${PHASEKIT_PYTHON:-python3}" "$tool_dir/phasekit-roadmap.py" "$@"
    ;;
  facts|scaffold-reads)
    # v0.18.3 (queue row 1194): the declared surface a downstream test reads
    # instead of the vendored loop's text, and the advisory that names tests
    # that still read scaffold-owned files. Read-only; run from the project.
    # v0.19.0: in a pinned project, the PINNED engine's tool (its facts).
    tool_dir="$SCRIPT_DIR"
    root="$(pk_project_root)"
    if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then tool_dir="$(pk_engine_for "$root")/scripts"; fi
    exec "${PHASEKIT_PYTHON:-python3}" "$tool_dir/phasekit-surface.py" "$@"
    ;;
  *)
    # No verb, a flag (-…), or a raw path/target: forward verbatim.
    exec "${ENGINE[@]}" "$@"
    ;;
esac
