#!/usr/bin/env python3
"""Writes after the completion commit (v0.18.1, queue row 831, fork (a)+(b)).

The incident: foundry-orchestrator iteration 142 (run 829, 2026-09-16,
light mode). The build turn ran `git commit` itself — the completion record
included — and the light review's turn after it edited
tests/test_record_run.py. The loop landed and squashed the committed record;
the tree could not rest ("changes after the completion commit"); the
supervisor spent two landing dispatches on it and a human settled it.

Decided (Aaron, 2026-09-30): (a) once the completion record is committed the
loop ENDS the model turn (v0.18.0's take-control: SIGTERM to the pid
run-phase.sh names) — the model gets no further tool call in it; the light
review, which precedes the final commit, does not run over one already
made; (b) the loop snapshots the tree at the completion commit and, at the
rest, restores every path written after it, names the paths (the record's
`post_completion`, stderr) and keeps the bytes under
artifacts/logs/post-completion/<stamp>/. It corrects; it never refuses.

Every scenario class here is RED on v0.18.0 (the tree does not rest), except
the non-final negative pin, which is green on both.

The loop is the shipped script, run whole in a scratch repo (the harness of
tests/test_boundary_state.py) with a run-phase.sh shaped like the real one:
it writes artifacts/logs/claude.pid and execs the stub "claude".

Run from the repo root: python3 -m unittest tests.test_post_completion
"""

import importlib.util
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_pc", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

RUN_PHASE_WITH_PID = """#!/usr/bin/env bash
set -euo pipefail
""" + H.STUB_ROOT_LINE + """
cd "$ROOT_DIR"
mkdir -p artifacts/logs
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
PIDFILE="artifacts/logs/claude.pid"
trap 'rm -f "$PIDFILE"' EXIT
( echo "$BASHPID ${PHASEKIT_ITER:-manual}" > "$PIDFILE"; CALL_N="$n" exec bash "$STUB_DIR/claude" ) 2>&1 | cat
"""

COMPLETE_AND_SELF_COMMIT = r"""
echo "the phase's work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete: v0 shipped"}' \
  > artifacts/project-complete.json
git add -A
git commit -qm "phase 1 + completion (the model ran git commit itself)"
"""

# The iteration-142 shape: the turn goes on after its own completion commit.
# Its next tool call comes a model round-trip later (a second here); the
# guard sees the commit on a clean tree first and ends the turn. A TERM is
# honoured after the running tool (sleep) returns — and the handler makes
# more writes: the in-flight tool's writes racing the take-control.
KEEPS_WRITING = r"""
trap 'echo "after the completion commit (racing the signal)" >> src.txt; echo "a write racing the signal" >> docs/PHASES.md; exit 143' TERM
sleep 1
for i in 1 2 3 4 5 6 7 8 9 10; do
  echo "after the completion commit $i" >> src.txt
  sleep 0.3
done
touch "$STUB_DIR/turn-ran-to-its-end"
"""


class _Base(unittest.TestCase):
    squash = False

    def _repo(self, scenario):
        repo = H.Repo(squash=self.squash)
        self.addCleanup(repo.cleanup)
        repo.put_engine("scripts/run-phase.sh", RUN_PHASE_WITH_PID, executable=True,
                        commit="run-phase with a pidfile")
        (repo.stub / "claude").write_text(scenario)
        return repo

    def _state(self, repo, r):
        out = r.stdout + r.stderr
        return out, repo.record() or {}


class TheTurnIsEndedAtItsOwnCompletionCommit(_Base):
    """(a)+(b), standard mode: the model commits the completion, then keeps
    writing. RED on v0.18.0: the turn runs to its end and the rest is dirty."""

    def test_the_turn_is_ended_the_residue_restored_and_named_and_the_tree_rests(self):
        repo = self._repo(COMPLETE_AND_SELF_COMMIT + KEEPS_WRITING)
        r = repo.run(env={"MAX_ITERATIONS": "2", "PHASEKIT_ITER_RETRY": "1"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("Run finished successfully.", out)
        self.assertIn("completion guard: the completion record was committed during the turn", out)
        self.assertFalse((repo.stub / "turn-ran-to-its-end").exists(), "the turn was not ended\n" + out)
        self.assertEqual(repo.calls(), 1, "no CLI retry, no verdict retry, no second pass\n" + out)
        self.assertNotIn("retrying in continue mode", out)
        self.assertNotIn("did not rest", out)
        # (b): the tree rests clean; the committed bytes stand
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertEqual(rec.get("step"), H.RESTED, rec)
        self.assertTrue(rec.get("final"), rec)
        head_src = repo.git("show", "HEAD:src.txt")
        self.assertNotIn("after the completion commit", head_src, "a post-completion write landed")
        self.assertEqual((repo.repo / "src.txt").read_text().rstrip("\n"), head_src)
        # named: the record and stderr; kept: the residue directory
        pc = rec.get("post_completion") or {}
        self.assertIn("src.txt", pc.get("restored", []), rec)
        self.assertIn("written AFTER the completion commit", r.stderr)
        residue = repo.repo / pc["residue"]
        self.assertIn("after the completion commit", (residue / "tracked.patch").read_text())
        # the landing is unaffected: the model's completion commit is on the target
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"))


class TheTurnIsEndedAtItsOwnCompletionCommitSquash(TheTurnIsEndedAtItsOwnCompletionCommit):
    squash = True

    def test_the_turn_is_ended_the_residue_restored_and_named_and_the_tree_rests(self):
        super().test_the_turn_is_ended_the_residue_restored_and_named_and_the_tree_rests()
        self.assertEqual(self._head_branch(), "main")

    def _head_branch(self):
        return self._repo_ref.head_branch()

    def _repo(self, scenario):
        self._repo_ref = super()._repo(scenario)
        return self._repo_ref


class AWriteRacingTheTakeControl(_Base):
    """The TERM handler's own write lands AFTER the signal: still after the
    completion commit, still restored and named."""

    def test_the_racing_write_is_restored_and_named(self):
        repo = self._repo(COMPLETE_AND_SELF_COMMIT + KEEPS_WRITING)
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.porcelain_all(), [], out)
        restored = (rec.get("post_completion") or {}).get("restored", [])
        self.assertIn("docs/PHASES.md", restored, out)
        self.assertEqual((repo.repo / "docs" / "PHASES.md").read_text(), "# Phases\n")


class UntrackedWritesAfterTheCompletion(_Base):
    """Decided: an untracked file written after the completion commit is
    residue too — an untracked `??` blocks the rest exactly as a tracked
    edit does. It is removed from the tree and KEPT, byte for byte, under
    the residue directory's untracked/."""

    def test_an_untracked_write_is_removed_kept_and_named(self):
        repo = self._repo(COMPLETE_AND_SELF_COMMIT + KEEPS_WRITING.replace(
            "exit 143' TERM",
            "mkdir -p notes; echo \"scratch the model wrote after committing\" > notes/scratch.txt; exit 143' TERM"))
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertFalse((repo.repo / "notes" / "scratch.txt").exists())
        pc = rec.get("post_completion") or {}
        self.assertIn("notes/scratch.txt", pc.get("restored", []), rec)
        kept = repo.repo / pc["residue"] / "untracked" / "notes" / "scratch.txt"
        self.assertEqual(kept.read_text(), "scratch the model wrote after committing\n")


class DirtOlderThanTheCompletionCommitIsNeverJudged(_Base):
    """Dirt that predates the completion commit and that the model's partial
    commit did not carry is NOT a post-completion write — never restored on a
    guess (v0.18.1). v0.18.2 (row 1233 shape 1): it is not left dirty either
    — the whole-tree check at step 3 lands it through the loop's own
    verify-gated completion commit."""

    def test_older_dirt_is_left_and_named(self):
        # Review round 1 (MAJOR 3b): the write is in the SAME shell command
        # as the commit — the same second — and is still before it.
        repo = self._repo(r"""
echo "unclaimed older work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "only the completion record (a partial commit)"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("did not rest", out)
        self.assertIn("unclaimed older work", (repo.repo / "src.txt").read_text())
        self.assertIn("unclaimed older work", repo.git("show", "HEAD:src.txt"), "v0.18.2: it lands")
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertNotIn("post_completion", rec)


class TheLightReviewDoesNotRunOverACommittedCompletion(_Base):
    """The exact iteration-142 sequence, light mode, branch-per-iteration
    (the fleet's mode): the build turn commits the completion itself and
    yields; v0.18.0 then ran the default-model review, whose edit to a
    tracked test file could not rest. RED on v0.18.0."""

    squash = True

    def test_no_review_turn_and_the_tree_rests(self):
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  echo "    # the docstring the review expanded" >> tests_record_run.py
  exit 0
fi
""" + COMPLETE_AND_SELF_COMMIT)
        repo.write("tests_record_run.py", "def test(): pass\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "a tracked test file")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.calls(), 1, "the review ran over a committed completion\n" + out)
        self.assertIn("the final review, which precedes the final commit, does not run", out)
        self.assertIn("completion guard:", out)
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertEqual(rec.get("step"), H.RESTED, rec)


class ANonFinalBoundaryIsUnaffected(_Base):
    """Negative pin: a model that commits a PHASE approval itself and keeps
    working is not ended, nothing is restored, and its later work lands
    with the loop's own approval commit, exactly as before."""

    def test_a_self_committed_phase_approval_does_not_end_the_turn(self):
        repo = self._repo(r"""
if [ "$CALL_N" -ge 2 ]; then
  jq -n '{blocked: true, reason: "stop the test run"}' > artifacts/phase-blocked.json
  exit 0
fi
echo "phase work" >> src.txt
jq -n '{phase: "phase-1", approved: true, summary: "built it", final_phase: false,
        suggested_commit_message: "Phase 1 (APPROVED): built it"}' > artifacts/phase-approval.json
git add -A
git commit -qm "phase 1 (the model ran git commit itself)"
for i in 1 2 3; do echo "more work $i" >> src.txt; sleep 0.3; done
touch artifacts/phase-approval.json
touch "$STUB_DIR/turn-ran-to-its-end"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "turn-ran-to-its-end").exists(), out)
        self.assertNotIn("completion guard", out)
        self.assertNotIn("written AFTER the completion commit", out)
        self.assertIn("more work 3", repo.git("show", "HEAD:src.txt"), out)
        self.assertNotIn("post_completion", rec)


class AKillBetweenTheCompletionCommitAndTheRest(unittest.TestCase):
    """The kill matrix's post-completion cell: the LOOP commits the
    completion, a writer the model left running writes afterwards, and the
    session is SIGKILLed at every later seam of the walk
    (PHASEKIT_BOUNDARY_KILL_PROBE). The next session's recovery restores
    the write, names it, and rests clean. RED on v0.18.0 (the recovery walk
    rests dirty)."""

    WRITER = r"""
echo "the phase's work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
( until git cat-file -e HEAD:artifacts/project-complete.json 2>/dev/null; do sleep 0.05; done
  sleep 1.5
  echo "a late write by a process the turn left running" >> src.txt
  touch "$STUB_DIR/late-written" ) >/dev/null 2>&1 </dev/null &
"""

    def _case(self, squash, probe):
        repo = H.Repo(squash=squash)
        try:
            repo.scenario(self.WRITER)
            r1 = repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_BOUNDARY_KILL_PROBE": probe})
            deadline = time.time() + 10
            while not (repo.stub / "late-written").exists() and time.time() < deadline:
                time.sleep(0.05)
            repo.scenario(H.NOTHING_SCENARIO)
            r2 = repo.run(env={"MAX_ITERATIONS": "1"})
            out = r1.stdout + r1.stderr + "\n--- resume ---\n" + r2.stdout + r2.stderr
            return dict(rc=r2.returncode, out=out, rec=repo.record() or {},
                        porcelain=repo.porcelain_all(), head_src=repo.git("show", "HEAD:src.txt"),
                        late=(repo.stub / "late-written").exists(), branch=repo.head_branch())
        finally:
            repo.cleanup()

    def test_every_later_kill_point_rests_clean_with_the_write_named(self):
        for squash in (False, True):
            for probe in ("3:post", "4:pre", "4:post", "5:post", "6:pre", "6:post"):
                with self.subTest(mode="squash" if squash else "plain", probe=probe):
                    res = self._case(squash, probe)
                    out = res["out"]
                    self.assertTrue(res["late"], out)
                    self.assertEqual(res["rc"], 0, out)
                    self.assertEqual(res["porcelain"], [], out)
                    self.assertEqual(res["rec"].get("step"), H.RESTED, out)
                    self.assertNotIn("late write", res["head_src"], out)
                    self.assertIn("src.txt", (res["rec"].get("post_completion") or {}).get("restored", []), out)
                    if squash:
                        self.assertEqual(res["branch"], "main", out)


class ReviewRound1(_Base):
    """Regressions for the v0.18.1 review, round 1 (each red on the round-1
    build)."""

    def test_a_deletion_older_than_a_record_only_commit_is_never_restored(self):
        # MAJOR 3a: "gone" once read as after.
        repo = self._repo(r"""
rm old.txt
sleep 1.2
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "only the record"
""")
        repo.write("old.txt", "old\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "old")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertFalse((repo.repo / "old.txt").exists(), "an older deletion was resurrected\n" + out)
        self.assertNotIn("post_completion", rec)
        # v0.18.2: the deletion lands through the loop's gated completion commit
        self.assertFalse(repo.tracked("old.txt", "HEAD"), out)
        self.assertEqual(repo.porcelain_all(), [], out)

    def test_a_completion_never_seen_clean_is_never_judged(self):
        # Round 4: the loop judges only what it observed. A write in the SAME
        # shell command as the commit leaves the tree dirty at every poll —
        # no snapshot, nothing restored. v0.18.2: nor left dirty — the rest
        # lands through the loop's verify-gated completion commit.
        repo = self._repo(COMPLETE_AND_SELF_COMMIT.rstrip("\n") + " && rm old.txt\n"
                          + "for i in 1 2 3; do echo \"more $i\" >> src.txt; sleep 0.3; done\n")
        repo.write("old.txt", "old\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "old")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertFalse((repo.repo / "old.txt").exists(), out)
        self.assertIn("more 3", (repo.repo / "src.txt").read_text(), out)
        self.assertIn("more 3", repo.git("show", "HEAD:src.txt"), out)
        self.assertFalse(repo.tracked("old.txt", "HEAD"), out)
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertNotIn("post_completion", rec)

    def test_light_a_record_only_commit_over_older_work_still_gets_its_review_and_lands_the_work(self):
        # MAJOR 1: the skipped review once left the work unlanded and the tree dirty.
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  jq '.summary = "complete (reviewed)"' artifacts/project-complete.json > "$STUB_DIR/rec" \
    && cat "$STUB_DIR/rec" > artifacts/project-complete.json
  exit 0
fi
echo "the task's work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "record the completion (only the record)"
""")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.calls(), 2, "the review runs over uncommitted older work\n" + out)
        self.assertIn("the task's work", repo.git("show", "HEAD:src.txt"), out)
        self.assertIn("reviewed", repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertNotIn("post_completion", rec)

    def test_a_checkpoint_that_sweeps_a_carried_record_does_not_end_the_repair_turn(self):
        # MAJOR 2: the guard fired on a record that claims nothing.
        repo = self._repo(r"""
if [ "$CALL_N" = 1 ]; then
  echo "work" >> src.txt; touch BAD
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  exit 0
fi
git add -A; git commit -qm "wip: checkpoint before the fix"
sleep 1.2
git rm -q BAD; echo fixed >> src.txt
jq -n '{done: true, summary: "complete, fixed", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
touch "$STUB_DIR/repair-finished"
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "repair-finished").exists(), out)
        self.assertNotIn("completion guard: the completion record was committed", out)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("Run finished successfully.", out)
        self.assertIn("fixed", repo.git("show", "HEAD:src.txt"))
        self.assertEqual(repo.porcelain_all(), [], out)

    def test_a_repair_turn_the_loop_dispatched_is_never_residue(self):
        # The recovered snapshot's floor: this pass's own turn is not "after".
        # Squash mode: the catch-up squash's gate is what goes red over the
        # model's self-committed completion and sends the loop to a repair pass.
        self.squash = True
        repo = self._repo(r"""
if [ "$CALL_N" = 1 ]; then
  echo "work" >> src.txt
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  touch BAD
  git add -A; git commit -qm "completion, self-committed over a red tree"
  exit 0
fi
sleep 1.2
git rm -q BAD; echo "the repair" >> src.txt
touch "$STUB_DIR/repair-finished"
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "repair-finished").exists(), out)
        self.assertIn("the repair", (repo.repo / "src.txt").read_text(), "a repair turn's work was discarded\n" + out)
        self.assertNotIn("written AFTER the completion commit", out)

    def test_residue_that_cannot_be_kept_is_not_restored(self):
        # MINOR 4: a restore never discards bytes the residue did not keep.
        repo = self._repo(COMPLETE_AND_SELF_COMMIT + KEEPS_WRITING)
        (repo.repo / "artifacts" / "logs").mkdir(parents=True, exist_ok=True)
        (repo.repo / "artifacts" / "logs" / "post-completion").write_text("a file where the directory goes\n")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("could not be kept", out)
        self.assertIn("after the completion commit", (repo.repo / "src.txt").read_text())
        self.assertNotIn("post_completion", rec)

    def test_a_turn_that_committed_and_yielded_on_its_own_is_not_called_ended(self):
        # MINOR 7. (The boundary harness's run-phase writes no pidfile, so the
        # guard cannot race the turn's own exit: deterministic.)
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.scenario(COMPLETE_AND_SELF_COMMIT)
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("and ended on its own", out)
        self.assertNotIn("the turn was ended there", out)
        self.assertEqual(repo.porcelain_all(), [], out)


class ReviewRound2(_Base):
    """Regressions for the v0.18.1 review, round 2 (each red on the round-2
    build)."""

    def test_a_repair_pass_that_restores_the_record_byte_for_byte_is_never_residue(self):
        # BLOCKER 1: the recovered snapshot judged the repair turn's writes
        # "after" and restored them; the gate went red to phase-blocked.
        self.squash = True
        repo = self._repo(r"""
if [ "$CALL_N" = 1 ]; then
  echo "work" >> src.txt
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  touch BAD
  git add -A; git commit -qm "completion, self-committed over a red tree"
  exit 0
fi
sleep 1.2
git rm -q BAD; echo "the repair" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
touch "$STUB_DIR/repair-finished"
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "repair-finished").exists(), out)
        # (v0.18.1 owned: the repair is never restored or discarded, and the
        # loop does not stall. v0.18.2 (row 1233 shape 2): the catch-up squash
        # lands the REPAIRED tree — the repair lands first through the loop's
        # gated commit, and the squash judges exactly the tree it lands.)
        self.assertFalse(repo.artifact("phase-blocked.json").exists(), out)
        self.assertIn("the repair", (repo.repo / "src.txt").read_text(), out)
        self.assertFalse((repo.repo / "BAD").exists(), out)
        self.assertFalse(repo.tracked("BAD", "main"), "a red tree reached the target\n" + out)
        self.assertIn("the repair", repo.git("show", "main:src.txt"), out)
        self.assertNotIn("written AFTER the completion commit", out)

    def test_the_light_reviews_fix_is_its_own_work_even_when_it_leaves_the_record_as_is(self):
        # MAJOR 2.
        self.squash = True
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  sleep 1.2
  echo "review fix" >> docs/PHASES.md
  exit 0
fi
echo "the task's work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
sleep 1.1
git add artifacts/project-complete.json
git commit -qm "record the completion (only the record)"
""")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertEqual(repo.calls(), 2, out)
        self.assertIn("review fix", (repo.repo / "docs" / "PHASES.md").read_text(), out)
        self.assertNotIn("written AFTER the completion commit", out)

    def test_an_older_deletion_beside_a_later_sibling_write_is_never_resurrected(self):
        # MAJOR 3: a directory's time once judged an older deletion "after",
        # and the review was then skipped, so the deletion never landed.
        self.squash = True
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  jq '.summary = "complete (reviewed)"' artifacts/project-complete.json > "$STUB_DIR/rec" && cat "$STUB_DIR/rec" > artifacts/project-complete.json
  touch "$STUB_DIR/review-ran"
  exit 0
fi
rm docs/old.md
sleep 1.2
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "only the record"
sed -i 's/Phases/Phases!/' docs/PHASES.md
sleep 3
""")
        repo.write("docs/old.md", "old\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "old")
        if repo.squash:
            repo.git("branch", "-f", "main", "HEAD")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "review-ran").exists(), out)
        self.assertFalse((repo.repo / "docs" / "old.md").exists(), out)
        self.assertFalse(repo.tracked("docs/old.md", "HEAD"), out)

    def test_light_plain_mode_keeps_the_review_and_its_gate_over_a_self_commit(self):
        # MAJOR 4: nothing after a model's own commit gates it in plain mode.
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  rm -f BAD
  jq '.summary = "complete (reviewed)"' artifacts/project-complete.json > "$STUB_DIR/rec" && cat "$STUB_DIR/rec" > artifacts/project-complete.json
  touch "$STUB_DIR/review-ran"
  exit 0
fi
echo work >> src.txt; touch BAD
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
git add -A; git commit -qm "self-commit incl record over red tree"
""")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "review-ran").exists(), out)
        self.assertGreaterEqual(repo.verify_calls(), 1, out)
        self.assertFalse(repo.tracked("BAD", "HEAD"), out)


class ReviewRound3(_Base):
    """Regressions for the v0.18.1 review, round 3 (each red on the round-3
    build)."""

    def _record_then_feature(self):
        return self._repo(r"""
echo "the feature itself" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' \
  > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "record the completion first"
sleep 1.5
touch "$STUB_DIR/second-commit-made"
git add -A
git commit -qm "then the feature itself"
sleep 2
""")

    def test_a_record_only_commit_does_not_end_the_turn_before_the_work_is_committed(self):
        # MAJOR 1: the guard stranded the work v0.18.0 landed.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                self.squash = squash
                repo = self._record_then_feature()
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out, rec = self._state(repo, r)
                self.assertTrue((repo.stub / "second-commit-made").exists(), out)
                self.assertEqual(r.returncode, 0, out)
                self.assertIn("the feature itself", repo.git("show", "main:src.txt"), out)
                self.assertEqual(repo.porcelain_all(), [], out)

    def test_a_deletion_racing_the_signal_is_restored(self):
        # MINOR 1 (resolved in round 4 by observation): a deletion after the
        # clean snapshot is after the commit like any write — restored.
        repo = self._repo(COMPLETE_AND_SELF_COMMIT + r"""
trap 'rm -f old.txt; exit 143' TERM
sleep 1
for i in 1 2 3 4 5 6 7 8 9 10; do echo "after $i" >> src.txt; sleep 0.3; done
""")
        repo.write("old.txt", "old\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "old")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertEqual((repo.repo / "old.txt").read_text(), "old\n", out)
        self.assertIn("old.txt", (rec.get("post_completion") or {}).get("restored", []), out)

    def test_a_repair_that_re_claims_the_carried_record_and_commits_it_is_guarded(self):
        # MINOR 3: an identical re-claim committed by the model was invisible.
        repo = self._repo(r"""
if [ "$CALL_N" = 1 ]; then
  echo "work" >> src.txt; touch BAD
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  exit 0
fi
rm -f BAD
cp artifacts/project-complete.json "$STUB_DIR/r" && cat "$STUB_DIR/r" > artifacts/project-complete.json
git add -A; git commit -qm "the repair, with the completion re-claimed"
""" + KEEPS_WRITING)
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out, rec = self._state(repo, r)
        self.assertIn("completion guard: the completion record was committed during the turn", out)
        self.assertFalse((repo.stub / "turn-ran-to-its-end").exists(), out)
        # v0.18.2: plain step 4 now gates the model's own commit — green here,
        # and the gate clears phase-verify-failed.json, which the model's
        # `git add -A` had swept into its commit (a bypass: the guard refuses
        # that add). The loop's rest ignores transient signals (step 7 is
        # proven) and the next start's heal untracks it.
        self.assertEqual([ln for ln in repo.porcelain_all() if "phase-verify-failed.json" not in ln], [], out)
        self.assertEqual((repo.record() or {}).get("step"), H.RESTED, out)

    def test_a_skipped_review_still_runs_for_the_repaired_completion(self):
        # MINOR 5: a builder's own commit once bypassed the review for good.
        self.squash = True
        repo = self._repo(r"""
if [ "$PHASEKIT_ITER" = light-review ]; then touch "$STUB_DIR/review-ran"; exit 0; fi
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt; touch BAD
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  git add -A; git commit -qm "self-committed over a red tree"
  exit 0
fi
git rm -q BAD
jq -n '{done: true, summary: "complete, repaired", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
""")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out, rec = self._state(repo, r)
        self.assertIn("does not run", out)
        self.assertTrue((repo.stub / "review-ran").exists(), out)


class ReviewRound5(_Base):
    """Regressions for the v0.18.1 review, round 5 (each red on the round-5
    build)."""

    def test_a_stash_during_the_turn_never_reads_as_a_clean_completion(self):
        # MAJOR 1: `git stash -u` left the tree clean, the guard fired, the
        # pop never came and the work rested in refs/stash, unnamed.
        repo = self._repo(r"""
echo "older work" >> src.txt
jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
git add artifacts/project-complete.json
git commit -qm "record first"
git stash -u -q
sleep 1.5
git stash pop -q
sleep 1
touch "$STUB_DIR/turn-ran-to-its-end"
git add -A
git commit -qm "then the work"
sleep 2
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out, rec = self._state(repo, r)
        self.assertTrue((repo.stub / "turn-ran-to-its-end").exists(), out)
        self.assertIn("older work", repo.git("show", "HEAD:src.txt"), out)
        self.assertEqual(repo.git("stash", "list"), "", out)

    def test_a_cli_failure_after_a_record_only_commit_is_retried(self):
        # MINOR 2: the git-evidence fallback read any non-zero exit as the
        # guard's; the CLI retry that could commit the rest was skipped.
        repo = self._repo(r"""
if [ "$CALL_N" = 1 ]; then
  echo "older work" >> src.txt
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete"}' > artifacts/project-complete.json
  git add artifacts/project-complete.json
  git commit -qm "record first"
  exit 1
fi
git add -A
git commit -qm "the retry commits the rest"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2", "PHASEKIT_ITER_RETRY": "1"})
        out, rec = self._state(repo, r)
        self.assertEqual(repo.calls(), 2, out)
        self.assertIn("retrying in continue mode", out)
        self.assertNotIn("the turn was ended there", out)
        self.assertIn("older work", repo.git("show", "HEAD:src.txt"), out)


class StructuralPins(unittest.TestCase):
    SOURCE = H.SOURCE

    def test_every_turn_runs_under_the_guard_and_the_trap_reaps_it(self):
        fn = H._extract_block(r"^run_once\(\) \{", r"^\}")
        self.assertLess(fn.index("completion_snapshot_drop"), fn.index("completion_guard_start"))
        self.assertLess(fn.index("completion_guard_start"), fn.index("$RUN_PHASE_SCRIPT"))
        self.assertGreater(fn.rindex("completion_guard_stop"), fn.rindex("$RUN_PHASE_SCRIPT"))
        trap = H._extract_block(r"^run_until_done_exit_trap\(\) \{", r"^\}")
        self.assertIn("completion_guard_stop", trap)
        self.assertEqual(self.SOURCE.count("\ntrap run_until_done_exit_trap EXIT"), 1)

    def test_the_guard_is_detached_and_checks_the_loop_is_alive(self):
        start = H._extract_block(r"^completion_guard_start\(\) \{", r"^\}")
        self.assertIn("</dev/null &", start)
        self.assertIn("completion-guard.log", start)
        watch = H._extract_block(r"^completion_guard_watch\(\) \{", r"^\}")
        self.assertIn('kill -0 "$$"', watch)
        self.assertIn("GIT_OPTIONAL_LOCKS=0", watch, "the guard never takes the index lock")
        observe = H._extract_block(r"^completion_guard_observe\(\) \{", r"^\}")
        self.assertIn("kill -TERM", observe)
        self.assertIn("claude_turn_pid", observe)
        self.assertIn("committed_record_claims", observe)
        # Round 4: the snapshot is taken on the tree the guard just saw clean
        # (cheap — a clean tree has no records to stat), THEN the turn is
        # ended, so every write racing the signal is after the snapshot.
        self.assertLess(observe.index("_boundary_tree_clean"), observe.index("completion_snapshot_take"))
        self.assertLess(observe.index("completion_snapshot_take"), observe.index("kill -TERM"))
        self.assertNotIn("stat -c", observe, "never judged by a file's time")

    def test_the_walk_settles_at_step_3_and_before_the_rest_is_proven(self):
        walk = H._extract_block(r"^_land_boundary\(\) \{", r"^\}")
        self.assertIn("completion_snapshot_ensure", walk)
        self.assertEqual(walk.count("completion_residue_settle"), 3)   # walk start, step 3, the rest
        rest = walk.index('if [[ "$step" -eq "$BOUNDARY_STEP_RESTED" ]] && boundary_final; then completion_residue_settle')
        self.assertLess(rest, walk.index('if ! boundary_prove "$step"; then'))

    def test_the_contract_declares_the_record_keys_and_the_logs(self):
        m = json.loads(H.MANIFEST.read_text())
        entry = [a for a in m["artifacts"] if a["name"] == "boundary-state.json"][0]
        for key in ("completion_snapshot", "post_completion"):
            self.assertIn(key, entry["keys"])
        self.assertIn("v0.18.1", entry["when"])
        logs = [a for a in m["artifacts"] if a["name"] == "logs"][0]
        for name in (".completion-yield", ".completion-guard-start", "completion-guard.log", "post-completion/"):
            self.assertIn(name, logs["when"])


if __name__ == "__main__":
    unittest.main()
