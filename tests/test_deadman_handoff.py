#!/usr/bin/env python3
"""Tests for the dead-man handoff baton (v0.10.1).

The gap being closed: the wrap-up baton is written BY the wrap-up, and a
session killed mid-iteration (deadline class (a)) or one that exits silently
dies before wrap-up runs — so the next session inherits a dirty tree with no
explanation, which the record shows "confused the next session. Three times."

The mechanism under test, all three legs:
  1. the loop writes a provisional baton (session-interrupted.json) at every
     iteration start — the dead-man property: a kill cannot cooperate and
     does not need to;
  2. the EXIT trap removes it iff the iteration concluded with a verdict —
     a leftover "you were killed" note must never lie;
  3. the NEXT session's startup promotes a survivor into session-handoff.json
     (the slot the orientation already reads-then-deletes), unless a real
     wrap-up baton exists, which knows strictly more and wins.

The bash is exercised for real: the functions and the promotion block are
extracted from scripts/run-until-done.sh by their own delimiters and run in a
scratch environment, so these tests break if the shipped code does.

Run from the repo root: `python3 -m unittest tests.test_deadman_handoff`
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
PROMPT = REPO_ROOT / "CONTINUE_PROMPT.txt"

SOURCE = LOOP_SCRIPT.read_text()

SIX_KEYS = {"stopped_at_phase", "in_flight", "verified", "next_step", "note", "ts"}


def _extract(pattern_start, pattern_end):
    lines = SOURCE.splitlines()
    out, taking = [], False
    for line in lines:
        if not taking and re.match(pattern_start, line):
            taking = True
        if taking:
            out.append(line)
            if len(out) > 1 and re.match(pattern_end, line):
                return "\n".join(out)
    raise AssertionError(f"could not extract {pattern_start!r} from the loop")


WRITE_FN = _extract(r"^write_provisional_handoff\(\) \{", r"^\}")
CLEAR_FN = _extract(r"^clear_provisional_handoff_on_exit\(\) \{", r"^\}")
PROMOTE_BLOCK = _extract(r"^# Dead-man promotion", r"^fi$")

VERDICTS = ("project-complete.json phase-approval.json phase-update.json "
            "phase-blocked.json scope-refusal.json light-escalation.json")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-deadman-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.artifacts = self.tmp / "artifacts"
        self.artifacts.mkdir()
        self.marker = self.tmp / "iter-marker"

    def bash(self, body, marker_set=True):
        prelude = [
            f'ARTIFACTS_DIR="{self.artifacts}"',
            'ITERATION_MODE="standard"',
            f'VERDICT_ARTIFACTS=({VERDICTS})',
        ]
        if marker_set:
            prelude.append(f'ITER_START_MARKER="{self.marker}"')
        script = "\n".join(prelude) + "\n" + body
        return subprocess.run(["bash", "-c", script],
                              capture_output=True, text=True, timeout=30)

    @property
    def interrupted(self):
        return self.artifacts / "session-interrupted.json"

    @property
    def handoff(self):
        return self.artifacts / "session-handoff.json"


class WriteLeg(Fixture):
    def test_provisional_has_the_batons_six_keys_and_the_deadman_note(self):
        result = self.bash(WRITE_FN + '\nwrite_provisional_handoff 3')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.interrupted.read_text())
        self.assertEqual(set(data), SIX_KEYS)
        self.assertIn("dead-man", data["note"])
        self.assertIn("iteration 3", data["in_flight"])
        self.assertFalse(data["verified"])

    def test_provisional_never_touches_the_real_baton_slot(self):
        self.handoff.write_text('{"real": "baton"}')
        self.bash(WRITE_FN + '\nwrite_provisional_handoff 1')
        self.assertEqual(json.loads(self.handoff.read_text()), {"real": "baton"})


class ClearLeg(Fixture):
    def _clear(self, marker_set=True):
        return self.bash(CLEAR_FN + '\nclear_provisional_handoff_on_exit',
                         marker_set=marker_set)

    def test_fresh_verdict_removes_the_provisional(self):
        self.marker.touch()
        self.interrupted.write_text("{}")
        (self.artifacts / "phase-approval.json").write_text("{}")
        self._clear()
        self.assertFalse(self.interrupted.exists())

    def test_no_verdict_leaves_it_and_says_so(self):
        self.marker.touch()
        self.interrupted.write_text("{}")
        result = self._clear()
        self.assertTrue(self.interrupted.exists())
        self.assertIn("dead-man handoff left in place", result.stderr)

    def test_stale_verdict_from_before_the_iteration_does_not_clear(self):
        self.interrupted.write_text("{}")
        verdict = self.artifacts / "phase-approval.json"
        verdict.write_text("{}")
        past = os.path.getmtime(verdict) - 3600
        os.utime(verdict, (past, past))
        self.marker.touch()  # iteration started after that verdict
        self._clear()
        self.assertTrue(self.interrupted.exists(),
                        "a previous iteration's verdict says nothing about this one")

    def test_no_iteration_marker_means_no_opinion(self):
        self.interrupted.write_text("{}")
        self._clear(marker_set=False)
        self.assertTrue(self.interrupted.exists())


class PromoteLeg(Fixture):
    def _promote(self):
        return self.bash(PROMOTE_BLOCK)

    def test_survivor_is_promoted_into_the_baton_slot(self):
        self.interrupted.write_text('{"note": "dead-man ..."}')
        result = self._promote()
        self.assertFalse(self.interrupted.exists())
        self.assertEqual(json.loads(self.handoff.read_text()),
                         {"note": "dead-man ..."})
        self.assertIn("promoted", result.stderr)

    def test_a_real_wrapup_baton_wins(self):
        self.interrupted.write_text('{"note": "dead-man ..."}')
        self.handoff.write_text('{"real": "baton"}')
        self._promote()
        self.assertFalse(self.interrupted.exists())
        self.assertEqual(json.loads(self.handoff.read_text()), {"real": "baton"})

    def test_nothing_to_promote_is_a_silent_no_op(self):
        result = self._promote()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.handoff.exists())


class SourcePins(Fixture):
    def test_provisional_is_written_right_after_the_iteration_marker(self):
        touch_at = SOURCE.index('touch "$ITER_START_MARKER"')
        write_at = SOURCE.index('write_provisional_handoff "$iteration"')
        self.assertGreater(write_at, touch_at)
        self.assertLess(write_at - touch_at, 400,
                        "the provisional must be written at iteration start, "
                        "beside the marker it is scoped to")

    def test_the_exit_trap_is_registered(self):
        # v0.13.0 folded the baton-clear into the loop's combined EXIT trap
        # (one trap per shell; the deadline watchdog must be killed there
        # too). The property this test protects is unchanged: the baton-clear
        # runs on every normal exit.
        self.assertIn("trap run_until_done_exit_trap EXIT", SOURCE)
        trap_fn = _extract(r"^run_until_done_exit_trap\(\) \{", r"^\}")
        self.assertIn("clear_provisional_handoff_on_exit", trap_fn)

    def test_the_provisional_is_a_hidden_transient(self):
        for array in ("TRANSIENT_SIGNALS", "HIDDEN_TRANSIENTS"):
            block = _extract(rf"^{array}=\(", r"^\)")
            self.assertIn('"session-interrupted.json"', block, array)

    def test_orientation_prompt_explains_the_deadman_flavor(self):
        text = PROMPT.read_text()
        self.assertIn("dead-man", text)
        self.assertIn("work-in-progress", text)


if __name__ == "__main__":
    unittest.main()
