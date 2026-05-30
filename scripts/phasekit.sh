#!/usr/bin/env bash
# phasekit — CLI wrapper around enrich-project.py.
#
# Verbs (operate on the current directory):
#   phasekit adopt [profile]      enrich an existing repo (no overwrite)
#   phasekit bootstrap [profile]  enrich a greenfield project
#   phasekit upgrade [flags...]   re-provision against the current scaffold
#   phasekit check                detect file drift vs the recorded manifest
#   phasekit check-version        is a newer scaffold release available?
#   phasekit self-update          move this phasekit clone to the latest release tag
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

# self-update: move the phasekit clone this wrapper lives in to the latest
# release tag. Guarded so it only ever touches a real phasekit checkout, never
# a downstream project's git.
self_update() {
  local repo="$SCAFFOLD_ROOT"
  if [[ ! -d "$repo/.git" || ! -f "$repo/capabilities/project-capabilities.yaml" \
        || ! -f "$repo/scripts/enrich-project.py" ]]; then
    echo "phasekit self-update: $repo is not a phasekit checkout; refusing." >&2
    echo "  (self-update only works on the canonical install, e.g. ~/.local/share/phasekit)" >&2
    return 1
  fi
  local before after latest
  before="$(git -C "$repo" describe --tags --always --dirty 2>/dev/null || echo unknown)"
  echo "phasekit self-update: fetching tags…"
  git -C "$repo" fetch --tags --quiet
  latest="$(git -C "$repo" tag -l 'v*' | sort -V | tail -n1)"
  if [[ -z "$latest" ]]; then
    echo "  No release tags found upstream; nothing to update to." >&2
    return 0
  fi
  git -C "$repo" checkout --quiet "$latest"
  after="$(git -C "$repo" describe --tags --always 2>/dev/null || echo "$latest")"
  # Refresh venv deps if a venv is present (idempotent; quiet).
  if [[ -x "$repo/.venv/bin/pip" ]]; then
    "$repo/.venv/bin/pip" install --quiet --upgrade pyyaml || true
  fi
  echo "phasekit self-update: ${before} → ${after}"
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
  self-update)
    self_update
    ;;
  *)
    # No verb, a flag (-…), or a raw path/target: forward verbatim.
    exec "${ENGINE[@]}" "$@"
    ;;
esac
