#!/usr/bin/env bash
# phasekit — CLI wrapper around enrich-project.py.
#
# Verbs (operate on the current directory):
#   phasekit adopt [profile]      enrich an existing repo (no overwrite)
#   phasekit bootstrap [profile]  enrich a greenfield project
#   phasekit upgrade [flags...]   re-provision against the current scaffold
#   phasekit check                detect file drift vs the recorded manifest
#   phasekit check-version        is a newer scaffold release available?
#   phasekit status               current phase state (derived from artifacts)
#   phasekit channel [name]       show or set the self-update channel (stable|edge|<ref>)
#   phasekit self-update          move this phasekit clone along its channel
#   phasekit roadmap next|done|init  the OPTIONAL docs/ROADMAP.md (see scripts/phasekit-roadmap.py)
#   phasekit verify [--tier fast|full]  run THIS project's gate exactly as the loop's
#                                 commit gate will; a green verdict is reused by it
#   phasekit scope [--iteration N] [--phase P] [--json]
#                                 what this iteration (or one phase) changed
#   phasekit facts [--json]       the facts phasekit DECLARES for downstream tests
#                                 (contracts/interface.json `facts`; read-only)
#   phasekit scaffold-reads [--json]  the `scaffold-reads` advisory: test files that
#                                 read scaffold-owned files instead (warn-only)
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
    shift
    root="$(pk_project_root)"
    if [[ -n "$root" && "$(pk_layout "$root")" == pinned ]]; then
      require_pin_tool check
      exec "${PIN[@]}" check "$@"
    fi
    exec "${ENGINE[@]}" --check "$PWD" "$@"
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
  verify|scope)
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
