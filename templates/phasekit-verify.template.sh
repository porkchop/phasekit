#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop.
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
#     verification-sprint gate (docs/QUALITY_GATES.md), which Claude
#     drives at phase boundaries with cumulative risk.
#
# Environment overrides (advanced):
#   PHASEKIT_VERIFY_CMD="..."  Replace this script with a one-shot command.
#   VERIFY_SKIP=1              Skip verify entirely for this iteration
#                              (use sparingly — e.g. docs-only phases or
#                              TDD phases that intentionally commit red).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

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
# See docs/CONTRACTS.md.
if [[ -f contracts.yaml && -f scripts/phasekit-contracts.py ]]; then
  python3 scripts/phasekit-contracts.py check
fi


# ============================================================================
# CONFIGURE: uncomment the checks that match your stack.
#
# Once you've enabled at least one real check, FLIP THE SENTINEL below to "1"
# so the stub-mode warning at the bottom of this file stops firing.
# ============================================================================

# --- Node / TypeScript projects -------------------------------------------
# npm run lint
# npm run typecheck
# npm test -- --run

# --- Python projects ------------------------------------------------------
# ruff check .
# mypy .
# pytest -q

# --- Go projects ----------------------------------------------------------
# go vet ./...
# go test ./...

# ============================================================================
# Sentinel — set to "1" once at least one real check above is enabled. While
# this is "0", the gate fail-opens (the loop continues) but the stub message
# below fires every iteration as a reminder that {{PROJECT_NAME}}'s verify
# isn't actually configured yet.
# ============================================================================
PHASEKIT_VERIFY_CONFIGURED=0

if [[ "$PHASEKIT_VERIFY_CONFIGURED" != "1" ]]; then
  echo "phasekit-verify.sh: STUB MODE — no checks configured." >&2
  echo "  Edit scripts/phasekit-verify.sh, enable real checks, set" >&2
  echo "  PHASEKIT_VERIFY_CONFIGURED=1 in this file. See docs/QUALITY_GATES.md." >&2
fi
exit 0
