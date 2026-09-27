#!/usr/bin/env python3
"""List the loop-internal surfaces a release changed, for its release note (v0.16.0).

WHY. A downstream project may pin a loop internal in its own tests — xmeo-v3
pinned the argument list of the loop's jq capture filter — and v0.14.10 added
three bindings to that filter without the release saying so. The project's
next session found out by going red. The contract (contracts/interface.json)
covers the surfaces phasekit PROMISES; this script also lists the internals a
project can nevertheless pin, so a release note names every one it moved and a
project can update its pin in the same upgrade.

What it compares between two refs of this repository:

  * contracts/interface.json — every family (env, artifacts, exit_codes,
    conventions): entries added, removed, or whose text changed;
  * the loop scripts (run-until-done.sh, run-phase.sh) — the PHASEKIT_*
    environment variables they read;
  * the loop scripts — for each shell function, the names its jq calls bind
    with --arg / --argjson / --slurpfile / --rawfile (a binding outside any
    function is reported under "(top level)").

Usage:
  python3 scripts/release-surfaces.py FROM [TO]      # TO defaults to the working tree

Scaffold-internal: a release tool for this repository, never installed downstream.
Exit 0 always when both refs are readable (an empty report is a valid answer);
2 when a ref cannot be read.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "contracts/interface.json"
LOOPS = ("scripts/run-until-done.sh", "scripts/run-phase.sh")
FAMILIES = ("env", "artifacts", "exit_codes", "conventions")

FUNC_RE = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")
JQ_BIND_RE = re.compile(r"--(?:arg|argjson|slurpfile|rawfile)\s+([A-Za-z_][A-Za-z0-9_]*)")
ENV_RE = re.compile(r"\$\{?(PHASEKIT_[A-Z0-9_]+)")


class RefError(Exception):
    pass


def read(ref: str | None, path: str, missing_ok: bool = False) -> str:
    if ref is None:
        f = REPO_ROOT / path
        if missing_ok and not f.exists():
            return ""
        return f.read_text(encoding="utf-8")
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # a script the ref predates is an empty surface, not an unreadable ref
        if missing_ok and ("exists on disk, but not in" in r.stderr
                           or "does not exist in" in r.stderr):
            return ""
        raise RefError(f"cannot read {path} at {ref}: {r.stderr.strip()}")
    return r.stdout


def contract_entries(text: str) -> dict[str, dict[str, str]]:
    doc = json.loads(text)
    out: dict[str, dict[str, str]] = {}
    for family in FAMILIES:
        value = doc.get(family)
        entries: dict[str, str] = {}
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    entries[str(item.get("name"))] = json.dumps(item, sort_keys=True)
        elif isinstance(value, dict):
            for key, item in value.items():
                entries[str(key)] = json.dumps(item, sort_keys=True)
        out[family] = entries
    return out


def loop_surfaces(text: str, script: str) -> tuple[set[str], dict[str, set[str]]]:
    env = set(ENV_RE.findall(text))
    binds: dict[str, set[str]] = {}
    top = f"{script} (top level)"
    current = top
    closer = None  # the line that ends the current function: its indent + "}"
    for line in text.splitlines():
        fm = FUNC_RE.match(line)
        if fm and closer is None:
            if line.rstrip().endswith("}"):  # a one-line function: f() { ...; }
                binds.setdefault(fm.group(2), set()).update(JQ_BIND_RE.findall(line))
                continue
            current, closer = fm.group(2), fm.group(1) + "}"
        elif closer is not None and line.rstrip() == closer:
            current, closer = top, None
            continue
        found = JQ_BIND_RE.findall(line)
        if found:
            binds.setdefault(current, set()).update(found)
    return env, binds


def report(frm: str, to: str | None) -> list[str]:
    lines: list[str] = []
    a, b = contract_entries(read(frm, CONTRACT)), contract_entries(read(to, CONTRACT))
    for family in FAMILIES:
        old, new = a[family], b[family]
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
        for label, names in (("added", added), ("removed", removed), ("changed", changed)):
            if names:
                lines.append(f"contract {family} {label}: {', '.join(names)}")
    env_a: set[str] = set()
    env_b: set[str] = set()
    bind_a: dict[str, set[str]] = {}
    bind_b: dict[str, set[str]] = {}
    for script in LOOPS:
        name = script.rsplit("/", 1)[-1]
        ea, ba = loop_surfaces(read(frm, script, missing_ok=True), name)
        eb, bb = loop_surfaces(read(to, script, missing_ok=True), name)
        env_a |= ea
        env_b |= eb
        for func, names in ba.items():
            bind_a.setdefault(func, set()).update(names)
        for func, names in bb.items():
            bind_b.setdefault(func, set()).update(names)
    if env_b - env_a:
        lines.append(f"loop env read, added: {', '.join(sorted(env_b - env_a))}")
    if env_a - env_b:
        lines.append(f"loop env read, removed: {', '.join(sorted(env_a - env_b))}")
    for func in sorted(set(bind_a) | set(bind_b)):
        before, after = bind_a.get(func, set()), bind_b.get(func, set())
        if before != after:
            label = func if func.endswith(")") else f"{func}()"
            plus = ", ".join(sorted(after - before)) or "—"
            minus = ", ".join(sorted(before - after)) or "—"
            lines.append(f"loop jq bindings in {label}: +[{plus}] -[{minus}]")
    return lines


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__.split("Usage:")[1].split("Scaffold-internal")[0].strip(), file=sys.stderr)
        return 2
    frm, to = argv[0], (argv[1] if len(argv) == 2 else None)
    try:
        lines = report(frm, to)
    except (RefError, ValueError) as exc:
        print(f"release-surfaces: {exc}", file=sys.stderr)
        return 2
    target = to or "working tree"
    print(f"Loop-internal surfaces changed, {frm} -> {target}:")
    for line in lines or ["(none)"]:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
