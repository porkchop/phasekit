#!/usr/bin/env python3
"""phasekit's declared surface for downstream tests (v0.18.3, queue row 1194).

Two verbs, stdlib only, read-only:

  facts [--json]
      The facts phasekit DECLARES for downstream tests: the `facts` section of
      this project's installed contracts/interface.json (a vendored provider
      copy under vendor/contracts/phasekit/ is the same file). A test that
      needs to know how the loop behaves reads this — never the loop's text.
      phasekit's own suite proves every fact against the loop's behaviour
      (tests/test_declared_surface.py), so a reshape that keeps the facts true
      cannot break a consumer, and one that changes a fact changes this file.

  scaffold-reads [--json] [ROOT]
      The `scaffold-reads` advisory: project test files that READ a
      scaffold-owned file (ownership `scaffold` in .scaffold/manifest.json —
      the vendored loop and scripts, the hooks, the scaffold docs) instead of
      the declared surface. Warn-only, never a refusal: it prints, and its
      JSON form is what the loop records as boundary-state.json
      `scaffold_reads`. Exit 0 always (2 only on a usage error).

The rule it advises on (docs/QUALITY_GATES.md "Tests read the declared
surface"): a test reads the project's own tree and phasekit's declared surface
(contracts/interface.json, `phasekit facts`), never scaffold-owned files; a
fact a test needs that the surface lacks is a request to phasekit, not a
parse.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ADVISORY_ID = "scaffold-reads"
RECORD_FIELD = "scaffold_reads"

# The declared surface itself: reading these is exactly what a test should do.
SURFACE_PATHS = ("contracts/interface.json",)
SURFACE_PREFIXES = ("vendor/contracts/",)

# Which tracked files are tests (the hermetic-tests advisory's selection).
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|\.(test|spec)\.[A-Za-z]+$|_test\.[A-Za-z]+$"
    r"|(^|/)test_[^/]*\.py$")
SKIP_RE = re.compile(r"(^|/)(node_modules|fixtures|__fixtures__|testdata)/")
# test CODE, not the data a test reads (a captured log naming a path is not a read)
CODE_RE = re.compile(r"\.(py|ts|tsx|mts|cts|js|jsx|mjs|cjs|sh|bash|bats)$")

# A read, in a test's own words — the path must be what is read, on the line
# that reads it:
#   a call:    open(…path…), readFileSync(…path…), read_text(path), or a
#              test's own helper named for reading (`_read(p)`,
#              `extractBlock(p, …)`, `loadText(p)`, `parseLoop(p)`);
#   a member:  (…path…).read_text(), LOOP.read_text(), p.open();
#   the shell: cat / grep / sed / awk / head / tail / source / `.` … path.
# A path merely named (a docstring, a comment, a list of names, argv of a
# script the test RUNS) is not a read.
READ_CALL = (r"(?<![A-Za-z0-9_])(?!loads\b|dumps\b)(?:open|read_text|read_bytes|readlines|"
             r"readFileSync|readFile|createReadStream|[A-Za-z_]*(?:[Rr]ead|[Ll]oad|[Pp]arse|"
             r"[Ee]xtract|[Ss]lurp)[A-Za-z_]*)\s*\(")
MEMBER_READ = r"[\s)]*\.\s*(?:read_text|read_bytes|readlines|open)\s*\("
SHELL_WORDS = r"(?:cat|grep|egrep|fgrep|sed|awk|head|tail|source)"
# in a shell file: the command at a command position; in any other file the
# command must sit in a string (`["grep", "-n", …]`, `execSync('cat …')`) —
# prose ("the source of truth is scripts/x") is not a command
SHELL_READ = r"(?:(?:^|[\s;&|(`$])" + SHELL_WORDS + r"|(?:^|[;&|(]|\s)\.)\s[^|;&\n]*?"
QUOTED_SHELL_READ = r"""["'`]\s*""" + SHELL_WORDS + r"""\b[^;\n]*?"""
SHELL_FILE_RE = re.compile(r"\.(sh|bash|bats)$")
ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|const\s+|let\s+|var\s+|readonly\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=(?!=)")


def _tracked(root):
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def scaffold_owned(root):
    """The scaffold-owned paths the manifest records, minus the declared surface."""
    try:
        text = (Path(root) / ".scaffold" / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(text)
    except (OSError, ValueError):
        return []
    files = manifest.get("files") if isinstance(manifest, dict) else None
    out = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or f.get("ownership") != "scaffold":
            continue
        p = f.get("path")
        if isinstance(p, str) and p not in SURFACE_PATHS and not p.startswith(SURFACE_PREFIXES):
            out.append(p)
    return sorted(set(out))


def _q(s):
    return r"""["'`]""" + re.escape(s) + r"""["'`]"""


def _refs(path):
    """How a test names `path`: the literal (any quoting, any prefix such as
    `ROOT/` or `${root}/` or `../`), or its pieces joined (`ROOT / "scripts" /
    "run-until-done.sh"`, `path.join(root, 'scripts', 'run-until-done.sh')`,
    `os.path.join(ROOT, ".claude", "hooks", "x.sh")`)."""
    parts = path.split("/")
    alts = [r"(?<![A-Za-z0-9_.-])" + re.escape(path) + r"(?![A-Za-z0-9_.-])"]
    if len(parts) > 1:
        alts.append(r"\s*(?:/|,)\s*".join(_q(p) for p in parts))
        alts.append(_q("/".join(parts[:-1])) + r"\s*(?:/|,)\s*" + _q(parts[-1]))
    return "(?:" + "|".join(alts) + ")"


def _read_res(ref, shell):
    """The three read shapes around one reference pattern."""
    return [re.compile(READ_CALL + r"[^;\n]*?" + ref),
            re.compile(ref + r"""["'`]?""" + MEMBER_READ),
            re.compile((SHELL_READ if shell else QUOTED_SHELL_READ) + ref)]


def _reads_in(text, paths, shell=False):
    """Which of `paths` this test text reads — directly, or through a
    variable assigned the path (`LOOP = ROOT / "scripts" / "x.sh"` …
    `LOOP.read_text()`)."""
    lines = text.splitlines()
    found, names = set(), {}
    for p in paths:
        ref = _refs(p)
        ref_re = re.compile(ref)
        reads = _read_res(ref, shell)
        for ln in lines:
            if not ref_re.search(ln):
                continue
            if any(r.search(ln) for r in reads):
                found.add(p)
            m = ASSIGN_RE.match(ln)
            # a name bound to the PATH (not to a list or map of names)
            if m and not ln[m.end():].lstrip().startswith(("[", "{")):
                names[m.group(1)] = p
    for name, p in names.items():
        if p in found:
            continue
        # `LOOP`, `self.LOOP`, `H.LOOP` — never `MY_LOOP` or `LOOP_DIR`
        reads = _read_res(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", shell)
        if any(r.search(ln) for ln in lines for r in reads):
            found.add(p)
    return sorted(found)


def scaffold_reads(root):
    """[{"test": <test file>, "paths": [<scaffold-owned paths it reads>]}], sorted."""
    root = Path(root)
    paths = scaffold_owned(root)
    if not paths:
        return []
    out = []
    for rel in _tracked(root):
        if rel in paths or SKIP_RE.search(rel):
            continue
        if not (TEST_FILE_RE.search(rel) and CODE_RE.search(rel)):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # cheap prefilter: only the paths whose file name the text mentions
        cand = [p for p in paths if p.rsplit("/", 1)[-1] in text]
        hit = _reads_in(text, cand, bool(SHELL_FILE_RE.search(rel))) if cand else []
        if hit:
            out.append({"test": rel, "paths": hit})
    return sorted(out, key=lambda e: e["test"])


def advisory_line(reads):
    """The one-line named advisory (empty when there is nothing to say)."""
    if not reads:
        return ""
    names = [e["test"] for e in reads]
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    return (f"ADVISORY {ADVISORY_ID}: {len(names)} test file(s) read scaffold-owned files "
            f"({shown}) — a test reads the project's own tree and phasekit's declared surface "
            "(contracts/interface.json `facts`, `phasekit facts --json`), never the vendored "
            "loop, scripts, hooks or scaffold docs; a fact the surface lacks is a request to "
            "phasekit. See docs/QUALITY_GATES.md \"Tests read the declared surface\". Advisory "
            "only: the gate is unchanged.")


def _contract(root):
    for rel in ("contracts/interface.json", "vendor/contracts/phasekit/interface.json"):
        p = Path(root) / rel
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("interface") != "phasekit":
            continue
        if isinstance(data.get("facts"), dict):
            return data
    return None


def _top(root):
    """The project's top level: the nearest directory at or above `root` that
    holds `.git` (a directory, or a worktree's file); `root` itself when none.
    Found on disk, not by asking git: a downstream tree may pin that it has
    exactly one "which repository is this" rule (foundry-orchestrator does)."""
    start = Path(root).resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    root = Path(rest[0]) if rest else Path.cwd()
    if verb == "facts":
        # the facts of THIS project's phasekit (its installed contract, found
        # from any subdirectory) — never a newer install's, which may not
        # describe the loop this project vendors
        data = _contract(_top(root))
        if data is None:
            print("phasekit facts: no contracts/interface.json with a `facts` section here "
                  "(phasekit v0.18.3 or later installs it)", file=sys.stderr)
            return 1
        facts = data["facts"]
        if as_json:
            print(json.dumps(facts, indent=2, sort_keys=True))
        else:
            for name in sorted(k for k in facts if not k.startswith("_")):
                fact = facts[name]
                print(f"{name}: {fact.get('summary', '') if isinstance(fact, dict) else fact}")
        return 0
    if verb == "scaffold-reads":
        # the project's top level, from any subdirectory (never a silent all-clear)
        reads = scaffold_reads(_top(root))
        if as_json:
            record = {"advisory": ADVISORY_ID, RECORD_FIELD: reads, "line": advisory_line(reads)}
            print(json.dumps(record, sort_keys=True))
        else:
            line = advisory_line(reads)
            if line:
                print(line)
                for e in reads:
                    print(f"  {e['test']}: {', '.join(e['paths'])}")
        return 0
    print(f"phasekit-surface: unknown verb {verb!r} (facts | scaffold-reads)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
