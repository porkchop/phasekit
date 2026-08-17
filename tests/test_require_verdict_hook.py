#!/usr/bin/env python3
"""Tests for the require-verdict Stop hook (session-lifetime batch, item 1).

The incident being replayed: foundry-orchestrator runs 288/289/290 each did
real, green work and committed nothing. Each ended its own turn while its own
background work was still running — `claude -p` exits on the final response and
takes every backgrounded task with it — so the loop found no artifact, exited 1
with no retry, and left a growing dirty tree for the next session to inherit.

The missing invariant: a session may not stop without a verdict. Silence is not
an allowed outcome.

The hook is exercised directly (it is a shell script with an env-var contract),
which is the level the three load-bearing properties live at:
  (a) bounded      — at most N blocks per iteration, then it steps aside
  (b) actionable   — every block names STATE-CHANGING moves, or it thrashes
  (c) not fooled by a stale artifact from an earlier iteration

Plus the property that keeps it out of everyone's way: inert outside the loop.

Run from the repo root: `python3 -m unittest tests.test_require_verdict_hook`
"""

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "require-verdict.sh"
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
SETTINGS_TEMPLATE = REPO_ROOT / "templates" / "settings.template.json"

BLOCK = 2          # exit code that blocks the stop
ALLOW = 0

# Kept in sync with the loop by test_vocabulary_matches_the_loop below.
VERDICTS = ("project-complete.json phase-approval.json phase-update.json "
            "phase-blocked.json scope-refusal.json light-escalation.json")


class HookFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="pk-verdict-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.artifacts = self.tmp / "artifacts"
        self.artifacts.mkdir()
        self.marker = self.tmp / "iter-marker"
        self.marker.touch()

    def env(self, **overrides):
        env = dict(os.environ)
        env.update({
            "PHASEKIT_VERDICT_ARTIFACTS": VERDICTS,
            "PHASEKIT_ARTIFACTS_DIR": str(self.artifacts),
            "PHASEKIT_ITER_MARKER": str(self.marker),
        })
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def run_hook(self, **overrides):
        return subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps({"session_id": "x", "hook_event_name": "Stop"}),
            capture_output=True, text=True, env=self.env(**overrides),
        )

    def write_artifact(self, name, fresh=True):
        path = self.artifacts / name
        path.write_text('{"ok": true}\n', encoding="utf-8")
        if fresh:
            # Ensure it is strictly newer than the marker even on coarse
            # filesystem timestamps.
            future = time.time() + 5
            os.utime(path, (future, future))
        else:
            past = self.marker.stat().st_mtime - 60
            os.utime(path, (past, past))
        return path

    @property
    def counter(self):
        return self.artifacts / ".stop-hook-blocks"


class HappyPath(HookFixture):
    def test_a_fresh_verdict_allows_the_stop(self):
        self.write_artifact("phase-approval.json")
        r = self.run_hook()
        self.assertEqual(r.returncode, ALLOW, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_the_counter_file_is_never_created_on_a_healthy_session(self):
        """The stated cost of the hook on the happy path."""
        self.write_artifact("phase-update.json")
        self.run_hook()
        self.assertFalse(self.counter.exists())

    def test_every_verdict_kind_satisfies_the_hook(self):
        for name in VERDICTS.split():
            with self.subTest(artifact=name):
                for f in self.artifacts.iterdir():
                    f.unlink()
                self.write_artifact(name)
                self.assertEqual(self.run_hook().returncode, ALLOW)

    def test_a_terminal_signal_that_is_not_a_phase_artifact_still_counts(self):
        """scope-refusal and light-escalation are legitimate endings; blocking
        on them would be the hook fighting the loop."""
        self.write_artifact("scope-refusal.json")
        self.assertEqual(self.run_hook().returncode, ALLOW)


class InertOutsideTheLoop(HookFixture):
    """An interactive `claude` session has no iteration and no verdict to give.
    Blocking one would break phasekit's standing rule that ordinary direct work
    must not be made cumbersome."""

    def test_no_vocabulary_means_no_opinion(self):
        r = self.run_hook(PHASEKIT_VERDICT_ARTIFACTS=None)
        self.assertEqual(r.returncode, ALLOW)
        self.assertEqual(r.stderr, "")

    def test_no_artifacts_dir_means_no_opinion(self):
        r = self.run_hook(PHASEKIT_ARTIFACTS_DIR=None)
        self.assertEqual(r.returncode, ALLOW)

    def test_no_marker_means_no_opinion(self):
        r = self.run_hook(PHASEKIT_ITER_MARKER=None)
        self.assertEqual(r.returncode, ALLOW)

    def test_a_marker_path_that_does_not_exist_means_no_opinion(self):
        self.marker.unlink()
        self.assertEqual(self.run_hook().returncode, ALLOW)

    def test_an_unwritable_artifacts_dir_cannot_wedge_the_session(self):
        """Cannot count => cannot bound => must not block."""
        self.artifacts.chmod(0o500)
        self.addCleanup(self.artifacts.chmod, 0o700)
        r = self.run_hook()
        self.assertEqual(r.returncode, ALLOW, r.stderr)


class Blocking(HookFixture):
    def test_no_verdict_blocks_the_stop(self):
        r = self.run_hook()
        self.assertEqual(r.returncode, BLOCK)

    def test_the_message_names_state_changing_actions(self):
        """A hook that only says 'you cannot stop' leaves the agent no move
        except stopping again — same silent failure at 3x the cost."""
        r = self.run_hook()
        msg = r.stderr
        # The specific correction the incident needed.
        self.assertIn("killed the moment this turn ends", msg)
        self.assertIn("FOREGROUND", msg)
        # The move that was available to all three failed sessions.
        self.assertIn("phase-update.json", msg)
        self.assertIn("the loop continues", msg)
        # The honest exit.
        self.assertIn("phase-blocked.json", msg)

    def test_it_is_bounded_and_then_steps_aside(self):
        self.assertEqual(self.run_hook().returncode, BLOCK)
        self.assertEqual(self.run_hook().returncode, BLOCK)
        r = self.run_hook()
        self.assertEqual(r.returncode, ALLOW, "the hook must be a prompt, not a cage")
        self.assertIn("releasing the session", r.stderr)

    def test_the_bound_is_configurable_and_still_bounded(self):
        r = self.run_hook(PHASEKIT_STOP_BLOCK_LIMIT="1")
        self.assertEqual(r.returncode, BLOCK)
        self.assertEqual(self.run_hook(PHASEKIT_STOP_BLOCK_LIMIT="1").returncode, ALLOW)

    def test_a_garbage_bound_falls_back_to_the_default(self):
        for _ in range(2):
            self.assertEqual(self.run_hook(PHASEKIT_STOP_BLOCK_LIMIT="lots").returncode, BLOCK)
        self.assertEqual(self.run_hook(PHASEKIT_STOP_BLOCK_LIMIT="lots").returncode, ALLOW)

    def test_a_corrupt_counter_does_not_unbound_it(self):
        self.counter.write_text("not-a-number\n", encoding="utf-8")
        self.assertEqual(self.run_hook().returncode, BLOCK)
        self.assertEqual(self.run_hook().returncode, BLOCK)
        self.assertEqual(self.run_hook().returncode, ALLOW)

    def test_writing_a_verdict_after_a_block_allows_the_stop(self):
        """The intended arc: blocked, agent complies, session ends properly."""
        self.assertEqual(self.run_hook().returncode, BLOCK)
        self.write_artifact("phase-update.json")
        self.assertEqual(self.run_hook().returncode, ALLOW)


class StaleArtifactsDoNotSatisfyIt(HookFixture):
    """phase-approval.json persists across iterations as the durable record of
    the last approved phase. A hook fooled by it would be inert exactly when it
    is needed — the same 'predates this iteration' trap the loop's own
    no-artifact message already warns about."""

    def test_a_stale_approval_does_not_count(self):
        self.write_artifact("phase-approval.json", fresh=False)
        self.assertEqual(self.run_hook().returncode, BLOCK)

    def test_a_stale_artifact_of_every_kind_does_not_count(self):
        for name in VERDICTS.split():
            with self.subTest(artifact=name):
                for f in self.artifacts.iterdir():
                    f.unlink()
                self.write_artifact(name, fresh=False)
                self.assertEqual(self.run_hook().returncode, BLOCK)

    def test_a_fresh_artifact_beside_a_stale_one_counts(self):
        self.write_artifact("phase-approval.json", fresh=False)
        self.write_artifact("phase-update.json", fresh=True)
        self.assertEqual(self.run_hook().returncode, ALLOW)


class WiringTest(unittest.TestCase):
    def test_hook_parses(self):
        r = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_is_executable(self):
        self.assertTrue(os.access(HOOK, os.X_OK), "hooks must ship executable")

    def test_vocabulary_matches_the_loop(self):
        """The hook reads the vocabulary from the loop rather than restating
        it, so they cannot disagree about what an ending is. This pins the
        constant this test file uses against the loop's array."""
        text = LOOP_SCRIPT.read_text(encoding="utf-8")
        block = text.split("VERDICT_ARTIFACTS=(", 1)[1].split(")", 1)[0]
        loop_list = [ln.strip().strip('"') for ln in block.strip().splitlines()]
        self.assertEqual(loop_list, VERDICTS.split())
        # And it is exported under the name the hook reads.
        self.assertIn('export PHASEKIT_VERDICT_ARTIFACTS="${VERDICT_ARTIFACTS[*]}"', text)
        self.assertIn('export PHASEKIT_ARTIFACTS_DIR="$ARTIFACTS_DIR"', text)
        self.assertIn('export PHASEKIT_ITER_MARKER="$ITER_START_MARKER"', text)

    def test_the_hook_does_not_restate_the_vocabulary(self):
        """A second copy of the list is the drift this design avoids."""
        hook_text = HOOK.read_text(encoding="utf-8")
        for name in ("project-complete.json", "scope-refusal.json",
                     "light-escalation.json"):
            self.assertNotIn(name, hook_text)

    def test_the_counter_is_a_registered_transient(self):
        """It must never be committed and must not dirty `git status`."""
        text = LOOP_SCRIPT.read_text(encoding="utf-8")
        transient = text.split("TRANSIENT_SIGNALS=(", 1)[1].split(")", 1)[0]
        hidden = text.split("HIDDEN_TRANSIENTS=(", 1)[1].split(")", 1)[0]
        self.assertIn(".stop-hook-blocks", transient)
        self.assertIn(".stop-hook-blocks", hidden)

    def test_the_counter_is_cleared_at_iteration_start(self):
        """The block budget is per-iteration; a stale counter would make the
        hook inert for the rest of the session."""
        text = LOOP_SCRIPT.read_text(encoding="utf-8")
        cleanup = text.split("cleanup_artifacts() {", 1)[1].split("\n}", 1)[0]
        self.assertIn(".stop-hook-blocks", cleanup)

    def test_the_hook_is_registered_in_the_settings_template(self):
        settings = json.loads(SETTINGS_TEMPLATE.read_text(encoding="utf-8"))
        stop = settings["hooks"]["Stop"]
        commands = [h["command"] for entry in stop for h in entry["hooks"]]
        self.assertIn("./.claude/hooks/require-verdict.sh", commands)

    def test_the_scaffolds_own_settings_match_the_template(self):
        own = (REPO_ROOT / ".claude" / "settings.json").read_bytes()
        self.assertEqual(own, SETTINGS_TEMPLATE.read_bytes())

    def test_the_hook_is_registered_in_capabilities_and_the_default_profile(self):
        caps = (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text(
            encoding="utf-8")
        self.assertIn(".claude/hooks/require-verdict.sh", caps)
        include_hooks = caps.split("include_hooks:", 1)[1].split("include_scripts:", 1)[0]
        self.assertIn("- require-verdict", include_hooks)

    def test_the_prompt_teaches_the_correction_before_the_hook_has_to(self):
        prompt = (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text(encoding="utf-8")
        self.assertIn("BACKGROUND WORK DIES WITH YOUR TURN", prompt)
        self.assertIn("phase-update.json", prompt)


if __name__ == "__main__":
    unittest.main()
