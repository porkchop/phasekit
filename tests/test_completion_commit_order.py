#!/usr/bin/env python3
"""v0.12.2: an unlanded phase approval commits under its OWN message before
any completion sweep.

The failure this pins away happened twice before it was mechanized (xmeo
iteration 28 phase-74; iteration 9 phase-25): approval and completion
written in the same iteration, the completion branch runs first, and a whole
phase's substantive work ships inside the generic `chore(workflow)` commit
with the approval's `suggested_commit_message` unused. Structural pins in
`tests/test_commit_gate_v048.py`'s style — the ordering is bash embedded in
the loop, so the load-bearing facts are pinned so deleting them fails.

Run from the repo root: `python3 -m unittest tests.test_completion_commit_order`
"""

import os
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "run-until-done.sh")


class CompletionCommitOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(SCRIPT) as f:
            self.text = f.read()

    def test_parses(self) -> None:
        r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_helper_exists_and_is_gated_on_never_landed(self) -> None:
        self.assertIn("commit_pending_approval_first() {", self.text)
        body = self.text.split("commit_pending_approval_first() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"', body)
        self.assertIn("commit_from_artifact", body)
        # The helper must never gate on its own: a verify failure falls through
        # to the completion path, whose re-loop machinery owns the fix cycle.
        self.assertIn("return 0", body)
        self.assertNotIn("exit ", body)

    def test_the_helper_runs_before_both_completion_commit_sites(self) -> None:
        """Every completion-record commit_from_artifact call has the helper
        call above it, closer than the previous completion site — i.e. the
        ordering holds at BOTH sites (the stranded-at-start path and the
        final-commit gate), not just one."""
        completion_msg = (
            "chore(workflow): final session work + project completion record"
        )
        sites = []
        start = 0
        while True:
            i = self.text.find(completion_msg, start)
            if i == -1:
                break
            sites.append(i)
            start = i + 1
        self.assertEqual(
            len(sites), 2,
            "two completion commit sites expected (stranded-at-start, final gate); "
            "a third needs its own commit_pending_approval_first call and this "
            "test extended",
        )
        prev_end = self.text.find("commit_pending_approval_first() {")
        helper_def_end = self.text.find("\n}", prev_end)
        prev = helper_def_end
        for site in sites:
            call = self.text.rfind("commit_pending_approval_first\n", prev, site)
            if call == -1:
                call = self.text.rfind("commit_pending_approval_first ", prev, site)
            self.assertNotEqual(
                call, -1,
                f"completion commit at offset {site} has no "
                f"commit_pending_approval_first call between it and the previous "
                f"completion site — the sweep can swallow a phase again",
            )
            prev = site


class DoctrineDocPins(unittest.TestCase):
    def test_the_completion_walks_the_change_requests_asks(self) -> None:
        with open(os.path.join(REPO_ROOT, "docs", "QUALITY_GATES.md")) as f:
            gates = f.read()
        flat = " ".join(gates.split()).lower()
        for phrase in (
            "walk the change-request's asks",
            "shipped, carried by a `deferrals` entry, or the completion record says why not",
        ):
            self.assertIn(phrase, flat, phrase)

    def test_continuity_keeps_a_fresh_round_for_completion_class_phases(self) -> None:
        with open(os.path.join(REPO_ROOT, "docs", "QUALITY_GATES.md")) as f:
            gates = f.read()
        flat = " ".join(gates.split()).lower()
        for phrase in (
            "completion-class phase",
            "one fresh-context review round before its verdict",
            "a data point, not a fleet default",
        ):
            self.assertIn(phrase, flat, phrase)


if __name__ == "__main__":
    unittest.main()
