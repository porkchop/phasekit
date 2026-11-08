#!/usr/bin/env python3
"""The test-suite hygiene measure, `phasekit tests` (v0.27.0).

foundry-meta designs/DESIGN-test-suite-hygiene.md rule 10 and §8.2: a map for
budget work — per-tier time against the budgets, the files that wait on a real
clock and the slowest ones, never-failed files from a supervisor's history,
criterion-named suites, evidence weight. `scripts/phasekit-tests.py` is the
tool; it is a measure, never a gate.

Pinned here:
  * the declaration: two literal lines in the project's own gate, read as
    text; a value the shell would expand, a command without {file}, a missing
    line or gate is "not declared" (measure exit 3, nothing run); the last line
    wins; commented lines are not declarations; the globs' semantics;
  * measure: each file alone, wall and the child tree's CPU time (a fake
    runner that sleeps reads as waiting, one that burns CPU — itself or in a
    grandchild — does not; thresholds generous, nothing depends on how fast
    this host is), a failing and a timed-out file are results, the results
    file and its pruning, SIGTERM kills the running file and writes nothing;
  * report: P90 exactly as the loop takes it, the budgets and their bases
    (env, session bound, the tool-limit cap), and exit 0 over budget; injected
    per-file timings give the waiting order and the 50 % / 80 % counts; the
    newest readable results file is the one read;
  * the criterion-label regex (a table of names that are and are not led by a
    label), by name, first describe and first class;
  * never-failed needs --failures and never invents a window; its format;
  * evidence counts tracked bytes only, images and other;
  * the CLI acts on the project root, in the vendored and the pinned layout;
  * the contract (facts.tests_measure, exit codes, the file sentinels), the
    registration, the doctrine and the seeded templates.

Run from the repo root: python3 -m unittest tests.test_tests_measure
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)
from test_engine_outside import EngineFixture, _git

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-tests.py"
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
GATE = "scripts/phasekit-verify.sh"
PY = sys.executable

SLEEPER = "import time\ntime.sleep(0.6)\n"
BURNER = ("import time\nt = time.process_time()\n"
          "while time.process_time() - t < 0.6:\n    pass\n")
GRANDCHILD_BURNER = ("import subprocess, sys\n"
                     f"subprocess.run([sys.executable, '-c', {BURNER!r}], check=True)\n")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load(TOOL, "phasekit_tests_under_test")


def gate(cmd=f"'{PY} {{file}}'", files="'tests/**/test_*.py'"):
    lines = ["#!/usr/bin/env bash", "PHASEKIT_VERIFY_CONFIGURED=1"]
    if cmd is not None:
        lines.append(f"PHASEKIT_TEST_FILE_CMD={cmd}")
    if files is not None:
        lines.append(f"PHASEKIT_TEST_FILES={files}")
    return "\n".join(lines + ["exit 0", ""])


class _Repo:
    def __init__(self, testcase, files=None, gate_text=None, vendored=False):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "demo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        if gate_text is not None:
            self.write(GATE, gate_text)
        if vendored:
            self.write("scripts/run-until-done.sh", "#!/usr/bin/env bash\nexit 0\n")
        for rel, text in (files or {}).items():
            self.write(rel, text)
        self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                              text=True, check=True).stdout

    def write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data)
        return p

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", "fixture")

    def tool(self, *args, env=None):
        return subprocess.run([PY, str(TOOL), *args, "--repo", str(self.root)],
                              capture_output=True, text=True, env=env, timeout=300)

    def report(self, *args, env=None):
        r = self.tool("report", "--json", *args, env=env)
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        return json.loads(r.stdout)

    def ledger(self, data):
        self.write(T.LEDGER_REL, json.dumps(data))

    def results(self, name, files, **extra):
        data = {"schema": 1, "kind": "phasekit-tests-measure",
                "measured_at": "2026-11-08T00:00:00Z",
                "head": None, "dirty": False, "source": GATE, "command": "x {file}",
                "globs": ["tests/*"], "timeout_s": 900, "cpus": 4,
                "total_wall_s": sum(f["wall_s"] for f in files),
                "files": [{"exit_code": 0, "timed_out": False, "tail": [], **f} for f in files]}
        data.update(extra)
        return self.write(f"{T.RESULTS_DIR_REL}/{name}.json", json.dumps(data))

    def results_files(self):
        d = self.root / T.RESULTS_DIR_REL
        return sorted(d.glob("*.json")) if d.is_dir() else []


def _env_without_budget():
    return {k: v for k, v in os.environ.items() if k != "PHASEKIT_VERIFY_BUDGET_SECONDS"}


class Declaration(unittest.TestCase):
    def _decl(self, text):
        r = _Repo(self, gate_text=text)
        return T.declaration(r.root)

    def test_the_literal_forms_are_read(self):
        for value, want in (("'npx vitest run {file}'", "npx vitest run {file}"),
                            ('"node --test {file}"', "node --test {file}"),
                            ("'x {file}'   # a comment", "x {file}")):
            with self.subTest(value=value):
                d = self._decl(gate(cmd=value))
                self.assertEqual(d["problems"], [])
                self.assertEqual(d["command"], want)
        d = self._decl(gate(cmd=None, files=None) + "export PHASEKIT_TEST_FILE_CMD='a {file}'\n"
                       "PHASEKIT_TEST_FILES=tests/*.py\n")
        self.assertEqual((d["command"], d["globs"], d["problems"]),
                         ("a {file}", ["tests/*.py"], []))

    def test_what_the_shell_would_expand_is_not_declared(self):
        for value in ('"$RUNNER {file}"', "$(which pytest)", "`x` {file}", "'unbalanced",
                      "a {file}"):
            with self.subTest(value=value):
                d = self._decl(gate(cmd=value))
                self.assertIsNone(d["command"])
                self.assertTrue(any("not a literal" in p for p in d["problems"]), d["problems"])

    def test_a_command_without_the_placeholder_or_a_missing_line_is_not_declared(self):
        d = self._decl(gate(cmd="'pytest -q'"))
        self.assertIn(f"{GATE}: PHASEKIT_TEST_FILE_CMD has no {{file}} placeholder", d["problems"])
        d = self._decl(gate(files=None))
        self.assertEqual(d["problems"], [f"{GATE} declares no PHASEKIT_TEST_FILES"])
        d = self._decl("#!/usr/bin/env bash\n# PHASEKIT_TEST_FILE_CMD='x {file}'\n"
                       "# PHASEKIT_TEST_FILES='tests/*'\n")
        self.assertEqual(len(d["problems"]), 2, "a commented line is not a declaration")
        r = _Repo(self)
        self.assertIn("there is no scripts/phasekit-verify.sh",
                      T.declaration(r.root)["problems"][0])

    def test_the_last_line_wins_as_in_the_shell(self):
        d = self._decl(gate(cmd="'first {file}'") + "PHASEKIT_TEST_FILE_CMD='second {file}'\n")
        self.assertEqual(d["command"], "second {file}")

    def test_glob_semantics(self):
        tracked = ["tests/test_a.py", "tests/unit/test_b.py", "tests/unit/deep/test_c.py",
                   "src/test_d.py", "tests/helper.py", "web/a.test.ts", "web/b.spec.tsx",
                   "web/c.test.js", "tests/x1.py", "tests/xy.py"]
        cases = {
            "tests/**/test_*.py": ["tests/test_a.py", "tests/unit/deep/test_c.py",
                                   "tests/unit/test_b.py"],
            "tests/test_*.py": ["tests/test_a.py"],
            "**/test_*.py": ["src/test_d.py", "tests/test_a.py", "tests/unit/deep/test_c.py",
                             "tests/unit/test_b.py"],
            "web/*.{test,spec}.{ts,tsx}": ["web/a.test.ts", "web/b.spec.tsx"],
            "tests/x[0-9].py": ["tests/x1.py"],
            "tests/x[!0-9].py": ["tests/xy.py"],
            "tests/x?.py": ["tests/x1.py", "tests/xy.py"],
        }
        for pattern, want in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(T.declared_files(pattern.split(), tracked), want)
        self.assertEqual(T.declared_files(["web/*.test.js", "tests/helper.py"], tracked),
                         ["tests/helper.py", "web/c.test.js"])


class Measure(unittest.TestCase):
    def _repo(self, test_files, **kw):
        return _Repo(self, files=test_files, gate_text=gate(**kw))

    def test_waiting_and_working_files_are_told_apart(self):
        r = self._repo({"tests/test_sleeps.py": SLEEPER, "tests/test_burns.py": BURNER,
                        "tests/test_grandchild_burns.py": GRANDCHILD_BURNER})
        m = r.tool("measure", "--on-host")
        self.assertEqual(m.returncode, 0, m.stdout + m.stderr)
        [path] = r.results_files()
        data = json.loads(path.read_text())
        rows = {f["file"]: f for f in data["files"]}
        self.assertEqual(sorted(rows), ["tests/test_burns.py", "tests/test_grandchild_burns.py",
                                        "tests/test_sleeps.py"])
        sleeps = rows["tests/test_sleeps.py"]
        self.assertGreaterEqual(sleeps["wall_s"], 0.5)
        self.assertLess(sleeps["cpu_s"], 0.3, "a sleeping run spends almost no CPU")
        for rel in ("tests/test_burns.py", "tests/test_grandchild_burns.py"):
            self.assertGreaterEqual(rows[rel]["cpu_s"], 0.5, f"{rel}: the child tree's CPU counts")
        self.assertEqual(data["command"], f"{PY} {{file}}")
        self.assertEqual((data["schema"], data["kind"], data["dirty"]),
                         (1, "phasekit-tests-measure", False))
        self.assertEqual(data["head"], r.git("rev-parse", "HEAD").strip())
        rep = r.report("--wait-min-wall", "0.3")
        self.assertEqual([w["file"] for w in rep["waiting"]["files"]], ["tests/test_sleeps.py"])
        self.assertEqual(rep["measure"]["file"], str(path.relative_to(r.root)))

    def test_a_failing_and_a_timed_out_file_are_results(self):
        r = self._repo({"tests/test_fails.py": "print('the reason')\nraise SystemExit(3)\n",
                        "tests/test_hangs.py": "import time\ntime.sleep(60)\n"})
        m = r.tool("measure", "--on-host", "--timeout", "0.5")
        self.assertEqual(m.returncode, 0, m.stdout + m.stderr)
        rows = {f["file"]: f for f in json.loads(r.results_files()[0].read_text())["files"]}
        self.assertEqual(rows["tests/test_fails.py"]["exit_code"], 3)
        self.assertIn("the reason", rows["tests/test_fails.py"]["tail"])
        hang = rows["tests/test_hangs.py"]
        self.assertTrue(hang["timed_out"])
        self.assertIsNone(hang["exit_code"])
        self.assertLess(hang["wall_s"], 30)
        self.assertIn("2 did not pass alone", m.stdout)

    def test_nothing_declared_exits_3_and_runs_nothing(self):
        r = _Repo(self, files={"tests/test_a.py": "open('ran', 'w')\n"},
                  gate_text=gate(cmd=None))
        m = r.tool("measure", "--on-host")
        self.assertEqual(m.returncode, 3, m.stdout + m.stderr)
        self.assertIn("phasekit never guesses a runner", m.stderr)
        self.assertEqual(r.results_files(), [])
        self.assertFalse((r.root / "ran").exists())

    def test_globs_that_match_no_tracked_file_exit_4(self):
        r = self._repo({"tests/test_a.py": "pass\n"}, files="'spec/**/*.py'")
        (r.root / "spec").mkdir()
        (r.root / "spec" / "untracked.py").write_text("pass\n")
        m = r.tool("measure", "--on-host")
        self.assertEqual(m.returncode, 4, m.stdout + m.stderr)

    def test_the_results_dir_keeps_the_newest_ten_and_out_writes_elsewhere(self):
        r = self._repo({"tests/test_a.py": "pass\n"})
        for i in range(12):
            r.results(f"20200101T0000{i:02d}Z", [{"file": "x", "wall_s": 1, "cpu_s": 1}])
        self.assertEqual(r.tool("measure", "--on-host").returncode, 0)
        names = [p.name for p in r.results_files()]
        self.assertEqual(len(names), 10)
        self.assertNotIn("20200101T000000Z.json", names)
        out = r.root.parent / "elsewhere.json"
        self.assertEqual(r.tool("measure", "--on-host", "--out", str(out)).returncode, 0)
        self.assertEqual(json.loads(out.read_text())["files"][0]["file"], "tests/test_a.py")
        self.assertEqual(len(r.results_files()), 10)

    def test_a_file_name_is_shell_quoted(self):
        r = self._repo({"tests/test_it's here.py": "pass\n"}, files="'tests/*.py'")
        self.assertEqual(r.tool("measure", "--on-host").returncode, 0)
        row = json.loads(r.results_files()[0].read_text())["files"][0]
        self.assertEqual((row["file"], row["exit_code"]), ("tests/test_it's here.py", 0))

    def test_sigterm_kills_the_running_file_and_writes_nothing(self):
        pidfile = Path(tempfile.mkdtemp()) / "pid"
        self.addCleanup(shutil.rmtree, pidfile.parent, True)
        r = self._repo({"tests/test_waits.py": (
            f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n")})
        proc = subprocess.Popen([PY, str(TOOL), "measure", "--on-host", "--repo", str(r.root)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 5, err)
        self.assertIn("interrupted (SIGTERM); nothing was written", err)
        self.assertEqual(r.results_files(), [])
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
        self.assertFalse(_alive(pid), "the running file's process outlived the measure")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie nobody reaped yet (a container's pid 1 may not) is dead
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


class Tiers(unittest.TestCase):
    def test_p90_is_the_loops_nearest_rank(self):
        loop = LOOP.read_text()
        self.assertIn("((length * 9) + 9) / 10 | floor) - 1", loop)
        for n in range(1, 25):
            vals = list(range(n, 0, -1))
            with self.subTest(n=n):
                self.assertEqual(T._p90(vals), sorted(vals)[((n * 9) + 9) // 10 - 1])
        self.assertEqual(T._p90([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]), 90)

    def test_the_budgets_and_their_bases(self):
        r = _Repo(self)
        r.ledger({"schema": 1, "samples": {"g_fast": [50, 70, 75, 80, 200],
                                           "g_full": [400, 500, 650]},
                  "sessions": [{"id": "a", "bound_s": 9999}, {"id": "b", "bound_s": 2000}]})
        t = r.report(env=_env_without_budget())["tiers"]
        self.assertEqual((t["session_bound_s"], t["session_bound_from"]),
                         (2000, "artifacts/logs/cost-ledger.json (newest session)"))
        fast, full = t["per_commit"], t["full"]
        self.assertEqual((fast["p90_s"], fast["budget_s"], fast["over_budget"], fast["over_by_s"]),
                         (200, 60, True, 140))
        self.assertEqual((fast["samples"], fast["median_s"], fast["last_s"]), (5, 75, 200))
        self.assertEqual((full["p90_s"], full["budget_s"], full["over_budget"]), (650, 500, True))
        self.assertIn("25% of the 2000 s session bound", full["budget_basis"])
        t = r.report("--session-bound", "5100", env=_env_without_budget())["tiers"]
        self.assertEqual((t["full"]["budget_s"], t["session_bound_from"]), (600, "--session-bound"))
        self.assertIn("capped at the 600 s in-session tool limit", t["full"]["budget_basis"])
        env = dict(_env_without_budget(), PHASEKIT_VERIFY_BUDGET_SECONDS="250")
        t = r.report(env=env)["tiers"]["per_commit"]
        self.assertEqual((t["budget_s"], t["over_budget"]), (250, False))
        env["PHASEKIT_VERIFY_BUDGET_SECONDS"] = "fast"
        self.assertEqual(r.report(env=env)["tiers"]["per_commit"]["budget_s"], 60)

    def test_over_budget_is_still_exit_0(self):
        r = _Repo(self)
        r.ledger({"samples": {"g_fast": [999], "g_full": [9999]}})
        for args in (("report",), ("report", "--json"), ()):
            res = subprocess.run([PY, str(TOOL), *args, "--repo", str(r.root)],
                                 capture_output=True, text=True, env=_env_without_budget())
            with self.subTest(args=args):
                self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OVER by 939 s", r.tool("report").stdout)

    def test_no_ledger_and_no_bound(self):
        r = _Repo(self)
        t = r.report(env=_env_without_budget())["tiers"]
        self.assertFalse(t["available"])
        self.assertIn("no artifacts/logs/cost-ledger.json", t["reason"])
        self.assertIsNone(t["session_bound_s"])
        for tier in ("per_commit", "full"):
            self.assertIsNone(t[tier]["over_budget"])
            self.assertIsNone(t[tier]["p90_s"])
        self.assertEqual(t["full"]["budget_s"], 600)
        self.assertIn("no session bound is known", t["full"]["budget_basis"])
        r.write(T.LEDGER_REL, "{not json")
        t = r.report()["tiers"]
        self.assertFalse(t["available"])
        self.assertIn("cannot be read", t["reason"])

    def test_the_constants_are_the_loops(self):
        loop = LOOP.read_text()
        self.assertIn('VERIFY_BUDGET_SECONDS="${PHASEKIT_VERIFY_BUDGET_SECONDS:-'
                      f'{T.PER_COMMIT_BUDGET_S}}}"', loop)
        self.assertIn(f"longer than a tool call may run ({T.TOOL_LIMIT_S} s)", loop)
        self.assertEqual(T.FULL_BUDGET_SHARE, 0.25)


class PerFile(unittest.TestCase):
    ROWS = [
        {"file": "tests/a", "wall_s": 50.0, "cpu_s": 45.0},
        {"file": "tests/b", "wall_s": 30.0, "cpu_s": 1.0},
        {"file": "tests/c", "wall_s": 10.0, "cpu_s": 2.0},
        {"file": "tests/d", "wall_s": 6.0, "cpu_s": 5.9},
        {"file": "tests/e", "wall_s": 3.0, "cpu_s": 1.4},
        {"file": "tests/f", "wall_s": 1.0, "cpu_s": 0.0},
    ]

    def test_waiting_is_most_idle_first_and_slowest_counts_the_shares(self):
        r = _Repo(self)
        r.results("20261108T000000Z", self.ROWS)
        rep = r.report("--top", "3")
        self.assertEqual([w["file"] for w in rep["waiting"]["files"]],
                         ["tests/b", "tests/c", "tests/e"],
                         "1 s is under the 2 s floor; a and d work")
        self.assertEqual(rep["waiting"]["files"][0],
                         {"file": "tests/b", "wall_s": 30.0, "cpu_s": 1.0, "cpu_share": 0.033,
                          "idle_s": 29.0, "timed_out": False})
        s = rep["slowest"]
        self.assertEqual((s["of_files"], s["total_wall_s"], s["files_for_50pct"],
                          s["files_for_80pct"]), (6, 100.0, 1, 2))
        self.assertEqual([(f["file"], f["share"]) for f in s["files"]],
                         [("tests/a", 0.5), ("tests/b", 0.3), ("tests/c", 0.1)])
        rep = r.report("--wait-min-wall", "0.5", "--wait-max-cpu-share", "0.05")
        self.assertEqual([w["file"] for w in rep["waiting"]["files"]], ["tests/b", "tests/f"])

    def test_the_newest_readable_results_file_is_read(self):
        r = _Repo(self)
        r.results("20261101T000000Z", self.ROWS[:1])
        r.results("20261102T000000Z", self.ROWS[:2])
        r.write(f"{T.RESULTS_DIR_REL}/20261103T000000Z.json", "{broken")
        rep = r.report()
        self.assertEqual(rep["measure"]["files"], 2)
        self.assertTrue(rep["measure"]["file"].endswith("20261102T000000Z.json"))
        self.assertIn("skipped 20261103T000000Z.json", rep["measure"]["notes"][0])
        rep = r.report("--measure", str(r.root / T.RESULTS_DIR_REL / "20261101T000000Z.json"))
        self.assertEqual(rep["measure"]["files"], 1)

    def test_no_results_says_how_to_get_them(self):
        rep = _Repo(self).report()
        for key in ("measure", "waiting", "slowest"):
            self.assertFalse(rep[key]["available"], key)
        self.assertIn("run `phasekit tests measure`", rep["measure"]["reason"])

    def test_an_unreadable_named_results_file_exits_5(self):
        r = _Repo(self)
        bad = r.write("x.json", json.dumps({"schema": 1, "kind": "other", "files": []}))
        for path in (bad, r.root / "missing.json"):
            res = r.tool("report", "--measure", str(path))
            with self.subTest(path=path.name):
                self.assertEqual(res.returncode, 5, res.stdout + res.stderr)
        bad.write_text(json.dumps({"schema": 1, "kind": "phasekit-tests-measure",
                                   "files": [{"file": "a", "wall_s": "slow", "cpu_s": 1}]}))
        self.assertEqual(r.tool("report", "--measure", str(bad)).returncode, 5)


class CriterionNamed(unittest.TestCase):
    LED = ["AC123.1 — a windowed match", "ac513", "AC#12 x", "AC-E125.3 — the scripts",
           "ac_42_layout", "AC 7", "phase 493 — the windowed page", "Phase12", "phase-3",
           "iteration-28-invariants", "Iteration 63", "iter12", "R11 — timingChip",
           "R180 (AC515.2): slow lane", "roadmap-7", "roadmap item 12", "(AC52.1) portable"]
    NOT_LED = ["the verdict is green", "MultiMapEngine", "verify-budget-ac452", "roadmap",
               "roadmap completion", "acme", "accessibility", "ac_e2e flows", "phases",
               "iterator", "R2D2 droid", "r11 lowercase", "parseArgs — AC12 later",
               "replay-link-check", "Rate limits"]

    def test_the_pinned_regex(self):
        for title in self.LED:
            with self.subTest(led=title):
                self.assertIsNotNone(T.CRITERION_LEAD.match(title))
        for title in self.NOT_LED:
            with self.subTest(not_led=title):
                self.assertIsNone(T.CRITERION_LEAD.match(title))

    def test_by_name_describe_and_class(self):
        cases = {
            "tests/iteration-28-invariants.test.ts": ("name", "iteration-28"),
            "tests/test_ac12_layout.py": ("name", "ac12"),
            "tests/ac12_test.go": ("name", "ac12"),
            "tests/board.test.ts": None,
            "tests/test_roadmap.py": None,
        }
        for rel, want in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(T.criterion_label(rel, ""), want)
        js = ("import x\n// describe('AC1 in a comment is not a describe')\n"
              "/* describe('AC2')\n*/\ndescribe('AC320.2 test 4', () => {})")
        self.assertEqual(T.criterion_label("tests/vec.test.ts", js), ("describe", "AC320.2"))
        js = "describe(\"MapModel\", () => { describe('AC9 nested', () => {}) })"
        self.assertIsNone(T.criterion_label("tests/map.test.ts", js), "only the FIRST describe")
        js = "test.describe(`phase 493 — x`, () => {})"
        self.assertEqual(T.criterion_label("e2e/x.spec.ts", js), ("describe", "phase 493"))
        py = "import unittest\n\nclass TestPhase4Checkout(unittest.TestCase):\n    pass\n"
        self.assertEqual(T.criterion_label("tests/test_cart.py", py), ("class", "Phase4"))
        py = "class Cart:\n    pass\nclass TestAC1(unittest.TestCase): pass\n"
        self.assertIsNone(T.criterion_label("tests/test_cart.py", py), "only the FIRST class")

    def test_the_universe_is_the_declared_globs_else_phasekits_selection(self):
        files = {"tests/test_ac1_x.py": "pass\n", "tests/test_cart.py": "pass\n",
                 "spec/phase2.test.ts": "", "tests/helpers.py": "", "README.md": ""}
        r = _Repo(self, files=files)
        c = r.report()["criterion_named"]
        self.assertEqual(c["test_files"], 3)
        self.assertIn("no PHASEKIT_TEST_FILES declared", c["basis"])
        self.assertEqual(sorted(f["file"] for f in c["files"]),
                         ["spec/phase2.test.ts", "tests/test_ac1_x.py"])
        r.write(GATE, gate(files="'tests/test_*.py'"))
        c = r.report()["criterion_named"]
        self.assertEqual((c["test_files"], c["count"], c["basis"]),
                         (2, 1, "PHASEKIT_TEST_FILES in scripts/phasekit-verify.sh"))
        self.assertEqual(c["pattern"], T.CRITERION_LEAD.pattern)


class NeverFailed(unittest.TestCase):
    FILES = {"tests/test_a.py": "", "tests/test_b.py": "", "tests/test_c.py": ""}

    def test_without_a_history_the_section_says_what_it_needs(self):
        n = _Repo(self, files=self.FILES).report()["never_failed"]
        self.assertEqual(n["available"], False)
        self.assertIn("needs --failures FILE", n["reason"])
        self.assertNotIn("window", n)

    def test_with_a_history(self):
        r = _Repo(self, files=self.FILES)
        hist = r.write("hist.json", json.dumps({
            "schema": 1, "window": {"from": "2026-10-07", "to": "2026-11-07"}, "gate_runs": 920,
            "failed_files": {"tests/test_b.py": 13, "tests/gone.py": 2}}))
        n = r.report("--failures", str(hist))["never_failed"]
        self.assertEqual((n["count"], n["test_files"], n["failed_files"], n["share"]),
                         (2, 3, 1, 0.6667))
        self.assertEqual(n["files"], ["tests/test_a.py", "tests/test_c.py"])
        self.assertEqual(n["failed_not_current"], ["tests/gone.py"])
        self.assertEqual((n["window"], n["gate_runs"]),
                         ({"from": "2026-10-07", "to": "2026-11-07"}, 920))
        self.assertIn("2026-10-07 to 2026-11-07", r.tool("report", "--failures", str(hist)).stdout)

    def test_a_history_not_in_the_format_exits_5(self):
        r = _Repo(self, files=self.FILES)
        good = {"schema": 1, "window": {"from": "a", "to": "b"}, "gate_runs": 1,
                "failed_files": {}}
        bad = [{**good, "schema": 2}, {**good, "window": {"from": "a"}},
               {**good, "window": {"from": "", "to": "b"}}, {**good, "gate_runs": -1},
               {**good, "gate_runs": True}, {**good, "failed_files": {"x": 0}},
               {**good, "failed_files": ["x"]}, [good]]
        for i, data in enumerate(bad):
            p = r.write(f"h{i}.json", json.dumps(data))
            res = r.tool("report", "--failures", str(p))
            with self.subTest(data=data):
                self.assertEqual(res.returncode, 5, res.stdout + res.stderr)
                self.assertIn("not the failures format", res.stderr)
        p = r.write("good.json", json.dumps(good))
        self.assertEqual(r.tool("report", "--failures", str(p)).returncode, 0)


class Evidence(unittest.TestCase):
    def test_tracked_bytes_only_images_and_other(self):
        r = _Repo(self, files={"artifacts/shots/a.png": b"\x89PNG" + b"x" * 996,
                               "artifacts/shots/b.JPG": b"y" * 500,
                               "artifacts/run/record.json": "z" * 300,
                               "src/big.png": b"n" * 5000})
        r.write("artifacts/untracked.png", b"u" * 9000)
        e = r.report()["evidence"]
        self.assertEqual((e["files"], e["bytes"]), (3, 1800))
        self.assertEqual(e["images"], {"files": 2, "bytes": 1500})
        self.assertEqual(e["other"], {"files": 1, "bytes": 300})
        self.assertEqual(e["largest"][0], {"file": "artifacts/shots/a.png", "bytes": 1000})

    def test_no_artifacts(self):
        e = _Repo(self).report()["evidence"]
        self.assertEqual((e["files"], e["bytes"], e["largest"]), (0, 0, []))


class Cli(unittest.TestCase):
    def _cli(self, cwd, *args):
        env = dict(_env_without_budget(), PHASEKIT_NO_UPDATE_CHECK="1")
        return subprocess.run(["bash", str(CLI), "tests", *args], cwd=cwd, capture_output=True,
                              text=True, env=env, timeout=300)

    def test_it_acts_on_the_project_root_from_a_subdirectory(self):
        r = _Repo(self, vendored=True, gate_text=gate(),
                  files={"tests/test_a.py": "pass\n", "sub/keep": ""})
        res = self._cli(r.root / "sub", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["project"], "demo")
        res = self._cli(r.root / "sub", "measure", "--on-host")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(r.results_files()), 1)
        self.assertFalse((r.root / "sub" / "artifacts").exists())
        res = self._cli(r.root / "sub")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("phasekit tests — test-suite hygiene for demo", res.stdout)

    def test_outside_a_phasekit_project_is_usage(self):
        r = _Repo(self)
        res = self._cli(r.root)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("run it inside a phasekit project", res.stderr)

    def test_usage_and_not_a_repository(self):
        r = _Repo(self)
        self.assertEqual(r.tool("report", "--top", "0").returncode, 2)
        self.assertEqual(r.tool("frobnicate").returncode, 2)
        res = subprocess.run([PY, str(TOOL), "--repo", tempfile.gettempdir()], capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 5, res.stderr)
        self.assertIn("not inside a git repository", res.stderr)

    def test_the_seeded_gates_declare_or_say_how(self):
        def decl(name):
            r = _Repo(self, gate_text=(REPO_ROOT / "templates" / name).read_text())
            return T.declaration(r.root)
        d = decl("phasekit-verify.template.python-uv.sh")
        self.assertEqual((d["command"], d["globs"], d["problems"]),
                         ("uv run pytest -q {file}", ["tests/**/test_*.py"], []))
        for name in ("phasekit-verify.template.sh", "phasekit-verify.template.static-web.sh",
                     "phasekit-verify.template.game-canvas.sh",
                     "phasekit-verify.template.docs-only.sh"):
            with self.subTest(template=name):
                self.assertEqual(len(decl(name)["problems"]), 2, "commented out: never a guess")

    def test_a_python_uv_bootstrap_is_declared_out_of_the_box(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "proj"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        env = dict(os.environ, PHASEKIT_UPGRADE_VERIFY="off")
        r = subprocess.run([PY, str(ENRICH), str(root), "--profile", "python-uv"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((root / "scripts" / "phasekit-tests.py").is_file())
        self.assertEqual(T.declaration(root)["command"], "uv run pytest -q {file}")


class PinnedEngine(unittest.TestCase):
    """A pinned project: the measure is served from its engine — a read-only
    copy outside the project — against the project root."""

    def setUp(self):
        self.r = _Repo(self, gate_text=gate(), files={".phasekit-version": "v0.27.0\n",
                                                      "tests/test_a.py": SLEEPER})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine = Path(tmp.name) / "engine"
        (self.engine / "scripts").mkdir(parents=True)
        (self.engine / "scripts" / "run-until-done.sh").write_text("#!/usr/bin/env bash\n")
        self.addCleanup(self._writable)

    def _writable(self):
        for p in [self.engine, *self.engine.rglob("*")]:
            if p.exists():
                os.chmod(p, 0o755 if p.is_dir() else 0o644)

    def _readonly(self):
        for p in sorted(self.engine.rglob("*"), reverse=True) + [self.engine]:
            os.chmod(p, (stat.S_IRUSR | stat.S_IXUSR) if p.is_dir() else stat.S_IRUSR)

    def _cli(self, cwd, *args):
        env = dict(_env_without_budget(), PHASEKIT_ENGINE_DIR=str(self.engine),
                   PHASEKIT_NO_UPDATE_CHECK="1")
        return subprocess.run(["bash", str(CLI), "tests", *args], cwd=cwd, capture_output=True,
                              text=True, env=env, timeout=300)

    def test_the_engines_tool_runs_against_the_project(self):
        (self.engine / "scripts" / "phasekit-tests.py").write_text(
            "import sys\nprint('engine-tool', *sys.argv[1:])\n")
        self._readonly()
        (self.r.root / "sub").mkdir()
        res = self._cli(self.r.root / "sub", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(),
                         ["engine-tool", "--json", "--repo", str(self.r.root.resolve())])

    def test_a_real_measure_from_a_read_only_engine(self):
        for name in ("phasekit-tests.py", "phasekit-surface.py"):
            shutil.copy2(REPO_ROOT / "scripts" / name, self.engine / "scripts" / name)
        self._readonly()
        res = self._cli(self.r.root, "measure", "--on-host")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.r.results_files()), 1)
        self.assertFalse(any(p.name.endswith(".json") for p in self.engine.rglob("*")))
        res = self._cli(self.r.root, "--json", "--wait-min-wall", "0.3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([w["file"] for w in json.loads(res.stdout)["waiting"]["files"]],
                         ["tests/test_a.py"])

    def test_an_engine_without_the_measure_falls_back_with_a_note(self):
        self._readonly()
        res = self._cli(self.r.root, "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("this project's engine has no test-suite measure", res.stderr)
        self.assertEqual(json.loads(res.stdout)["project"], "demo")


class PinnedStore(EngineFixture):
    """The real pinned layout: `phasekit init` pins a release of this tree, the
    engine lives read-only in the store, and `phasekit tests` resolves it."""

    def test_served_by_the_pinned_engine_against_the_project(self):
        proj = self.new_project()
        (proj / "sub").mkdir()
        r = self.cli(proj / "sub", "tests", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["project"], "proj")
        self.assertNotIn("has no test-suite measure", r.stderr, "the pin's engine serves it")
        r = self.cli(proj, "tests", "measure", "--on-host")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        g = proj / GATE
        g.write_text(g.read_text() + f"PHASEKIT_TEST_FILE_CMD='{PY} {{file}}'\n"
                                     "PHASEKIT_TEST_FILES='tests/*.py'\n")
        (proj / "tests").mkdir()
        (proj / "tests" / "test_a.py").write_text("pass\n")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "declare the measure")
        r = self.cli(proj / "sub", "tests", "measure", "--on-host")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(list((proj / T.RESULTS_DIR_REL).glob("*.json"))), 1)


class TheSurfaceIsDeclared(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((REPO_ROOT / "contracts" / "interface.json").read_text())
        self.fact = self.m["facts"]["tests_measure"]

    def test_the_report_keys_are_the_declared_ones(self):
        r = _Repo(self)
        rep = r.report()
        self.assertEqual(list(rep), self.fact["json_keys"])
        self.assertEqual(rep["schema"], self.fact["schema"])
        self.assertEqual(set(rep["tiers"]["per_commit"]),
                         {"measure", "samples", "budget_s", "budget_basis", "p90_s", "median_s",
                          "last_s", "over_budget", "over_by_s"})
        for section in self.fact["sections"]:
            self.assertIn(section, rep)

    def test_the_exit_codes_are_the_tools_constants(self):
        text = TOOL.read_text()
        consts = {int(c) for c in re.findall(r"(?m)^EXIT_[A-Z_]+ = (\d+)", text)}
        self.assertEqual(consts, {0, 2, 3, 4, 5, 6})
        declared = self.m["exit_codes"]["scripts/phasekit-tests.py"]["codes"]
        self.assertEqual({int(c) for c in declared}, consts)
        self.assertEqual(set(self.fact["exit_codes"]), {str(c) for c in consts})

    def test_the_declaration_lines_are_file_sentinels(self):
        env = {e["name"]: e for e in self.m["env"]}
        for name in (T.CMD_NAME, T.FILES_NAME):
            self.assertEqual(env[name]["kind"], "file-sentinel")
            self.assertEqual(env[name]["read_by"], ["scripts/phasekit-tests.py"])
        self.assertIn("scripts/phasekit-tests.py",
                      env["PHASEKIT_VERIFY_BUDGET_SECONDS"]["read_by"])
        logs = next(a for a in self.m["artifacts"] if a["name"] == "logs")
        self.assertIn("scripts/phasekit-tests.py", logs["writers"])
        self.assertIn("tests-measure/", logs["when"])

    def test_registered_shipped_and_documented(self):
        e = _load(ENRICH, "enrich_tests_measure_test")
        self.assertIn("scripts/phasekit-tests.py", e.ALWAYS_INSTALLED_FILE_PATHS)
        caps = (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text()
        self.assertIn("  phasekit-tests:\n    path: scripts/phasekit-tests.py\n", caps)
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        hygiene = gates[gates.index("### Test-suite hygiene (v0.26.0)"):gates.index("## DRY")]
        self.assertIn("#### The hygiene measure (v0.27.0)", hygiene)
        for phrase in ("never a session's", "phasekit never guesses a runner",
                       "PHASEKIT_TEST_FILE_CMD=", "facts.tests_measure"):
            self.assertIn(phrase, hygiene)
        self.assertIn("phasekit tests", (REPO_ROOT / "README.md").read_text())
        self.assertIn("phasekit tests measure", CLI.read_text())


if __name__ == "__main__":
    unittest.main()
