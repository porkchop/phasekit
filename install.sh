#!/usr/bin/env bash
# phasekit installer.
#
#   curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh | bash
#
# Installs a canonical phasekit clone (the single source of truth for running
# and upgrading) into ~/.local/share/phasekit, builds an isolated venv for its
# one dependency (pyyaml), and drops a `phasekit` launcher on PATH. It does NOT
# touch any project — enrich a project explicitly afterwards with `phasekit
# adopt`. Re-running updates an existing install to the latest release tag.
#
# Trust: this pipes a script to your shell. To inspect first:
#   curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh -o install.sh
#   less install.sh && bash install.sh
#
# Overridable via environment:
#   PHASEKIT_URL   git remote to clone (default: the public GitHub repo)
#   PHASEKIT_HOME  install location    (default: ${XDG_DATA_HOME:-~/.local/share}/phasekit)
#   PHASEKIT_BIN   launcher directory  (default: ~/.local/bin)
#   PHASEKIT_REF   one-shot ref to check out (a tag/branch/sha). Also sets the
#                  self-update channel: the default branch -> edge, a release tag
#                  -> stable, anything else -> a pin. Omit to follow the persisted
#                  channel (default: stable = latest v* tag). See ADR-0002.
set -euo pipefail

PHASEKIT_URL="${PHASEKIT_URL:-https://github.com/porkchop/phasekit.git}"
PHASEKIT_HOME="${PHASEKIT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/phasekit}"
PHASEKIT_BIN="${PHASEKIT_BIN:-$HOME/.local/bin}"
PHASEKIT_REF="${PHASEKIT_REF:-}"

say()  { printf 'phasekit-install: %s\n' "$*"; }
die()  { printf 'phasekit-install: error: %s\n' "$*" >&2; exit 1; }

# --- 1. prerequisites -------------------------------------------------------
command -v git     >/dev/null 2>&1 || die "git is required but not found."
command -v python3 >/dev/null 2>&1 || die "python3 is required but not found."
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
  || die "python3 venv support is missing. Install it (e.g. 'apt install python3-venv') and re-run."

# --- 2. clone or update -----------------------------------------------------
if [[ -d "$PHASEKIT_HOME/.git" ]]; then
  say "updating existing install at $PHASEKIT_HOME"
  git -C "$PHASEKIT_HOME" fetch --tags --quiet origin
elif [[ -e "$PHASEKIT_HOME" ]]; then
  die "$PHASEKIT_HOME exists but is not a git checkout; move it aside and re-run."
else
  say "cloning $PHASEKIT_URL → $PHASEKIT_HOME"
  mkdir -p "$(dirname "$PHASEKIT_HOME")"
  # Full history (no --depth) so --check-version can use commit ancestry.
  git clone --quiet "$PHASEKIT_URL" "$PHASEKIT_HOME"
fi

# --- 3. resolve channel + ref, then check out -------------------------------
# Channel model (docs/adr/ADR-0002-self-update-channels.md): the clone tracks a
# stable (latest tag), edge (default-branch tip), or pinned ref, persisted in
# <home>/.phasekit-channel and honored by `phasekit self-update`. The channel
# library is vendored in the clone, so it is available after section 2 — except
# on a one-time upgrade from a pre-ADR-0002 install, handled by the fallback.
if [[ -f "$PHASEKIT_HOME/scripts/phasekit-channel.sh" ]]; then
  # shellcheck source=scripts/phasekit-channel.sh
  source "$PHASEKIT_HOME/scripts/phasekit-channel.sh"
  if [[ -n "$PHASEKIT_REF" ]]; then
    # Explicit ref wins; classify it into the channel to remember going forward.
    channel="$(pk_channel_classify_ref "$PHASEKIT_HOME" "$PHASEKIT_REF")"
    say "checking out '$PHASEKIT_REF' (channel: $channel)"
    git -C "$PHASEKIT_HOME" checkout --quiet "$PHASEKIT_REF"
    git -C "$PHASEKIT_HOME" merge --ff-only --quiet "origin/$PHASEKIT_REF" 2>/dev/null || true
  else
    # No explicit ref: follow the persisted channel (default stable on a fresh
    # install), so re-running the installer respects a previously-set 'edge'.
    channel="$(pk_channel_read "$PHASEKIT_HOME")"
    say "channel: $channel"
    pk_channel_checkout "$PHASEKIT_HOME" "$channel" \
      || die "no ref resolved for channel '$channel' (empty remote?)."
  fi
  pk_channel_write "$PHASEKIT_HOME" "$channel"
  if pk_channel_is_edge "$channel"; then
    say "NOTE: channel '$channel' tracks unreleased phasekit; downstream upgrades may get pre-release scaffold."
  fi
else
  # Legacy fallback: upgrading a clone that predates the channel library. Use the
  # original resolve-and-checkout; the next run will have the library and honor
  # channels. Keep in sync with pk_channel_resolve_ref's stable/edge mapping.
  ref="$PHASEKIT_REF"
  if [[ -z "$ref" ]]; then
    ref="$(git -C "$PHASEKIT_HOME" tag -l 'v*' | sort -V | tail -n1)"
  fi
  if [[ -z "$ref" ]]; then
    ref="$(git -C "$PHASEKIT_HOME" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null \
           | sed 's#^origin/##')"
    ref="${ref:-master}"
    say "no release tags found; tracking '$ref'"
  else
    say "checking out release $ref"
  fi
  git -C "$PHASEKIT_HOME" checkout --quiet "$ref"
  git -C "$PHASEKIT_HOME" merge --ff-only --quiet "origin/$ref" 2>/dev/null || true
fi

# --- 4. isolated venv with the one dependency (pyyaml) ----------------------
if [[ ! -x "$PHASEKIT_HOME/.venv/bin/python" ]]; then
  say "creating venv at $PHASEKIT_HOME/.venv"
  python3 -m venv "$PHASEKIT_HOME/.venv"
fi
say "installing dependencies (pyyaml)"
"$PHASEKIT_HOME/.venv/bin/pip" install --quiet --upgrade pip pyyaml \
  || die "failed to install pyyaml into the venv (network required for a fresh install)."

# --- 5. launcher shim on PATH -----------------------------------------------
mkdir -p "$PHASEKIT_BIN"
shim="$PHASEKIT_BIN/phasekit"
cat > "$shim" <<EOF
#!/usr/bin/env bash
# phasekit launcher (generated by install.sh). Relocate by editing PHASEKIT_HOME.
export PHASEKIT_PYTHON="$PHASEKIT_HOME/.venv/bin/python"
exec "$PHASEKIT_HOME/scripts/phasekit.sh" "\$@"
EOF
chmod +x "$shim"

version="$(git -C "$PHASEKIT_HOME" describe --tags --always 2>/dev/null || echo "$ref")"
say "installed phasekit $version → $shim"

# --- 6. PATH advice + next steps --------------------------------------------
case ":$PATH:" in
  *":$PHASEKIT_BIN:"*) : ;;
  *) say "NOTE: $PHASEKIT_BIN is not on your PATH. Add it, e.g.:"
     printf '       echo '\''export PATH="%s:$PATH"'\'' >> ~/.bashrc && source ~/.bashrc\n' "$PHASEKIT_BIN" ;;
esac

cat <<EOF

Done. Next:
  cd your-project && phasekit adopt          # enrich an existing repo
  cd new-project  && phasekit bootstrap      # greenfield (optionally: phasekit bootstrap <profile>)

Other commands:
  phasekit check-version     # is a newer scaffold release out?
  phasekit upgrade           # re-provision this project against the current scaffold
  phasekit self-update       # move this phasekit install to the latest release tag
EOF
