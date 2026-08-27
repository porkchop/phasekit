#!/usr/bin/env python3
"""Tests for the deadline watchdog (v0.13.0).

The incident run being closed out: 2026-08-26/27, five heavy first sessions
in a row killed at their bound (exit 124) with a full session of coherent
work uncommitted — every strand needed an out-of-band hand. And the one
repair that WAS made by hand (2026-08-27 04:09) taught the second lesson:
a strand commit that carries the dead session's mid-build
ready-to-deploy.json makes the tree look like a verified release, and an
mtime-watching deploy seam ships it.

The mechanism under test, both phases plus the lead math:
  1. compute_wrapup_lead: 15% of span clamped to [300, 900]; explicit env
     override wins; a span too short for its lead gets half the span.
  2. deadline_lastresort_commit: on a dirty tree it restores the
     deploy-arming artifacts to HEAD (delete where untracked), refreshes the
     dead-man baton from outside the loop process, and commits --no-verify
     with transients unstaged; on a clean tree it does nothing.

The bash is exercised for real: the functions are extracted from
scripts/run-until-done.sh by their own delimiters and run in a scratch git
repo, so these tests break if the shipped code does.

Run from the repo root: `python3 -m unittest tests.test_deadline_watchdog`
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"

SOURCE = LOOP_SCRIPT.read_text()

SIX_KEYS = {"stopped_at_phase", "in_flight", "verified", "next_step", "note", "ts"}


def _extract(pattern_start, pattern_end):
    lines = SOURCE.splitlines()
    out, taking = [], False
    for line in lines:
        if not taking and re.match(pattern_start, line):
            taking = True
        if taking:
            out.append(line)
            if len(out) > 1 and re.match(pattern_end, line):
                return "\n".join(out)
    raise AssertionError(f"could not extract {pattern_start!r} from the loop")


LEAD_FN = _extract(r"^compute_wrapup_lead\(\) \{", r"^\}")
COMMIT_FN = _extract(r"^deadline_lastresort_commit\(\) \{", r"^\}")
DISARM_FN = _extract(r"^_disarm_deploy_artifact\(\) \{", r"^\}")
UNSTAGE_FN = _extract(r"^unstage_transient_adds\(\) \{", r"^\}")
# The transient list the unstage helper iterates — extracted so a rename
# there breaks here rather than silently testing nothing.
TRANSIENTS_ARR = _extract(r"^TRANSIENT_SIGNALS=\(", r"^\)")


def _bash(script, cwd=None, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=e,
        capture_output=True, text=True, timeout=60,
    )


class ComputeWrapupLead(unittest.TestCase):
    def _lead(self, span, override=None):
        env = {}
        if override is not None:
            env["PHASEKIT_WRAPUP_LEAD_SECONDS"] = str(override)
        r = _bash(f"{LEAD_FN}\ncompute_wrapup_lead {span}", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return int(r.stdout.strip())

    def test_a_70_minute_session_gets_a_630s_lead(self):
        self.assertEqual(self._lead(4200), 630)

    def test_the_floor_is_300(self):
        # 30 minutes: 15% = 270 -> clamped up to 300.
        self.assertEqual(self._lead(1800), 300)

    def test_the_ceiling_is_900(self):
        # 4 hours: 15% = 2160 -> clamped down to 900.
        self.assertEqual(self._lead(14400), 900)

    def test_a_tiny_session_gets_half_its_span_not_the_floor(self):
        # 8 minutes: floor(300) would leave 180s of work; half the span wins.
        self.assertEqual(self._lead(480), 240)

    def test_the_env_override_wins_verbatim(self):
        self.assertEqual(self._lead(4200, override=120), 120)

    def test_override_zero_disables(self):
        self.assertEqual(self._lead(4200, override=0), 0)


class LastResortCommit(unittest.TestCase):
    """The phase-2 body, run against a real scratch repo."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phasekit-watchdog-")
        self.root = Path(self.dir)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=self.dir, check=True, capture_output=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _commit_all(self, msg):
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "--no-verify", "-m", msg],
            cwd=self.dir, check=True, capture_output=True,
        )

    def _run_lastresort(self):
        # set -e matches production (the watchdog subshell inherits the
        # loop's set -euo pipefail) — review finding 8: a harness without -e
        # would pass a future early-exit regression this suite must catch.
        script = "\n".join(
            [
                "set -euo pipefail",
                f'ROOT_DIR="{self.dir}"',
                f'ARTIFACTS_DIR="{self.artifacts}"',
                f'WRAPUP_SENTINEL="{self.artifacts}/wrapup-requested"',
                TRANSIENTS_ARR,
                UNSTAGE_FN,
                COMMIT_FN,
                DISARM_FN,
                "deadline_lastresort_commit",
            ]
        )
        return _bash(script, cwd=self.dir)

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.dir, capture_output=True, text=True
        ).stdout

    def test_a_clean_tree_is_left_alone(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        before = self._git("rev-parse", "HEAD")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._git("rev-parse", "HEAD"), before)

    def test_dirty_work_is_committed_and_named_as_last_resort(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.root / "src.txt").write_text("v2 in progress\n")
        (self.root / "new-module.txt").write_text("half built\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        subject = self._git("log", "-1", "--format=%s")
        self.assertIn("last-resort deadline commit", subject)
        # Everything except the baton lands in the commit. (The baton stays
        # uncommitted by design; in production it is invisible to git status
        # via info/exclude, which this scratch repo does not set up.)
        residue = [
            line
            for line in self._git("status", "--porcelain", "-uall").splitlines()
            if line.strip() and "session-interrupted.json" not in line
        ]
        self.assertEqual(residue, [])

    def test_a_mid_build_ready_to_deploy_is_restored_to_head(self):
        rtd = self.artifacts / "ready-to-deploy.json"
        rtd.write_text('{"deploy_ready": false, "iteration": "old"}\n')
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        # The dying session armed a fresh, unverified release...
        rtd.write_text('{"deploy_ready": true, "iteration": "doomed"}\n')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        # ...and the watchdog put HEAD's version back, in the tree and in the
        # commit.
        self.assertIn("old", rtd.read_text())
        self.assertNotIn("doomed", self._git("show", "HEAD:artifacts/ready-to-deploy.json"))

    def test_an_untracked_project_complete_is_deleted_not_committed(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.artifacts / "project-complete.json").write_text('{"complete": true}\n')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.artifacts / "project-complete.json").exists())
        self.assertNotIn(
            "project-complete", self._git("ls-tree", "-r", "--name-only", "HEAD")
        )

    def test_transient_signals_are_not_swept_into_the_commit(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.artifacts / "phase-blocked.json").write_text('{"blocked": true}\n')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(
            "phase-blocked", self._git("ls-tree", "-r", "--name-only", "HEAD")
        )

    def test_the_commit_bypasses_a_failing_hook(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        hooks = self.root / ".git" / "hooks"
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("last-resort", self._git("log", "-1", "--format=%s"))

    def test_the_baton_is_written_with_the_six_keys_when_absent(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        baton = json.loads((self.artifacts / "session-interrupted.json").read_text())
        self.assertEqual(set(baton), SIX_KEYS)
        self.assertIn("watchdog", baton["note"])

    def test_an_existing_baton_is_annotated_not_replaced(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        prior = {
            "stopped_at_phase": "phase-7",
            "in_flight": "iteration 3 was IN FLIGHT",
            "verified": False,
            "next_step": "audit",
            "note": "dead-man baton: written at iteration start.",
            "ts": "2026-01-01T00:00:00Z",
        }
        (self.artifacts / "session-interrupted.json").write_text(json.dumps(prior))
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        baton = json.loads((self.artifacts / "session-interrupted.json").read_text())
        self.assertEqual(baton["stopped_at_phase"], "phase-7")
        self.assertIn("last-resort wip commit", baton["note"])
        self.assertNotEqual(baton["ts"], "2026-01-01T00:00:00Z")

    def test_the_baton_itself_is_not_committed(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(
            "session-interrupted", self._git("ls-tree", "-r", "--name-only", "HEAD")
        )


class ReviewFindings0131(unittest.TestCase):
    """Behavioral pins for the v0.13.1 review fixes (findings 1, 2, 4, 7)."""

    def setUp(self):
        LastResortCommit.setUp(self)
        self.tearDown = lambda: LastResortCommit.tearDown(self)

    def test_a_legitimately_armed_clean_artifact_keeps_its_fresh_mtime(self):
        # Finding 7: clean at HEAD + fresh mtime = a pending deploy the
        # session honestly earned; the disarm must not age it away.
        rtd = self.artifacts / "ready-to-deploy.json"
        rtd.write_text('{"deploy_ready": true, "iteration": "verified"}\n')
        (self.root / "src.txt").write_text("v1\n")
        LastResortCommit._commit_all(self, "verified release")
        import time as _t

        now = _t.time()
        os.utime(rtd, (now, now))
        (self.root / "scratch.txt").write_text("dirty\n")
        r = LastResortCommit._run_lastresort(self)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(rtd.stat().st_mtime, now - 60)
        self.assertIn("verified", rtd.read_text())

    def test_the_strand_commit_never_contains_an_armed_artifact(self):
        # Finding 2's gate: the staged-clean check on the two artifact paths
        # exists and guards the commit.
        self.assertIn("ready-to-deploy.json", COMMIT_FN)
        self.assertIn("git diff --cached --quiet -- \\", COMMIT_FN)
        # And the disarm is INSIDE the retry loop: it appears after the
        # `git add -A` line within the loop body.
        add_at = COMMIT_FN.index("git add -A")
        self.assertIn("_disarm_deploy_artifact ready-to-deploy.json", COMMIT_FN[add_at:])

    def test_phase2_stands_down_when_wrapup_is_in_progress(self):
        # Finding 1: mutual exclusion via the marker, checked before the
        # last-resort commit; the marker is transient and iteration-cleared.
        self.assertIn('.wrapup-in-progress', SOURCE)
        arm_fn = _extract(r"^arm_deadline_watchdog\(\) \{", r"^\}")
        self.assertIn("standing down", arm_fn)
        self.assertIn('".wrapup-in-progress"', _extract(r"^TRANSIENT_SIGNALS=\(", r"^\)"))
        self.assertIn('".wrapup-in-progress"', _extract(r"^HIDDEN_TRANSIENTS=\(", r"^\)"))
        self.assertIn('rm -f "$ARTIFACTS_DIR/.wrapup-in-progress"', SOURCE)

    def test_wrapup_commit_survives_a_stolen_index(self):
        # Finding 1 interleave (b): the wrap-up's commit is tolerant — a
        # failure must not propagate under set -e.
        wrapup = _extract(r"^wrapup_commit\(\) \{", r"^\}")
        self.assertIn('touch "$ARTIFACTS_DIR/.wrapup-in-progress"', wrapup)
        self.assertIn("if ! git commit -m", wrapup)
        self.assertNotIn("\n  git commit -m", wrapup)

    def test_the_exit_trap_reaps_the_watchdog_before_clearing_the_baton(self):
        # Finding 4: kill alone is asynchronous; wait makes the ordering real.
        trap_fn = _extract(r"^run_until_done_exit_trap\(\) \{", r"^\}")
        self.assertIn('wait "$DEADLINE_WATCHDOG_PID"', trap_fn)
        self.assertLess(
            trap_fn.index("wait"), trap_fn.index("clear_provisional_handoff_on_exit")
        )


class WatchdogWiring(unittest.TestCase):
    """Cheap structural pins: the loop arms it, the trap kills it."""

    def test_the_loop_arms_the_watchdog_when_a_deadline_exists(self):
        self.assertIn('arm_deadline_watchdog "$SESSION_DEADLINE"', SOURCE)

    def test_the_exit_trap_kills_the_watchdog_and_keeps_the_baton_clear(self):
        self.assertIn("trap run_until_done_exit_trap EXIT", SOURCE)
        trap_fn = _extract(r"^run_until_done_exit_trap\(\) \{", r"^\}")
        self.assertIn("DEADLINE_WATCHDOG_PID", trap_fn)
        self.assertIn("clear_provisional_handoff_on_exit", trap_fn)
        # The old single-purpose trap must not ALSO be registered — one EXIT
        # trap per shell; a second registration would silently replace this
        # one.
        self.assertNotIn("trap clear_provisional_handoff_on_exit EXIT", SOURCE)

    def test_the_sentinel_touch_is_idempotent_with_the_supervisors(self):
        arm_fn = _extract(r"^arm_deadline_watchdog\(\) \{", r"^\}")
        self.assertIn('[[ ! -f "$WRAPUP_SENTINEL" ]]', arm_fn)


if __name__ == "__main__":
    unittest.main()
