#!/usr/bin/env python3
"""Tests for the no-verdict retry backstop (session-lifetime batch, item 2).

The incident replayed as a fixture: a session that writes no artifact and
leaves a dirty tree must not end silently.

This is the BACKSTOP half. It fires after the process has already died, so
whatever the session had backgrounded is gone — the Stop hook catches the same
condition while that work is still alive. The kickoff wants both, and wants
this one proven to fire WHEN THE HOOK IS ABSENT, which is exactly the situation
the loop harness reproduces (a stub run-phase.sh, no claude, no hooks).

Run from the repo root: `python3 -m unittest tests.test_no_verdict_retry`
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

try:
    from test_run_until_done_v060 import LoopHarness, VERIFY_OK
except ImportError:  # pragma: no cover
    from tests.test_run_until_done_v060 import LoopHarness, VERIFY_OK

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"


class NoVerdictRetryTest(LoopHarness):
    def setUp(self):
        super().setUp()
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)

    def _prompts(self):
        """Every prompt the stub was handed, in order."""
        out = []
        n = 1
        while True:
            path = os.path.join(self.stub_dir, f"prompt-{n}.txt")
            if not os.path.exists(path):
                return out
            with open(path) as f:
                out.append(f.read())
            n += 1

    # --- the incident ------------------------------------------------------

    def test_dirty_tree_and_no_artifact_gets_one_more_chance(self):
        """Runs 288/289/290: real work in the tree, nothing claiming it."""
        scenario = 'echo "in-flight work" >> src.txt\n'
        r = self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertEqual(self._calls(), 2, "the session was not re-invoked")
        self.assertIn("without writing a verdict artifact", self._prompts()[1])

    def test_the_retry_can_rescue_the_iteration(self):
        """Second invocation writes the verdict; the work gets committed."""
        scenario = (
            'case "$CALL_N" in\n'
            '  1) echo w1 >> src.txt ;;\n'
            "  2) jq -n '{suggested_commit_message: \"phase-1: rescued\"}'"
            " > artifacts/phase-update.json ;;\n"
            "esac\n"
        )
        r = self._run_loop(scenario, env={"MAX_ITERATIONS": "2"})
        self.assertIn("phase-1: rescued", self._messages(), r.stdout + r.stderr)

    def test_a_still_silent_session_falls_through_to_the_existing_exit(self):
        """The backstop is one extra chance, not a new way to hang."""
        scenario = 'echo "in-flight work" >> src.txt\n'
        r = self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("No expected artifact found", r.stdout)

    def test_it_is_bounded_to_one_retry_per_iteration(self):
        scenario = 'echo "more" >> src.txt\n'
        self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertEqual(self._calls(), 2, "exactly one extra invocation")

    def test_the_retry_prompt_says_background_work_is_already_dead(self):
        """Without this the session re-waits on a corpse — the whole point."""
        self._run_loop('echo w >> src.txt\n', env={"MAX_ITERATIONS": "1"})
        prompt = self._prompts()[1]
        self.assertIn("already dead", prompt)
        self.assertIn("Do not wait for it", prompt)

    def test_the_retry_prompt_offers_phase_update_first(self):
        self._run_loop('echo w >> src.txt\n', env={"MAX_ITERATIONS": "1"})
        prompt = self._prompts()[1]
        self.assertIn("phase-update.json", prompt)
        self.assertLess(prompt.index("phase-update.json"),
                        prompt.index("phase-blocked.json"))
        self.assertIn("Almost always right", prompt)

    # --- it must not fire otherwise ----------------------------------------

    def test_a_clean_tree_with_no_artifact_is_not_re_prodded(self):
        """A legitimately empty iteration has nothing to claim; nudging it
        would just burn a turn."""
        r = self._run_loop("true\n", env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertEqual(self._calls(), 1)
        self.assertEqual(r.returncode, 1)

    def test_a_healthy_iteration_never_triggers_it(self):
        scenario = (
            "echo w >> src.txt\n"
            "jq -n '{suggested_commit_message: \"phase-1: work\"}'"
            " > artifacts/phase-approval.json\n"
        )
        r = self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertEqual(self._calls(), 1)
        self.assertIn("phase-1: work", self._messages())

    def test_a_stale_approval_does_not_count_as_this_iterations_verdict(self):
        """Same trap the Stop hook guards: the durable record of a previously
        approved phase must not make the backstop inert."""
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "OLD PHASE"}\n')
        scenario = 'echo "in-flight work" >> src.txt\n'
        r = self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertNotIn("OLD PHASE", self._messages())

    def test_a_pending_commit_retry_suppresses_it(self):
        """A dirty tree the loop ALREADY knows what to do with.

        A stranded phase-approval.json is committed under its own phase's
        message at this same boundary, so prodding for a verdict here spends a
        turn asking for one that already exists — and, because the stub
        scenarios key off the call counter, silently derails the rest of the
        iteration. Caught by the v0.6.0 verify-retry and v0.6.3
        stranded-approval regression tests when this gate was too broad."""
        scenario = (
            'case "$CALL_N" in\n'
            "  1) : ;;\n"
            "  2) jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        self._prepare_scenario(scenario)
        self._write("src.txt", "base\nstranded work\n")
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "phase-7: stranded"}\n')
        r = self._run_loop(None)
        self.assertNotIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertIn("phase-7: stranded", self._messages())

    def test_every_verdict_kind_suppresses_the_retry(self):
        for artifact in ("phase-update.json", "phase-blocked.json",
                         "project-complete.json"):
            with self.subTest(artifact=artifact):
                self.setUp()
                scenario = (
                    "echo w >> src.txt\n"
                    f"jq -n '{{suggested_commit_message: \"m\"}}' > artifacts/{artifact}\n"
                )
                r = self._run_loop(scenario, env={"MAX_ITERATIONS": "1"})
                self.assertNotIn("asking once for a verdict", r.stdout + r.stderr)


class StructuralTest(unittest.TestCase):
    def setUp(self):
        self.text = LOOP_SCRIPT.read_text(encoding="utf-8")

    def _fn(self, name):
        return self.text.split(name + "() {", 1)[1].split("\n}", 1)[0]

    def test_parses(self):
        r = subprocess.run(["bash", "-n", str(LOOP_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_backstop_uses_the_shared_vocabulary(self):
        """Not a second hand-written list of what an ending is."""
        body = self._fn("has_verdict_artifact")
        self.assertIn('"${VERDICT_ARTIFACTS[@]}"', body)
        self.assertIn("artifact_written_this_iteration", body)

    def test_the_backstop_is_gated_on_a_dirty_tree(self):
        self.assertIn('&& [[ -n "$(git status --porcelain 2>/dev/null)" ]]', self.text)

    def test_the_retry_flag_resets_every_iteration(self):
        loop_top = self.text.split('echo "=== Iteration $iteration ==="', 1)[1][:300]
        self.assertIn("verdict_retry_used=0", loop_top)


if __name__ == "__main__":
    unittest.main()
