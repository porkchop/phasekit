#!/usr/bin/env python3
"""Temporaries never outlive their run (v0.18.5; Aaron 2026-10-02, rider 2).

Measured 2026-10-01: a full suite run under a fresh TMPDIR left ~1,226 empty
tmp.XXXXXXXXXX files there (plus phasekit-upgrade-gate-* entries). Release
sessions run the suite many times as one host user and left 122k files in
host /tmp, part of an OOM diagnosis. Two sources, two halves of the fix:

  * the loop's ~20 bare `$(mktemp)` sites (harmless in production, where the
    container's /tmp dies with it): the loop now keeps its temporaries in ONE
    private directory, which its one EXIT trap removes;
  * the suite's own `mktemp`/`tempfile` use: every test runs under its own
    TMPDIR inside one suite directory the harness removes (tests/_suite_tmp.py).

Acceptance (also a release gate step, docs/RELEASING.md): a full suite run
under a fresh TMPDIR leaves that TMPDIR empty. The tests here pin it on a
loop-driving slice of the suite; each is red on v0.18.4.

Run from the repo root: python3 -m unittest tests.test_suite_tmpdir
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"

_spec = importlib.util.spec_from_file_location("pk_boundary_harness_tmp", TESTS / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def _left(d):
    return sorted(str(p.relative_to(d)) for p in Path(d).rglob("*"))


class TheLoopCleansItsTemporaries(unittest.TestCase):
    def _fresh(self):
        d = tempfile.mkdtemp(prefix="loop-tmpdir-")
        return d

    def test_a_loop_run_leaves_its_tmpdir_as_it_found_it(self):
        repo = H.Repo(squash=True)
        self.addCleanup(repo.cleanup)
        repo.scenario(H.APPROVE_SCENARIO)
        tmp = self._fresh()
        r = repo.run(env={"MAX_ITERATIONS": "1", "FINAL_KIND": "both", "TMPDIR": tmp})
        self.assertIn("boundary-state: rested (step 7)", r.stdout + r.stderr, r.stdout + r.stderr)
        self.assertEqual(_left(tmp), [], "the loop's temporaries outlived it")

    def test_a_red_gate_and_its_repair_path_leave_nothing_either(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.scenario("touch BAD\n" + H.APPROVE_SCENARIO)
        tmp = self._fresh()
        repo.run(env={"MAX_ITERATIONS": "2", "VERIFY_MAX_ATTEMPTS": "1", "TMPDIR": tmp})
        self.assertEqual(_left(tmp), [])

    def test_phasekit_verify_leaves_nothing(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        tmp = self._fresh()
        r = repo.run_verb("verify", env={"TMPDIR": tmp})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_left(tmp), [])

    def test_one_exit_trap_and_it_removes_the_private_directory(self):
        src = LOOP.read_text()
        self.assertEqual(len(re.findall(r"^\s*trap .*\bEXIT\b", src, re.M)), 1, "one EXIT trap per shell")
        body = src.split("run_until_done_exit_trap() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("PK_LOOP_TMP", body)
        # created after the trap is registered, before any top-level code runs
        trap_at = src.index("trap run_until_done_exit_trap EXIT")
        made_at = src.index('PK_LOOP_TMP="$(command mktemp -d')
        first_top_level_mktemp = src.index('LIGHT_PROMPT_FILE="$(mktemp)"')
        self.assertLess(trap_at, made_at)
        self.assertLess(made_at, first_top_level_mktemp)


class TheSuiteCleansItsTemporaries(unittest.TestCase):
    def test_every_test_module_runs_under_the_suite_tmpdir(self):
        missing = [p.name for p in sorted(TESTS.glob("test_*.py"))
                   if not re.search(r"^import _suite_tmp\b", p.read_text(), re.M)]
        self.assertEqual(missing, [], "each module imports tests/_suite_tmp.py (one line)")

    def test_a_test_sees_its_own_tmpdir_and_it_is_gone_after(self):
        own = os.environ["TMPDIR"]
        self.assertEqual(tempfile.gettempdir(), own)
        self.assertTrue(Path(own).name.startswith("test-"), own)
        self.assertTrue(Path(own).parent.name.startswith("phasekit-suite-"), own)

    def test_a_suite_slice_that_drives_the_loop_leaves_its_tmpdir_empty(self):
        # the acceptance, on a slice: the full suite takes ~20 minutes
        tmp = tempfile.mkdtemp(prefix="suite-acceptance-")
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_scaffold_reads.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
            env={**os.environ, "TMPDIR": tmp})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(_left(tmp), [], "the suite's temporaries outlived it")


if __name__ == "__main__":
    unittest.main()
