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
    exec "${ENGINE[@]}" --upgrade "$PWD" "$@"
    ;;
  check)
    shift
    exec "${ENGINE[@]}" --check "$PWD" "$@"
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
  *)
    # No verb, a flag (-…), or a raw path/target: forward verbatim.
    exec "${ENGINE[@]}" "$@"
    ;;
esac
