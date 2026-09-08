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
        # v0.12.3: the helper RETURNS the commit rc (callers decide) and never
        # exits on its own.
        self.assertIn('return "$acrc"', body)
        self.assertNotIn("exit ", body)

    def test_the_in_loop_site_requires_freshness_and_short_circuits_red(self) -> None:
        """v0.12.3 review MAJOR: a STALE approval at the in-loop gate must NOT
        drive the sweep (it would mislabel this iteration's completion work
        under an old phase's message), and a verify-red phase commit must not
        be followed by a second full-tier run on the same red tree.
        Re-aimed for v0.14.5: the site computes freshness and hands it to
        land_boundary as any_age=0; inside the sequence a stale approval is
        left to the completion sweep (step 2 declines, step 3 carries it) and
        a red rc from step 2 returns before step 3 is attempted."""
        in_loop_region = self.text.split("Final-commit gate.")[1][:2600]
        self.assertIn(
            'artifact_written_this_iteration "$ARTIFACTS_DIR/phase-approval.json"',
            in_loop_region,
        )
        self.assertIn('"$PENDING_COMMIT_RETRY" == "phase-approval"', in_loop_region)
        self.assertIn('land_boundary 1 completion "$approval_fresh"', in_loop_region)
        # Inside the sequence: a stale approval never commits on its own, and
        # any non-zero, non-2 rc stops the walk before the next step's action.
        do_fn = self.text.split("boundary_do() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('[[ "$any_age" != 1 ]]', do_fn)
        self.assertIn("BOUNDARY_APPROVAL_RIDES_COMPLETION=1", do_fn)
        land_fn = self.text.split("land_boundary() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('*) COMPLETION_COMMIT_IN_PROGRESS="$prior_ccip"', land_fn)
        self.assertIn('return "$rc" ;;', land_fn)

    def test_the_stranded_site_is_any_age_on_purpose(self) -> None:
        """Re-aimed for v0.14.5: the stranded-at-start site calls land_boundary
        with any_age=1 (the pairing argument is kept verbatim beside it), so
        step 2 commits the approval under its own message whatever its age."""
        stranded_region = self.text.split(
            "# --- Boundary recovery at loop start (v0.14.5)"
        )[1][:6000]
        self.assertIn('land_boundary "$recover_from" stranded 1', stranded_region)
        self.assertIn("pairing them", stranded_region)

    def test_the_helper_runs_before_both_completion_commit_sites(self) -> None:
        """The completion-record commit has the helper call above it — the
        phase commits under its own message first. Re-aimed for v0.14.5:
        there is exactly ONE completion commit site now (boundary_do step 3),
        and the helper is step 2 of the same walk, so the ordering holds for
        every entry point by construction instead of at each site by hand.
        The two former sites (stranded-at-start, final gate) both reach it
        through land_boundary."""
        import re

        completion_msg = (
            "chore(workflow): final session work + project completion record"
        )
        sites = [m.start() for m in re.finditer(re.escape(completion_msg), self.text)]
        self.assertEqual(
            len(sites), 1,
            "one completion commit site expected (boundary_do step 3); a second "
            "site bypasses the sequence and this test must be extended",
        )
        do_fn_start = self.text.find("boundary_do() {")
        do_fn = self.text[do_fn_start:self.text.find("\n}", do_fn_start)]
        self.assertLess(do_fn_start, sites[0])
        # Indented BARE-call form only (v0.12.3 review nit): a commented-out
        # line would satisfy a plain substring search.
        call_re = re.compile(r"\n\s+commit_pending_approval_first(\s*\|\| \S+)?\s*\n")
        helper_calls = [m.start() for m in call_re.finditer(do_fn)]
        self.assertEqual(len(helper_calls), 1, "step 2 calls the helper exactly once")
        self.assertLess(do_fn_start + helper_calls[0], sites[0],
                        "step 2 (the helper) precedes step 3 (the completion commit)")
        # and the walk itself is ascending: step N's action runs before N+1's.
        land_fn = self.text.split("land_boundary() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("for (( step=1; step<=BOUNDARY_STEP_RESTED; step++ )); do", land_fn)


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
