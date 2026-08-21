#!/usr/bin/env python3
"""Tests for the wrapup-nudge PostToolUse hook (deadline post-mortem class (a)).

The incident being replayed: xmeo-v3 runs 388/390 (2026-08-21). The
supervisor's wrap-up timer fired at T-minus-300s and touched the sentinel —
and the session never saw it, because the loop polls the sentinel only
BETWEEN iterations and one model turn spanned the whole remaining deadline.
The wrapper's own diagnostic: "the wrap-up sentinel was never observed: one
iteration spanned the deadline." The session was killed mid-sentence with
real, uncommitted work.

The hook is the mid-turn seam: PostToolUse fires after every tool call, and
exit 2 surfaces stderr to the model while the turn continues. Properties:
  (a) cheap and silent when no sentinel exists (the overwhelming case)
  (b) once per iteration — marker file, `-nt` freshness against the loop's
      own iteration marker, cleared by the loop at iteration start
  (c) inert outside the loop, fail-open on anything unreadable

Run from the repo root: `python3 -m unittest tests.test_wrapup_nudge_hook`
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "wrapup-nudge.sh"
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
SETTINGS_TEMPLATE = REPO_ROOT / "templates" / "settings.template.json"
SETTINGS_LOCAL = REPO_ROOT / ".claude" / "settings.json"

NUDGE = 2  # exit code whose stderr PostToolUse surfaces to the model
QUIET = 0


class HookFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-nudge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.artifacts = self.tmp / "artifacts"
        self.artifacts.mkdir()
        self.iter_marker = self.tmp / "iter-marker"
        self.iter_marker.touch()
        self.sentinel = self.artifacts / "wrapup-requested"

    def env(self, **overrides):
        env = dict(os.environ)
        env.update({
            "PHASEKIT_ARTIFACTS_DIR": str(self.artifacts),
            "PHASEKIT_ITER_MARKER": str(self.iter_marker),
        })
        env.update(overrides)
        for key, value in list(env.items()):
            if value is None:
                env.pop(key)
        return env

    def run_hook(self, **overrides):
        return subprocess.run(
            ["bash", str(HOOK)],
            input="{}",
            capture_output=True,
            text=True,
            timeout=30,
            env=self.env(**overrides),
        )

    # -- happy path ----------------------------------------------------------

    def test_no_sentinel_means_silence(self):
        result = self.run_hook()
        self.assertEqual(result.returncode, QUIET)
        self.assertEqual(result.stderr, "")

    def test_no_sentinel_creates_no_marker(self):
        self.run_hook()
        self.assertFalse((self.artifacts / ".wrapup-nudge-sent").exists())

    # -- the nudge -----------------------------------------------------------

    def test_sentinel_present_nudges_once(self):
        self.sentinel.touch()
        first = self.run_hook()
        self.assertEqual(first.returncode, NUDGE)
        self.assertIn("phase-update.json", first.stderr)
        second = self.run_hook()
        self.assertEqual(second.returncode, QUIET,
                         "a second tool call in the same iteration must not be nagged")
        self.assertEqual(second.stderr, "")

    def test_the_nudge_names_the_state_changing_move(self):
        # Property (b) of require-verdict applies here too: feedback that
        # names no action just burns attention in the session's last minutes.
        self.sentinel.touch()
        result = self.run_hook()
        self.assertIn("artifacts/phase-update.json", result.stderr)
        self.assertIn("end your turn", result.stderr)

    def test_a_stale_marker_from_a_previous_iteration_does_not_silence(self):
        # The loop clears the marker at iteration start, but the freshness
        # test must hold even if it did not: a marker OLDER than the current
        # iteration's start is evidence about a previous iteration only.
        self.sentinel.touch()
        marker = self.artifacts / ".wrapup-nudge-sent"
        marker.touch()
        past = time.time() - 3600
        os.utime(marker, (past, past))
        self.iter_marker.touch()  # iteration started after that marker
        result = self.run_hook()
        self.assertEqual(result.returncode, NUDGE)

    def test_custom_sentinel_path_is_honoured(self):
        custom = self.tmp / "elsewhere"
        custom.touch()
        result = self.run_hook(PHASEKIT_WRAPUP_SENTINEL=str(custom))
        self.assertEqual(result.returncode, NUDGE)

    def test_custom_sentinel_absent_is_silent_even_if_default_exists(self):
        # An explicit override owns the decision completely; the default path
        # must not be consulted behind its back.
        self.sentinel.touch()
        custom = self.tmp / "elsewhere"  # never created
        result = self.run_hook(PHASEKIT_WRAPUP_SENTINEL=str(custom))
        self.assertEqual(result.returncode, QUIET)

    # -- inert outside the loop ----------------------------------------------

    def test_no_artifacts_dir_means_no_opinion(self):
        self.sentinel.touch()
        result = self.run_hook(PHASEKIT_ARTIFACTS_DIR=None)
        self.assertEqual(result.returncode, QUIET)

    def test_no_iter_marker_means_no_opinion(self):
        self.sentinel.touch()
        result = self.run_hook(PHASEKIT_ITER_MARKER=None)
        self.assertEqual(result.returncode, QUIET)

    def test_a_marker_path_that_does_not_exist_means_no_opinion(self):
        self.sentinel.touch()
        result = self.run_hook(PHASEKIT_ITER_MARKER=str(self.tmp / "never"))
        self.assertEqual(result.returncode, QUIET)

    def test_unwritable_artifacts_dir_stays_silent_not_nagging(self):
        # Cannot record the nudge => cannot bound it => the fail direction is
        # SILENCE, never a nag on every tool call to the kill.
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions; case is unreachable")
        self.sentinel.touch()
        self.artifacts.chmod(0o555)
        self.addCleanup(self.artifacts.chmod, 0o755)
        result = self.run_hook()
        self.assertEqual(result.returncode, QUIET)


class RegistrationSync(unittest.TestCase):
    """The hook exists, ships, and the loop clears its marker."""

    def test_hook_is_executable(self):
        self.assertTrue(HOOK.is_file())
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_registered_in_both_settings_files_on_both_boundaries(self):
        # PostToolUse catches the tail of a completed call; PreToolUse catches
        # the model ABOUT TO LAUNCH work that will not fit inside the deadline
        # (Aaron's straddling-tool case, 08-21). Both boundaries, one script,
        # one once-per-iteration marker between them.
        for path in (SETTINGS_TEMPLATE, SETTINGS_LOCAL):
            hooks = json.loads(path.read_text())["hooks"]
            for event in ("PostToolUse", "PreToolUse"):
                entries = hooks.get(event) or []
                commands = [h.get("command")
                            for entry in entries for h in entry.get("hooks", [])]
                self.assertIn("./.claude/hooks/wrapup-nudge.sh", commands,
                              f"{path}:{event}")

    def test_loop_clears_the_marker_at_iteration_start(self):
        source = LOOP_SCRIPT.read_text()
        # Beside the stop-hook counter, on the same lifecycle — one clearing
        # site, not a parallel mechanism. Anchored on the rm statements
        # themselves (comments mention both names elsewhere).
        counter_rm = source.index('rm -f "$ARTIFACTS_DIR/.stop-hook-blocks"')
        marker_rm = source.index('rm -f "$ARTIFACTS_DIR/.wrapup-nudge-sent"')
        self.assertLess(abs(marker_rm - counter_rm), 600,
                        "the two hook budgets should share one clearing site")


if __name__ == "__main__":
    unittest.main()
