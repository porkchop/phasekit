"""v0.6.6 loop checks: shared post-verify commit gates + add-A-proof heal.

Two defects from the v0.4.8→v0.6.5 ultrareview (2026-08-14):
- wrapup_commit reimplemented the commit sequence and silently dropped the
  post-verify gates — above all the docs/LEARNINGS.md credential scan — so a
  wrap-up could land a commit an identical iteration commit would refuse.
- The deferred v0.6.5 heal (dirty index at loop start) was cancelled by the
  commit path's own `git add -A` whenever the legacy tracked transient was
  still on disk (phase-verify-failed.json in a continued session): add -A
  re-added the on-disk copy, and unstage_transient_adds deliberately skipped
  members present in HEAD.

Same two layers as test_run_until_done_v060: structural pins + functional
runs of the real bash loop against a stub run-phase.sh.
"""

import os
import subprocess
import unittest

try:  # discover-style (CI: -s tests puts tests/ on sys.path)
    from test_run_until_done_v060 import LoopHarness, VERIFY_OK
except ImportError:  # package-style (python3 -m unittest tests.test_…)
    from tests.test_run_until_done_v060 import LoopHarness, VERIFY_OK

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "run-until-done.sh")

# Credential-shaped but obviously fake; assembled so secret scanners aimed at
# this repo never trip on the test file itself.
FAKE_KEY = "sk-ant-" + "x" * 16


class LoopV066StructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(LOOP_SCRIPT) as f:
            self.text = f.read()

    def _fn(self, name: str) -> str:
        return self.text.split(name + "() {", 1)[1].split("\n}", 1)[0]

    def test_parses(self) -> None:
        r = subprocess.run(["bash", "-n", LOOP_SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_post_verify_gates_shared_by_both_commit_paths(self) -> None:
        self.assertIn("post_verify_commit_gates() {", self.text)
        self.assertIn("post_verify_commit_gates iteration", self._fn("commit_from_artifact"))
        self.assertIn("post_verify_commit_gates wrapup", self._fn("wrapup_commit"))

    def test_learnings_scan_single_source(self) -> None:
        # The credential regex must exist exactly once — a second copy is the
        # drift that produced the wrap-up bypass.
        self.assertEqual(self.text.count("sk-ant-"), 1)
        self.assertIn("sk-ant-", self._fn("post_verify_commit_gates"))

    def test_security_pair_single_source(self) -> None:
        self.assertIn("staged_touches_security_pair() {", self.text)
        pair_re = r"^\.claude/settings\.json$|^\.github/workflows/"
        self.assertEqual(self.text.count(pair_re), 1)
        self.assertIn("staged_touches_security_pair", self._fn("commit_from_artifact"))
        self.assertIn("staged_touches_security_pair", self._fn("wrapup_commit"))

    def test_heal_restaged_after_add_a(self) -> None:
        # A legacy tracked member must be re-untracked AFTER git add -A, not
        # trusted to survive it.
        self.assertIn("git rm --cached", self._fn("unstage_transient_adds"))


class LoopV066WrapupGatesTest(LoopHarness):
    # --- wrap-up runs the same post-verify gates as an iteration commit ----

    def test_wrapup_refuses_learnings_secret(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._write("docs/LEARNINGS.md", "# Learnings\n")
        scenario = (
            "echo w >> src.txt\n"
            f"echo '- 2026-08-14: pasted error echoing {FAKE_KEY}' >> docs/LEARNINGS.md\n"
            "touch artifacts/wrapup-requested\n"
            "exit 7\n"
        )
        self._prepare_scenario(scenario)
        before = self._messages()
        r = self._run_loop(None, env={"PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stdout + r.stderr)
        self.assertEqual(before, self._messages())  # nothing landed in git
        # The refusal leaves a handoff baton so the next session isn't blind.
        self.assertTrue(os.path.exists(
            os.path.join(self.repo, "artifacts", "session-handoff.json")))

    def test_wrapup_commits_benign_learnings(self) -> None:
        # Control: the shared gates must not block an ordinary wrap-up.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._write("docs/LEARNINGS.md", "# Learnings\n")
        scenario = (
            "echo w >> src.txt\n"
            "echo '- 2026-08-14: jq chokes on NUL bytes in log tails' >> docs/LEARNINGS.md\n"
            "touch artifacts/wrapup-requested\n"
            "exit 7\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("session wrap-up", self._messages())
        committed = self._git("show", "--name-only", "--format=", "HEAD")
        self.assertIn("docs/LEARNINGS.md", committed)

    def test_wrapup_records_spec_change(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._write("docs/SPEC.md", "# Spec\n")
        scenario = (
            "echo 'new acceptance criterion' >> docs/SPEC.md\n"
            "touch artifacts/wrapup-requested\n"
            "exit 7\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("session wrap-up", self._messages())
        spec_change = os.path.join(self.repo, "artifacts", "spec-change.json")
        self.assertTrue(os.path.exists(spec_change))
        self.assertNotIn("artifacts/spec-change.json", self._git("ls-files"))

    def test_iteration_commit_still_refuses_learnings_secret(self) -> None:
        # Regression guard on the extraction: the iteration path must refuse
        # exactly as before the gates moved into the shared helper.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._write("docs/LEARNINGS.md", "# Learnings\n")
        scenario = (
            "echo w >> src.txt\n"
            f"echo '- leak {FAKE_KEY}' >> docs/LEARNINGS.md\n"
            "jq -n '{suggested_commit_message: \"phase-2: leaky\"}'"
            " > artifacts/phase-approval.json\n"
        )
        self._prepare_scenario(scenario)
        before = self._messages()
        r = self._run_loop(None, env={"MAX_ITERATIONS": "1"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stdout + r.stderr)
        self.assertIn("credential pattern", r.stdout + r.stderr)
        self.assertEqual(before, self._messages())


class LoopV066DeferredHealTest(LoopHarness):
    # --- the deferred heal must survive `git add -A` -----------------------

    TRANSIENT = '{"verify_failed": true, "attempts": 1}\n'
    COMPLETE_SCENARIO = (
        "echo w >> src.txt\n"
        "jq -n '{suggested_commit_message: \"final: done\"}'"
        " > artifacts/project-complete.json\n"
    )

    def _seed_legacy_tracked_on_disk(self, scenario: str) -> None:
        # Legacy history tracks phase-verify-failed.json; the file is STILL on
        # disk (a continued session, so the fresh-kickoff reset never ran);
        # unrelated work is already staged so the loop-start heal defers.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._write("artifacts/phase-verify-failed.json", self.TRANSIENT)
        self._prepare_scenario(scenario)  # commits it (legacy tracked)
        self._write("src.txt", "base\nwip\n")
        self._git("add", "src.txt")

    def test_deferred_heal_survives_iteration_commit(self) -> None:
        self._seed_legacy_tracked_on_disk(self.COMPLETE_SCENARIO)
        r = self._run_loop(None, env={"CLAUDE_MODE": "continue"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # The deferred branch was actually taken (not the immediate heal).
        self.assertIn("ride with the session's next commit", r.stdout)
        self.assertIn("final: done", self._messages())
        # The untracking rode into the commit: not in HEAD, tree fully clean.
        self.assertNotIn("artifacts/phase-verify-failed.json", self._git("ls-files"))
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_deferred_heal_survives_wrapup_commit(self) -> None:
        scenario = (
            "echo w >> src.txt\n"
            "touch artifacts/wrapup-requested\n"
            "exit 7\n"
        )
        self._seed_legacy_tracked_on_disk(scenario)
        r = self._run_loop(None, env={"CLAUDE_MODE": "continue",
                                      "PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ride with the session's next commit", r.stdout)
        self.assertIn("session wrap-up", self._messages())
        self.assertNotIn("artifacts/phase-verify-failed.json", self._git("ls-files"))
        self.assertEqual(self._git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
