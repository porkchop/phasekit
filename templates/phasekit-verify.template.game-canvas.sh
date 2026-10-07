#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop — game-canvas stack.
#
# Seeded by the `game-canvas` profile (docs/CONVENTIONS.md): node:test, the
# runtime-dependency ALLOWLIST (runtime-dependencies.json, one ADR per
# entry), and the game convention that the deterministic core — rules,
# simulation, state — is testable in node, so the gate exercises real game
# logic without a canvas. A project with a build step adds it here, before
# the tests that need it. This file is PROJECT-OWNED after seeding: tune the
# checks and keep the gate green.
#
# scripts/run-until-done.sh runs this script before creating any phase commit
# (whether or not AUTO_PUSH is enabled). A non-zero exit blocks the commit:
#   - the wrapper writes artifacts/phase-verify-failed.json
#   - the next iteration's CONTINUE_PROMPT prioritizes fixing the failure
#     before any new phase work
#
# Goals:
#   - Catch the cheap, embarrassing class of CI failures locally
#     (broken game-logic tests, imports that 404 in the browser, undeclared
#     dependencies)
#   - Stay within the verify budget (docs/QUALITY_GATES.md "Verify budget"):
#     this runs before every commit — a fast tier here, the full suite at the
#     verification sprint and at completion.
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

# 2) Runtime-dependency allowlist — game-canvas convention (docs/CONVENTIONS.md
#    "Dependency policy"). Every `dependencies`/`optionalDependencies` entry in
#    the root package.json and in each npm workspace must be declared in
#    runtime-dependencies.json under its workspace directory ("." = root),
#    naming the ADR that decided it; that ADR file must exist. Another
#    workspace's package needs no entry; devDependencies are not policed.
#    No allowlist file = an empty allowlist.
if [[ -f package.json ]]; then
  echo "==> check runtime dependencies are declared (runtime-dependencies.json)"
  python3 - <<'PYEOF'
import glob, json, os, sys

ALLOWLIST = "runtime-dependencies.json"
FIELDS = ("dependencies", "optionalDependencies")


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        sys.exit(f"{path}: expected a JSON object")
    return data


def norm(d):
    return os.path.normpath(d).replace(os.sep, "/")


root = load("package.json")
patterns = root.get("workspaces") or []
if isinstance(patterns, dict):
    patterns = patterns.get("packages") or []
manifests = {".": root}
for pattern in patterns:
    if not isinstance(pattern, str) or pattern.startswith("!"):
        continue
    for d in sorted(glob.glob(pattern, recursive=True)):
        d = norm(d)
        pj = os.path.join(d, "package.json")
        if "node_modules" in d.split("/") or d in manifests or not os.path.isfile(pj):
            continue
        manifests[d] = load(pj)
# Another workspace's package needs no entry when the spec can only resolve to
# that workspace: `workspace:`/`file:`/`link:`, or a plain version range. An
# alias (`npm:`), a git/URL source or a `owner/repo` shorthand is third-party
# code under a workspace's name and needs an entry like any other.
internal = {m.get("name") for m in manifests.values() if isinstance(m.get("name"), str)}


def is_workspace_link(name, spec):
    if name not in internal or not isinstance(spec, str):
        return False
    if spec.startswith(("workspace:", "file:", "link:")):
        return True
    return ":" not in spec and "/" not in spec

allow = {}
if os.path.exists(ALLOWLIST):
    try:
        allow = load(ALLOWLIST)
    except ValueError as e:
        sys.exit(f"{ALLOWLIST}: not valid JSON: {e}")
    if not all(isinstance(v, dict) for v in allow.values()):
        sys.exit(f'{ALLOWLIST}: expected {{"<workspace dir>": {{"<dependency>": "<ADR path>"}}}}')
    normalised = {}
    for k, v in allow.items():
        if norm(k) in normalised:
            sys.exit(f'{ALLOWLIST}: "{k}" names the same workspace as another key')
        normalised[norm(k)] = v
    allow = normalised

problems = []
for d, m in manifests.items():
    declared = allow.get(d, {})
    for field in FIELDS:
        deps = m.get(field) or {}
        if not isinstance(deps, dict):
            sys.exit(f"{d}/package.json: `{field}` is not an object")
        for name in sorted(deps):
            if is_workspace_link(name, deps[name]):
                continue
            adr = declared.get(name)
            if not adr:
                problems.append(f'{d}/package.json: runtime dependency "{name}" ({field}) '
                                f'is not declared under "{d}" in {ALLOWLIST}')
            elif not isinstance(adr, str) or not os.path.isfile(adr):
                problems.append(f'{ALLOWLIST}: "{d}" -> "{name}" names ADR {adr!r}, '
                                'which does not exist')
for d, entries in allow.items():
    m = manifests.get(d) or {}
    for name in entries:
        if not any(name in (m.get(field) or {}) for field in FIELDS):
            print(f'note: {ALLOWLIST}: "{d}" -> "{name}" is allowed but not declared there')
if problems:
    print("game-canvas dependency policy violated (docs/CONVENTIONS.md "
          '"Dependency policy"; add the entry with its ADR, or drop the dependency):',
          file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    sys.exit(1)
print(f"runtime dependencies declared ({len(manifests)} package.json checked).")
PYEOF
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
