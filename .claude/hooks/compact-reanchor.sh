#!/usr/bin/env bash
#
# SessionStart hook: re-anchor a COMPACTED autonomous session.
#
# After context compaction, the agent's grip on "which phase am I in, what
# are the acceptance criteria" is exactly what degrades. This hook fires ONLY
# when SessionStart's source is "compact" (matcher-scoped in settings AND
# re-checked inline — belt and braces) and emits a bounded manifest to stdout,
# re-derived from disk at fire time.
#
# Contract (ADOPTIONS-agentic-foundry item 1, v0.4.7):
#   - <= 2048 UTF-8 bytes of output, truncation-marked
#   - fail-open: ALWAYS exit 0; print nothing on any resolution failure
#   - no runtime deps beyond bash + python3 stdlib
set -u

# Capture the hook payload BEFORE the heredoc (which occupies python's
# stdin); pass it via env.
HOOK_PAYLOAD="$(cat 2>/dev/null || true)"
export HOOK_PAYLOAD

python3 - <<'PY' 2>/dev/null || true
import json, os, sys

def out(s: str) -> None:
    sys.stdout.write(s)

try:
    payload = json.loads(os.environ.get("HOOK_PAYLOAD", ""))
except Exception:
    sys.exit(0)
if payload.get("source") != "compact":
    sys.exit(0)

root = os.getcwd()
parts = []

# Last approved phase (artifacts/phase-approval.json), if any.
try:
    with open(os.path.join(root, "artifacts", "phase-approval.json")) as f:
        appr = json.load(f)
    phase = appr.get("phase") or appr.get("last_approved_phase")
    if phase:
        parts.append(f"Last approved phase: {phase}")
except Exception:
    pass

# Phase headings from docs/PHASES.md — the map the session navigates by.
try:
    with open(os.path.join(root, "docs", "PHASES.md")) as f:
        heads = [ln.rstrip() for ln in f if ln.startswith("## ") or ln.startswith("# Iteration")]
    if heads:
        parts.append("PHASES.md headings:\n" + "\n".join(heads[-30:]))
except Exception:
    pass

# SPEC acceptance criteria: first lines of the first section whose heading
# mentions acceptance.
try:
    with open(os.path.join(root, "docs", "SPEC.md")) as f:
        lines = f.readlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("#") and "cceptance" in ln), None)
    if start is not None:
        chunk = []
        for ln in lines[start:start + 40]:
            if chunk and ln.startswith("# ") :
                break
            chunk.append(ln.rstrip())
        parts.append("\n".join(chunk))
except Exception:
    pass

if not parts:
    sys.exit(0)

manifest = ("[compact-reanchor] Context was compacted. Re-derived from disk "
            "just now — trust THIS over compacted memory:\n\n" + "\n\n".join(parts))
data = manifest.encode("utf-8")
if len(data) > 2048:
    data = data[:2000] + b"\n[...truncated by compact-reanchor]"
out(data.decode("utf-8", errors="ignore"))
PY
exit 0
