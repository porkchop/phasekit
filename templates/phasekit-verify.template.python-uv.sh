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
#
# Arguments (v0.19.3):
#   --fast | --full            Force a tier regardless of what is on disk.
#   --plan                     Print the tier decision and the commands it
#                              would run, then exit 0 — running no check
#                              (only the contracts check below, which is
#                              inert without a contracts.yaml).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Arguments are read first and judged after the cross-project contracts check
# below, which must run before the gate can stop (it is inert without a
# contracts.yaml); --plan then runs nothing else.
TIER_OVERRIDE=""
PLAN=0
ARG_ERROR=""
for arg in "$@"; do
  case "$arg" in
    --fast|--full)
      if [[ -n "$TIER_OVERRIDE" && "$TIER_OVERRIDE" != "${arg#--}" ]]; then
        ARG_ERROR="--fast and --full are exclusive"
      fi
      TIER_OVERRIDE="${arg#--}" ;;
    --plan) PLAN=1 ;;
    *) ARG_ERROR="unknown argument '$arg' (--fast | --full | --plan)" ;;
  esac
done

# --- Cross-project contracts (phasekit v0.7.0) -----------------------------
# No-op unless this repo has a contracts.yaml declaring dependencies on other
# projects' interfaces. When it does, this refuses if a declared contract is
# unreadable or its vendored copy has drifted from the provider's. The
# autonomous loop runs the same check itself and that call is the
# authoritative one -- this file is project-owned, so a check living only here
# could be edited away by the repo it polices. The call is repeated here so a
# human or a CI job running the gate directly sees the same answer.
#
# Runs FIRST, before any stack check that may fail open on a young repo: a
# contract violation is not something to skip because pyproject.toml is absent.
# See docs/CONTRACTS.md. A project pinned to phasekit (.phasekit-version) has
# no checker in its tree: the engine's runs as `phasekit contracts check`.
if [[ -f contracts.yaml ]]; then
  if [[ -f scripts/phasekit-contracts.py ]]; then
    python3 scripts/phasekit-contracts.py check
  else
    phasekit contracts check
  fi
fi


# Sentinel consumed by phasekit tooling: this profile seeds a real gate.
PHASEKIT_VERIFY_CONFIGURED=1

if [[ -n "$ARG_ERROR" ]]; then
  echo "phasekit-verify.sh: $ARG_ERROR" >&2
  exit 2
fi

# The tier (docs/QUALITY_GATES.md "Verify budget"): FULL exactly when
# artifacts/project-complete.json exists — a project never reaches "done" on
# the fast tier alone — else FAST, which excludes tests marked `slow`.
if [[ -n "$TIER_OVERRIDE" ]]; then
  TIER="$TIER_OVERRIDE"; TIER_WHY="forced by --$TIER_OVERRIDE"
elif [[ -f artifacts/project-complete.json ]]; then
  TIER=full; TIER_WHY="artifacts/project-complete.json present: the completion runs everything"
else
  TIER=fast; TIER_WHY="no artifacts/project-complete.json: tests marked slow are excluded"
fi
if [[ "$TIER" == full ]]; then
  PYTEST_ARGS=(pytest -q)
else
  PYTEST_ARGS=(pytest -q -m "not slow")
fi
echo "phasekit-verify.sh: $TIER tier ($TIER_WHY)"
if [[ "$PLAN" == 1 ]]; then
  if [[ -f pyproject.toml ]]; then
    echo "phasekit-verify.sh: --plan: would run uv sync --extra dev, ruff check ., mypy ., ${PYTEST_ARGS[*]} — nothing was run"
  else
    echo "phasekit-verify.sh: --plan: no pyproject.toml yet, so no check would run — nothing was run"
  fi
  exit 0
fi


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

# Verify budget (docs/QUALITY_GATES.md "Verify budget"): this gate runs before
# every phase commit, so it targets ~30s (60s ceiling). The gate runs only the
# FAST tier — tests marked `slow` are excluded here, and the full suite stays
# mandatory at the verification-sprint gate. Splitting governs WHEN tests run,
# never WHETHER.
#
# The marker convention as the suite grows:
#   - register the marker in pyproject.toml:
#       [tool.pytest.ini_options]
#       markers = ["slow: excluded from the pre-commit gate; runs at the sprint"]
#   - mark tests by MEASURED duration (`uv run pytest --durations=25`), not by
#     module or intuition, using @pytest.mark.slow
#
# Completion runs full: once artifacts/project-complete.json exists, the gate
# runs the complete suite — a project never reaches "done" on the fast tier
# alone (fast tier per-commit; full suite at the sprint AND at completion).
# The tier was decided at the top (--fast / --full force it).
run "$UV_BIN" run "${PYTEST_ARGS[@]}"

echo "phasekit-verify.sh: all checks passed."
