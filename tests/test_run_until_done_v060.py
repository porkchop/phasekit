"""v0.6.x loop checks: light mode, commit atomicity, wrap-up, pacing, handoff.

Two layers:
- Structural pins on run-until-done.sh / container-setup.sh (delete a
  load-bearing behavior and a pin fails).
- Functional tests that run the real bash loop in a fixture git repo with a
  stub run-phase.sh playing a scripted scenario per iteration — no claude CLI,
  no network (PHASEKIT_NO_UPDATE_CHECK=1).
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "run-until-done.sh")
CONTAINER_SCRIPT = os.path.join(REPO_ROOT, "scripts", "container-setup.sh")

HAVE_TOOLS = all(shutil.which(c) for c in ("bash", "git", "jq"))

STUB_RUN_PHASE = """#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
: "${STUB_DIR:?stub state dir must be set}"
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
printf 'call=%s iter=%s model=%s mode=%s\\n' \
  "$n" "${PHASEKIT_ITER:-}" "${ANTHROPIC_MODEL-__unset__}" "${CLAUDE_MODE:-}" \
  >> "$STUB_DIR/log"
cp "$1" "$STUB_DIR/prompt-$n.txt"
sleep 0.05
CALL_N="$n" bash "$ROOT_DIR/scripts/scenario.sh"
"""

VERIFY_OK = "#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\nexit 0\n"
VERIFY_FAIL = "#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\nexit 1\n"
VERIFY_STUB = "#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=0\nexit 0\n"
# Fails while a file named BAD exists in the repo root.
VERIFY_BAD_FILE = (
    "#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\n"
    'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
    'if [ -f "$ROOT/BAD" ]; then exit 1; fi\nexit 0\n'
)


class LoopStructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(LOOP_SCRIPT) as f:
            self.text = f.read()

    def test_parses(self) -> None:
        r = subprocess.run(["bash", "-n", LOOP_SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_atomicity_marker_gates_approval_commits(self) -> None:
        self.assertIn('-nt "$ITER_START_MARKER"', self.text)
        self.assertIn("artifact_written_this_iteration", self.text)
        self.assertIn("PENDING_COMMIT_RETRY", self.text)

    def test_wrapup_sentinel(self) -> None:
        self.assertIn("wrapup-requested", self.text)
        self.assertIn("wrapup_commit", self.text)
        self.assertIn("PHASEKIT_WRAPUP_SENTINEL", self.text)

    def test_light_mode_pins(self) -> None:
        self.assertIn("PHASEKIT_ITERATION_MODE", self.text)
        self.assertIn("light-escalation.json", self.text)
        self.assertIn("light_verify_configured", self.text)
        # Eligibility rejects the stub sentinel (quoted pattern, not a live line).
        self.assertIn("PHASEKIT_VERIFY_CONFIGURED=0' ", self.text)
        # Model split: review pass drops ANTHROPIC_MODEL for the CLI default.
        self.assertIn('ANTHROPIC_MODEL=""', self.text)
        # The stub sentinel must never appear at line start here — it would
        # confuse greps aimed at verify scripts (stub-reseed machinery).
        for line in self.text.splitlines():
            self.assertFalse(line.startswith("PHASEKIT_VERIFY_CONFIGURED=0"))

    def test_light_escalation_is_transient(self) -> None:
        # cleanup_artifacts must clear a stale escalation record on re-runs.
        cleanup = self.text.split("cleanup_artifacts() {", 1)[1].split("}", 1)[0]
        self.assertIn("light-escalation.json", cleanup)

    def test_deadline_pacing_pins(self) -> None:
        self.assertIn("PHASEKIT_SESSION_DEADLINE", self.text)
        self.assertIn("deadline pacing: not starting iteration", self.text)
        # 1.2x average pass duration, integer arithmetic.
        self.assertIn("pass_elapsed_total * 12 / (passes_done * 10)", self.text)

    def test_stranded_recovery_pins(self) -> None:
        # v0.6.3: a stranded (written-but-never-committed) approval/completion
        # is recovered mechanically at loop start. Detection is git-status
        # based, not mtime based (clones/rsync skew mtimes).
        self.assertIn("artifact_never_landed", self.text)
        self.assertIn("--ignored=matching", self.text)
        self.assertIn("Stranded phase-approval.json", self.text)
        self.assertIn("Stranded project-complete.json", self.text)

    def test_verify_budget_advisory_pins(self) -> None:
        # v0.6.4: fail-open budget advisory — tunable ceiling, fires once per
        # session after 2+ over-budget runs, points at the doctrine.
        self.assertIn("PHASEKIT_VERIFY_BUDGET_SECONDS", self.text)
        self.assertIn("ADVISORY: verify exceeded its budget", self.text)
        self.assertIn("'Verify budget' for the fast/slow split", self.text)
        self.assertIn("VERIFY_OVER_BUDGET_RUNS >= 2 && VERIFY_BUDGET_ADVISED == 0",
                      self.text)

    def test_session_handoff_pins(self) -> None:
        self.assertIn("write_session_handoff", self.text)
        self.assertIn("session-handoff.json", self.text)
        # cleanup_artifacts must NOT delete the baton (comment lines aside).
        cleanup = self.text.split("cleanup_artifacts() {", 1)[1].split("}", 1)[0]
        rm_lines = [ln for ln in cleanup.splitlines()
                    if "ARTIFACTS_DIR/" in ln and not ln.strip().startswith("#")]
        self.assertTrue(rm_lines)
        self.assertFalse(any("session-handoff.json" in ln for ln in rm_lines))


class ContinuePromptStructuralTest(unittest.TestCase):
    def test_orientation_reads_then_deletes_handoff(self) -> None:
        with open(os.path.join(REPO_ROOT, "CONTINUE_PROMPT.txt")) as f:
            text = f.read()
        self.assertIn("session-handoff.json", text)
        self.assertIn("DELETE the file after orienting", text)


class ContainerStructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(CONTAINER_SCRIPT) as f:
            self.text = f.read()

    def test_parses(self) -> None:
        r = subprocess.run(["bash", "-n", CONTAINER_SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_forwards_iteration_mode(self) -> None:
        self.assertIn('-e PHASEKIT_ITERATION_MODE="$PHASEKIT_ITERATION_MODE"', self.text)

    def test_max_iterations_forwarded_only_when_set(self) -> None:
        # An unconditional -e MAX_ITERATIONS=50 would defeat light mode's cap.
        self.assertNotIn('-e MAX_ITERATIONS="${MAX_ITERATIONS:-50}"', self.text)
        self.assertIn('-e MAX_ITERATIONS="$MAX_ITERATIONS"', self.text)

    def test_forwards_session_deadline(self) -> None:
        self.assertIn('-e PHASEKIT_SESSION_DEADLINE="$PHASEKIT_SESSION_DEADLINE"',
                      self.text)


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq required")
class LoopHarness(unittest.TestCase):
    """Fixture repo + stub run-phase.sh; subclasses hold the actual tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pk-v060-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.stub_dir = os.path.join(self.tmp, "stub")
        os.makedirs(os.path.join(self.repo, "scripts"))
        os.makedirs(os.path.join(self.repo, "artifacts"))
        os.makedirs(os.path.join(self.repo, "docs"))
        os.makedirs(self.stub_dir)
        shutil.copy(LOOP_SCRIPT, os.path.join(self.repo, "scripts", "run-until-done.sh"))
        self._write("scripts/run-phase.sh", STUB_RUN_PHASE, executable=True)
        self._write("CONTINUE_PROMPT.txt", "standard continue prompt\n")
        self._write("docs/PHASES.md", "# Phases\n")
        self._write("src.txt", "base\n")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@test")
        self._git("config", "user.name", "test")
        self._git("add", "-A")
        self._git("commit", "-qm", "initial")

    def _write(self, rel: str, content: str, executable: bool = False) -> None:
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        if executable:
            os.chmod(path, 0o755)

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def _prepare_scenario(self, scenario: str) -> None:
        self._write("scripts/scenario.sh", scenario)
        self._git("add", "-A")
        self._git("commit", "-qm", "fixture setup")

    def _run_loop(self, scenario: str | None, env: dict | None = None) -> subprocess.CompletedProcess:
        if scenario is not None:
            self._prepare_scenario(scenario)
        run_env = dict(os.environ)
        for var in ("ANTHROPIC_MODEL", "PHASEKIT_ITERATION_MODE", "MAX_ITERATIONS",
                    "VERIFY_MAX_ATTEMPTS", "CLAUDE_MODE", "AUTO_PUSH",
                    "PHASEKIT_VERIFY_CMD", "VERIFY_SKIP", "PHASEKIT_WRAPUP_SENTINEL",
                    "PHASEKIT_SESSION_DEADLINE", "PHASEKIT_PACING_FLOOR_SECONDS",
                    "PHASEKIT_VERIFY_BUDGET_SECONDS"):
            run_env.pop(var, None)
        run_env.update({
            "PHASEKIT_NO_UPDATE_CHECK": "1",
            "PHASEKIT_ITER_RETRY": "0",
            "STUB_DIR": self.stub_dir,
        })
        run_env.update(env or {})
        return subprocess.run(
            ["bash", os.path.join(self.repo, "scripts", "run-until-done.sh")],
            cwd=self.repo, env=run_env, capture_output=True, text=True, timeout=120)

    def _calls(self) -> int:
        path = os.path.join(self.stub_dir, "calls")
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return int(f.read().strip())

    def _stub_log(self) -> list:
        with open(os.path.join(self.stub_dir, "log")) as f:
            return f.read().splitlines()

    def _messages(self) -> str:
        return self._git("log", "--format=%s")

    def _prompt(self, n: int) -> str:
        with open(os.path.join(self.stub_dir, f"prompt-{n}.txt")) as f:
            return f.read()


class LoopFunctionalTest(LoopHarness):
    # --- phase-commit atomicity ------------------------------------------

    def test_stale_approval_never_drives_a_commit(self) -> None:
        # The durable record of a previously approved phase sits on disk; an
        # iteration that does work but writes NO artifact must not get that
        # work committed under the stale phase's message (the dashboard-
        # documented defect), and must trip the loop contract instead.
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "OLD PHASE MESSAGE"}\n')
        self._prepare_scenario('echo "in-flight work" >> src.txt\n')
        before = self._messages()
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("No expected artifact", r.stdout)
        self.assertIn("predates this iteration", r.stdout)
        self.assertEqual(before, self._messages())
        self.assertNotIn("OLD PHASE MESSAGE", self._messages())

    def test_fresh_approval_commits_under_its_own_message(self) -> None:
        scenario = (
            'case "$CALL_N" in\n'
            "  1) echo w1 >> src.txt\n"
            "     jq -n '{suggested_commit_message: \"phase-2: real work\"}'"
            " > artifacts/phase-approval.json ;;\n"
            "  2) echo w2 >> src.txt\n"
            "     jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("phase-2: real work", self._messages())
        self.assertIn("final: done", self._messages())

    def test_verify_retry_commits_failed_phase_without_rewritten_artifact(self) -> None:
        # Verify fails on the approval commit; next iteration fixes the code
        # but "forgets" to re-touch the artifact. The pending-retry path must
        # still commit — under the SAME phase's message.
        self._write("scripts/phasekit-verify.sh", VERIFY_BAD_FILE, executable=True)
        scenario = (
            'case "$CALL_N" in\n'
            "  1) echo w1 >> src.txt; touch BAD\n"
            "     jq -n '{suggested_commit_message: \"phase-2: gated work\"}'"
            " > artifacts/phase-approval.json ;;\n"
            "  2) rm -f BAD ;;\n"
            "  3) jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("phase-2: gated work", self._messages())
        self.assertNotIn("BAD", self._git("ls-files"))

    # --- soft wrap-up ------------------------------------------------------

    def test_wrapup_sentinel_stops_loop_after_commit(self) -> None:
        scenario = (
            "echo w >> src.txt\n"
            "jq -n '{suggested_commit_message: \"phase-2: work\"}'"
            " > artifacts/phase-approval.json\n"
            "touch artifacts/wrapup-requested\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Wrap-up requested", r.stdout)
        self.assertIn("tree already clean", r.stdout)
        self.assertEqual(self._calls(), 1)
        self.assertIn("phase-2: work", self._messages())
        self.assertFalse(os.path.exists(
            os.path.join(self.repo, "artifacts", "wrapup-requested")))

    def test_stale_wrapup_sentinel_cleared_at_start(self) -> None:
        self._write("artifacts/wrapup-requested", "")
        scenario = (
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Clearing stale wrap-up sentinel", r.stdout)
        self.assertEqual(self._calls(), 1)

    def test_wrapup_never_commits_failing_verify(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_BAD_FILE, executable=True)
        scenario = (
            "echo w >> src.txt; touch BAD\n"
            "jq -n '{suggested_commit_message: \"phase-2: red\"}'"
            " > artifacts/phase-approval.json\n"
            "touch artifacts/wrapup-requested\n"
        )
        self._prepare_scenario(scenario)
        before = self._messages()
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Wrap-up: verify failed", r.stdout + r.stderr)
        self.assertEqual(before, self._messages())

    # --- light execution mode ---------------------------------------------

    def test_light_refused_on_stub_verify(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_STUB, executable=True)
        scenario = (
            "echo w >> src.txt\n"
            "jq -n '{suggested_commit_message: \"done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITERATION_MODE": "light"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("running standard mode instead", r.stdout)
        self.assertEqual(self._calls(), 1)  # no review pass in standard mode
        self.assertNotIn("PHASEKIT LIGHT MODE", self._prompt(1))

    def test_light_completes_with_default_model_review(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        scenario = (
            'case "$CALL_N" in\n'
            "  1) echo w >> src.txt\n"
            "     jq -n '{suggested_commit_message: \"light: done\"}'"
            " > artifacts/project-complete.json ;;\n"
            "  2) jq -n '{suggested_commit_message: \"light: done (reviewed)\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        r = self._run_loop(scenario, env={
            "PHASEKIT_ITERATION_MODE": "light",
            "ANTHROPIC_MODEL": "claude-sonnet-5",
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self._stub_log()
        self.assertEqual(len(log), 2)
        self.assertIn("model=claude-sonnet-5", log[0])
        # Review pass: fresh session, model dropped to the CLI default.
        self.assertIn("iter=light-review", log[1])
        self.assertIn("model= ", log[1])
        self.assertIn("mode=new", log[1])
        self.assertIn("PHASEKIT LIGHT MODE", self._prompt(1))
        self.assertIn("FINAL REVIEWER", self._prompt(2))
        self.assertIn("light: done (reviewed)", self._messages())

    def test_light_blocked_escalates(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        scenario = (
            "jq -n '{blocked: true, reason: \"needs a schema change\"}'"
            " > artifacts/phase-blocked.json\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITERATION_MODE": "light"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LIGHT ESCALATION", r.stdout + r.stderr)
        esc = os.path.join(self.repo, "artifacts", "light-escalation.json")
        self.assertTrue(os.path.exists(esc))
        with open(esc) as f:
            content = f.read()
        self.assertIn('"phase_blocked"', content)
        self.assertIn("needs a schema change", content)

    def test_light_two_verify_failures_escalate(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_FAIL, executable=True)
        scenario = (
            'case "$CALL_N" in\n'
            "  1) echo w >> src.txt\n"
            "     jq -n '{suggested_commit_message: \"light try1\"}'"
            " > artifacts/project-complete.json ;;\n"
            "  2) jq -n '{suggested_commit_message: \"light reviewed\"}'"
            " > artifacts/project-complete.json ;;\n"
            "  3) echo w2 >> src.txt\n"
            "     jq -n '{suggested_commit_message: \"light try2\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        self._prepare_scenario(scenario)
        before = self._messages()
        r = self._run_loop(None, env={"PHASEKIT_ITERATION_MODE": "light"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self._calls(), 3)
        esc = os.path.join(self.repo, "artifacts", "light-escalation.json")
        with open(esc) as f:
            self.assertIn('"verify_failures"', f.read())
        self.assertEqual(before, self._messages())

    def test_light_iteration_cap_escalates(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        scenario = (
            'echo "w$CALL_N" >> src.txt\n'
            "jq -n --arg n \"$CALL_N\" '{suggested_commit_message: (\"update \" + $n)}'"
            " > artifacts/phase-update.json\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITERATION_MODE": "light"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self._calls(), 2)
        esc = os.path.join(self.repo, "artifacts", "light-escalation.json")
        with open(esc) as f:
            self.assertIn('"iteration_cap"', f.read())


class LoopV061FunctionalTest(LoopHarness):
    # --- deadline-aware pacing --------------------------------------------

    def test_pacing_refuses_start_below_floor(self) -> None:
        # Deadline closer than the 3-minute floor: the loop must not start
        # iteration 1 at all — wrap-up path, clean exit, zero claude calls.
        scenario = 'echo ran > "$STUB_DIR/ran"\n'
        deadline = int(time.time()) + 60
        r = self._run_loop(scenario,
                           env={"PHASEKIT_SESSION_DEADLINE": str(deadline)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deadline pacing: not starting iteration 1", r.stdout)
        self.assertIn("Run wrapped up cleanly (deadline pacing).", r.stdout)
        self.assertEqual(self._calls(), 0)

    def test_far_deadline_changes_nothing(self) -> None:
        scenario = (
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        deadline = int(time.time()) + 3600
        r = self._run_loop(scenario,
                           env={"PHASEKIT_SESSION_DEADLINE": str(deadline)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("deadline pacing", r.stdout)
        self.assertEqual(self._calls(), 1)

    def test_malformed_deadline_ignored(self) -> None:
        scenario = (
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario,
                           env={"PHASEKIT_SESSION_DEADLINE": "five-minutes"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ignoring non-numeric PHASEKIT_SESSION_DEADLINE",
                      r.stdout + r.stderr)
        self.assertEqual(self._calls(), 1)

    # --- wrap-up handoff baton --------------------------------------------

    def test_handoff_committed_when_wrapup_rescues_interrupted_work(self) -> None:
        # claude "dies" mid-iteration after doing verify-green work; the
        # supervisor's sentinel arrives; the retry pass hits the wrap-up,
        # which must commit the standing work WITH the handoff baton inside.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        scenario = (
            "echo rescued-work >> src.txt\n"
            "touch artifacts/wrapup-requested\n"
            "exit 7\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Wrap-up requested", r.stdout)
        self.assertIn("session wrap-up", self._messages())
        committed = self._git("show", "--name-only", "--format=", "HEAD")
        self.assertIn("src.txt", committed)
        self.assertIn("artifacts/session-handoff.json", committed)
        with open(os.path.join(self.repo, "artifacts", "session-handoff.json")) as f:
            handoff = f.read()
        self.assertIn('"verified": true', handoff)
        self.assertIn("src.txt", handoff)

    def test_handoff_uncommitted_on_wrapup_verify_failure(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_BAD_FILE, executable=True)
        scenario = (
            "echo w >> src.txt; touch BAD\n"
            "jq -n '{phase: \"phase-9\", suggested_commit_message: \"phase-9: red\"}'"
            " > artifacts/phase-approval.json\n"
            "touch artifacts/wrapup-requested\n"
        )
        self._prepare_scenario(scenario)
        before = self._messages()
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(before, self._messages())  # nothing committed
        with open(os.path.join(self.repo, "artifacts", "session-handoff.json")) as f:
            handoff = f.read()
        self.assertIn('"verified": false', handoff)
        self.assertIn("phase-9", handoff)
        self.assertIn("phase-verify-failed.json", handoff)

    def test_handoff_survives_cleanup_into_next_session(self) -> None:
        # A baton left by a prior session must still exist when the next
        # session's first iteration runs (cleanup_artifacts leaves it alone);
        # the model, not the wrapper, deletes it after orienting.
        self._write("artifacts/session-handoff.json",
                    '{"stopped_at_phase": "phase-3"}\n')
        scenario = (
            "if [ -f artifacts/session-handoff.json ];"
            ' then echo yes > "$STUB_DIR/handoff-seen"; fi\n'
            "rm -f artifacts/session-handoff.json\n"
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.stub_dir, "handoff-seen")))


class LoopV063FunctionalTest(LoopHarness):
    # --- stranded-artifact recovery ---------------------------------------

    def test_stranded_approval_committed_at_first_boundary(self) -> None:
        # The live 2026-08-11 pathology: a prior session was killed after
        # writing phase-approval.json but before its commit. The next session's
        # model re-validates and writes nothing; the loop must still land the
        # stranded work — under the approval's own message — at the first
        # iteration boundary.
        scenario = (
            'case "$CALL_N" in\n'
            "  1) : ;;\n"  # model re-validates, writes no artifact
            "  2) jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json ;;\n"
            "esac\n"
        )
        self._prepare_scenario(scenario)
        # Simulate the guillotine: work + approval on disk, never committed.
        self._write("src.txt", "base\nstranded work\n")
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "phase-7: stranded"}\n')
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Stranded phase-approval.json", r.stdout)
        self.assertIn("phase-7: stranded", self._messages())
        self.assertIn("final: done", self._messages())
        committed = self._git("show", "--name-only", "--format=",
                              "HEAD~1")
        self.assertIn("src.txt", committed)

    def test_landed_approval_with_dirty_tree_not_retried(self) -> None:
        # The inverse guard: an approval that DID land (clean in git status)
        # must never drive a commit of later in-flight work, even with a dirty
        # tree at startup — that is exactly the v0.6.0 wrong-message bug.
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "OLD PHASE MESSAGE"}\n')
        self._prepare_scenario('echo w >> src.txt\n')  # commits the approval
        self._write("src.txt", "base\nleftover in-flight work\n")
        before = self._messages()
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Stranded", r.stdout)
        self.assertIn("No expected artifact", r.stdout)
        self.assertEqual(before, self._messages())
        self.assertNotIn("OLD PHASE MESSAGE", self._messages())

    def test_landed_approval_clean_tree_unchanged(self) -> None:
        # Clean tree + committed (stale) approval: no recovery triggers, the
        # run proceeds exactly as before.
        self._write("artifacts/phase-approval.json",
                    '{"suggested_commit_message": "OLD PHASE MESSAGE"}\n')
        scenario = (
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Stranded", r.stdout)
        self.assertEqual(self._calls(), 1)

    def test_stranded_completion_commits_without_claude(self) -> None:
        # A stranded project-complete.json would be deleted by iteration 1's
        # cleanup_artifacts and silently re-done; the startup recovery must
        # land it (verify-gated) with zero claude calls.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self._prepare_scenario('echo ran > "$STUB_DIR/ran"\n')
        self._write("src.txt", "base\nfinished work\n")
        self._write("artifacts/project-complete.json",
                    '{"suggested_commit_message": "final: stranded completion"}\n')
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Stranded project-complete.json", r.stdout)
        self.assertEqual(self._calls(), 0)
        self.assertIn("final: stranded completion", self._messages())
        committed = self._git("show", "--name-only", "--format=", "HEAD")
        self.assertIn("src.txt", committed)
        self.assertIn("artifacts/project-complete.json", committed)

    def test_stranded_completion_verify_failure_reenters_loop(self) -> None:
        # Startup recovery never lowers the bar: if the stranded completion
        # fails verify, no commit is made and the loop runs so the model can
        # fix and re-complete.
        self._write("scripts/phasekit-verify.sh", VERIFY_BAD_FILE, executable=True)
        scenario = (
            "rm -f BAD\n"
            "jq -n '{suggested_commit_message: \"final: fixed\"}'"
            " > artifacts/project-complete.json\n"
        )
        self._prepare_scenario(scenario)
        self._write("BAD", "")
        self._write("artifacts/project-complete.json",
                    '{"suggested_commit_message": "final: stranded red"}\n')
        r = self._run_loop(None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Stranded project-complete.json", r.stdout)
        self.assertIn("did not pass the commit gates", r.stdout + r.stderr)
        self.assertEqual(self._calls(), 1)
        self.assertIn("final: fixed", self._messages())
        self.assertNotIn("final: stranded red", self._messages())
        self.assertNotIn("BAD", self._git("ls-files"))


class LoopV064FunctionalTest(LoopHarness):
    # --- verify-budget advisory (fail-open) --------------------------------

    ADVISORY = "ADVISORY: verify exceeded its budget"

    TWO_COMMIT_SCENARIO = (
        'case "$CALL_N" in\n'
        "  1) echo w1 >> src.txt\n"
        "     jq -n '{suggested_commit_message: \"phase-2: work\"}'"
        " > artifacts/phase-approval.json ;;\n"
        "  2) echo w2 >> src.txt\n"
        "     jq -n '{suggested_commit_message: \"final: done\"}'"
        " > artifacts/project-complete.json ;;\n"
        "esac\n"
    )

    SLOW_VERIFY = (
        "#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\nsleep 2\nexit 0\n"
    )

    def test_advisory_fires_once_after_two_over_budget_runs(self) -> None:
        self._write("scripts/phasekit-verify.sh", self.SLOW_VERIFY, executable=True)
        r = self._run_loop(self.TWO_COMMIT_SCENARIO,
                           env={"PHASEKIT_VERIFY_BUDGET_SECONDS": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((r.stdout + r.stderr).count(self.ADVISORY), 1)
        self.assertIn("s > 1s", r.stdout)
        # Fail-open: both commits still landed.
        self.assertIn("phase-2: work", self._messages())
        self.assertIn("final: done", self._messages())

    def test_advisory_silent_on_single_over_budget_run(self) -> None:
        self._write("scripts/phasekit-verify.sh", self.SLOW_VERIFY, executable=True)
        scenario = (
            "echo w >> src.txt\n"
            "jq -n '{suggested_commit_message: \"final: done\"}'"
            " > artifacts/project-complete.json\n"
        )
        r = self._run_loop(scenario, env={"PHASEKIT_VERIFY_BUDGET_SECONDS": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.ADVISORY, r.stdout + r.stderr)

    def test_advisory_silent_under_budget(self) -> None:
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        r = self._run_loop(self.TWO_COMMIT_SCENARIO)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.ADVISORY, r.stdout + r.stderr)

    def test_malformed_budget_falls_back_to_default(self) -> None:
        # A garbage env value must not abort the loop (set -e arithmetic) —
        # it falls back to the 60s default and stays silent on fast verifies.
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        r = self._run_loop(self.TWO_COMMIT_SCENARIO,
                           env={"PHASEKIT_VERIFY_BUDGET_SECONDS": "brisk"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.ADVISORY, r.stdout + r.stderr)
        self.assertIn("final: done", self._messages())


if __name__ == "__main__":
    unittest.main()
