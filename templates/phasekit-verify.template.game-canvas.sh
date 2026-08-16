#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop — game-canvas stack.
#
# Seeded by the `game-canvas` profile. Same stack contract as static-web
# (browser-native ESM, zero runtime dependencies, node:test) with the game
# convention that the deterministic core — rules, simulation, state — is
# plain JS testable in node, so the gate exercises real game logic without
# a canvas. This file is PROJECT-OWNED after seeding: tune the checks and
# keep the gate green.
#
# scripts/run-until-done.sh runs this script before creating any phase commit
# (whether or not AUTO_PUSH is enabled). A non-zero exit blocks the commit:
#   - the wrapper writes artifacts/phase-verify-failed.json
#   - the next iteration's CONTINUE_PROMPT prioritizes fixing the failure
#     before any new phase work
#
# Goals:
#   - Catch the cheap, embarrassing class of CI failures locally
#     (broken game-logic tests, imports that 404 in the browser, dependency creep)
#   - Stay FAST. This runs every iteration. Aim for under ~30 seconds.
#   - Do NOT run browser/canvas automation here — that belongs to the
#     verification-sprint gate (docs/QUALITY_GATES.md), e.g. qa-playwright.
#
# Environment overrides (advanced):
#   PHASEKIT_VERIFY_CMD="..."  Replace this script with a one-shot command.
#   VERIFY_SKIP=1              Skip verify entirely for this iteration.

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


# Sentinel consumed by phasekit tooling: this profile seeds a real gate.
PHASEKIT_VERIFY_CONFIGURED=1

run() {
  echo "==> $*"
  "$@"
}

# 1) Unit tests — npm test when package.json defines a test script, else the
#    built-in node:test runner (exits 0 when no test files exist yet, so a
#    brand-new project isn't wedged). Keep engine/rules tests canvas-free so
#    they run here.
if [[ -f package.json ]] && node -e 'const p=require("./package.json"); process.exit(p.scripts && p.scripts.test ? 0 : 1)'; then
  run npm test
else
  run node --test
fi

# 2) No-runtime-dependency assertion — game-canvas convention
#    (docs/CONVENTIONS.md): the browser loads plain ESM; package.json exists
#    only for dev conveniences. Delete this check only with an ADR.
if [[ -f package.json ]]; then
  echo "==> assert no runtime dependencies"
  node -e '
    const p = require("./package.json");
    const deps = Object.keys(p.dependencies || {});
    if (deps.length) {
      console.error("game-canvas convention violated: runtime dependencies found: " + deps.join(", "));
      console.error("(dev tooling belongs in devDependencies; the game itself must be dependency-free)");
      process.exit(1);
    }'
fi

# 3) ESM import-graph check — every relative static import in the repo's JS
#    must resolve to a file on disk (a missing .js extension or a renamed
#    module 404s silently in the browser; catch it here).
echo "==> check relative ESM imports resolve"
python3 - <<'PYEOF'
import os, re, sys

IMPORT_RE = re.compile(
    r"""(?m)^\s*(?:import|export)\s+[^'"]*?\bfrom\s+['"](\.{1,2}/[^'"]+)['"]"""
)
SIDE_EFFECT_RE = re.compile(r"""(?m)^\s*import\s+['"](\.{1,2}/[^'"]+)['"]""")
SKIP_DIRS = {".git", "node_modules", "artifacts", ".scaffold", "dist"}

broken = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for name in files:
        if not name.endswith((".js", ".mjs")):
            continue
        path = os.path.join(root, name)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for spec in IMPORT_RE.findall(text) + SIDE_EFFECT_RE.findall(text):
            target = os.path.normpath(os.path.join(root, spec))
            if not os.path.isfile(target):
                broken.append(f"{path}: '{spec}' -> {target} (missing)")

if broken:
    print("broken relative imports:", file=sys.stderr)
    for line in broken:
        print(f"  {line}", file=sys.stderr)
    sys.exit(1)
print("all relative imports resolve.")
PYEOF

echo "phasekit-verify.sh: all checks passed."
