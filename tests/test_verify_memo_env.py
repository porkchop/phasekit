#!/usr/bin/env python3
"""v0.28.0: the verify memo's environment check, and the memo facts the
session prints at exit.

THE INCIDENT (foundry-meta REVIEW-2026-11-08 §3.1). `model_env_differences`
ignored a hard-coded list of harness names probed on claude 2.1.282; the
fleet moved to 2.1.319, which sets CLAUDE_EFFORT on every Bash tool call,
and from then on every model `phasekit verify` printed "this shell's
environment is not the loop's (CLAUDE_EFFORT)" and was not memoised: in one
week the loop ran the project gate 358 times and reused a memo once. Nothing
measured the miss.

Two properties, exercised on the shipped bash (the functions are extracted
from the loop by their own delimiters, so these break iff the script does):
  (a) a harness-owned name (any CLAUDE_*) is never a difference; a real
      difference (a project variable, a gate switch) still is;
  (b) `memo_stats_line` reports hits, misses and the model's runs as one JSON
      object, and the names that made a model run non-reusable.

Run from the repo root: `python3 -m unittest tests.test_verify_memo_env`
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
SOURCE = LOOP_SCRIPT.read_text()


def _extract_function(name):
    lines = SOURCE.splitlines()
    out, taking = [], False
    for line in lines:
        if not taking and re.match(rf"^{re.escape(name)}\(\) \{{", line):
            taking = True
        if taking:
            out.append(line)
            if line == "}":
                return "\n".join(out)
    raise AssertionError(f"could not extract {name}() from the loop")


FUNCTIONS = "\n".join(_extract_function(n) for n in
                      ("write_loop_env_snapshot", "model_env_differences", "memo_stats_line"))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-memo-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.artifacts = self.tmp / "artifacts"
        (self.artifacts / "logs").mkdir(parents=True)

    def bash(self, script, env=None):
        full = f"set -u\nARTIFACTS_DIR={self.artifacts}\n{FUNCTIONS}\n{script}\n"
        run_env = dict(os.environ)
        for k in list(run_env):
            if k.startswith("CLAUDE") or k in ("PHASEKIT_ENV_KEY",):
                run_env.pop(k)
        run_env.update(env or {})
        return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                              timeout=60, env=run_env)


class HarnessNamesAreNeverADifference(Fixture):
    def test_the_harness_variable_that_defeated_the_memo_is_ignored(self):
        # The loop snapshots its environment; a model's shell then carries
        # CLAUDE_EFFORT (claude 2.1.319) — and must still compare equal.
        r = self.bash('write_loop_env_snapshot; CLAUDE_EFFORT=high model_env_differences')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_every_claude_prefixed_name_is_the_harnesss(self):
        r = self.bash('write_loop_env_snapshot; CLAUDE_SOMETHING_NEW=1 CLAUDE_CODE_ENTRYPOINT=cli '
                      'CLAUDECODE=1 CLAUDE_PID=4 model_env_differences')
        self.assertEqual(r.stdout.strip(), "")

    def test_a_real_difference_is_still_named_never_its_value(self):
        r = self.bash('write_loop_env_snapshot; '
                      'SKIP_E2E=1 CLAUDE_EFFORT=high model_env_differences')
        self.assertEqual(r.stdout.strip(), "SKIP_E2E")
        self.assertNotIn("high", r.stdout)

    def test_a_changed_value_is_a_difference_too(self):
        r = self.bash('export PROJECT_FLAG=a; write_loop_env_snapshot; '
                      'PROJECT_FLAG=b model_env_differences')
        self.assertEqual(r.stdout.strip(), "PROJECT_FLAG")

    def test_no_snapshot_says_so(self):
        r = self.bash('model_env_differences')
        self.assertEqual(r.stdout.strip(), "(no loop snapshot)")


class MemoStatsLine(Fixture):
    def test_nothing_to_report_prints_nothing(self):
        r = self.bash('memo_stats_line; echo "rc=$?"')
        self.assertEqual(r.stdout.strip(), "rc=0")

    def test_hits_misses_and_the_models_runs_as_one_object(self):
        (self.artifacts / "logs" / ".memo-model-runs").write_text(
            "reusable\nshown CLAUDE_EFFORT\nreusable\nshown CLAUDE_EFFORT SKIP_E2E\n")
        r = self.bash('MEMO_LOOP_HITS=3 MEMO_LOOP_MISSES=1 memo_stats_line')
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got, {"schema": 1, "loop_hits": 3, "loop_misses": 1,
                               "model_runs": 4, "model_reusable": 2,
                               "model_shown_vars": ["CLAUDE_EFFORT", "SKIP_E2E"]})

    def test_counters_alone_are_enough(self):
        got = json.loads(self.bash('MEMO_LOOP_MISSES=2 memo_stats_line').stdout)
        self.assertEqual((got["loop_hits"], got["loop_misses"], got["model_runs"],
                          got["model_shown_vars"]), (0, 2, 0, []))

    def test_garbage_counters_read_as_zero(self):
        got = json.loads(self.bash('MEMO_LOOP_HITS=abc MEMO_LOOP_MISSES=1 memo_stats_line').stdout)
        self.assertEqual(got["loop_hits"], 0)


class Wiring(unittest.TestCase):
    """The counters and the exit line are wired where the gate runs and ends."""

    def test_the_loop_counts_a_hit_where_it_reuses_the_memo(self):
        i = SOURCE.index("reusing the green verdict recorded for this exact tree")
        self.assertIn("MEMO_LOOP_HITS=$((", SOURCE[i:i + 600])

    def test_the_loop_counts_a_miss_only_for_its_own_gate_runs(self):
        self.assertIn('if [[ "${VERIFY_INVOKER:-loop}" != model ]]; '
                      'then MEMO_LOOP_MISSES=$((', SOURCE)

    def test_the_model_run_is_tallied_and_the_tally_starts_empty_each_session(self):
        self.assertIn('>> "$ARTIFACTS_DIR/logs/.memo-model-runs"', SOURCE)
        self.assertIn('rm -f "$ARTIFACTS_DIR/logs/.memo-model-runs"', SOURCE)

    def test_the_exit_trap_prints_the_line_and_keeps_it_on_the_cost_sample(self):
        trap = SOURCE[SOURCE.index("run_until_done_exit_trap() {"):]
        trap = trap[:trap.index("\n}\n")]
        self.assertIn('echo "phasekit-memo: $_memo"', trap)
        self.assertIn("memo: $memo", trap)


class ModelSideProcessNeverPrintsTheLine(unittest.TestCase):
    """Review finding 1 (v0.28.0): the loop's EXIT trap also runs in a
    model's `phasekit verify` process. That process tallies its run and
    must print no `phasekit-memo:` line into the model's tool output, nor
    touch the tally it does not own; only the loop's main path does."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="pk-memo-proc-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "artifacts" / "logs").mkdir(parents=True)
        (self.repo / "README.md").write_text("x\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@x")
        for cmd in (["git", "init", "-q", "-b", "master"], ["git", "add", "README.md"],
                    ["git", "commit", "-q", "-m", "seed"]):
            subprocess.run(cmd, cwd=self.repo, check=True, env=env, capture_output=True)
        self.env = dict(os.environ, PHASEKIT_PROJECT_DIR=str(self.repo))
        for k in list(self.env):
            if k.startswith("CLAUDE") or k in ("PHASEKIT_ENV_KEY", "PHASEKIT_ARTIFACTS_DIR"):
                self.env.pop(k)

    def verify(self):
        return subprocess.run(["bash", str(LOOP_SCRIPT), "verify"], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_the_models_verify_tallies_its_run_and_prints_no_memo_line(self):
        tally = self.repo / "artifacts" / "logs" / ".memo-model-runs"
        tally.write_text("reusable\n")   # a previous run this session
        r = self.verify()
        out = r.stdout + r.stderr
        self.assertNotIn("phasekit-memo:", out, out[-800:])
        self.assertEqual(tally.read_text().splitlines()[0], "reusable",
                         "the tally is kept, not wiped")
        self.assertEqual(len(tally.read_text().splitlines()), 2, "one line for this run")
        self.assertEqual(tally.read_text().splitlines()[1], "shown",
                         "no snapshot = shown, with no sentinel string posing as a name")


if __name__ == "__main__":
    unittest.main()
