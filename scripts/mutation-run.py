#!/usr/bin/env python3
"""Mutation-testing execution harness (opt-in; `with-mutation` profile).

WHAT THIS AUTOMATES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
The value of mutation testing here has consistently been in DESIGNING the
mutants — choosing the edit that a weak test would not notice. In the phase
that triggered this protocol it caught a guard test that only discriminated
against the last of six reporting filters, a fail-open that answered "current"
for a missing file, and a detector blind to aliased imports. All three were
real defects that ordinary tests missed, and all three came from a human-or-LLM
judgement about where the tests were probably thin.

The COST was the hand-driven run loop: patch, run, revert, remember, repeat,
rebuilt into /tmp from scratch every phase.

So: the LLM designs the mutants and writes them into a spec file. This script
executes them. It invents no mutants of its own.

WHY THERE IS NO CACHE
---------------------
A resumable on-disk cache of mutant results was considered and REJECTED.
Results are only valid against the exact tree they ran on, so resuming turns
this into a cache-invalidation problem whose failure mode is a FALSE GREEN on a
quality gate — worse than having no gate at all, because it is trusted.

Instead: chunking. Mutants are independent (patch, run, revert), so a run is
just N sequential foreground calls. Nothing is backgrounded, so nothing can be
orphaned when a turn ends — which is the failure this whole release exists to
prevent. The audit record this writes is WRITE-ONLY: nobody resumes from it, so
it needs no invalidation and carries no false-green risk.

Usage:
  python3 scripts/mutation-run.py --spec docs/mutants/iteration-12.json
  python3 scripts/mutation-run.py --spec S --chunk 5 --index 0   # one chunk
  python3 scripts/mutation-run.py --spec S --max-seconds 600     # budget
  python3 scripts/mutation-run.py --spec S --list                # plan only

Spec format (see docs/MUTATION_TESTING.md):
  {
    "version": 1,
    "test_command": "python3 -m unittest discover -s tests -q",
    "mutants": [
      {"id": "m1", "file": "pkg/mod.py", "find": "if n > 0:", "replace": "if n >= 0:",
       "rationale": "boundary in the dispatch guard"},
      {"id": "m2", "file": "pkg/mod.py", "find": "x = 1", "replace": "x = 1 + 0",
       "equivalent": true, "rationale": "no observable difference"}
    ]
  }

Exit codes:
  0  every mutant that ran was KILLED (or was declared equivalent)
  1  at least one mutant SURVIVED — your tests do not discriminate there
  2  usage/spec error, dirty tree, or a mutant could not be applied
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SPEC_VERSION = 1
DEFAULT_CHUNK = 5
AUDIT_PATH = "artifacts/mutation-run.json"

KILLED = "killed"
SURVIVED = "survived"
EQUIVALENT = "equivalent"
ERROR = "error"
NOT_RUN = "not_run"


class SpecError(Exception):
    pass


def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"{path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise SpecError(f"{path}: top level must be an object")
    if spec.get("version") != SPEC_VERSION:
        raise SpecError(f"{path}: `version` must be {SPEC_VERSION}, got {spec.get('version')!r}")
    if not isinstance(spec.get("test_command"), str) or not spec["test_command"].strip():
        raise SpecError(f"{path}: `test_command` must be a non-empty string")
    mutants = spec.get("mutants")
    if not isinstance(mutants, list) or not mutants:
        raise SpecError(f"{path}: `mutants` must be a non-empty list")

    seen = set()
    for i, m in enumerate(mutants):
        if not isinstance(m, dict):
            raise SpecError(f"{path}: mutants[{i}] must be an object")
        for key in ("id", "file", "find", "rationale"):
            if not isinstance(m.get(key), str) or not m[key]:
                raise SpecError(f"{path}: mutants[{i}] needs a non-empty `{key}`")
        if m["id"] in seen:
            raise SpecError(f"{path}: duplicate mutant id {m['id']!r}")
        seen.add(m["id"])
        if not m.get("equivalent") and not isinstance(m.get("replace"), str):
            raise SpecError(
                f"{path}: mutants[{i}] ({m['id']}) needs `replace` "
                f"(or `equivalent: true` with a rationale)")
    return spec


def repo_is_dirty(root: Path) -> bool:
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def run_mutant(root: Path, mutant: dict, test_command: str, timeout: int | None):
    """Apply, test, revert. The revert is in a finally so an interrupted run
    cannot leave a mutated file behind — a stray mutation that survives into a
    commit is the worst outcome this script could produce."""
    target = root / mutant["file"]
    if not target.is_file():
        return ERROR, 0.0, f"{mutant['file']} does not exist"

    original = target.read_text(encoding="utf-8")
    occurrences = original.count(mutant["find"])
    if occurrences != 1:
        # Ambiguity makes the result meaningless: we would not know which site
        # the test outcome refers to. Make the spec more specific instead.
        return ERROR, 0.0, (
            f"`find` matches {occurrences} times in {mutant['file']} "
            f"(need exactly 1 — quote more surrounding context)")

    started = time.monotonic()
    try:
        target.write_text(original.replace(mutant["find"], mutant["replace"], 1),
                          encoding="utf-8")
        try:
            proc = subprocess.run(test_command, shell=True, cwd=root,
                                  capture_output=True, text=True, timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            # A hanging suite is a kill: the mutant changed observable
            # behaviour. Recorded distinctly so the audit does not imply the
            # tests asserted anything.
            return KILLED, time.monotonic() - started, "test command timed out"
    finally:
        target.write_text(original, encoding="utf-8")

    elapsed = time.monotonic() - started
    if rc == 0:
        return SURVIVED, elapsed, "tests passed with the mutation applied"
    return KILLED, elapsed, f"tests failed (exit {rc})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mutation-run",
        description="Execute an LLM-designed mutation spec; write an audit record.")
    ap.add_argument("--spec", required=True, help="Path to the mutant spec JSON.")
    ap.add_argument("--repo", default=".", help="Repository root (default: cwd).")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help=f"Mutants per chunk (default {DEFAULT_CHUNK}; "
                         f"about what fits in one foreground tool call).")
    ap.add_argument("--index", type=int, default=None,
                    help="Run only this chunk (0-based). Omit to run all chunks.")
    ap.add_argument("--max-seconds", type=int, default=None,
                    help="Budget. Stop starting new mutants once exceeded; the "
                         "remainder is recorded as not_run, never as passing.")
    ap.add_argument("--timeout", type=int, default=None,
                    help="Per-mutant test timeout in seconds.")
    ap.add_argument("--audit", default=AUDIT_PATH, help=f"Audit record path (default {AUDIT_PATH}).")
    ap.add_argument("--list", action="store_true", help="Print the plan and exit.")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="Run even though the tree has uncommitted changes (unsafe).")
    args = ap.parse_args(argv)

    root = Path(args.repo).resolve()
    try:
        spec = load_spec(Path(args.spec))
    except SpecError as exc:
        print(f"mutation-run: {exc}", file=sys.stderr)
        return 2

    mutants = spec["mutants"]
    if args.chunk < 1:
        print("mutation-run: --chunk must be >= 1", file=sys.stderr)
        return 2
    chunks = [mutants[i:i + args.chunk] for i in range(0, len(mutants), args.chunk)]

    if args.list:
        print(f"{len(mutants)} mutant(s) in {len(chunks)} chunk(s) of {args.chunk}:")
        for c, chunk in enumerate(chunks):
            ids = ", ".join(m["id"] for m in chunk)
            print(f"  chunk {c}: {ids}")
        print(f"test_command: {spec['test_command']}")
        return 0

    if args.index is not None:
        if not 0 <= args.index < len(chunks):
            print(f"mutation-run: --index {args.index} out of range "
                  f"(0..{len(chunks) - 1})", file=sys.stderr)
            return 2
        selected = chunks[args.index]
    else:
        selected = mutants

    # A dirty tree makes patch/revert unsafe: an interrupted run could restore
    # the file over the top of uncommitted edits.
    if not args.allow_dirty and repo_is_dirty(root):
        print("mutation-run: the working tree has uncommitted changes. Mutants are "
              "applied and reverted in place, so commit or stash first "
              "(or pass --allow-dirty if you accept the risk).", file=sys.stderr)
        return 2

    results = []
    budget_exhausted = False
    started_at = time.monotonic()
    for mutant in selected:
        if mutant.get("equivalent"):
            print(f"  {mutant['id']}: equivalent (declared) — not run")
            results.append({"id": mutant["id"], "file": mutant["file"],
                            "rationale": mutant["rationale"],
                            "outcome": EQUIVALENT, "seconds": 0.0,
                            "detail": "declared equivalent by the spec author"})
            continue

        if args.max_seconds is not None and not budget_exhausted \
                and time.monotonic() - started_at >= args.max_seconds:
            budget_exhausted = True
        if budget_exhausted:
            # Recorded as not_run, never silently dropped: an unrun mutant that
            # looked like a pass would be the false green this design refuses.
            results.append({"id": mutant["id"], "file": mutant["file"],
                            "rationale": mutant["rationale"],
                            "outcome": NOT_RUN, "seconds": 0.0,
                            "detail": "budget exhausted before this mutant started"})
            continue

        outcome, seconds, detail = run_mutant(root, mutant, spec["test_command"], args.timeout)
        marker = {KILLED: "killed", SURVIVED: "SURVIVED", ERROR: "ERROR"}[outcome]
        print(f"  {mutant['id']}: {marker} ({seconds:.1f}s) — {detail}")
        results.append({"id": mutant["id"], "file": mutant["file"],
                        "rationale": mutant["rationale"], "outcome": outcome,
                        "seconds": round(seconds, 2), "detail": detail})

    summary = {name: sum(1 for r in results if r["outcome"] == name)
               for name in (KILLED, SURVIVED, EQUIVALENT, ERROR, NOT_RUN)}

    audit = {
        "version": 1,
        "spec": str(args.spec),
        "test_command": spec["test_command"],
        "chunk_size": args.chunk,
        "chunk_index": args.index,
        "budget_seconds": args.max_seconds,
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "results": results,
        "summary": summary,
    }
    audit_path = root / args.audit
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")

    print(f"mutation-run: killed {summary[KILLED]}, SURVIVED {summary[SURVIVED]}, "
          f"equivalent {summary[EQUIVALENT]}, error {summary[ERROR]}, "
          f"not run {summary[NOT_RUN]} -> {args.audit}")

    if summary[SURVIVED]:
        print("mutation-run: a surviving mutant means your tests do not discriminate "
              "there. Strengthen the test, or declare the mutant equivalent in the "
              "spec WITH a rationale — never by deleting it.", file=sys.stderr)
        return 1
    if summary[ERROR]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
