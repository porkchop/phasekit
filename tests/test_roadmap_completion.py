#!/usr/bin/env python3
"""What an iteration adds to the roadmap, it resolves before it completes (v0.25.0, queue row 2560).

xmeo-v3 iteration 300 (2026-11-03) wrote R175-R187 under `## Next` of its
docs/ROADMAP.md, built them in the same iteration, and left every one
unchecked. A supervisor's planner follows unchecked `## Next` entries, so it
filed each one again: iterations 301, 302, 303 and 304 each re-proved one entry
on the current tree and ticked it, R177 "done as not built". Whole iterations
spent ticking boxes.

Pinned here, against the shipped loop run whole in a scratch repo with a stub
model (the boundary-state harness):
  * a COMPLETION that leaves an entry the iteration added under `## Next`
    unchecked is REFUSED before the gate, through the repair channel
    (phase-verify-failed.json, commit_refusal); the refusal names each entry
    and the three ways out;
  * the repair turn that ticks it (or ticks it as not built) lands, and so does
    one that names it with a reason in the record's `roadmap_open`; a
    `roadmap_open` item without a reason resolves nothing;
  * entries the roadmap already carried at the iteration's base are never
    judged, even when this iteration edited them; a renamed id is a new entry;
  * a final approval is judged like a completion, an ordinary phase approval
    is not (the iteration has not finished);
  * no roadmap, no derivable base, or a reader that fails: the completion lands
    as before (a project without a roadmap notices nothing);
  * `phasekit verify` says RED for the same tree and runs no gate; a wrap-up
    keeps the verdicts out and spends no second breaker attempt.

Run from the repo root: python3 -m unittest tests.test_roadmap_completion
"""

import importlib.util
import json
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-roadmap.py"

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
Repo = H.Repo

BASE_ROADMAP = """# Roadmap

Direction: settle rules 3

## Next
- [ ] R1 An entry the roadmap already had — scope
## Later
- [ ] R2 Parked — scope
## Done
- [x] R0 Shipped — iteration 1 (2026-11-01)
"""

LABEL = "refused before the gate runs"


def add_entries(*lines):
    """Scenario shell: append unchecked entries under ## Next (after R1)."""
    body = "\\n".join(lines)
    return f"sed -i 's/^- \\[ \\] R1 An entry.*$/&\\n{body}/' docs/ROADMAP.md\n"


def tick(eid):
    return f"sed -i 's/^- \\[ \\] {eid} /- [x] {eid} /' docs/ROADMAP.md\n"


def completion(**extra):
    rec = {"done": True, "summary": "built it", "deferrals": []}
    rec.update(extra)
    return ("echo \"work by call $CALL_N\" >> src.txt\n"
            "cat > artifacts/project-complete.json <<'JSON'\n" + json.dumps(rec) + "\nJSON\n")


def approval(final):
    rec = {"phase": "phase-1", "approved": True, "summary": "built it",
           "suggested_commit_message": "Phase 1 (APPROVED): built it", "deferrals": []}
    if final:
        rec["final_phase"] = True
    return ("echo \"work by call $CALL_N\" >> src.txt\n"
            "cat > artifacts/phase-approval.json <<'JSON'\n" + json.dumps(rec) + "\nJSON\n")


class RoadmapAtCompletion(unittest.TestCase):
    def _repo(self, roadmap=BASE_ROADMAP, supervised=True, tool=True):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        if tool:
            repo.put_engine("scripts/phasekit-roadmap.py", TOOL.read_text(), executable=True,
                            commit="the roadmap tool")
        if roadmap is not None:
            repo.write("docs/ROADMAP.md", roadmap)
            repo.git("add", "-A")
            repo.git("commit", "-qm", "a roadmap")
        if supervised:
            # the supervisor's intake: the iteration's base
            repo.write("artifacts/iteration-mode.json",
                       json.dumps({"mode": "standard", "iteration": 300}) + "\n")
            repo.git("add", "-A")
            repo.git("commit", "-qm", "iteration 300 intake")
        return repo

    def _out(self, r):
        return r.stdout + r.stderr

    def _refused(self, repo, r, record="project-complete.json"):
        out = self._out(r)
        self.assertIn("REFUSED — docs/ROADMAP.md entries this iteration added", out)
        pvf = repo.repo / "artifacts" / "phase-verify-failed.json"
        self.assertTrue(pvf.exists(), out)
        rec = json.loads(pvf.read_text())
        self.assertTrue(rec.get("commit_refusal"), rec)
        self.assertEqual(rec.get("label"), "roadmap entries this iteration added left unresolved")
        self.assertNotIn(record, repo.git("ls-files", "artifacts"),
                         "a refused landing commits nothing of the record")
        self.assertEqual(repo.verify_calls(), 0, LABEL)
        return rec, out

    def _landed(self, repo, r, record="project-complete.json"):
        out = self._out(r)
        self.assertNotIn("REFUSED", out)
        self.assertIn(record, repo.git("ls-files", "artifacts"), out)
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(), out)
        return out

    # -- the refusal ------------------------------------------------------
    def test_an_added_entry_left_unchecked_refuses_the_completion_and_names_it(self):
        repo = self._repo()
        head = repo.git("rev-parse", "HEAD")
        repo.scenario(add_entries("- [ ] R175 Quest stage five wins — scope",
                                  "- [ ] R176 Halt and resume decided — scope (needs: R175)")
                      + completion())
        rec, out = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertEqual(repo.git("rev-parse", "HEAD"), head)
        tail = rec["log_tail"]
        self.assertIn("R175 (docs/ROADMAP.md line 7): Quest stage five wins", tail)
        self.assertIn("R176 (docs/ROADMAP.md line 8)", tail)
        self.assertNotIn("R1 ", tail, "an entry the base carried is not this iteration's")
        self.assertNotIn("R2 ", tail)
        for way in ("phasekit roadmap done R<n> --iteration 300",
                    "--not-built", "\"roadmap_open\": [{\"id\": \"R<n>\", \"reason\""):
            self.assertIn(way, tail)

    def test_the_repair_turn_that_ticks_it_and_re_writes_the_record_lands(self):
        repo = self._repo()
        repo.scenario('if [ "$CALL_N" = 1 ]; then\n' + add_entries("- [ ] R175 Built — scope")
                      + "fi\n" + 'if [ "$CALL_N" = 2 ]; then\n' + tick("R175") + "fi\n"
                      + completion())
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("REFUSED — docs/ROADMAP.md", out)
        self.assertIn("project-complete.json", repo.git("ls-files", "artifacts"), out)
        self.assertIn("- [x] R175 Built", repo.git("show", "HEAD:docs/ROADMAP.md"))

    def test_a_tick_only_repair_gets_the_verdict_retry_and_then_lands(self):
        # review round 1, MAJOR 1: a repair that only ticks the roadmap
        # re-writes no record, so the carried record claims nothing. The
        # refusal says to re-write it; the loop's one no-verdict retry asks
        # again, and the record written then lands.
        repo = self._repo()
        repo.scenario('if [ "$CALL_N" = 1 ]; then\n' + add_entries("- [ ] R175 Built — scope")
                      + completion() + 'elif [ "$CALL_N" = 2 ]; then\n' + tick("R175")
                      + "else\n" + completion() + "fi\n")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("asking once for a verdict", out)
        self.assertIn("project-complete.json", repo.git("ls-files", "artifacts"), out)
        self.assertIn("- [x] R175 Built", repo.git("show", "HEAD:docs/ROADMAP.md"))

    def test_ticked_as_not_built_by_the_tool_lands(self):
        # the R177 shape: conditional on a decision that went against it
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R177 Only if R176 picks a rule — scope")
                      + f"python3 '{TOOL}' done R177 --iteration 300 "
                        "--not-built 'R176 accepted the behaviour as written'\n"
                      + completion())
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("— iteration 300 (", repo.git("show", "HEAD:docs/ROADMAP.md"))
        self.assertIn("not built: R176 accepted the behaviour as written",
                      repo.git("show", "HEAD:docs/ROADMAP.md"))

    def test_an_entry_left_open_with_a_reason_lands_and_stays_unchecked(self):
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R178 For the next iteration — scope")
                      + completion(roadmap_open=[{"id": "R178",
                                                  "reason": "theme two; the next iteration builds it"}]))
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("- [ ] R178 For the next iteration", repo.git("show", "HEAD:docs/ROADMAP.md"))

    def test_roadmap_open_without_a_reason_resolves_nothing(self):
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R178 Open — scope", "- [ ] R179 Also open — scope")
                      + completion(roadmap_open=[{"id": "R178", "reason": "  "}, {"id": "R179"},
                                                 "R178"]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("R178", rec["log_tail"])
        self.assertIn("R179", rec["log_tail"])
        self.assertIn("RE-WRITE THE RECORD YOU WROTE", rec["log_tail"])

    def test_only_the_unresolved_entry_is_named(self):
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R175 Built — s", "- [ ] R176 Open — s", "- [ ] R177 Not this one — s")
                      + tick("R175")
                      + completion(roadmap_open=[{"id": "R176", "reason": "later"}]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("R177", rec["log_tail"])
        self.assertNotIn("R175 (", rec["log_tail"])
        self.assertNotIn("R176 (", rec["log_tail"])

    def test_added_under_later_or_superseded_is_not_judged(self):
        repo = self._repo()
        repo.scenario("sed -i 's/^- \\[ \\] R2 Parked.*$/&\\n- [ ] R180 Parked too — s/' docs/ROADMAP.md\n"
                      + add_entries("- [ ] R181 SUPERSEDED by R1: replaced at once")
                      + completion())
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))

    # -- what is never judged ----------------------------------------------
    def test_a_pre_existing_entry_is_never_judged_even_when_edited(self):
        repo = self._repo()
        repo.scenario("sed -i 's/already had/already had, reworded/' docs/ROADMAP.md\n" + completion())
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("- [ ] R1 An entry the roadmap already had, reworded",
                      repo.git("show", "HEAD:docs/ROADMAP.md"))

    def test_a_renamed_id_is_a_new_entry(self):
        # ids are append-only; a renumbered line is new work to a planner
        repo = self._repo()
        repo.scenario("sed -i 's/^- \\[ \\] R1 /- [ ] R9 /' docs/ROADMAP.md\n" + completion())
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("R9 (docs/ROADMAP.md line 6)", rec["log_tail"])

    def test_no_roadmap_lands_as_before_and_says_nothing(self):
        repo = self._repo(roadmap=None)
        repo.scenario(completion())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertNotIn("roadmap", out.lower())

    def test_a_roadmap_the_iteration_creates_is_all_added(self):
        repo = self._repo(roadmap=None)
        repo.scenario("printf '## Next\\n- [ ] R1 First — s\\n' > docs/ROADMAP.md\n" + completion())
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("R1 (docs/ROADMAP.md line 2): First", rec["log_tail"])

    def test_no_derivable_base_lands(self):
        # standalone, no supervisor intake, no branch-per-iteration work base
        repo = self._repo(supervised=False)
        repo.scenario(add_entries("- [ ] R175 Built — s") + completion())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertNotIn("roadmap check", out)

    def test_a_failing_reader_warns_and_lands(self):
        repo = self._repo()
        repo.scenario("printf '\\xff\\xfe not utf-8\\n' >> docs/ROADMAP.md\n" + completion())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("WARN — roadmap check skipped: the roadmap reader exited 5", out)

    def test_a_missing_reader_warns_and_lands(self):
        repo = self._repo(tool=False)
        repo.scenario(add_entries("- [ ] R175 Built — s") + completion())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("WARN — roadmap check skipped", out)

    # -- which landings ------------------------------------------------------
    def test_an_ordinary_phase_approval_is_not_judged(self):
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R175 Built later this iteration — s") + approval(final=False))
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}), record="phase-approval.json")

    def test_a_final_approval_is_judged(self):
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R175 Built — s") + approval(final=True))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}), record="phase-approval.json")
        self.assertIn("R175", rec["log_tail"])

    # -- the session's own check and the wrap-up -----------------------------
    def test_phasekit_verify_is_red_and_runs_no_gate(self):
        repo = self._repo()
        repo.write("docs/ROADMAP.md", BASE_ROADMAP.replace(
            "## Later", "- [ ] R175 Built — s\n## Later"))
        repo.write("artifacts/project-complete.json", json.dumps({"done": True, "summary": "s"}) + "\n")
        r = repo.run_verb("verify")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("phasekit verify: RED — the landing will refuse this completion", r.stderr)
        self.assertIn("R175", r.stderr)
        self.assertEqual(repo.verify_calls(), 0)
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(),
                         "the model's own check spends no breaker attempt")

    def test_a_wrap_up_keeps_the_verdict_out_and_spends_no_second_attempt(self):
        # a final approval: the boundary refuses it, then the supervisor's
        # sentinel sends the loop to the wrap-up, which must not land it
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R175 Built — s") + approval(final=True)
                      + "touch artifacts/wrapup-requested\n")
        r = repo.run(env={"MAX_ITERATIONS": "3", "VERIFY_MAX_ATTEMPTS": "2"})
        out = self._out(r)
        self.assertIn("committing the rest as a checkpoint", out)
        self.assertNotIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        self.assertIn("work by call 1", repo.git("show", "HEAD:src.txt"), "the work itself is kept")
        self.assertIn("no breaker attempt spent", out)
        self.assertFalse((repo.repo / "artifacts" / "phase-blocked.json").exists(), out)
        pvf = json.loads((repo.repo / "artifacts" / "phase-verify-failed.json").read_text())
        self.assertEqual(pvf["attempts"], 1)
        self.assertIn("R175", pvf["log_tail"])
        self.assertTrue((repo.repo / "artifacts" / "phase-approval.json").exists(), "kept for the repair")
        handoff = repo.git("show", "HEAD:artifacts/session-handoff.json")
        self.assertIn("docs/ROADMAP.md entries", handoff, "review round 1, finding 2")
        self.assertNotIn("deferral entries", handoff)

    def test_a_refused_completion_with_a_wrap_up_lands_no_record(self):
        # the record is carried (unclaimed) after the refusal, so the wrap-up
        # never sees it as a verdict
        repo = self._repo()
        repo.scenario(add_entries("- [ ] R175 Built — s") + completion()
                      + "touch artifacts/wrapup-requested\n")
        r = repo.run(env={"MAX_ITERATIONS": "3", "VERIFY_MAX_ATTEMPTS": "2"})
        out = self._out(r)
        self.assertNotIn("project-complete.json", repo.git("ls-files", "artifacts"), out)
        self.assertFalse((repo.repo / "artifacts" / "phase-blocked.json").exists(), out)
        # The rest is the carried-record design every commit refusal of a
        # completion shares since v0.18.2 (declined with record in v0.24.0):
        # the wrap-up commit clears the refusal and the unclaimed record is
        # dropped at exit; the next session's completion is judged afresh.

    def test_the_refusal_precedes_the_gate_and_uses_the_repair_channel(self):
        src = H.LOOP_SCRIPT.read_text()
        body = src[src.index("\n_commit_from_artifact() {"):]
        body = body[:body.index("\n}\n")]
        i_check = body.index("roadmap_open_refusals")
        self.assertLess(body.index("deferral_shape_refusals"), i_check)
        self.assertLess(i_check, body.index("if ! run_verify_gate; then"))
        seg = body[i_check:body.index("if ! run_verify_gate; then")]
        self.assertIn("record_roadmap_open_refusal", seg)
        self.assertIn("unstage_verdicts_after_refusal", seg)
        helper = src[src.index("\nrecord_roadmap_open_refusal() {"):]
        self.assertIn("record_commit_refusal", helper[:helper.index("\n}\n")])


if __name__ == "__main__":
    unittest.main()
