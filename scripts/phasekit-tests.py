#!/usr/bin/env python3
"""The test-suite hygiene measure (v0.27.0): a map for budget work.

WHY THIS EXISTS
---------------
foundry-meta designs/DESIGN-test-suite-hygiene.md (decided 2026-11-07). One
fleet project's per-commit gate had grown to about 197 s against phasekit's
60 s ceiling and its full tier to about 600 s, the in-session tool limit; half
of each suite's time sat in a few dozen files, most of the gate's reds were
tests of tree state, and the heaviest checks spent their time waiting on a real
clock. Budgets are work (a supervisor files a delegated row when a tier is over
budget); this is the map that work starts from. It is a MEASURE, not a rule: it
never refuses a landing, never turns a gate red and never edits a project file.

WHAT IT REPORTS (`report`, the default)
---------------------------------------
  tiers           the per-commit (fast) and full tier's measured time, from the
                  loop's own cost ledger (artifacts/logs/cost-ledger.json: the
                  last 20 gate runs per tier), P90 against the budget: per-commit
                  PHASEKIT_VERIFY_BUDGET_SECONDS (default 60, the loop's
                  ceiling); full 25 % of the session bound, capped at the 600 s
                  in-session tool limit (the tool limit alone when no bound is
                  known: --session-bound, else the ledger's newest session).
  measure         the newest per-file results file (`measure`, below), and from
                  it: `waiting` (files whose wall time is high and whose CPU
                  share is low: waiting on a real clock; listed first) and
                  `slowest` (the slowest files, and how many make 50 % / 80 %
                  of the summed per-file time).
  never_failed    test files that never failed a gate in a logged window, ONLY
                  from --failures FILE (the failure history lives with the
                  supervisor; the format is below). Without it the section says
                  it needs the file; a window is never invented.
  criterion_named test files whose name, or whose first describe/class, is led
                  by an AC / phase / iteration / roadmap label (CRITERION_LEAD).
  evidence        tracked bytes under artifacts/ (git's index), images vs other.

`phasekit` does not judge what "fast" means in a project's domain: the waiting
thresholds are flags, and the per-file numbers are raw.

THE DECLARATION (`measure`)
---------------------------
phasekit does not know a project's test runner and never guesses one. A project
declares its per-file command and its test files in ONE place, its own gate
`scripts/phasekit-verify.sh` (project-owned; phasekit already reads its
PHASEKIT_VERIFY_CONFIGURED sentinel line from it), as two literal assignments
the gate itself never needs to use:

    PHASEKIT_TEST_FILE_CMD='npx vitest run {file}'
    PHASEKIT_TEST_FILES='tests/**/*.test.ts packages/*/test/**/*.test.ts'

  * Each is read as TEXT (never sourced or evaluated): a line
    `NAME=<value>` (optionally `export NAME=…`), the last such line winning as
    in the shell. The value is single-quoted, double-quoted without `$`, a
    backtick or a backslash, or one bare word; anything the shell would expand
    is a problem, not a guess.
  * The command holds `{file}`, replaced by the file's repository-relative
    path, shell-quoted; it runs under `bash -c` from the project root.
  * The files are space-separated globs over TRACKED files (`git ls-files`):
    `*` and `?` stay inside a path segment, `**/` spans directories (zero or
    more), `[abc]`/`[!abc]` and `{a,b}` as in the shell.

`measure` runs the command once per file, ALONE, one at a time, and records
wall time and the CPU time of the whole child tree (os.wait4's rusage of the
`bash -c` child: everything it and its descendants used and waited for; a
process that escapes into its own reaper is not counted). It is expensive: a
supervisor's nightly run, never a session. It writes a results file and changes
nothing else. A file that fails or times out is recorded, never retried.

RESULTS FILE (schema 1; artifacts/logs/tests-measure/<UTC stamp>.json, the
newest 10 kept, or --out FILE)
    {"schema": 1, "kind": "phasekit-tests-measure", "measured_at": ISO-8601 UTC,
     "head": commit or null, "dirty": bool, "source": "scripts/phasekit-verify.sh",
     "command": template, "globs": [glob], "timeout_s": n, "cpus": n or null,
     "total_wall_s": x,
     "files": [{"file", "wall_s", "cpu_s", "exit_code" (null when timed out),
                "timed_out", "tail" (the last lines of a run that did not pass)}]}

FAILURES FILE (schema 1; --failures FILE; written by a supervisor from its
session logs)
    {"schema": 1, "window": {"from": ISO-8601, "to": ISO-8601},
     "gate_runs": n (gate runs the window holds),
     "failed_files": {"<repository-relative test file>": <times it failed, >= 1>}}

Usage:
  python3 scripts/phasekit-tests.py [report] [--repo DIR] [--json] [--measure FILE]
      [--failures FILE] [--session-bound S] [--top N] [--wait-min-wall S]
      [--wait-max-cpu-share F]
  python3 scripts/phasekit-tests.py measure [--repo DIR] [--timeout S] [--out FILE]
      [--on-host]

WHERE `measure` RUNS: it runs the project's own tests, so it belongs where the
gate runs, inside the runner image (scaffold-runner), not on a supervisor
host: a suite can act on its host (foundry-orchestrator's reconcile kills
live phasekit-* containers), and host toolchains differ from the gate's. It
refuses (exit 6) unless it finds itself in a container (/.dockerenv or
/run/.containerenv), or `--on-host` says the caller chose the host on
purpose.

Exit codes:
  0  `report`: the report was printed — over budget or not (the budget verdict
     is `over_budget` in the JSON, never an exit code); `measure`: every
     declared file was run and the results file was written (a file that failed
     or timed out is a result, not an error)
  2  usage error (argparse)
  3  `measure`: no runner is declared (no gate, or no PHASEKIT_TEST_FILE_CMD /
     PHASEKIT_TEST_FILES line, or one that is not a literal or has no {file});
     nothing was run
  4  `measure`: the declared globs match no tracked file; nothing was run
  5  could not complete: not inside a git repository, a --measure or
     --failures file that cannot be read or is not the format above, a results
     file that cannot be written, interrupted (SIGINT/SIGTERM: the running
     file's process group is killed, nothing is written), phasekit-surface.py
     missing beside this script, or an unexpected internal error
  6  `measure`: not inside a container and no --on-host; nothing was run
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNDECLARED = 3
EXIT_NO_FILES = 4
EXIT_FILE = 5
EXIT_NOT_CONTAINED = 6

SCHEMA = 1
REPORT_SCHEMA = "phasekit-tests-report/1"
RESULTS_KIND = "phasekit-tests-measure"
GATE_REL = "scripts/phasekit-verify.sh"
LEDGER_REL = "artifacts/logs/cost-ledger.json"
RESULTS_DIR_REL = "artifacts/logs/tests-measure"
EVIDENCE_DIR = "artifacts/"
KEEP_RESULTS = 10
CMD_NAME = "PHASEKIT_TEST_FILE_CMD"
FILES_NAME = "PHASEKIT_TEST_FILES"
BUDGET_ENV = "PHASEKIT_VERIFY_BUDGET_SECONDS"
PER_COMMIT_BUDGET_S = 60     # the loop's VERIFY_BUDGET_SECONDS default (run-until-done.sh)
FULL_BUDGET_SHARE = 0.25     # of the session bound (design D1)
TOOL_LIMIT_S = 600           # how long one in-session tool call may run
WAIT_MIN_WALL_S = 2.0
WAIT_MAX_CPU_SHARE = 0.5
TIMEOUT_S = 900
KILL_GRACE_S = 5
TAIL_LINES = 8
TOP = 10
SHOWN = 20
IMAGE_EXT = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico",
                       "tif", "tiff", "heic"})

DECL_RE = re.compile(rf"^[ \t]*(?:export[ \t]+)?({CMD_NAME}|{FILES_NAME})=(.*)$", re.M)
VALUE_RE = re.compile(r"""^(?:'([^']*)'|"([^"$`\\]*)"|([^\s'"$`\\#;&|<>()]+))"""
                      r"""[ \t]*(?:#.*)?$""")

# Rule 3 (behaviour-named tests): a label LEADS the name. Pinned in
# tests/test_tests_measure.py. AC123, AC#12, ac_513, AC-E125.3; phase 493,
# iteration-28, iter12; roadmap-7, roadmap item 7; R11 / R180 (a roadmap entry id,
# capital R). A feature NAMED roadmap (test_roadmap.py) is behaviour, not a label.
CRITERION_LEAD = re.compile(
    r"""^[\W_]*(?:
          ac[\s_#-]*(?:(?-i:[A-Z])[\s_-]?)?\d+(?:\.\d+)*
        | (?:phase|iteration|iter)[\s_#-]*\d+
        | roadmap[\s_#-]*(?:item[\s_#-]*)?\d+
        | (?-i:R)\d+(?![A-Za-z])
    )""", re.I | re.X)
_NAME_TAIL = re.compile(r"(?:[._-](?:test|spec))?\.[A-Za-z0-9]+$", re.I)
_NAME_HEAD = re.compile(r"^test[_-]?", re.I)
_JS_DESCRIBE = re.compile(r"""(?<![\w$.])(?:test\.)?describe(?:\.\w+)*\s*\(\s*"""
                          r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""")
_PY_CLASS = re.compile(r"^class[ \t]+(\w+)", re.M)
_JS_COMMENTS = re.compile(r"/\*.*?\*/|^[ \t]*//[^\n]*", re.S | re.M)


class TestsError(Exception):
    """Could not complete (exit 5)."""


class Interrupted(Exception):
    """SIGINT / SIGTERM during `measure`."""


# --- the project -----------------------------------------------------------------

def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          timeout=timeout)


def project_root(arg: str) -> Path:
    if not os.path.isdir(arg):
        raise TestsError(f"--repo {arg} is not a directory")
    try:
        r = _git(Path(arg), "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestsError(f"git is not usable ({type(exc).__name__})") from exc
    if r.returncode != 0:
        raise TestsError(f"{arg} is not inside a git repository")
    return Path(r.stdout.decode("utf-8", "replace").strip()).resolve()


def _head(root: Path) -> str | None:
    r = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip() or None


def _surface():
    """phasekit-surface.py beside this script: the one definition of which
    tracked files are tests (`_test_files`) and of the tracked list."""
    path = Path(__file__).resolve().parent / "phasekit-surface.py"
    spec = importlib.util.spec_from_file_location("phasekit_surface_for_tests", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise TestsError(f"no phasekit-surface.py beside {Path(__file__).name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- the declaration ---------------------------------------------------------------

def glob_re(pattern: str) -> re.Pattern:
    """A tracked-path glob as a regex (see THE DECLARATION)."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and "]" in pattern[i + 2:]:
            j = pattern.index("]", i + 2)
            body = pattern[i + 1:j]
            neg = body[:1] == "!"
            body = body[1:] if neg else body
            out.append("[" + ("^" if neg else "") + body.replace("\\", "\\\\") + "]")
            i = j
        elif c == "{" and "}" in pattern[i:]:
            j = pattern.index("}", i)
            alts = pattern[i + 1:j].split(",")
            out.append("(?:" + "|".join(glob_re(a).pattern[1:-1] for a in alts) + ")")
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _literal(raw: str) -> str | None:
    m = VALUE_RE.match(raw.strip())
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def declaration(root: Path) -> dict:
    """{"command", "globs", "problems"} from the project's gate (see THE
    DECLARATION); `problems` is non-empty when nothing usable is declared."""
    out: dict = {"source": GATE_REL, "command": None, "globs": [], "problems": []}
    try:
        text = (root / GATE_REL).read_text(encoding="utf-8", errors="replace")
    except OSError:
        out["problems"].append(f"there is no {GATE_REL}, so nothing declares how to run one "
                               "test file")
        return out
    found: dict[str, str] = {}
    for m in DECL_RE.finditer(text):
        found[m.group(1)] = m.group(2)
    for name in (CMD_NAME, FILES_NAME):
        if name not in found:
            out["problems"].append(f"{GATE_REL} declares no {name}")
            continue
        value = _literal(found[name])
        if value is None:
            out["problems"].append(f"{GATE_REL}: {name} is not a literal value (phasekit reads "
                                   "it as text and never evaluates it): single-quote it")
            continue
        if name == CMD_NAME:
            if "{file}" not in value:
                out["problems"].append(f"{GATE_REL}: {CMD_NAME} has no {{file}} placeholder")
            elif re.search(r"[\"'][^\"']*\{file\}", value):
                # {file} is replaced shell-quoted; inside quotes the quoting
                # breaks and a file name's $( ) or backticks would expand.
                out["problems"].append(f"{GATE_REL}: {CMD_NAME} puts {{file}} inside quotes; "
                                       "write it bare (phasekit quotes the file name itself)")
            else:
                out["command"] = value
        else:
            out["globs"] = value.split()
            if not out["globs"]:
                out["problems"].append(f"{GATE_REL}: {FILES_NAME} is empty")
    return out


def declared_files(globs: list[str], tracked: list[str]) -> list[str]:
    pats = [glob_re(g) for g in globs]
    return sorted(p for p in tracked if any(r.match(p) for r in pats))


# --- measure ------------------------------------------------------------------------

def _killpg(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def run_one(root: Path, command: str, timeout: float) -> dict:
    """Run COMMAND alone from ROOT: wall time, the child tree's CPU time, the
    exit code, whether it timed out, and the tail of a run that did not pass."""
    with tempfile.TemporaryFile() as log:
        start = time.monotonic()
        proc = subprocess.Popen(["bash", "-c", command], cwd=str(root), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out, done = threading.Event(), threading.Event()

        def expire() -> None:
            timed_out.set()
            _killpg(proc.pid, signal.SIGTERM)
            if not done.wait(KILL_GRACE_S):
                _killpg(proc.pid, signal.SIGKILL)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            _, status, usage = os.wait4(proc.pid, 0)
        except BaseException:
            _killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.wait4(proc.pid, 0)
            proc.returncode = -9
            raise
        finally:
            done.set()
            timer.cancel()
        wall = time.monotonic() - start
        proc.returncode = os.waitstatus_to_exitcode(status)
        _killpg(proc.pid, signal.SIGKILL)   # nothing it started outlives the measurement
        code = None if timed_out.is_set() else proc.returncode
        tail: list[str] = []
        if code != 0:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            tail = log.read().decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
    return {"wall_s": round(wall, 3), "cpu_s": round(usage.ru_utime + usage.ru_stime, 3),
            "exit_code": code, "timed_out": timed_out.is_set(), "tail": tail}


def _raise_interrupted(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise TestsError(f"cannot write {path}: {os.strerror(exc.errno or 0)}") from exc


def _prune(results_dir: Path, keep: int) -> None:
    files = sorted(results_dir.glob("*.json"))
    for old in files[:-keep] if keep > 0 else []:
        with contextlib.suppress(OSError):
            old.unlink()


def _in_container() -> bool:
    return any(os.path.exists(p) for p in ("/.dockerenv", "/run/.containerenv"))


def _exclude_logs(root: Path) -> None:
    """Keep the results out of `git status` in a clone where the loop (which
    writes the same line) never ran. Best-effort."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        got = os.fsdecode(_git(root, "rev-parse", "--git-path", "info/exclude").stdout).strip()
        if not got:
            return
        path = Path(got) if Path(got).is_absolute() else root / got
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if "artifacts/logs/" not in lines:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("artifacts/logs/\n")


def cmd_measure(args) -> int:
    root = project_root(args.repo)
    if not args.on_host and not _in_container():
        print("phasekit tests measure: not inside a container, so nothing was run. It runs "
              "the project's own tests, which belong where the gate runs (the runner image): "
              "a suite can act on its host. Run it in scaffold-runner, or pass --on-host if "
              "this host is the right place on purpose.", file=sys.stderr)
        return EXIT_NOT_CONTAINED
    decl = declaration(root)
    if decl["problems"]:
        for p in decl["problems"]:
            print(f"phasekit tests measure: {p}", file=sys.stderr)
        print(f"phasekit tests measure: nothing was run — declare the per-file command and the "
              f"test files in {GATE_REL}, e.g.\n  {CMD_NAME}='python3 -m pytest -q {{file}}'\n"
              f"  {FILES_NAME}='tests/**/test_*.py'\n(docs/QUALITY_GATES.md \"The hygiene "
              "measure\"; phasekit never guesses a runner)", file=sys.stderr)
        return EXIT_UNDECLARED
    files = declared_files(decl["globs"], _surface()._tracked(root))
    if not files:
        print(f"phasekit tests measure: {FILES_NAME} ({' '.join(decl['globs'])}) matches no "
              "tracked file; nothing was run", file=sys.stderr)
        return EXIT_NO_FILES
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip())
    started = time.gmtime()
    results = {"schema": SCHEMA, "kind": RESULTS_KIND,
               "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", started),
               "head": _head(root), "dirty": dirty, "source": GATE_REL,
               "command": decl["command"], "globs": decl["globs"], "timeout_s": args.timeout,
               "cpus": os.cpu_count(), "total_wall_s": 0.0, "files": []}
    old = {s: signal.signal(s, _raise_interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for i, rel in enumerate(files, start=1):
            arg = rel if not rel.startswith("-") else f"./{rel}"   # never read as an option
            cmd = decl["command"].replace("{file}", shlex.quote(arg))
            row = {"file": rel, **run_one(root, cmd, args.timeout)}
            results["files"].append(row)
            state = ("timed out" if row["timed_out"]
                     else "ok" if row["exit_code"] == 0 else f"exit {row['exit_code']}")
            print(f"[{i}/{len(files)}] {rel}: {row['wall_s']:.2f} s wall, {row['cpu_s']:.2f} s "
                  f"cpu ({state})", file=sys.stderr)
    except Interrupted as exc:
        raise TestsError(f"interrupted ({exc}); nothing was written") from None
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    results["total_wall_s"] = round(sum(f["wall_s"] for f in results["files"]), 3)
    if args.out:
        out = Path(args.out)
        out = out if out.is_absolute() else Path.cwd() / out
        _write_json(out, results)
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", started)
        out = root / RESULTS_DIR_REL / f"{stamp}.json"
        _exclude_logs(root)
        _write_json(out, results)
        _prune(out.parent, KEEP_RESULTS)
    bad = sum(1 for f in results["files"] if f["exit_code"] != 0)
    print(f"phasekit tests measure: {len(files)} file(s), {results['total_wall_s']:.1f} s; "
          f"{bad} did not pass alone; wrote {out}")
    return EXIT_OK


# --- report: tiers -------------------------------------------------------------------

def _p90(values: list[float]) -> float:
    """Nearest-rank P90, exactly as the loop's cost_p90 takes it."""
    s = sorted(values)
    return s[((len(s) * 9) + 9) // 10 - 1]


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _ledger(root: Path) -> tuple[dict | None, str | None]:
    path = root / LEDGER_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no {LEDGER_REL} (the loop writes one sample per gate run)"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"{LEDGER_REL} cannot be read ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, f"{LEDGER_REL} is not a JSON object"
    return data, None


def _session_bound(ledger: dict | None, override: int | None) -> tuple[int | None, str | None]:
    if override is not None:
        return override, "--session-bound"
    sessions = (ledger or {}).get("sessions")
    for s in reversed(sessions if isinstance(sessions, list) else []):
        b = s.get("bound_s") if isinstance(s, dict) else None
        if _num(b) and b > 0:
            return int(b), f"{LEDGER_REL} (newest session)"
    return None, None


def tiers(root: Path, session_bound: int | None) -> dict:
    ledger, why = _ledger(root)
    raw = os.environ.get(BUDGET_ENV, "")
    given = re.fullmatch(r"[0-9]+", raw) is not None   # the loop's own test, ASCII only
    fast_budget = int(raw) if given else PER_COMMIT_BUDGET_S
    fast_basis = (f"{BUDGET_ENV}={raw}" if given
                  else "phasekit's per-commit ceiling")
    bound, bound_from = _session_bound(ledger, session_bound)
    if bound:
        share = int(bound * FULL_BUDGET_SHARE)
        full_budget = min(share, TOOL_LIMIT_S)
        full_basis = (f"25% of the {bound} s session bound ({share} s), capped at the "
                      f"{TOOL_LIMIT_S} s in-session tool limit")
    else:
        full_budget = TOOL_LIMIT_S
        full_basis = (f"the {TOOL_LIMIT_S} s in-session tool limit only: no session bound is "
                      "known (pass --session-bound S)")
    samples = (ledger or {}).get("samples")
    samples = samples if isinstance(samples, dict) else {}
    out = {"source": LEDGER_REL, "available": ledger is not None, "reason": why,
           "session_bound_s": bound, "session_bound_from": bound_from}
    for tier, measure, budget, basis in (("per_commit", "g_fast", fast_budget, fast_basis),
                                         ("full", "g_full", full_budget, full_basis)):
        got = samples.get(measure)
        vals = [v for v in (got if isinstance(got, list) else []) if _num(v)]
        row = {"measure": measure, "samples": len(vals), "budget_s": budget,
               "budget_basis": basis, "p90_s": None, "median_s": None, "last_s": None,
               "over_budget": None, "over_by_s": None}
        if vals:
            p90 = math.floor(_p90(vals))
            row.update({"p90_s": p90, "median_s": round(_median(vals), 1), "last_s": vals[-1],
                        "over_budget": p90 > budget, "over_by_s": max(0, p90 - budget)})
        out[tier] = row
    return out


# --- report: per-file results ---------------------------------------------------------

def _num(v) -> bool:
    # a bool is not a number here; nor is Infinity or NaN (json accepts both)
    return type(v) in (int, float) and math.isfinite(v) and v >= 0


def load_results(path: Path) -> dict:
    """A results file, validated; TestsError when it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TestsError(f"cannot read {path} as JSON ({type(exc).__name__})") from exc
    if not (isinstance(data, dict) and data.get("schema") == SCHEMA
            and data.get("kind") == RESULTS_KIND and isinstance(data.get("files"), list)):
        raise TestsError(f"{path} is not a `phasekit tests measure` results file (schema "
                         f"{SCHEMA})")
    for f in data["files"]:
        if not (isinstance(f, dict) and isinstance(f.get("file"), str)
                and _num(f.get("wall_s")) and _num(f.get("cpu_s"))):
            raise TestsError(f"{path}: a file entry is not {{file, wall_s, cpu_s, …}}")
    return data


def newest_results(root: Path) -> tuple[Path | None, dict | None, list[str]]:
    """The newest readable results file under RESULTS_DIR_REL (its path and
    data), and notes on newer ones that could not be read."""
    notes: list[str] = []
    d = root / RESULTS_DIR_REL
    for path in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            return path, load_results(path), notes
        except TestsError as exc:
            notes.append(f"skipped {path.name}: {exc}")
    return None, None, notes


def per_file(data: dict | None, path: Path | None, root: Path, top: int, min_wall: float,
             max_share: float, notes: list[str]) -> tuple[dict, dict, dict]:
    if data is None:
        why = (f"no results file: run `phasekit tests measure` (a supervisor's nightly run; "
               f"it writes {RESULTS_DIR_REL}/)")
        none = {"available": False, "reason": why}
        return {**none, "notes": notes}, dict(none), dict(none)
    try:
        shown = str(path.resolve().relative_to(root)) if path else None
    except ValueError:
        shown = str(path)
    rows = data["files"]
    total = sum(f["wall_s"] for f in rows)
    measure = {"available": True, "file": shown, "measured_at": data.get("measured_at"),
               "head": data.get("head"), "dirty": data.get("dirty"),
               "command": data.get("command"), "files": len(rows),
               "total_wall_s": round(total, 3),
               "did_not_pass": [f["file"] for f in rows if f.get("exit_code") != 0],
               "notes": notes}
    waiting = []
    for f in rows:
        wall, cpu = f["wall_s"], f["cpu_s"]
        if wall >= min_wall and wall > 0 and cpu / wall <= max_share:
            waiting.append({"file": f["file"], "wall_s": wall, "cpu_s": cpu,
                            "cpu_share": round(cpu / wall, 3), "idle_s": round(wall - cpu, 3),
                            "timed_out": bool(f.get("timed_out"))})
    waiting.sort(key=lambda w: (-w["idle_s"], w["file"]))
    ranked = sorted(rows, key=lambda f: (-f["wall_s"], f["file"]))
    marks: dict[str, int | None] = {"50": None, "80": None}
    if total > 0:
        acc = 0.0
        for k, f in enumerate(ranked, start=1):
            acc += f["wall_s"]
            for p in ("50", "80"):
                if marks[p] is None and acc >= total * int(p) / 100:
                    marks[p] = k
    slowest = {"available": True, "of_files": len(rows), "total_wall_s": round(total, 3),
               "files_for_50pct": marks["50"], "files_for_80pct": marks["80"],
               "files": [{"file": f["file"], "wall_s": f["wall_s"],
                          "share": round(f["wall_s"] / total, 4) if total else None}
                         for f in ranked[:top]]}
    return (measure, {"available": True, "min_wall_s": min_wall, "max_cpu_share": max_share,
                      "files": waiting}, slowest)


# --- report: the static sections ------------------------------------------------------

def test_universe(root: Path, surface, tracked: list[str]) -> tuple[list[str], str]:
    """The test files the static sections judge: the declared globs when the
    gate declares them, else phasekit's own test-file selection."""
    decl = declaration(root)
    if decl["globs"]:
        return declared_files(decl["globs"], tracked), f"{FILES_NAME} in {GATE_REL}"
    files = [rel for rel, _ in surface._test_files(root, tracked, named=True)]
    return sorted(files), "phasekit's test-file selection (no PHASEKIT_TEST_FILES declared)"


def criterion_label(rel: str, text: str) -> tuple[str, str] | None:
    """(by, label) when the file's name, or its first describe/class, is led
    by a criterion label (CRITERION_LEAD); None otherwise."""
    name = _NAME_HEAD.sub("", _NAME_TAIL.sub("", rel.rsplit("/", 1)[-1]))
    m = CRITERION_LEAD.match(name)
    if m:
        return "name", m.group(0).strip(" _-#.")
    if rel.endswith(".py"):
        cm = _PY_CLASS.search(text)
        if cm:
            cls = re.sub(r"_?Tests?$", "", re.sub(r"^Test_?", "", cm.group(1)))
            m = CRITERION_LEAD.match(cls)
            if m:
                return "class", m.group(0).strip(" _-#.")
        return None
    dm = _JS_DESCRIBE.search(_JS_COMMENTS.sub("", text))
    if dm:
        m = CRITERION_LEAD.match(dm.group(2))
        if m:
            return "describe", m.group(0).strip(" _-#.:")
    return None


def criterion_named(root: Path, files: list[str], basis: str) -> dict:
    hits = []
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        found = criterion_label(rel, text)
        if found:
            hits.append({"file": rel, "by": found[0], "label": found[1]})
    return {"pattern": CRITERION_LEAD.pattern, "test_files": len(files), "basis": basis,
            "count": len(hits), "files": hits}


def load_failures(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TestsError(f"--failures {path}: cannot read it as JSON "
                         f"({type(exc).__name__})") from exc
    window = data.get("window") if isinstance(data, dict) else None
    failed = data.get("failed_files") if isinstance(data, dict) else None
    runs = data.get("gate_runs") if isinstance(data, dict) else None
    ok = (isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(window, dict)
          and all(isinstance(window.get(k), str) and window[k].strip() for k in ("from", "to"))
          and isinstance(runs, int) and not isinstance(runs, bool) and runs >= 0
          and isinstance(failed, dict)
          and all(isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool)
                  and v >= 1 for k, v in failed.items()))
    if not ok:
        raise TestsError(f"--failures {path}: not the failures format (schema {SCHEMA}: window "
                         "{from, to}, gate_runs >= 0, failed_files {file: count >= 1})")
    return data


def never_failed(files: list[str], failures: dict | None, path: str | None) -> dict:
    if failures is None:
        return {"available": False,
                "reason": "needs --failures FILE: the gate's failure history is kept by the "
                          "supervisor, not in the repository (format: scripts/phasekit-tests.py "
                          "docstring, FAILURES FILE)"}
    failed = failures["failed_files"]
    current = set(files)
    never = [f for f in files if f not in failed]
    return {"available": True, "source": path, "window": failures["window"],
            "gate_runs": failures["gate_runs"], "test_files": len(files),
            "failed_files": sum(1 for f in files if f in failed),
            "failed_not_current": sorted(f for f in failed if f not in current),
            "count": len(never), "share": round(len(never) / len(files), 4) if files else None,
            "files": never}


def evidence(root: Path) -> dict:
    """Tracked bytes under artifacts/ (git's index), images vs other."""
    r = _git(root, "ls-files", "-s", "-z", "--", EVIDENCE_DIR)
    entries = []
    for rec in r.stdout.decode("utf-8", "replace").split("\0") if r.returncode == 0 else []:
        meta, _, rel = rec.partition("\t")
        parts = meta.split()
        if rel and len(parts) >= 2 and parts[0] != "160000":
            entries.append((parts[1], rel))
    sizes: dict[str, int] = {}
    if entries:
        objs = "\n".join(sorted({s for s, _ in entries})) + "\n"
        c = subprocess.run(["git", "-C", str(root), "cat-file",
                            "--batch-check=%(objectname) %(objectsize)"],
                           input=objs.encode(), capture_output=True, timeout=120)
        for line in c.stdout.decode().splitlines():
            sha, _, size = line.partition(" ")
            if size.isdigit():
                sizes[sha] = int(size)
    out = {"dir": EVIDENCE_DIR, "files": 0, "bytes": 0,
           "images": {"files": 0, "bytes": 0}, "other": {"files": 0, "bytes": 0}, "largest": []}
    rows = []
    for sha, rel in entries:
        size = sizes.get(sha, 0)
        kind = "images" if Path(rel).suffix.lower().lstrip(".") in IMAGE_EXT else "other"
        out[kind]["files"] += 1
        out[kind]["bytes"] += size
        rows.append((size, rel))
    out["files"] = len(rows)
    out["bytes"] = sum(s for s, _ in rows)
    rows.sort(key=lambda x: (-x[0], x[1]))
    out["largest"] = [{"file": rel, "bytes": s} for s, rel in rows[:5]]
    return out


# --- report -------------------------------------------------------------------------

def build_report(args) -> dict:
    root = project_root(args.repo)
    surface = _surface()
    tracked = surface._tracked(root)
    notes: list[str] = []
    if args.measure:
        rpath: Path | None = Path(args.measure)
        rpath = rpath if rpath.is_absolute() else Path.cwd() / rpath
        data = load_results(rpath)
    else:
        rpath, data, notes = newest_results(root)
    failures = None
    if args.failures:
        fpath = Path(args.failures)
        failures = load_failures(fpath if fpath.is_absolute() else Path.cwd() / fpath)
    measure, waiting, slowest = per_file(data, rpath, root, args.top, args.wait_min_wall,
                                         args.wait_max_cpu_share, notes)
    files, basis = test_universe(root, surface, tracked)
    return {"schema": REPORT_SCHEMA, "project": root.name, "generated_at": _now(),
            "head": _head(root), "tiers": tiers(root, args.session_bound), "measure": measure,
            "waiting": waiting, "slowest": slowest,
            "never_failed": never_failed(files, failures, args.failures),
            "criterion_named": criterion_named(root, files, basis),
            "evidence": evidence(root)}


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.1f} kB"


def render(rep: dict, top: int) -> str:
    out = [f"phasekit tests — test-suite hygiene for {rep['project']} (a measure: it never "
           "fails a gate; docs/QUALITY_GATES.md \"The hygiene measure\")", ""]
    t = rep["tiers"]
    out.append(f"Tier time (P90 of the loop's gate runs, {t['source']})")
    if not t["available"]:
        out.append(f"  {t['reason']}")
    for tier, label in (("per_commit", "per-commit"), ("full", "full")):
        row = t[tier]
        if row["p90_s"] is None:
            got = "no samples yet"
            verdict = ""
        else:
            got = f"P90 {row['p90_s']} s (median {row['median_s']} s, {row['samples']} runs)"
            verdict = (f"  OVER by {row['over_by_s']} s" if row["over_budget"]
                       else "  within budget")
        out.append(f"  {label:<10}  {got}; budget {row['budget_s']} s "
                   f"({row['budget_basis']}){verdict}")
    out.append("")
    m = rep["measure"]
    if not m["available"]:
        out.append(f"Per-file runs: {m['reason']}")
    else:
        out.append(f"Per-file runs ({m['file']}, measured {m['measured_at']} at "
                   f"{(m['head'] or '?')[:12]}{' (dirty tree)' if m['dirty'] else ''}): "
                   f"{m['files']} files alone, {m['total_wall_s']:.1f} s in all")
        w = rep["waiting"]
        out.append(f"  Waiting on a clock (wall >= {w['min_wall_s']:g} s, CPU <= "
                   f"{w['max_cpu_share']:.0%} of wall), most idle first: {len(w['files'])}")
        for f in w["files"][:SHOWN]:
            out.append(f"    {f['file']}  {f['wall_s']:.1f} s wall, {f['cpu_s']:.1f} s CPU "
                       f"({f['cpu_share']:.0%}){'  TIMED OUT' if f['timed_out'] else ''}")
        if len(w["files"]) > SHOWN:
            out.append(f"    … and {len(w['files']) - SHOWN} more (--json)")
        s = rep["slowest"]
        out.append(f"  Slowest files: {s['files_for_50pct']} of {s['of_files']} make 50% of the "
                   f"time, {s['files_for_80pct']} make 80%")
        for i, f in enumerate(s["files"][:top], start=1):
            share = f"{f['share']:.1%}" if f["share"] is not None else "-"
            out.append(f"    {i:>2}. {f['file']}  {f['wall_s']:.1f} s ({share})")
        if m["did_not_pass"]:
            out.append(f"  Did not pass when run alone: {len(m['did_not_pass'])} ("
                       + ", ".join(m["did_not_pass"][:5])
                       + (", …" if len(m["did_not_pass"]) > 5 else "") + ")")
    for note in m.get("notes") or []:
        out.append(f"  note: {note}")
    out.append("")
    n = rep["never_failed"]
    if not n["available"]:
        out.append(f"Never failed in the logged window: {n['reason']}")
    else:
        share = f"{n['share']:.1%}" if n["share"] is not None else "-"
        out.append(f"Never failed in the logged window ({n['window']['from']} to "
                   f"{n['window']['to']}, {n['gate_runs']} gate runs): {n['count']} of "
                   f"{n['test_files']} test files ({share}). A ranking for review beside time, "
                   "never by itself a reason to delete a test.")
    out.append("")
    c = rep["criterion_named"]
    out.append(f"Criterion-named suites (led by an AC / phase / iteration / roadmap label; name "
               f"tests for behaviour): {c['count']} of {c['test_files']} test files "
               f"[{c['basis']}]")
    for f in c["files"][:SHOWN]:
        out.append(f"    {f['file']}  ({f['by']}: {f['label']})")
    if c["count"] > SHOWN:
        out.append(f"    … and {c['count'] - SHOWN} more (--json)")
    out.append("")
    e = rep["evidence"]
    pct = f" ({e['images']['bytes'] / e['bytes']:.0%})" if e["bytes"] else ""
    out.append(f"Evidence weight (tracked under {e['dir']}): {e['files']} files, "
               f"{_mb(e['bytes'])}; images {e['images']['files']} files, "
               f"{_mb(e['images']['bytes'])}{pct}; other {e['other']['files']} files, "
               f"{_mb(e['other']['bytes'])}")
    for f in e["largest"]:
        out.append(f"    {f['file']}  {_mb(f['bytes'])}")
    return "\n".join(out)


def cmd_report(args) -> int:
    rep = build_report(args)
    print(json.dumps(rep, indent=2) if args.json else render(rep, args.top))
    return EXIT_OK


# --- CLI ----------------------------------------------------------------------------

def _positive(kind):
    def parse(text: str):
        try:
            v = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if v <= 0:
            raise argparse.ArgumentTypeError(f"must be above 0: {text!r}")
        return v
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phasekit tests",
        description="The test-suite hygiene measure: a map for budget work, never a gate.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="print the hygiene report (the default)")
    rep.add_argument("--repo", default=".", help="the project (default: .)")
    rep.add_argument("--json", action="store_true", help="print the report as JSON")
    rep.add_argument("--measure", metavar="FILE",
                     help=f"a results file (default: the newest under {RESULTS_DIR_REL}/)")
    rep.add_argument("--failures", metavar="FILE",
                     help="the gate's failure history (format: FAILURES FILE in the docstring)")
    rep.add_argument("--session-bound", type=_positive(int), metavar="SECONDS",
                     help="the session bound the full tier's budget is 25%% of")
    rep.add_argument("--top", type=_positive(int), default=TOP, help="slowest files shown")
    rep.add_argument("--wait-min-wall", type=_positive(float), default=WAIT_MIN_WALL_S,
                     metavar="S", help="a waiting file runs at least this long alone")
    rep.add_argument("--wait-max-cpu-share", type=_positive(float), default=WAIT_MAX_CPU_SHARE,
                     metavar="F", help="… and uses at most this share of its wall time in CPU")
    mea = sub.add_parser("measure", help="run each declared test file alone; write a results file")
    mea.add_argument("--repo", default=".", help="the project (default: .)")
    mea.add_argument("--timeout", type=_positive(float), default=TIMEOUT_S, metavar="S",
                     help=f"seconds before one file is stopped (default {TIMEOUT_S})")
    mea.add_argument("--out", metavar="FILE", help="write the results here instead")
    mea.add_argument("--on-host", action="store_true",
                     help="run outside a container on purpose (see WHERE `measure` RUNS)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "report")
    args = build_parser().parse_args(argv)
    try:
        return cmd_measure(args) if args.cmd == "measure" else cmd_report(args)
    except TestsError as exc:
        print(f"phasekit tests: {exc}", file=sys.stderr)
        return EXIT_FILE
    except Exception as exc:  # noqa: BLE001 — never let a traceback become a verdict
        print(f"phasekit tests: internal error ({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_FILE


if __name__ == "__main__":
    sys.exit(main())
