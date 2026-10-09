#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop — static-web stack.
#
# Seeded by the `static-web` profile from the fleet's real static-web
# practice (node --test / npm test, zero runtime dependencies, browser-native
# ESM). This file is PROJECT-OWNED after seeding: tune the checks and keep
# the gate green.
#
# scripts/run-until-done.sh runs this script before creating any phase commit
# (whether or not AUTO_PUSH is enabled). A non-zero exit blocks the commit:
#   - the wrapper writes artifacts/phase-verify-failed.json
#   - the next iteration's CONTINUE_PROMPT prioritizes fixing the failure
#     before any new phase work
#
# Goals:
#   - Catch the cheap, embarrassing class of CI failures locally
#     (broken unit tests, imports that 404 in the browser, dependency creep)
#   - Stay FAST. This runs every iteration. Aim for under ~30 seconds.
#   - Do NOT run full E2E or browser automation here — those belong to the
#     verification-sprint gate (docs/QUALITY_GATES.md).
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
#    brand-new project isn't wedged).
#    v0.19.3: the run is judged on what node's runner REPORTS as well as on its
#    exit status. The output is captured outside the tree (the gate is
#    read-only over it) and the status is the runner's, never the pipe's, so
#    nothing masks a failure; a reported failure is red even under a status of
#    0; and ZERO tests reported while the tree tracks test files is refused (a
#    glob or a script that silently ran nothing). node prints its totals as
#    `ℹ tests N` (the spec reporter: node 22+ piped, any TTY) or `# tests N`
#    (TAP: node 20 piped); both are read, and a runner that prints neither is
#    judged on its status alone. NODE_TEST_CONTEXT is dropped, so a gate
#    started from inside a node test still prints its own totals.
if [[ -f package.json ]] && node -e 'const p=require("./package.json"); process.exit(p.scripts && p.scripts.test ? 0 : 1)'; then
  TEST_CMD=(npm test)
else
  TEST_CMD=(node --test)
fi
TEST_LOG="$(mktemp "${TMPDIR:-/tmp}/phasekit-verify.XXXXXX")"
trap 'rm -f "$TEST_LOG"' EXIT
echo "==> ${TEST_CMD[*]}"
set +e
env -u NODE_TEST_CONTEXT "${TEST_CMD[@]}" 2>&1 | tee "$TEST_LOG"
test_status="${PIPESTATUS[0]}"
set -e
if [[ "$test_status" -ne 0 ]]; then
  echo "phasekit-verify.sh: the test run exited $test_status" >&2
  exit "$test_status"
fi
summary() {
  # "<tests> <fail>" summed over node's OWN summary blocks — consecutive
  # `ℹ`/`#` lines tests … duration_ms, one per runner invocation (a script may
  # run node more than once) — or nothing when there is none. A lone
  # `fail 3` a test prints is not a block, so it is never counted.
  awk '
    function flush() { if (ht && hf) { T += t; F += f; seen = 1 } ht = hf = 0 }
    /^[[:space:]]*(ℹ|#)[[:space:]]+(tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)[[:space:]]+[0-9.]+[[:space:]]*$/ {
      n = split($0, w, /[[:space:]]+/); key = w[n - 1]; val = w[n]
      if (key == "tests") { flush(); ht = 1; t = val }
      else if (key == "fail" && ht) { hf = 1; f = val }
      else if (key == "duration_ms") { flush() }
      next
    }
    { ht = hf = 0 }
    END { flush(); if (seen) print T, F }' "$TEST_LOG"
}
read -r reported_tests reported_fail <<<"$(summary)" || true
if [[ -n "$reported_fail" && "$reported_fail" -gt 0 ]]; then
  echo "phasekit-verify.sh: the runner reported $reported_fail failing test(s) under exit status 0" >&2
  exit 1
fi
if [[ "$reported_tests" == 0 ]]; then
  # captured first: `grep -q` stopping early would SIGPIPE git, and under
  # pipefail that reads as "no test files" (review F1)
  tracked_files="$(git ls-files 2>/dev/null || true)"
  if grep -E '(^|/)(test|tests|__tests__)/.+\.[cm]?[jt]s$|[._-]test\.[cm]?[jt]s$|(^|/)test(-[^/]*)?\.[cm]?[jt]s$' \
       <<<"$tracked_files" >/dev/null; then
    echo "phasekit-verify.sh: the runner reported ZERO tests, but the tree tracks test files — the test command ran nothing" >&2
    exit 1
  fi
fi

# 2) No-runtime-dependency assertion — static-web convention
#    (docs/CONVENTIONS.md): the browser loads plain ESM; package.json exists
#    only for dev conveniences. Delete this check only with an ADR.
if [[ -f package.json ]]; then
  echo "==> assert no runtime dependencies"
  node -e '
    const p = require("./package.json");
    const deps = Object.keys(p.dependencies || {});
    if (deps.length) {
      console.error("static-web convention violated: runtime dependencies found: " + deps.join(", "));
      console.error("(dev tooling belongs in devDependencies; the app itself must be dependency-free)");
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
