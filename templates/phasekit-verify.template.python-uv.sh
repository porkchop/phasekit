#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop — python-uv stack.
#
# Seeded by the `python-uv` profile from the fleet's real Python gate
# (uv sync → ruff → mypy → pytest). This file is PROJECT-OWNED after
# seeding: tune the commands to your repo (e.g. scope mypy to your
# package) and keep the gate green.
#
# scripts/run-until-done.sh runs this script before creating any phase commit
# (whether or not AUTO_PUSH is enabled). A non-zero exit blocks the commit:
#   - the wrapper writes artifacts/phase-verify-failed.json
#   - the next iteration's CONTINUE_PROMPT prioritizes fixing the failure
#     before any new phase work
#
# Goals:
#   - Catch the cheap, embarrassing class of CI failures locally
#     (lint, typecheck, broken unit tests, formatter drift)
#   - Stay FAST. This runs every iteration. Aim for under ~30 seconds.
#   - Do NOT run full E2E or integration here — those belong to the
#     verification-sprint gate (docs/QUALITY_GATES.md).
#
# Environment overrides (advanced):
#   PHASEKIT_VERIFY_CMD="..."  Replace this script with a one-shot command.
#   VERIFY_SKIP=1              Skip verify entirely for this iteration.
#   UV_BIN=/path/to/uv         Pin a specific uv binary (defaults to `which uv`,
#                              falling back to ~/.local/bin/uv).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Sentinel consumed by phasekit tooling: this profile seeds a real gate.
PHASEKIT_VERIFY_CONFIGURED=1

# A brand-new project may not have a pyproject.toml yet (first sessions often
# commit docs/specs only). Fail-open until the Python project materializes so
# the gate can't wedge iteration 1.
if [[ ! -f pyproject.toml ]]; then
  echo "phasekit-verify.sh: no pyproject.toml yet — skipping python-uv checks." >&2
  exit 0
fi

# Toolchain: `uv` manages the venv and runs the checks. `uv sync` installs the
# project + dev extras from pyproject.toml; subsequent runs are effectively
# no-ops when the lockfile is satisfied, keeping the gate fast.
#
# Look uv up explicitly so this works under systemd / docker where PATH may
# not include the user's ~/.local/bin.
UV_BIN="${UV_BIN:-}"
if [[ -z "$UV_BIN" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    echo "phasekit-verify.sh: \`uv\` not found on PATH. Install uv (https://docs.astral.sh/uv) or set UV_BIN." >&2
    exit 127
  fi
fi

run() {
  echo "==> $*"
  "$@"
}

# Convention (docs/CONVENTIONS.md): dev tools live in the `dev` optional
# dependency group — ruff, mypy, pytest.
run "$UV_BIN" sync --extra dev --quiet
run "$UV_BIN" run ruff check .
# Scope mypy to your package (e.g. `mypy src/`) if the tree-wide run is noisy,
# or set `files = [...]` under [tool.mypy] in pyproject.toml and drop the dot.
run "$UV_BIN" run mypy .
run "$UV_BIN" run pytest -q

echo "phasekit-verify.sh: all checks passed."
