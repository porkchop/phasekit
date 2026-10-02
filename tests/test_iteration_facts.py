#!/usr/bin/env python3
"""Session efficiency and iteration integrity (v0.18.0) — the end-to-end pins.

foundry-meta designs/DESIGN-session-efficiency.md, approved 2026-09-28. Every
test here names the link of the causal chain it closes and is RED on v0.17.0:

  §4 (links [H]-[J]) — iteration facts are the loop's: trailers on every loop
     commit; the subject prefix generated from the record and a stale one
     corrected (the 494fb6f fixture: iteration 88's work under iteration 87's
     subject); records composed fresh with a tool-maintained deferral ledger;
     phase-close evidence; `phasekit scope`.
  §5 — hermetic tests: an advisory names test files that read git history,
     and the gate stays green.
  §2.3 — the model's `phasekit verify` counts: the commit gate reuses it, and
     light mode's final commit does not re-run the full tier.
  §3.2 (link [F]) — a red stranded completion keeps its record on disk,
     unstaged, for the repair turn (never the `AD` state).

The loop is the shipped script, run whole in a scratch repo with a stub
model (the boundary-state harness, tests/test_boundary_state.py).

Run from the repo root: python3 -m unittest tests.test_iteration_facts
"""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The stub model exports two variables of its own (CALL_N, the loop pid for
# its kill scenarios); a real model's shell carries only what the harness adds.
PV = 'env -u CALL_N -u PHASEKIT_TEST_LOOP_PID bash scripts/phasekit.sh verify'

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
Repo = H.Repo

# A logging gate that also names its tier (the doctrine: full exactly when
# the completion record exists).
VERIFY_TIERED = """#!/usr/bin/env bash
PHASEKIT_VERIFY_CONFIGURED=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/artifacts/project-complete.json" ]; then t=full; else t=fast; fi
echo "run $t" >> "${STUB_DIR:?}/verify-calls"
if [ -f "$ROOT/BAD" ]; then exit 1; fi
exit 0
"""


def supervised(repo, iteration, intake_subject="unrelated intake words", mode="standard"):
    """Commit the supervisor's iteration marker the way an intake does, on
    the target; the work branch (squash mode) then starts from it."""
    if repo.squash:
        repo.git("checkout", "-q", "main")
    repo.write("artifacts/iteration-mode.json",
               json.dumps({"mode": mode, "iteration": iteration}) + "\n")
    repo.git("add", "-A")
    repo.git("commit", "-qm", intake_subject)
    sha = repo.git("rev-parse", "HEAD")
    if repo.squash:
        repo.git("checkout", "-q", "-B", "iter/1-test", "main")
    return sha


def with_cli(repo):
    shutil.copy(REPO_ROOT / "scripts" / "phasekit.sh", repo.repo / "scripts" / "phasekit.sh")
    repo.git("add", "-A")
    repo.git("commit", "-qm", "cli")


def trailers(repo, ref):
    out = repo.git("log", "-1", "--format=%(trailers:only,unfold)", ref)
    return dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)


APPROVE_188 = r"""
echo "work by call $CALL_N" >> src.txt
echo "a new module" > lib.txt
jq -n '{phase: "phase-188", approved: true, iteration: "88",
        summary: "Create panel seat choice reaches the door",
        suggested_commit_message: "iteration 88 phase 188: the seat choice reaches the door"}' \
  > artifacts/phase-approval.json
"""


class IterationFacts(unittest.TestCase):
    def _repo(self, squash=False):
        repo = Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        return repo

    # --- trailers (§4.2) -------------------------------------------------
    def test_squash_carries_iteration_and_phase_trailers(self):
        repo = self._repo(squash=True)
        supervised(repo, 88)
        repo.scenario(APPROVE_188)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("boundary-state: rested (step 7)", r.stdout, r.stdout + r.stderr)
        squash = trailers(repo, "main")
        self.assertEqual(squash.get("Phasekit-Iteration"), "88", squash)
        self.assertEqual(squash.get("Phasekit-Phase"), "188")
        self.assertEqual(squash.get("Phasekit-Kind"), "squash")
        self.assertTrue(squash.get("phasekit-squash", "").startswith("iter/1-test@"),
                        "the v0.14.0 squash trailer stays, first, unchanged")
        # the branch's own phase commit (the merge-back's first parent)
        phase = trailers(repo, "iter/1-test^1")
        self.assertEqual((phase.get("Phasekit-Iteration"), phase.get("Phasekit-Phase"),
                          phase.get("Phasekit-Kind")), ("88", "188", "phase"))
        # and a reader finds it by trailer, not by subject
        found = repo.git("log", "--format=%(trailers:key=Phasekit-Iteration,valueonly)", "-1", "main")
        self.assertEqual(found.strip(), "88")

    # --- subject prefix + record integrity (§4.3, link [I]) ---------------
    def test_a_stale_subject_prefix_is_corrected_from_the_record(self):
        # The 494fb6f fixture: iteration 88's record was built by loading
        # iteration 87's; the stale message came along.
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(r"""
echo "iteration 88 work" >> src.txt
jq -n '{phase: "phase-188", approved: true, iteration: "87",
        summary: "Create panel seat choice reaches the door",
        suggested_commit_message: "iteration 87 phase 187: select a piece, drop it where you meant, at 390x844 too (roadmap R4)"}' \
  > artifacts/phase-approval.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        subject = repo.git("log", "-1", "--format=%s", "HEAD")
        self.assertEqual(subject, "iteration 88 phase 188: Create panel seat choice reaches the door", out)
        self.assertIn("commit subject corrected: 'iteration 87 phase 187' → 'iteration 88 phase 188' "
                      "(the record says so)", out)
        committed = json.loads(repo.git("show", "HEAD:artifacts/phase-approval.json"))
        self.assertEqual(committed["iteration"], 88, "the record names the supervisor's iteration")
        self.assertIn("record corrected", out)

    def test_an_agreeing_prefix_is_kept_as_written(self):
        # a regression guard (green before and after): no double prefix, no churn
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(APPROVE_188)
        repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertEqual(repo.git("log", "-1", "--format=%s", "HEAD"),
                         "iteration 88 phase 188: the seat choice reaches the door")

    def test_an_underivable_phase_keeps_the_subject_and_warns_never_refuses(self):
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(r"""
echo w >> src.txt
jq -n '{approved: true, suggested_commit_message: "feat: something"}' > artifacts/phase-approval.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual(repo.git("log", "-1", "--format=%s", "HEAD"), "feat: something", out)
        self.assertIn("cannot be derived", out)
        t = trailers(repo, "HEAD")
        self.assertEqual(t.get("Phasekit-Iteration"), "88")
        self.assertNotIn("Phasekit-Phase", t, "no trailer for a fact the loop cannot see (F5)")

    # --- fresh records + the deferral ledger (§4.4) -----------------------
    def _previous_iteration_record(self, repo):
        """Iteration 87 completed with two open deferrals; iteration 88's
        intake archived (deleted) the record, as the supervisor does."""
        repo.write("artifacts/project-complete.json", json.dumps({
            "done": True, "iteration": 87, "summary": "iteration 87",
            "deferrals": [
                {"item": "Polish the lobby later", "reason": "r", "suggested_task": "t", "key": "a-key"},
                {"item": "AC#3 integer math", "reason": "r", "suggested_task": "t"},
            ]}) + "\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "iteration 87 complete")
        repo.git("rm", "-q", "artifacts/project-complete.json")
        repo.git("commit", "-qm", "archive the record")

    def test_the_record_is_composed_fresh_and_the_ledger_carries_the_open_set(self):
        repo = self._repo()
        self._previous_iteration_record(repo)
        supervised(repo, 88)
        repo.scenario(r"""
echo w >> src.txt
jq -n '{done: true, summary: "iteration 88 done",
        deferrals: [{item: "Tune the new panel", reason: "r", suggested_task: "t", key: "c-key"}],
        closes: ["a-key"]}' > artifacts/project-complete.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("Run finished successfully.", out)
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual([d["key"] for d in record["deferrals"]], ["AC#3", "c-key"], out)
        self.assertEqual(record["closes"], ["a-key"])
        ledger = json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))
        self.assertEqual([d["key"] for d in ledger["open"]], ["AC#3", "c-key"])
        self.assertEqual(record["iteration"], 88)
        self.assertEqual(record["base"], repo.git("log", "-1", "--format=%H", "--", "artifacts/iteration-mode.json"))

    def test_a_copied_forward_record_changes_nothing_on_the_first_composition(self):
        # The migration check (design §4.6): the first composed landing's
        # ledger key set equals the last copy-forward record's. A regression
        # guard, green before and after — the orchestrator's open-deferral
        # counts must not move on migration.
        repo = self._repo()
        self._previous_iteration_record(repo)
        supervised(repo, 88)
        repo.scenario(r"""
echo w >> src.txt
jq -n '{done: true, summary: "copied forward",
        deferrals: [
          {item: "Polish the lobby later", reason: "r", suggested_task: "t", key: "a-key"},
          {item: "AC#3 integer math", reason: "r", suggested_task: "t"},
          {item: "Tune the new panel", reason: "r", suggested_task: "t", key: "c-key"}]}' \
  > artifacts/project-complete.json
""")
        repo.run(env={"MAX_ITERATIONS": "1"})
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual([d["key"] for d in record["deferrals"]], ["a-key", "AC#3", "c-key"])

    # --- phase-close evidence + scope (§4.5) -------------------------------
    def test_phase_close_writes_evidence_with_base_and_changed_files(self):
        repo = self._repo()
        base = supervised(repo, 88)
        repo.scenario(APPROVE_188)
        repo.run(env={"MAX_ITERATIONS": "1"})
        ev = json.loads(repo.git("show", "HEAD:artifacts/iterations/88/188.json"))
        self.assertEqual((ev["iteration"], ev["phase"], ev["base"]), ("88", "188", base))
        changed = {c["path"]: c for c in ev["changed"]}
        src = (repo.repo / "src.txt").read_bytes()
        self.assertEqual(changed["src.txt"]["status"], "M")
        self.assertEqual(changed["src.txt"]["sha256"], hashlib.sha256(src).hexdigest())
        self.assertEqual(changed["lib.txt"]["status"], "A")
        self.assertNotIn("artifacts/iterations/88/188.json", changed, "the evidence never lists itself")

    def test_phasekit_scope_answers_without_subject_grep(self):
        repo = self._repo()
        with_cli(repo)
        # no subject anywhere says "iteration 88" — scope must not need one
        base = supervised(repo, 88, intake_subject="chore: bump things")
        repo.scenario(APPROVE_188.replace("iteration 88 phase 188: ", ""))
        repo.run(env={"MAX_ITERATIONS": "1"})
        r = subprocess.run(["bash", "scripts/phasekit.sh", "scope", "--json"], cwd=repo.repo,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        scope = json.loads(r.stdout)
        self.assertEqual((scope["iteration"], scope["base"]), ("88", base))
        self.assertIn({"path": "lib.txt", "status": "A"}, scope["changed"])
        self.assertEqual([p["phase"] for p in scope["phase_commits"]], ["188"])
        r = subprocess.run(["bash", "scripts/phasekit.sh", "scope", "--phase", "phase-188", "--json"],
                           cwd=repo.repo, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        phase = json.loads(r.stdout)
        self.assertTrue(phase["source"].startswith("evidence artifacts/iterations/88/188.json"), phase)
        self.assertIn({"path": "src.txt", "status": "M"}, phase["changed"])

    # --- hermetic tests (§5) ------------------------------------------------
    def test_a_test_that_reads_git_history_is_named_in_an_advisory_and_the_gate_stays_green(self):
        repo = self._repo()
        repo.write("tests/iteration-57-scope.test.ts",
                   "const A = git('rev-list', '-n', '1', '--grep=^iteration 57 intake', 'HEAD');\n")
        repo.write("tests/plain.test.ts", "expect(add(1, 2)).toBe(3);\n")
        repo.write("tests/scratch.test.ts", "const repo = makeScratchRepo(); // builds its own\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "tests")
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("ADVISORY: 1 test files appear to read git history (tests/iteration-57-scope.test.ts)", out)
        self.assertEqual(out.count("appear to read git history"), 1, "once per session")
        self.assertIn("boundary-state: rested (step 7)", out, "advisory only — the gate stays green")


class ModelVerifyCounts(unittest.TestCase):
    """§2.3: close-out does its most expensive work once."""

    def _repo(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("scripts/phasekit-verify.sh", VERIFY_TIERED, executable=True)
        with_cli(repo)
        return repo

    def runs(self, repo):
        p = repo.stub / "verify-calls"
        return p.read_text().split("\n") if p.exists() else []

    def test_phasekit_verify_on_the_exact_tree_is_reused_by_the_commit_gate(self):
        repo = self._repo()
        repo.scenario(H.APPROVE_SCENARIO + (
            PV + ' > "$STUB_DIR/pv.out" 2>&1; echo $? > "$STUB_DIR/pv.rc"\n'))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual((repo.stub / "pv.rc").read_text().strip(), "0", (repo.stub / "pv.out").read_text())
        self.assertIn("GREEN", (repo.stub / "pv.out").read_text())
        self.assertEqual(repo.verify_calls(), 1, "the model's run is the only run\n" + out)
        self.assertIn("reusing the green verdict recorded for this exact tree", out)
        self.assertEqual(repo.git("log", "-1", "--format=%s", "HEAD"), "phase 1: built it")
        self.assertIn("boundary-state: rested (step 7)", out)
        self.assertFalse(repo.artifact("phase-verify-failed.json").exists())

    def test_a_red_model_verify_spends_no_breaker_attempt(self):
        repo = self._repo()
        repo.scenario("touch BAD\n" + H.APPROVE_SCENARIO + (
            PV + ' > "$STUB_DIR/pv.out" 2>&1; echo $? > "$STUB_DIR/pv.rc"\n'
            'rm -f BAD\n'))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertEqual((repo.stub / "pv.rc").read_text().strip(), "1")
        self.assertIn("spends no breaker attempt", (repo.stub / "pv.out").read_text())
        self.assertIn("boundary-state: rested (step 7)", r.stdout, r.stdout + r.stderr)

    def test_the_light_review_run_counts_and_the_final_commit_does_not_rerun_the_full_tier(self):
        # The stub model follows whatever its prompt says: the tool the prompt
        # names, else the project's gate script directly (what v0.17.0 asked).
        repo = self._repo()
        repo.scenario(r"""
prompt="$STUB_DIR/prompt-$CALL_N.txt"
check() { if grep -q "phasekit.sh verify" "$prompt"; then env -u CALL_N -u PHASEKIT_TEST_LOOP_PID bash scripts/phasekit.sh verify >/dev/null 2>&1; else bash scripts/phasekit-verify.sh; fi; }
if [ "$CALL_N" = 1 ]; then
  echo "built" >> src.txt
  check
  jq -n '{done: true, summary: "light task", suggested_commit_message: "light: done"}' > artifacts/project-complete.json
else
  jq '.summary = "light task (reviewed)"' artifacts/project-complete.json > "$STUB_DIR/r.json" && cat "$STUB_DIR/r.json" > artifacts/project-complete.json
  check
fi
""")
        r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        runs = [x for x in self.runs(repo) if x]
        self.assertEqual(runs.count("run full"), 1, f"the full tier ran {runs.count('run full')}x: {runs}\n{out}")
        self.assertIn("reusing the green verdict", out)


class ReviewRound1(unittest.TestCase):
    """Regressions for the v0.18.0 review, round 1 (each red on the round-1 build)."""

    def _repo(self, squash=False):
        repo = Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        return repo

    def test_a_large_open_set_composes_through_files_not_arguments(self):
        # BLOCKER 1: xmeo's 38 open deferrals are 218 KB — over one argument's
        # 128 KiB limit; the ledger silently never formed and the committed
        # record kept only the session's own entry.
        repo = self._repo()
        big = [{"item": f"Deferred thing {i}", "reason": "r" * 6000, "suggested_task": "t", "key": f"k{i}"}
               for i in range(40)]
        repo.write("artifacts/project-complete.json", json.dumps({"done": True, "deferrals": big}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "iteration 87 complete")
        repo.git("rm", "-q", "artifacts/project-complete.json"); repo.git("commit", "-qm", "archive")
        supervised(repo, 88)
        repo.scenario('echo w >> src.txt\n'
                      'jq -n \'{done: true, summary: "s", deferrals: [{item: "NEW", reason: "r", suggested_task: "t", key: "new"}]}\''
                      ' > artifacts/project-complete.json\n')
        repo.run(env={"MAX_ITERATIONS": "1"})
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual(len(record["deferrals"]), 41)
        self.assertEqual(len(json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))["open"]), 41)

    def test_a_draft_verdict_abandoned_after_verify_leaves_no_derived_state_behind(self):
        # MAJOR 2: the ledger and the evidence are recomputed by the landing
        # that owns them; a checkpoint never carries a draft's.
        repo = self._repo()
        with_cli(repo)
        supervised(repo, 88)
        repo.scenario(APPROVE_188.replace('iteration: "88",', 'iteration: "88", deferrals: [{item: "C", reason: "r", suggested_task: "t", key: "c"}],')
                      + PV + ' >/dev/null 2>&1\n'
                      'rm -f artifacts/phase-approval.json\n'
                      'jq -n \'{summary: "not done yet"}\' > artifacts/phase-update.json\n')
        repo.run(env={"MAX_ITERATIONS": "1"})
        tracked = repo.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertNotIn("artifacts/deferrals.json", tracked)
        self.assertFalse([t for t in tracked if t.startswith("artifacts/iterations/")], tracked)
        self.assertEqual([ln for ln in repo.porcelain() if "deferrals" in ln or "iterations" in ln], [])

    def test_a_landed_approval_rewritten_identically_is_no_second_landing(self):
        # MAJOR 3: rewriting the evidence of a landed approval defeated the
        # no-churn gate (a second commit, a second squash, an empty evidence).
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(APPROVE_188)
        repo.run(env={"MAX_ITERATIONS": "1"})
        head = repo.git("rev-parse", "HEAD")
        evidence = repo.git("show", "HEAD:artifacts/iterations/88/188.json")
        repo.reset_stub()
        repo.scenario('cp artifacts/phase-approval.json "$STUB_DIR/a" && cat "$STUB_DIR/a" > artifacts/phase-approval.json\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("No substantive change staged", r.stdout, r.stdout + r.stderr)
        self.assertEqual(repo.git("rev-parse", "HEAD"), head)
        self.assertEqual(repo.git("show", "HEAD:artifacts/iterations/88/188.json"), evidence)

    def test_a_closed_key_is_never_reopened_by_a_copy_forward(self):
        # MAJOR 4: the ledger remembers what was closed.
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(r"""
if [ "$CALL_N" = 1 ]; then
  echo a >> src.txt
  jq -n '{phase: "phase-188", approved: true, summary: "s",
          deferrals: [{item: "one", reason: "r", suggested_task: "t", key: "old1"},
                      {item: "two", reason: "r", suggested_task: "t", key: "old2"}]}' > artifacts/phase-approval.json
elif [ "$CALL_N" = 2 ]; then
  echo b >> src.txt
  jq -n '{phase: "phase-189", approved: true, summary: "s", closes: ["old1"]}' > artifacts/phase-approval.json
else
  echo c >> src.txt
  jq -n '{done: true, summary: "done",
          deferrals: [{item: "one", reason: "r", suggested_task: "t", key: "old1"},
                      {item: "two", reason: "r", suggested_task: "t", key: "old2"}]}' > artifacts/project-complete.json
fi
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = r.stdout + r.stderr
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual([d["key"] for d in record["deferrals"]], ["old2"], out)
        self.assertIn("were closed earlier", out)

    def test_a_model_verify_in_a_changed_environment_is_shown_never_reused(self):
        # MAJOR 6: `SKIP_E2E=1 phasekit verify` must not stand in for the gate.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        with_cli(repo)
        repo.scenario(H.APPROVE_SCENARIO + 'SKIP_E2E=1 ' + PV + ' > "$STUB_DIR/pv.out" 2>&1\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("not the loop's (SKIP_E2E)", (repo.stub / "pv.out").read_text())
        self.assertEqual(repo.verify_calls(), 2, "the commit gate ran its own")
        self.assertIn("boundary-state: rested (step 7)", r.stdout)

    def test_a_green_model_verify_never_clears_the_breakers_capture(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        with_cli(repo)
        repo.write("artifacts/phase-verify-failed.json", json.dumps({"verify_failed": True, "attempts": 1}) + "\n")
        subprocess.run(["bash", "-c", PV], cwd=repo.repo, capture_output=True, text=True, timeout=60,
                       env={**__import__("os").environ, "STUB_DIR": str(repo.stub)})
        self.assertTrue(repo.artifact("phase-verify-failed.json").exists())

    def test_prose_that_starts_with_the_words_is_not_a_prefix_and_a_bare_prefix_gets_prose(self):
        # MINOR 9.
        for msg, want in (("phase-out of legacy: drop the old API", "iteration 88 phase 188: phase-out of legacy: drop the old API"),
                          ("iteration 88 phase 188:", "iteration 88 phase 188: Create panel seat choice reaches the door")):
            with self.subTest(msg=msg):
                repo = self._repo()
                supervised(repo, 88)
                repo.scenario(APPROVE_188.replace("iteration 88 phase 188: the seat choice reaches the door", msg))
                r = repo.run(env={"MAX_ITERATIONS": "1"})
                self.assertEqual(repo.git("log", "-1", "--format=%s", "HEAD"), want)
                self.assertNotIn("commit subject corrected", r.stdout + r.stderr)

    def test_a_catch_up_squash_after_a_new_intake_keeps_the_records_iteration(self):
        # MINOR 11: the squash belongs to the record's (stamped) iteration.
        repo = self._repo(squash=True)
        supervised(repo, 88)
        repo.scenario(APPROVE_188)
        repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_BOUNDARY_KILL_PROBE": "3:post"})
        # killed after the branch commit, before the squash; a new intake lands
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 89}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "next intake")
        repo.reset_stub(); repo.scenario(H.NOTHING_SCENARIO)
        repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertEqual(trailers(repo, "main").get("Phasekit-Iteration"), "88")
        self.assertTrue(repo.git("log", "-1", "--format=%s", "main").startswith("iteration 88 phase 188:"))

    def test_an_entry_without_a_derivable_key_stays_in_the_record(self):
        # MINOR 7: it cannot enter the ledger; it is never dropped.
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario('echo w >> src.txt\n'
                      'jq -n \'{done: true, summary: "s", deferrals: [{item: "修复登录页面", reason: "r", suggested_task: "t"},'
                      ' {item: "ok thing", reason: "r", suggested_task: "t", key: "ok"}]}\' > artifacts/project-complete.json\n')
        repo.run(env={"MAX_ITERATIONS": "1"})
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual([d.get("item") for d in record["deferrals"]], ["ok thing", "修复登录页面"])


class ReviewRound2(unittest.TestCase):
    """Regressions for the v0.18.0 review, round 2."""

    def _repo(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        with_cli(repo)
        return repo

    def test_a_models_verify_never_rewrites_the_tree_and_leaves_nothing_to_settle(self):
        # BLOCKER 1: a model's gate wrote gate_pending; the next settle then
        # restored or deleted the model's own concurrent edits as footprint.
        repo = self._repo()
        repo.write("scripts/phasekit-verify.sh", H.VERIFY_LOGGING.replace(
            "exit 0\n", 'echo "gate wrote this" >> "$ROOT/notes.md"\nexit 0\n'), executable=True)
        repo.write("notes.md", "the model's notes\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "noisy gate")
        r = subprocess.run(["bash", "-c", PV], cwd=repo.repo, capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "STUB_DIR": str(repo.stub)})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("NOT restored", r.stderr)
        self.assertIn("gate wrote this", (repo.repo / "notes.md").read_text(), "nothing rewritten in a model's turn")
        rec = repo.record() or {}
        self.assertNotIn("gate_pending", rec)

    def test_the_carried_record_survives_the_verdict_retry_of_its_repair_turn(self):
        # MAJOR 3 (round 2): a per-turn drop lost the record to a retry.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.scenario(r"""
case "$CALL_N" in
1) echo feature >> src.txt; touch BAD
   echo '{"summary":"complete","suggested_commit_message":"Project complete: v1"}' > artifacts/project-complete.json ;;
2) rm -f BAD; echo fixed >> src.txt ;;
3) if [ -f artifacts/project-complete.json ]; then echo present > "$STUB_DIR/c3"; else echo GONE > "$STUB_DIR/c3"; fi
   jq '.summary += " (fixed)"' artifacts/project-complete.json > "$STUB_DIR/r" && cat "$STUB_DIR/r" > artifacts/project-complete.json ;;
esac
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        self.assertEqual((repo.stub / "c3").read_text().strip(), "present", r.stdout + r.stderr)
        self.assertIn("Run finished successfully.", r.stdout)

    def test_an_unreclaimed_carried_record_is_dropped_after_its_repair_turn_and_never_resurrected(self):
        # BLOCKER 2: the carry outlived its repair turn and the next session
        # landed it as a completion with zero model turns.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("src.txt", "base\nwork\n")
        repo.write("artifacts/project-complete.json", json.dumps({"done": True, "summary": "s"}) + "\n")
        repo.write("BAD", "red\n")
        repo.scenario('rm -f BAD\necho more >> src.txt\n'
                      'jq -n \'{phase: "phase-5", approved: true, suggested_commit_message: "Phase 5: not the end"}\''
                      ' > artifacts/phase-approval.json\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("not re-written this session — removed at exit", out)
        self.assertFalse(repo.artifact("project-complete.json").exists())
        repo.reset_stub(); repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("Project complete", repo.git("log", "--format=%s"), r.stdout)
        self.assertFalse(repo.tracked("artifacts/project-complete.json"))

    def test_a_record_deleted_after_a_verify_is_never_left_staged(self):
        # MINOR 12: `phasekit verify` staged the record; the model deleted it.
        repo = self._repo()
        repo.write("src.txt", "base\nw\n")
        repo.write("artifacts/project-complete.json", json.dumps({"done": True}) + "\n")
        subprocess.run(["bash", "-c", PV], cwd=repo.repo, capture_output=True, text=True, timeout=60,
                       env={**__import__("os").environ, "STUB_DIR": str(repo.stub)})
        repo.artifact("project-complete.json").unlink()
        self.assertFalse([ln for ln in repo.git("status", "--porcelain").splitlines() if ln.startswith("AD")])

    def test_a_tier_longer_than_a_tool_call_is_skipped_not_run(self):
        repo = self._repo()
        repo.write("artifacts/logs/cost-ledger.json", json.dumps({"schema": 1, "samples": {"g_fast": [800, 820]}}))
        r = subprocess.run(["bash", "-c", PV], cwd=repo.repo, capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "STUB_DIR": str(repo.stub)})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("skipped", r.stdout)
        self.assertEqual(repo.verify_calls(), 0)


class ReviewRound4(unittest.TestCase):
    """Regressions for the v0.18.0 review, round 4."""

    def test_a_claim_the_wip_kept_out_lands_first_beside_its_evidence_and_the_tree_rests(self):
        # §9a "§3.1 completed", with a `phasekit verify` draft left beside it
        # (MAJOR 2: the draft once defeated the claim's landing).
        repo = Repo(squash=True, gate="consistency")
        self.addCleanup(repo.cleanup)
        repo.scenario(H.APPROVE_SCENARIO)
        repo.run(env={"MAX_ITERATIONS": "1"})          # phase-1 landed and squashed, d1/d1
        # a later turn re-arms both for d2, its evidence rides a wip, the claim is kept out
        repo.write("artifacts/evidence/transcript.json", '{"digest": "d2"}\n')
        repo.write("artifacts/ready-to-deploy.json", '{"deploy_ready": true, "digest": "d2"}\n')
        repo.write("artifacts/deferrals.json", '{"schema": 1, "open": [{"key": "draft"}], "closed": []}\n')
        repo.git("add", "artifacts/evidence/transcript.json")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — test",
                 "-m", "Phasekit-Kind: wip")
        repo.reset_stub(); repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("landing it with its tree, verify-gated", out)
        self.assertIn('"d2"', repo.git("show", "HEAD:artifacts/ready-to-deploy.json"))

    def test_a_synthesized_record_that_never_landed_is_not_left_on_disk_at_exit(self):
        # MAJOR 3: a supervisor reads a record on disk at exit as a completion.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("scripts/phasekit-verify.sh", H.VERIFY_LOGGING, executable=True)
        repo.write("src.txt", "base\nwork\n")
        repo.write("artifacts/phase-approval.json", json.dumps(
            {"phase": "phase-1", "approved": True, "final_phase": True, "summary": "s"}) + "\n")
        repo.write("BAD", "red\n")
        repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("synthesized did not land — removed at exit", r.stderr, r.stdout + r.stderr)
        self.assertFalse(repo.artifact("project-complete.json").exists())
        self.assertTrue(repo.artifact("phase-approval.json").exists())


def _all_functions():
    """Every top-level function and the two signal arrays of the shipped loop,
    for driving the kill path by hand (review round 5's repro shape)."""
    src = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text().split("\n")
    out, i = [], 0
    import re as _re
    while i < len(src):
        line = src[i]
        if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", line) or _re.match(r"^(TRANSIENT_SIGNALS|HIDDEN_TRANSIENTS)=\(", line):
            j = i
            while src[j] not in ("}", ")"):
                j += 1
            out += src[i:j + 1]
            i = j + 1
            continue
        i += 1
    return "\n".join(out)


class ReviewRound5(unittest.TestCase):
    """A kill while the LOOP's own commit gate is running (T-60 inside a slow
    landing): the index is the landing in flight and is committed whole."""

    def _setup(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="pk-r5-"))
        self.addCleanup(shutil.rmtree, d, True)
        run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)
        run("git", "init", "-q", "-b", "main"); run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
        (d / "artifacts" / "logs").mkdir(parents=True)
        (d / "artifacts" / "phase-approval.json").write_text('{"phase":"1","summary":"old","suggested_commit_message":"phase 1"}\n')
        (d / "src.txt").write_text("base\n")
        run("git", "add", "-A"); run("git", "commit", "-qm", "base")
        return d

    def _script(self, d, body):
        return "\n".join([
            "set -uo pipefail", f'ROOT_DIR="{d}"', f'ARTIFACTS_DIR="{d}/artifacts"', 'SQUASH_TARGET=""',
            f'WRAPUP_SENTINEL="{d}/artifacts/wrapup-requested"', f'BOUNDARY_STATE_FILE="{d}/artifacts/boundary-state.json"',
            _all_functions(), "GATE_FOOTPRINT_RULE=r; GATE_FOOTPRINT_RECIPE=r; VERIFY_MAX_ATTEMPTS=3",
            f'cd "{d}"',
            "echo work >> src.txt",
            """echo '{"phase":"2","summary":"new","suggested_commit_message":"phase 2"}' > artifacts/phase-approval.json""",
            "git add -A",
            "fpb=$(mktemp); gate_status_snapshot $fpb; gate_pending_record $fpb cmd lbl",
            "deadline_lastresort_commit kill >/dev/null 2>&1",
            body])

    def _bash(self, script):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
        self.addCleanup(os.unlink, f.name)
        return subprocess.run(["bash", f.name], capture_output=True, text=True, timeout=120)

    def test_the_settle_never_reverts_the_sessions_approval(self):
        d = self._setup()
        r = self._bash(self._script(d, "gate_settle_pending >/dev/null 2>&1; cat artifacts/phase-approval.json"))
        self.assertIn('"phase":"2"', r.stdout, r.stdout + r.stderr)
        head = subprocess.run(["git", "show", "HEAD:artifacts/phase-approval.json"], cwd=d, capture_output=True, text=True).stdout
        self.assertIn('"phase":"2"', head, "the in-flight landing's index is committed whole")

    def test_the_live_gate_sees_no_footprint_from_the_kill_path(self):
        d = self._setup()
        r = self._bash(self._script(d, 'fpa=$(mktemp); fpp=$(mktemp); gate_status_snapshot $fpa; '
                                       'gate_footprint_diff $fpb $fpa $fpp; echo "FP=$(gate_footprint_json $fpp)"'))
        self.assertIn("FP=[]", r.stdout, r.stdout + r.stderr)


class ReviewRound6(unittest.TestCase):
    """Regressions for the v0.18.0 review, round 6."""

    def test_plain_mode_an_approval_that_rode_an_unverified_wip_meets_the_gate(self):
        # MAJOR: no catch-up squash re-verifies it in plain mode.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("src.txt", "base\nwork\n")
        repo.write("artifacts/phase-approval.json", json.dumps({"phase": "phase-2", "approved": True,
                   "suggested_commit_message": "Phase 2: x"}) + "\n")
        repo.write("BAD", "red\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — t",
                 "-m", "Phasekit-Kind: wip")
        repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("the approval rode an unverified wip — running the verify gate", out)
        self.assertIn("boundary-state: stopped at step 0", out)
        self.assertNotIn("boundary-state: rested (step 7)", out)

    def test_plain_mode_a_legacy_wip_without_trailers_meets_the_gate(self):
        # review round 9: v0.17.0 kill wips carried the approval and no trailer.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("src.txt", "base\nwork\n")
        repo.write("artifacts/phase-approval.json", json.dumps({"phase": "phase-2", "approved": True,
                   "suggested_commit_message": "Phase 2: x"}) + "\n")
        repo.write("BAD", "red\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — legacy")
        repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("running the verify gate before its boundary counts", r.stdout + r.stderr)

    def test_plain_mode_a_later_wip_does_not_hide_the_approvals_unverified_wip(self):
        # review round 7: the check follows the last commit that touched the
        # approval, not HEAD.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("src.txt", "base\nwork\n")
        repo.write("artifacts/phase-approval.json", json.dumps({"phase": "phase-2", "approved": True,
                   "suggested_commit_message": "Phase 2: x"}) + "\n")
        repo.write("BAD", "red\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — a",
                 "-m", "Phasekit-Kind: wip")
        repo.write("src.txt", "base\nwork\nmore\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — b",
                 "-m", "Phasekit-Kind: wip")
        repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("running the verify gate before its boundary counts", out)
        self.assertNotIn("boundary-state: rested (step 7)", out)

    def test_the_records_own_hyphenated_labels_are_an_agreeing_prefix(self):
        for n, phase, msg in ((88, "phase-3-a", "iteration 88 phase 3-a: real prose"),
                              (88, "phase-187", "iteration-88-phase-187: real prose")):
            with self.subTest(msg=msg):
                repo = Repo(squash=False)
                self.addCleanup(repo.cleanup)
                supervised(repo, n)
                repo.scenario(APPROVE_188.replace("phase-188", phase)
                              .replace("iteration 88 phase 188: the seat choice reaches the door", msg))
                r = repo.run(env={"MAX_ITERATIONS": "1"})
                self.assertTrue(repo.git("log", "-1", "--format=%s", "HEAD").endswith(": real prose"),
                                repo.git("log", "-1", "--format=%s", "HEAD"))
                self.assertNotIn("commit subject corrected", r.stdout + r.stderr)

    def test_a_commit_a_hook_refuses_leaves_no_verdict_staged(self):
        # MINOR 3: the no-commit path unstages the verdicts like any refusal.
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        hook = repo.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text('#!/bin/sh\n[ -f "$STUB_DIR/refused" ] && exit 0\ntouch "$STUB_DIR/refused"; exit 1\n')
        hook.chmod(0o755)
        repo.scenario('echo w >> src.txt\njq -n \'{done: true, summary: "s"}\' > artifacts/project-complete.json\n')
        repo.run(env={"MAX_ITERATIONS": "1"})
        staged = [ln for ln in repo.git("status", "--porcelain", "--untracked-files=all").splitlines()
                  if ln[:1] in ("A", "M") and ("project-complete" in ln or "deferrals" in ln)]
        self.assertEqual(staged, [])


class ReviewRound8(unittest.TestCase):
    """The one rule of round 7 — no second committer while the loop lands —
    pinned by behaviour, plus round 8's regressions."""

    def test_a_landing_marks_itself_in_flight_and_clears_the_mark(self):
        repo = Repo(squash=True)
        self.addCleanup(repo.cleanup)
        repo.scenario(H.APPROVE_SCENARIO)
        # killed inside the landing: the mark is on disk (the watchdog would stand down)
        repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_BOUNDARY_KILL_PROBE": "3:post"})
        self.assertTrue(repo.artifact("logs/.landing-in-flight").exists(), "the landing is marked in flight")
        # the next start clears the stale mark and lands; a finished landing leaves none
        repo.reset_stub(); repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("boundary-state: rested (step 7)", r.stdout, r.stdout + r.stderr)
        self.assertFalse(repo.artifact("logs/.landing-in-flight").exists())

    def test_the_kept_out_claim_lands_with_the_learnings_the_wip_kept_out(self):
        repo = Repo(squash=True, gate="consistency")
        self.addCleanup(repo.cleanup)
        repo.write("docs/LEARNINGS.md", "# Learnings\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "learnings")
        repo.scenario(H.APPROVE_SCENARIO)
        repo.run(env={"MAX_ITERATIONS": "1"})
        repo.write("artifacts/evidence/transcript.json", '{"digest": "d2"}\n')
        repo.write("artifacts/ready-to-deploy.json", '{"deploy_ready": true, "digest": "d2"}\n')
        repo.write("docs/LEARNINGS.md", "# Learnings\n- 2026-09-28: a lesson\n")
        repo.git("add", "artifacts/evidence/transcript.json")
        repo.git("commit", "-qm", "wip: last-resort deadline commit (phasekit deadline watchdog) — test",
                 "-m", "Phasekit-Kind: wip")
        repo.reset_stub(); repo.scenario(H.NOTHING_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("landing it with its tree, verify-gated", r.stdout + r.stderr)
        self.assertIn("a lesson", repo.git("show", "HEAD:docs/LEARNINGS.md"))


class CarriedCompletionRecord(unittest.TestCase):
    """§3.2 (link [F]): a red stranded landing keeps its record for the repair turn."""

    RECORD = {"done": True, "summary": "the session's own record, not an archive's",
              "suggested_commit_message": "Project complete"}

    def test_a_red_stranded_completion_keeps_its_record_on_disk_unstaged(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        # The killed session's tree: work + its completion record, uncommitted.
        repo.write("src.txt", "base\nfinal work\n")
        repo.write("artifacts/project-complete.json", json.dumps(self.RECORD) + "\n")
        repo.write("BAD", "red\n")
        # The repair turn looks at the tree it was handed, then fixes the
        # gate and re-writes the record: it lands.
        repo.scenario('git status --porcelain --untracked-files=all > "$STUB_DIR/status"\n'
                      'cp artifacts/project-complete.json "$STUB_DIR/record"\n'
                      'rm -f BAD\njq \'.summary += " (fixed)"\' artifacts/project-complete.json > "$STUB_DIR/r" '
                      '&& cat "$STUB_DIR/r" > artifacts/project-complete.json\n')
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out = r.stdout + r.stderr
        self.assertIn("boundary-state: stopped at step", out)
        status = (repo.stub / "status").read_text().splitlines()
        self.assertIn("?? artifacts/project-complete.json", status, out)
        self.assertFalse([ln for ln in status if ln[:2] in ("AD", "MD")], status)
        rec = json.loads((repo.stub / "record").read_text())
        self.assertEqual(rec["summary"], self.RECORD["summary"], "edited in place, never rebuilt")
        out = r.stdout + r.stderr
        self.assertIn("Run finished successfully.", out)
        self.assertEqual(repo.porcelain(), [], out)


class ReviewRound11Minors(unittest.TestCase):
    """v0.18.1 (queue row 1193): the four MINORs round 11 of the v0.18.0
    review deferred. Each test is RED on v0.18.0."""

    def _repo(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        with_cli(repo)
        return repo

    def _ledger(self, repo, samples):
        repo.write("artifacts/logs/cost-ledger.json", json.dumps({"schema": 1, "samples": samples}))

    def _g_fast(self, repo):
        return json.loads(repo.artifact("logs/cost-ledger.json").read_text())["samples"].get("g_fast", [])

    # (1) `phasekit verify` on a locked index -------------------------------
    def test_a_stale_index_lock_is_named_with_what_to_do_and_nothing_runs(self):
        repo = self._repo()
        lock = repo.repo / ".git" / "index.lock"
        lock.write_text("")
        r = subprocess.run(["bash", "-c", PV], cwd=repo.repo, capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "STUB_DIR": str(repo.stub)})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(str(lock), r.stderr)
        self.assertIn("rm -f", r.stderr)
        self.assertIn("phasekit verify: not run", r.stderr)
        self.assertEqual(repo.verify_calls(), 0)
        self.assertTrue(lock.exists(), "a model's verify never removes a lock another git may hold")

    # (2) which gate runs are cost samples ----------------------------------
    def test_a_fast_failing_red_never_lowers_the_gate_estimate(self):
        repo = self._repo()
        self._ledger(repo, {"g_fast": [100, 100, 100, 100, 100]})
        repo.write("BAD", "red\n")
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertGreaterEqual(repo.verify_calls(), 1, r.stdout + r.stderr)
        self.assertEqual(self._g_fast(repo), [100, 100, 100, 100, 100], r.stdout + r.stderr)

    def test_a_red_run_longer_than_the_estimate_raises_it(self):
        repo = self._repo()
        self._ledger(repo, {"g_fast": [0, 0, 0]})
        repo.write("scripts/phasekit-verify.sh", H.VERIFY_LOGGING.replace(
            'if [ -f "$ROOT/BAD" ]; then exit 1; fi', 'if [ -f "$ROOT/BAD" ]; then sleep 2; exit 1; fi'),
            executable=True)
        repo.git("add", "-A"); repo.git("commit", "-qm", "a slow red gate")
        repo.write("BAD", "red\n")
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        g = self._g_fast(repo)
        self.assertEqual(len(g), 4, f"{g}\n{r.stdout + r.stderr}")
        self.assertGreaterEqual(g[-1], 2)

    def test_a_green_run_is_always_a_sample(self):
        repo = self._repo()
        self._ledger(repo, {"g_fast": [100, 100, 100]})
        repo.scenario(H.APPROVE_SCENARIO)
        repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertEqual(len(self._g_fast(repo)), 4)

    # (4) every green path clears LAST_GATE_RED ------------------------------
    def _gate_fns(self):
        return "\n".join(H._extract_block(rf"^{name}\(\) \{{", r"^\}") + "\n}"
                         for name in ("run_contracts_gate", "_clear_verify_failed", "verify_command_resolve", "run_verify_gate"))

    def _last_gate_red_after(self, root, env_line):
        script = (f'set -euo pipefail\nROOT_DIR="{root}"\nARTIFACTS_DIR="$ROOT_DIR/artifacts"\n'
                  + self._gate_fns() + "\nLAST_GATE_RED=1\n" + env_line
                  + "\nrun_verify_gate >/dev/null 2>&1 || true\necho \"LGR=$LAST_GATE_RED\"\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() + r.stderr

    def test_a_green_early_return_clears_the_last_gates_red(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="pk-lgr-")
        self.addCleanup(shutil.rmtree, root, True)
        (Path(root) / "artifacts").mkdir()
        self.assertEqual(self._last_gate_red_after(root, "VERIFY_SKIP=1"), "LGR=0")
        self.assertEqual(self._last_gate_red_after(root, ""), "LGR=0", "no gate configured is green")
        self.assertEqual(self._last_gate_red_after(root, "VERIFY_SKIP=1; VERIFY_INVOKER=model"), "LGR=1",
                         "a model's own verify never speaks for the loop's gate")

    def test_every_green_return_of_the_gate_runs_through_the_clear(self):
        fn = H._extract_block(r"^run_verify_gate\(\) \{", r"^\}").splitlines()
        returns = [i for i, ln in enumerate(fn) if ln.strip() == "return 0"]
        self.assertTrue(returns)
        for i in returns:
            self.assertTrue(any("_clear_verify_failed" in ln for ln in fn[max(0, i - 4):i]), fn[i - 4:i + 1])


if __name__ == "__main__":
    unittest.main()
