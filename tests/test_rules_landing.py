#!/usr/bin/env python3
"""Every landing keeps the project rules register honest (v0.26.0).

xmeo-v3 (2026-11-07): three halt-mode bugs had one shape — a rule an earlier
incident taught ("the spawn ring and the painted pieces come from one board")
was held as code in two places, and the new mode updated one of them. Nothing
had written the rule down, so nothing could ask whether a change kept it.

Pinned here, against the shipped loop run whole in a scratch repo with a stub
model (the boundary-state harness):
  * a landing whose record names a `rules_touched` id the register does not
    have is REFUSED before the gate, through the repair channel
    (phase-verify-failed.json, commit_refusal), naming the id;
  * a register with a finding (a Test naming a file that does not exist)
    refuses even an ordinary approval that names no rule — whatever the
    record, the tree's register must be honest;
  * a known id, an empty list, an absent `rules_touched`, a valid register:
    the landing goes on as before; the repair turn that fixes the register and
    re-writes the record lands;
  * no register and no `rules_touched`: nothing runs and nothing is said; a
    missing checker or one that cannot complete WARNs and lands;
  * `phasekit verify` says RED for the same tree and runs no gate; a wrap-up
    keeps the verdict out and spends no second breaker attempt.

Run from the repo root: python3 -m unittest tests.test_rules_landing
"""

import importlib.util
import json
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-rules.py"

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_rules", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
Repo = H.Repo

LABEL = "project rules register (docs/project/RULES.md)"
REGISTER = """# demo — project rules

## RULE-1 — The ring and the pieces come from one board
- Incident: 2026-11-07, multiplayer halt drew the ring off the pieces
- Implemented in: `src/board.ts` (`placementBoard`)
- Test: `tests/board.test.ts` `ring and pieces share one board`
"""
TEST_FILE = "it('ring and pieces share one board', () => {})\n"


def approval(**extra):
    rec = {"phase": "phase-1", "approved": True, "summary": "built it",
           "suggested_commit_message": "Phase 1 (APPROVED): built it", "deferrals": []}
    rec.update(extra)
    return ("echo \"work by call $CALL_N\" >> src.txt\n"
            "cat > artifacts/phase-approval.json <<'JSON'\n" + json.dumps(rec) + "\nJSON\n")


class RulesAtLanding(unittest.TestCase):
    def _repo(self, register=REGISTER, tool=True):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        if tool:
            repo.put_engine("scripts/phasekit-rules.py", TOOL.read_text(), executable=True,
                            commit="the rules checker")
        if register is not None:
            repo.write("docs/project/RULES.md", register)
            repo.write("tests/board.test.ts", TEST_FILE)
            repo.git("add", "-A")
            repo.git("commit", "-qm", "a rules register")
        return repo

    def _out(self, r):
        return r.stdout + r.stderr

    def _refused(self, repo, r, record="phase-approval.json"):
        out = self._out(r)
        self.assertIn("REFUSED — the project rules register", out)
        pvf = repo.repo / "artifacts" / "phase-verify-failed.json"
        self.assertTrue(pvf.exists(), out)
        rec = json.loads(pvf.read_text())
        self.assertTrue(rec.get("commit_refusal"), rec)
        self.assertEqual(rec.get("label"), LABEL)
        self.assertNotIn(record, repo.git("ls-files", "artifacts"),
                         "a refused landing commits nothing of the record")
        self.assertEqual(repo.verify_calls(), 0, "refused before the gate runs")
        return rec, out

    def _landed(self, repo, r, record="phase-approval.json"):
        out = self._out(r)
        self.assertNotIn("REFUSED", out)
        self.assertIn(record, repo.git("ls-files", "artifacts"), out)
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(), out)
        return out

    # -- rules_touched ----------------------------------------------------
    def test_an_unknown_rules_touched_id_refuses_the_landing_and_names_it(self):
        repo = self._repo()
        head = repo.git("rev-parse", "HEAD")
        repo.scenario(approval(rules_touched=["RULE-1", "RULE-9"]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertEqual(repo.git("rev-parse", "HEAD"), head)
        tail = rec["log_tail"]
        self.assertIn("artifacts/phase-approval.json: rules_touched names RULE-9", tail)
        self.assertNotIn("names RULE-1", tail)
        for way in ("add the rule to docs/project/RULES.md", "RE-WRITE THE RECORD YOU WROTE",
                    "phasekit rules check"):
            self.assertIn(way, tail)

    def test_rules_touched_without_a_register_refuses(self):
        repo = self._repo(register=None)
        repo.scenario(approval(rules_touched=["RULE-1"]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("there is no docs/project/RULES.md", rec["log_tail"])

    def test_rules_touched_that_is_not_an_array_refuses(self):
        repo = self._repo()
        repo.scenario(approval(rules_touched="RULE-1"))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("must be an array of register ids", rec["log_tail"])

    def test_a_known_id_lands(self):
        repo = self._repo()
        repo.scenario(approval(rules_touched=["RULE-1"]))
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn('"rules_touched"', repo.git("show", "HEAD:artifacts/phase-approval.json"))

    def test_an_empty_list_and_an_absent_field_land(self):
        for extra in ({"rules_touched": []}, {}, {"rules_touched": None}):
            with self.subTest(extra=extra):
                repo = self._repo()
                repo.scenario(approval(**extra))
                self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))

    def test_a_completion_record_is_judged_too(self):
        repo = self._repo()
        rec = {"done": True, "summary": "built it", "deferrals": [], "rules_touched": ["NOPE-1"]}
        repo.scenario("echo \"work by call $CALL_N\" >> src.txt\n"
                      "cat > artifacts/project-complete.json <<'JSON'\n" + json.dumps(rec)
                      + "\nJSON\n")
        pvf, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}),
                               record="project-complete.json")
        self.assertIn("artifacts/project-complete.json: rules_touched names NOPE-1",
                      pvf["log_tail"])

    # -- the register itself ------------------------------------------------
    def test_a_register_naming_a_missing_test_refuses_an_ordinary_approval(self):
        repo = self._repo()
        repo.scenario("rm tests/board.test.ts\n" + approval())
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("RULE-1: test file `tests/board.test.ts` does not exist", rec["log_tail"])

    def test_a_malformed_entry_refuses(self):
        repo = self._repo()
        repo.scenario("printf '\\n## RULE-2 — Half written\\n- Incident: x\\n' "
                      ">> docs/project/RULES.md\n" + approval())
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("RULE-2: missing `- Implemented in:`, `- Test:`", rec["log_tail"])

    def test_the_repair_turn_that_fixes_it_and_re_writes_the_record_lands(self):
        repo = self._repo()
        repo.scenario('if [ "$CALL_N" = 1 ]; then rm tests/board.test.ts; fi\n'
                      'if [ "$CALL_N" = 2 ]; then '
                      "sed -i 's#^- Test: .*#- Test: no test yet#' docs/project/RULES.md; fi\n"
                      + approval(rules_touched=["RULE-1"]))
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self._out(r)
        self.assertIn("REFUSED — the project rules register", out)
        self.assertIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        self.assertIn("- Test: no test yet", repo.git("show", "HEAD:docs/project/RULES.md"))

    def test_a_valid_register_lands(self):
        repo = self._repo()
        repo.scenario(approval())
        self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))

    # -- what is never judged -----------------------------------------------
    def test_no_register_and_no_rules_touched_lands_and_says_nothing(self):
        repo = self._repo(register=None)
        repo.scenario(approval())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertNotIn("rules register", out)

    def test_a_missing_checker_warns_and_lands(self):
        repo = self._repo(tool=False)
        repo.scenario("rm tests/board.test.ts\n" + approval())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("WARN — rules register check skipped", out)

    def test_a_checker_that_cannot_complete_warns_and_lands(self):
        repo = self._repo()
        repo.scenario("printf '\\xff\\xfe not utf-8\\n' >> docs/project/RULES.md\n" + approval())
        out = self._landed(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("WARN — rules register check skipped: the rules checker exited 5", out)

    # -- the session's own check and the wrap-up -----------------------------
    def test_phasekit_verify_is_red_and_runs_no_gate(self):
        repo = self._repo()
        repo.write("artifacts/phase-approval.json", json.dumps(
            {"phase": "phase-1", "approved": True, "summary": "s",
             "rules_touched": ["RULE-7"]}) + "\n")
        r = repo.run_verb("verify")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("phasekit verify: RED — the landing will refuse this verdict", r.stderr)
        self.assertIn("RULE-7", r.stderr)
        self.assertEqual(repo.verify_calls(), 0)
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(),
                         "the model's own check spends no breaker attempt")

    def test_phasekit_verify_judges_only_a_tree_that_carries_a_verdict(self):
        # a phase-update lands whatever the register says; verify agrees
        repo = self._repo()
        (repo.repo / "tests" / "board.test.ts").unlink()
        r = repo.run_verb("verify")
        self.assertNotIn("rules register", r.stdout + r.stderr)

    def test_a_wrap_up_keeps_the_verdict_out_and_spends_no_second_attempt(self):
        repo = self._repo()
        repo.scenario(approval(rules_touched=["RULE-9"]) + "touch artifacts/wrapup-requested\n")
        r = repo.run(env={"MAX_ITERATIONS": "3", "VERIFY_MAX_ATTEMPTS": "2"})
        out = self._out(r)
        self.assertIn("committing the rest as a checkpoint", out)
        self.assertNotIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        self.assertIn("work by call 1", repo.git("show", "HEAD:src.txt"), "the work itself is kept")
        self.assertIn("no breaker attempt spent", out)
        self.assertFalse((repo.repo / "artifacts" / "phase-blocked.json").exists(), out)
        pvf = json.loads((repo.repo / "artifacts" / "phase-verify-failed.json").read_text())
        self.assertEqual(pvf["attempts"], 1)
        self.assertIn("RULE-9", pvf["log_tail"])
        self.assertTrue((repo.repo / "artifacts" / "phase-approval.json").exists(), "kept for the repair")
        handoff = repo.git("show", "HEAD:artifacts/session-handoff.json")
        self.assertIn("project rules register", handoff)
        self.assertNotIn("deferral entries", handoff)

    def test_the_refusal_follows_the_roadmap_check_and_precedes_the_gate(self):
        src = H.LOOP_SCRIPT.read_text()
        body = src[src.index("\n_commit_from_artifact() {"):]
        body = body[:body.index("\n}\n")]
        i_check = body.index("rules_register_refusals")
        self.assertLess(body.index("roadmap_open_refusals"), i_check)
        self.assertLess(i_check, body.index("if ! run_verify_gate; then"))
        seg = body[i_check:body.index("if ! run_verify_gate; then")]
        self.assertIn("record_rules_register_refusal", seg)
        self.assertIn("unstage_verdicts_after_refusal", seg)
        helper = src[src.index("\nrecord_rules_register_refusal() {"):]
        self.assertIn("record_commit_refusal", helper[:helper.index("\n}\n")])


if __name__ == "__main__":
    unittest.main()
