#!/usr/bin/env bash
#
# Pre-commit verification gate for the autonomous loop — docs-only stack.
#
# Seeded by the `docs-only` profile. A docs repo's cheap, embarrassing
# failure class is broken internal links and dangling anchors, so that is
# exactly what this gate checks — internal links and references ONLY.
# External URLs are never fetched and never fail the gate. This file is
# PROJECT-OWNED after seeding: tune the checks and keep the gate green.
#
# scripts/run-until-done.sh runs this script before creating any phase commit
# (whether or not AUTO_PUSH is enabled). A non-zero exit blocks the commit:
#   - the wrapper writes artifacts/phase-verify-failed.json
#   - the next iteration's CONTINUE_PROMPT prioritizes fixing the failure
#     before any new phase work
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

echo "==> check markdown internal links + references"
python3 - <<'PYEOF'
"""Markdown internal-link checker (python stdlib only).

Checks, for every tracked-ish .md file:
  - inline links/images [text](target) whose target is RELATIVE resolve to
    an existing file or directory
  - fragment links (#anchor, file.md#anchor) resolve to a real heading in
    the target file (GitHub-style slugs, duplicate headings get -1, -2, …)

Deliberately NOT checked (decision: links + internal refs only):
  - external URLs (http/https/mailto/…) — never fetched
  - root-absolute paths (/foo) — deploy-dependent
  - prose style, spelling
"""
import os, re, sys, unicodedata
from urllib.parse import unquote

SKIP_DIRS = {".git", "node_modules", "artifacts", ".scaffold", "dist", ".claude"}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)<>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.*)$")
FENCE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1\s*$")
CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def md_files():
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


def strip_code(text):
    return CODE_SPAN_RE.sub("", FENCE_RE.sub("", text))


def slugify(heading):
    # Strip markdown decoration, then GitHub-style slug.
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [t](u) -> t
    heading = heading.replace("`", "").replace("*", "").replace("_", " ")
    heading = unicodedata.normalize("NFKD", heading).strip().lower()
    heading = re.sub(r"[^\w\s-]", "", heading, flags=re.UNICODE)
    return re.sub(r"\s+", "-", heading).replace("_", "-")


def anchors_of(path, cache={}):
    if path not in cache:
        try:
            text = strip_code(open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            cache[path] = None
            return None
        slugs, seen = set(), {}
        for h in HEADING_RE.findall(text):
            base = slugify(h)
            n = seen.get(base, 0)
            seen[base] = n + 1
            slugs.add(base if n == 0 else f"{base}-{n}")
        cache[path] = slugs
    return cache[path]


broken = []
for md in md_files():
    text = strip_code(open(md, encoding="utf-8", errors="replace").read())
    for raw in LINK_RE.findall(text):
        target = unquote(raw)
        if SCHEME_RE.match(target) or target.startswith(("/", "//")):
            continue  # external or deploy-dependent — out of scope
        path_part, _, frag = target.partition("#")
        if path_part:
            resolved = os.path.normpath(os.path.join(os.path.dirname(md), path_part))
            if not os.path.exists(resolved):
                broken.append(f"{md}: ({raw}) -> {resolved} does not exist")
                continue
        else:
            resolved = md
        if frag and resolved.endswith(".md"):
            slugs = anchors_of(resolved)
            if slugs is not None and slugs and frag.lower() not in slugs:
                broken.append(f"{md}: ({raw}) -> no heading '#{frag}' in {resolved}")

if broken:
    print("broken internal links/references:", file=sys.stderr)
    for line in broken:
        print(f"  {line}", file=sys.stderr)
    sys.exit(1)
print("all internal links and references resolve.")
PYEOF

echo "phasekit-verify.sh: all checks passed."
