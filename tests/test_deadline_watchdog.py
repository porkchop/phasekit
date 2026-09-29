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
     deploy-arming artifacts to HEAD (delete an untracked ready-to-deploy.json;
     v0.14.7: an untracked project-complete.json is kept on disk, unstaged —
     never deleted, never committed), refreshes the dead-man baton from
     outside the loop process, and commits --no-verify with transients
     unstaged; on a clean tree it does nothing.

The bash is exercised for real: the functions are extracted from
scripts/run-until-done.sh by their own delimiters and run in a scratch git
repo, so these tests break if the shipped code does.

Run from the repo root: `python3 -m unittest tests.test_deadline_watchdog`
"""

import json
import os
import pathlib
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


def _extract_between(start_re, end_re):
    lines = SOURCE.splitlines()
    out, taking = [], False
    for line in lines:
        if not taking and re.match(start_re, line):
            taking = True
        if taking:
            if re.match(end_re, line):
                return "\n".join(out)
            out.append(line)
    raise AssertionError(f"could not extract {start_re!r} from the loop")


# v0.18.0: the lead is measured — the cost-model block the formula reads
# (resolved per test, so a loop without one fails these tests, not the module).
def _cost_block():
    try:
        return _extract_between(r"^# --- cost model \(v0\.18\.0\)", r"^# --- Deadline watchdog")
    except AssertionError:
        return ""
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
    # A script that embeds the loop's definitions is larger than one argv
    # entry may be (MAX_ARG_STRLEN, 128 KiB — first hit at v0.14.10): run it
    # from a file, as bash would a real script.
    if len(script.encode()) > 100_000:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            return subprocess.run(
                ["bash", path], cwd=cwd, env=e,
                capture_output=True, text=True, timeout=60,
            )
        finally:
            os.unlink(path)
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=e,
        capture_output=True, text=True, timeout=60,
    )


class ComputeWrapupLead(unittest.TestCase):
    """v0.18.0 (design §2.2, fork F1): one formula, one home, measured.

        T_y = G_full + W + 60 s        L = T_y + M (+ R in light mode)
        L clamped to [300 s, 25% of the span]; above the cap the lead stays
        at the cap (push-back, never wider); T_y never exceeds L.

    The priors stand in for an empty ledger; the design's worked table is
    reproduced from measured gate times. Red on v0.17.0: the lead there is
    15% of the span clamped to [300, 900] and knows no ledger."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pk-lead-")
        self.artifacts = Path(self.tmp) / "artifacts"
        (self.artifacts / "logs").mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, **samples):
        (self.artifacts / "logs" / "cost-ledger.json").write_text(json.dumps(
            {"schema": 1, "samples": {k: v for k, v in samples.items()}}))

    def _plan(self, span, mode="standard", override=None, lastresort=None):
        env = {"ITERATION_MODE": mode}
        if override is not None:
            env["PHASEKIT_WRAPUP_LEAD_SECONDS"] = str(override)
        if lastresort is not None:
            env["PHASEKIT_LASTRESORT_LEAD_SECONDS"] = str(lastresort)
        r = _bash(f'ARTIFACTS_DIR="{self.artifacts}"\nITERATION_MODE="{mode}"\n{_cost_block()}\n{LEAD_FN}\n'
                  f"compute_wrapup_lead {span}", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        lead, ty, uncapped, cap = (int(x) for x in r.stdout.split())
        return lead, ty, uncapped, cap

    def test_the_lead_derives_from_the_cost_ledger_within_floor_and_cap(self):
        # The design's worked numbers (§2.2), from measured full-tier P90s.
        self._ledger(g_full=[726, 780, 838, 800, 840])
        # orchestrator standard, 110 min: T_y 960, L uncapped 1920 > cap 1650.
        self.assertEqual(self._plan(6600), (1650, 960, 1920, 1650))
        # orchestrator light: + R (G_full + 300) = 3060 uncapped, still the cap.
        self.assertEqual(self._plan(6600, mode="light"), (1650, 960, 3060, 1650))
        self._ledger(g_full=[129, 200, 260, 240])
        # xmeo standard, 85 min: T_y 380, L 760 — inside the cap, in force.
        self.assertEqual(self._plan(5100), (760, 380, 760, 1275))
        # xmeo light: 1320 uncapped, the cap (1275) in force.
        self.assertEqual(self._plan(5100, mode="light"), (1275, 380, 1320, 1275))

    def test_the_priors_stand_in_for_an_empty_ledger(self):
        # G_full 300, W 60, M = 420: T_y 420, L 840 (70 minutes: cap 1050).
        self.assertEqual(self._plan(4200), (840, 420, 840, 1050))

    def test_the_floor_is_300_and_the_cap_is_a_quarter_of_the_span(self):
        self._ledger(g_full=[10], w=[5], m=[10])
        lead, ty, uncapped, cap = self._plan(7200)
        self.assertEqual((lead, uncapped, cap), (300, 85, 1800))
        self.assertEqual(ty, 75)

    def test_a_session_under_twenty_minutes_gets_half_its_span_at_most(self):
        # 25% of 480 s is under the 300 s floor: half the span stands in.
        self.assertEqual(self._plan(480)[0], 240)

    def test_take_control_never_precedes_the_nudge(self):
        # A measured T_y beyond the capped lead is pulled back to the lead.
        self._ledger(g_full=[3000])
        lead, ty, _, _ = self._plan(6600)
        self.assertEqual(lead, 1650)
        self.assertEqual(ty, lead - 60, "60 s with the nudge before control is taken")

    def test_a_take_control_inside_the_last_resort_lead_is_dropped(self):
        self.assertEqual(self._plan(4200, override=50)[1], 0)

    def test_the_env_override_wins_verbatim_and_caps_take_control(self):
        lead, ty, _, _ = self._plan(4200, override=120, lastresort=0)
        self.assertEqual((lead, ty), (120, 80), "the model keeps a window with the nudge")

    def test_override_zero_disables_both_stages(self):
        lead, ty, _, _ = self._plan(4200, override=0)
        self.assertEqual((lead, ty), (0, 0))


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
        #
        # Deliberately narrow (v0.14.6): this prelude carries only the v0.13.x
        # primitives, so the v0.14.5 deferral-keying block is skipped by its
        # own `command -v normalize_deferral_keys` guard here — which is why
        # these tests never saw run 682's `artifact_never_landed: command not
        # found`. The block runs for real, with exactly the function set the
        # fork inherits, in LastResortCommitAsTheForkSeesIt below.
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
                # v0.18.0 trailers: stubbed here (this prelude is the narrow
                # v0.13.x set); LastResortCommitAsTheForkSeesIt runs the real ones.
                'phasekit_trailers() { echo "Phasekit-Kind: $1"; }',
                "_wip_phase_source() { :; }",
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

    def test_the_deploy_claim_is_never_rewritten_only_kept_out_of_unverified_commits(self):
        # v0.18.0 (design §3.1, link [E]): the dying session re-armed the
        # claim AND its evidence for the new iteration. Until v0.17.0 the
        # watchdog restored the claim to HEAD and left the evidence at the
        # new digest — the committed tree contradicted the project's own
        # consistency check (xmeo run 999: 40 digest failures at the next
        # start). Now the claim is unstaged: the wip carries HEAD's claim,
        # the worktree keeps the session's, byte for byte, mtime untouched.
        # Red on v0.17.0: the worktree claim is HEAD's ("old").
        rtd = self.artifacts / "ready-to-deploy.json"
        rtd.write_text('{"deploy_ready": true, "digest": "old"}\n')
        (self.artifacts / "evidence.json").write_text('{"digest": "old"}\n')
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        rtd.write_text('{"deploy_ready": true, "digest": "new"}\n')
        (self.artifacts / "evidence.json").write_text('{"digest": "new"}\n')
        (self.root / "src.txt").write_text("v2\n")
        import time as _t
        stamp = _t.time() - 5
        os.utime(rtd, (stamp, stamp))
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("last-resort", self._git("log", "-1", "--format=%s"))
        # the unverified commit never carries a changed claim ...
        self.assertIn('"old"', self._git("show", "HEAD:artifacts/ready-to-deploy.json"))
        self.assertEqual(self._git("diff", "HEAD~1", "HEAD", "--", "artifacts/ready-to-deploy.json"), "")
        # ... and the session's claim is left exactly as written
        self.assertEqual(rtd.read_text(), '{"deploy_ready": true, "digest": "new"}\n')
        self.assertAlmostEqual(rtd.stat().st_mtime, stamp, delta=1)
        self.assertIn(" M artifacts/ready-to-deploy.json", self._git("status", "--porcelain").splitlines())

    def test_a_torn_tracked_claim_comes_back_from_head(self):
        rtd = self.artifacts / "ready-to-deploy.json"
        rtd.write_text('{"deploy_ready": false}\n')
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        rtd.write_text('{"deploy_rea')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rtd.read_text(), '{"deploy_ready": false}\n')
        self.assertNotIn("ready-to-deploy", self._git("status", "--porcelain"))

    # v0.14.7 (orchestrator #658, iteration 122): "restore to HEAD" of a path
    # HEAD does not carry was `rm -f` — the watchdog deleted the completion
    # record the ship session had just written, three kills in a row. The
    # record now stays on disk as written (deferral keys normalized as at any
    # landing — this record has none, so byte-for-byte), and still never rides the
    # --no-verify wip (a committed completion reads as "complete" to a
    # supervisor inferring from git; the next loop start's recovery lands the
    # never-landed record verify-gated instead). Red on v0.14.6: the file is
    # gone after the commit.
    def test_an_untracked_project_complete_is_kept_on_disk_byte_for_byte_and_never_committed(self):
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        record = '{"done": true, "iteration": "iteration-122", "summary": "the session wrote this"}\n'
        (self.artifacts / "project-complete.json").write_text(record)
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.artifacts / "project-complete.json").read_text(), record)
        self.assertIn("last-resort", self._git("log", "-1", "--format=%s"))
        self.assertEqual(self._git("show", "HEAD:src.txt"), "v2\n")
        self.assertNotIn(
            "project-complete", self._git("ls-tree", "-r", "--name-only", "HEAD")
        )
        # Untracked and unstaged: exactly the never-landed shape the loop
        # start's boundary recovery looks for first.
        self.assertIn("?? artifacts/project-complete.json",
                      self._git("status", "--porcelain", "--untracked-files=all"))

    def test_a_torn_untracked_project_complete_is_still_deleted(self):
        # v0.14.7 review MAJOR-1: a writer the kill interrupted leaves an
        # empty or truncated file; kept, it would land at the next start as a
        # silent false completion. Not a record — deleted as before.
        for torn in ("", '{"done": tr'):
            with self.subTest(torn=torn):
                LastResortCommit.tearDown(self); LastResortCommit.setUp(self)
                (self.root / "src.txt").write_text("v1\n")
                self._commit_all("base")
                (self.artifacts / "project-complete.json").write_text(torn)
                (self.root / "src.txt").write_text("v2\n")
                r = self._run_lastresort()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse((self.artifacts / "project-complete.json").exists())
                self.assertIn("last-resort", self._git("log", "-1", "--format=%s"))
                self.assertNotIn("project-complete", self._git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_an_untracked_ready_to_deploy_is_kept_on_disk_never_committed(self):
        # v0.18.0: the same rule for the claim as for the record — unstaged,
        # never deleted. An unverified first-ever claim never rides the wip;
        # the next verify-gated landing judges it with the rest of the tree
        # (pinned end-to-end in test_run_until_done_v060's fall-through test).
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        (self.artifacts / "ready-to-deploy.json").write_text('{"deploy_ready": true, "iteration": "first-ever"}\n')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.artifacts / "ready-to-deploy.json").exists())
        self.assertIn("?? artifacts/ready-to-deploy.json",
                      self._git("status", "--porcelain", "--untracked-files=all"))
        self.assertIn("last-resort", self._git("log", "-1", "--format=%s"))
        self.assertNotIn(
            "ready-to-deploy", self._git("ls-tree", "-r", "--name-only", "HEAD")
        )

    def test_a_tracked_dirty_project_complete_is_kept_out_not_restored(self):
        # v0.18.0: one rule for both files — the wip carries HEAD's record,
        # the worktree keeps the session's.
        rec = self.artifacts / "project-complete.json"
        rec.write_text('{"done": true, "iteration": "iteration-121"}\n')
        (self.root / "src.txt").write_text("v1\n")
        self._commit_all("base")
        rec.write_text('{"done": true, "iteration": "iteration-122-unverified"}\n')
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_lastresort()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("iteration-122-unverified", rec.read_text())
        self.assertNotIn("unverified", self._git("show", "HEAD:artifacts/project-complete.json"))

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
        # v0.18.0: the keep-out is an unstage — no checkout of the path, no rm
        # of a parseable file.
        self.assertIn('git reset -q -- "$p"', DISARM_FN)

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


# ---------------------------------------------------------------------------
# v0.14.6: every function the watchdog fork reaches is defined before the fork
# ---------------------------------------------------------------------------
#
# The incident (foundry-orchestrator run 682, 2026-09-09 01:02 UTC): the
# watchdog is forked as a background subshell at the TOP-LEVEL
# arm_deadline_watchdog call, and a bash function is visible inside that fork
# only if its definition was parsed before the fork. v0.14.5's kill path
# called artifact_never_landed, defined ~50 lines AFTER the arm site —
# `line 2044: artifact_never_landed: command not found`, twice, in
# deadline-watchdog.log — so the clause that keys the deferrals of a swept
# approval was dead in exactly the path it was written for. The harnesses
# above never saw it: LastResortCommit's prelude does not define
# normalize_deferral_keys, so the `command -v` guard skips the whole block,
# and tests/test_boundary_state.py's STUBS re-defined the helper.
#
# The pin is by construction, not by list: parse the shipped script, find the
# top-level arm line, walk the transitive call graph from the subshell body
# (function names referenced in each reached body, comments stripped), and
# assert every reached definition precedes the arm line. It reports the
# offending names and lines. Red on v0.14.5 — artifact_never_landed at 2696
# against the arm at 2641 (the ONLY late definition the fork's graph reaches;
# commit_pending_approval_first, 2718, is reachable solely through boundary_do,
# which the kill path never calls — it moved with its neighbour all the same)
# — and green on the fix.

FUNC_DEF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) *\{")


def _strip_comments(text):
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"(^|\s)#.*$", r"\1", line))
    return "\n".join(out)


def function_index(source):
    """name -> (definition line, 1-based; the text through its closing brace).

    Top-level `name() {` … `}` only, the delimiters _extract uses; a nested
    helper (the fork's own _sleep_until) belongs to its parent's body.
    """
    lines = source.splitlines()
    funcs, i = {}, 0
    while i < len(lines):
        m = FUNC_DEF_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j] != "}":
            j += 1
        funcs[m.group(1)] = (i + 1, "\n".join(lines[i:j + 1]))
        i = j + 1
    return funcs


def top_level_call_line(source, funcs, name):
    """Line of the first call to `name` that sits outside every function body."""
    spans = [(start, start + body.count("\n")) for start, body in funcs.values()]
    for n, line in enumerate(source.splitlines(), 1):
        if re.match(rf"^\s*{re.escape(name)}\b", line) and not any(a <= n <= b for a, b in spans):
            return n
    raise AssertionError(f"no top-level call to {name} in the loop")


def watchdog_fork_body(funcs):
    """The `( … ) >>…deadline-watchdog.log 2>&1 </dev/null &` subshell."""
    assert "arm_deadline_watchdog" in funcs, (
        "arm_deadline_watchdog is not indexed as a top-level `name() {` … `}` definition")
    body = funcs["arm_deadline_watchdog"][1].splitlines()
    open_at = body.index("  (")
    close_at = next(i for i, line in enumerate(body) if line.startswith("  ) >>"))
    return "\n".join(body[open_at + 1:close_at])


def reached_functions(root_text, funcs):
    seen, todo = set(), [root_text]
    while todo:
        text = _strip_comments(todo.pop())
        for tok in set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)):
            if tok in funcs and tok not in seen:
                seen.add(tok)
                todo.append(funcs[tok][1])
    return seen


def fork_visibility(source):
    """(arm line, reached function names, [(name, definition line)] defined
    AFTER the arm line — the ones the fork cannot see)."""
    funcs = function_index(source)
    arm = top_level_call_line(source, funcs, "arm_deadline_watchdog")
    reached = reached_functions(watchdog_fork_body(funcs), funcs)
    late = sorted((name, funcs[name][0]) for name in reached if funcs[name][0] > arm)
    return arm, reached, late


def definitions_before_arm(source):
    """Every top-level function definition that precedes the arm line, in
    file order — exactly the function set the fork inherits, and nothing the
    main line executes."""
    funcs = function_index(source)
    arm = top_level_call_line(source, funcs, "arm_deadline_watchdog")
    return "\n".join(body for start, body in sorted(funcs.values()) if start < arm)


class ForkVisibility(unittest.TestCase):
    """The call-graph pin (v0.14.6)."""

    def test_every_function_the_watchdog_fork_reaches_is_defined_before_the_fork(self):
        arm, _reached, late = fork_visibility(SOURCE)
        self.assertEqual(
            late, [],
            "defined AFTER the top-level arm_deadline_watchdog call (line %d), so the "
            "forked watchdog cannot see them — `command not found` on the kill path: %s"
            % (arm, ", ".join(f"{n} (line {ln})" for n, ln in late)),
        )

    def test_the_parser_indexes_every_definition(self):
        # Review MINOR-2 (v0.14.6): function_index understands exactly the
        # `name() {` … `}` shape. A definition in any other shape (`function
        # name {`, `name () {`, a one-line `name() { …; }`) or a heredoc with
        # a column-0 `}` would be swallowed silently and the walk would go
        # GREEN past a real late definition — so the counts must agree, and
        # a future edit in another shape turns into a loud red here.
        # Column-0 definitions only: an INDENTED definition is a nested helper
        # (the fork's own _sleep_until, _boundary_write's _boundary_write_locked)
        # whose text is part of its parent's body and is walked with it.
        indexed = len(function_index(SOURCE))
        def_shaped = len(re.findall(r"^(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*\{", SOURCE, re.M))
        bare_close = len(re.findall(r"^\}$", SOURCE, re.M))
        self.assertEqual((indexed, def_shaped, bare_close), (indexed, indexed, indexed),
                         "a function definition the pin's parser cannot index (indexed, "
                         "definition-shaped lines, bare closing braces)")
        self.assertGreater(indexed, 50)

    def test_the_walk_reaches_the_kill_path(self):
        # Not vacuous: the graph from the subshell body must contain the
        # last-resort commit and everything v0.14.5's clause depends on.
        _arm, reached, _late = fork_visibility(SOURCE)
        for name in ("deadline_lastresort_commit", "artifact_never_landed", "normalize_deferral_keys",
                     "boundary_mark_killed", "_disarm_deploy_artifact", "unstage_transient_adds"):
            self.assertIn(name, reached)

    def test_the_pin_names_a_late_definition(self):
        synthetic = "\n".join([
            "early_helper() {", "  :", "}",
            "arm_deadline_watchdog() {",
            "  early_helper",
            "  (",
            "    # late_helper mentioned in a comment does not count",
            "    late_helper   # trailing comment",
            "  ) >>\"$log\" 2>&1 </dev/null &",
            "}",
            'arm_deadline_watchdog "$deadline"',
            "late_helper() {", "  early_helper", "  :", "}",
            "",
        ])
        arm, reached, late = fork_visibility(synthetic)
        self.assertEqual(arm, 11)
        self.assertEqual(late, [("late_helper", 12)])
        self.assertIn("early_helper", reached, "reached through the late helper's body")


class LastResortCommitAsTheForkSeesIt(unittest.TestCase):
    """The live shape (v0.14.6): the last-resort path run with exactly the
    function set the fork inherits — every definition that precedes the arm
    line, no stubs, no `command -v` escape — against an unlanded approval
    whose deferral has no key. Red on v0.14.5 (`artifact_never_landed:
    command not found`, the approval commits keyless); green on the fix."""

    def setUp(self):
        LastResortCommit.setUp(self)
        self.tearDown = lambda: LastResortCommit.tearDown(self)

    def _run_as_fork(self, source=SOURCE):
        script = "\n".join([
            "set -euo pipefail",
            f'ROOT_DIR="{self.dir}"',
            f'ARTIFACTS_DIR="{self.artifacts}"',
            f'WRAPUP_SENTINEL="{self.artifacts}/wrapup-requested"',
            f'BOUNDARY_STATE_FILE="{self.artifacts}/boundary-state.json"',
            TRANSIENTS_ARR,
            definitions_before_arm(source),
            "deadline_lastresort_commit kill",
        ])
        return _bash(script, cwd=self.dir)

    def test_a_keyless_deferral_on_an_unlanded_approval_is_keyed_through_the_kill_path(self):
        (self.root / "src.txt").write_text("v1\n")
        LastResortCommit._commit_all(self, "base")
        (self.root / "src.txt").write_text("v2 in progress\n")
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-3", "approved": True,
            "suggested_commit_message": "feat: phase 3",
            "deferrals": [{"item": "Polish the lobby animation timing later on",
                           "reason": "out of this session's bound",
                           "suggested_task": "a light follow-up"}],
        }) + "\n")
        r = self._run_as_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("command not found", r.stderr)
        self.assertIn("last-resort", LastResortCommit._git(self, "log", "-1", "--format=%s"))
        # v0.18.0 (review round 4): the approval never rides the kill wip; it
        # stays on disk, keyed, for the next start's verify-gated landing.
        self.assertNotIn("phase-approval", LastResortCommit._git(self, "ls-tree", "-r", "--name-only", "HEAD"))
        on_disk = json.loads((self.artifacts / "phase-approval.json").read_text())
        self.assertEqual(on_disk["deferrals"][0]["key"], "polish-the-lobby-animation-timing-later")

    def test_the_kill_path_stands_down_while_the_loops_own_landing_is_in_flight(self):
        # v0.18.0, review rounds 5-7: no second committer during a landing —
        # a kill leaves its verdicts on disk for the next start instead.
        (self.root / "src.txt").write_text("v1\n")
        LastResortCommit._commit_all(self, "base")
        (self.root / "src.txt").write_text("v2\n")
        (self.artifacts / "logs").mkdir(exist_ok=True)
        (self.artifacts / "logs" / ".landing-in-flight").touch()
        before = LastResortCommit._git(self, "rev-parse", "HEAD")
        r = self._run_as_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("standing down", r.stdout)
        self.assertEqual(LastResortCommit._git(self, "rev-parse", "HEAD"), before)

    def test_the_kill_path_never_commits_the_security_pair(self):
        # review round 7 (open since v0.13.0): a --no-verify wip is a path too.
        (self.root / "src.txt").write_text("v1\n")
        wf = self.root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("safe: true\n")
        LastResortCommit._commit_all(self, "base")
        (wf / "ci.yml").write_text("safe: false # model edit\n")
        (self.root / "src.txt").write_text("v2\n")
        r = self._run_as_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("last-resort", LastResortCommit._git(self, "log", "-1", "--format=%s"))
        self.assertIn("safe: true", LastResortCommit._git(self, "show", "HEAD:.github/workflows/ci.yml"))
        self.assertIn("safe: false", (wf / "ci.yml").read_text(), "kept on disk, never committed")

    def test_the_kill_path_stands_down_on_a_complete_iteration(self):
        # v0.14.9: the completion landed (record final, step 6 — the rest
        # unproven because the gate re-measured after the commit); a dirty
        # tree here is the gate's noise, not in-flight work. No wip commit,
        # no baton, the dirt left as it is. Run with the fork's function set.
        (self.root / "src.txt").write_text("v1\n")
        (self.root / "measure.txt").write_text("baseline\n")
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-9", "approved": True, "final_phase": True,
            "suggested_commit_message": "Phase 9 (APPROVED): last"}) + "\n")
        (self.artifacts / "project-complete.json").write_text(json.dumps({"done": True}) + "\n")
        LastResortCommit._commit_all(self, "Phase 9 (APPROVED): last + completion")
        (self.artifacts / "boundary-state.json").write_text(json.dumps({
            "schema": 2, "pass": 1, "iteration": 129, "branch": "master", "step": 6,
            "step_name": "armed", "phase": "phase-9", "final": True, "sha_at_step": {}}) + "\n")
        (self.root / "measure.txt").write_text("re-measured after the commit\n")
        before = LastResortCommit._git(self, "rev-parse", "HEAD")
        r = self._run_as_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("command not found", r.stderr)
        self.assertIn("standing down", r.stdout)
        self.assertEqual(LastResortCommit._git(self, "rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "measure.txt").read_text(), "re-measured after the commit\n")
        self.assertFalse((self.artifacts / "session-interrupted.json").exists())

    def test_a_session_authored_completion_record_survives_the_kill_path(self):
        # v0.14.7, the live shape of orchestrator #658: the approval landed,
        # the ship session wrote its own completion record (naming the
        # iteration), the deadline kill came before the completion commit.
        # Run with exactly the function set the fork inherits.
        (self.root / "src.txt").write_text("v1\n")
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-275", "approved": True, "final_phase": True,
            "iteration": "iteration-122",
            "suggested_commit_message": "Phase 275 (APPROVED): closes the iteration"}) + "\n")
        LastResortCommit._commit_all(self, "Phase 275 (APPROVED): closes the iteration")
        record = json.dumps({"done": True, "iteration": "iteration-122",
                             "summary": "the ship phase wrote this",
                             "suggested_commit_message": "Iteration 122 complete"}) + "\n"
        (self.artifacts / "project-complete.json").write_text(record)
        (self.root / "docs.md").write_text("close-out prose in flight\n")
        r = self._run_as_fork()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("command not found", r.stderr)
        self.assertIn("last-resort", LastResortCommit._git(self, "log", "-1", "--format=%s"))
        self.assertEqual((self.artifacts / "project-complete.json").read_text(), record)
        self.assertNotIn(
            "project-complete", LastResortCommit._git(self, "ls-tree", "-r", "--name-only", "HEAD"))
        self.assertIn("?? artifacts/project-complete.json",
                      LastResortCommit._git(self, "status", "--porcelain", "--untracked-files=all"))


# ---------------------------------------------------------------------------
# v0.18.0: the loop takes control back (design §2.2, fork F3)
# ---------------------------------------------------------------------------
#
# Link [C] of the chain: the sentinel reached only a model that yielded, and
# 31 of 31 timeouts since 2026-09-14 never observed it. At the take-control
# point the watchdog now ends a turn that has not yielded (SIGTERM to the
# claude process run-phase.sh names in artifacts/logs/claude.pid; probed in
# scaffold-runner before it was built) and the loop lands what stands,
# verify-gated. The stub below is shaped like the real thing: run-phase.sh
# writes the pidfile and execs a "claude" that does some work and then never
# yields. Red on v0.17.0: nothing ends the turn, the loop never gets control
# back, and the session runs into its bound (the harness timeout).

import importlib.util as _ilu  # noqa: E402

_hspec = _ilu.spec_from_file_location("pk_boundary_harness_wd", Path(__file__).resolve().parent / "test_boundary_state.py")
_H = _ilu.module_from_spec(_hspec)
_hspec.loader.exec_module(_H)

TAKE_CONTROL_RUN_PHASE = """#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
mkdir -p artifacts/logs
PIDFILE="artifacts/logs/claude.pid"
trap 'rm -f "$PIDFILE"' EXIT
( echo "$BASHPID ${PHASEKIT_ITER:-manual}" > "$PIDFILE"; exec bash "$STUB_DIR/claude" ) 2>&1 | cat
"""

STUB_CLAUDE = """#!/usr/bin/env bash
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
echo "work the turn did before the deadline" >> src.txt
# never yields: a long wait (its child detached from the loop's pipes; this
# process stays the "claude" the pidfile names, as the real one does)
sleep 120 >/dev/null 2>&1 </dev/null &
echo $! > "$STUB_DIR/sleeper.pid"
wait $!
"""


class TakeControl(unittest.TestCase):
    def setUp(self):
        self.repo = _H.Repo(squash=False)
        self.addCleanup(self.repo.cleanup)
        self.repo.write("scripts/run-phase.sh", TAKE_CONTROL_RUN_PHASE, executable=True)
        (self.repo.stub / "claude").write_text(STUB_CLAUDE)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "stub")
        self.addCleanup(self._reap)

    def _reap(self):
        for pf in (self.repo.artifact("logs/claude.pid"), self.repo.stub / "sleeper.pid"):
            if pf.exists():
                try:
                    os.kill(int(pf.read_text().split()[0]), 9)
                except (OSError, ValueError):
                    pass

    def _run(self):
        import time as _t
        env = {
            "PHASEKIT_SESSION_DEADLINE": str(int(_t.time()) + 25),
            "PHASEKIT_WRAPUP_LEAD_SECONDS": "18",     # sentinel at T-18s, take-control at T-12s
            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0",
            "PHASEKIT_PACING_FLOOR_SECONDS": "1",
            "PHASEKIT_ITER_RETRY": "1",               # a CLI retry WOULD be available
            "MAX_ITERATIONS": "3",
        }
        return self.repo.run(env=env, timeout=45)

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("deadline watchdog: took control", out)
        self.assertIn("Run wrapped up cleanly (take-control)", out)
        self.assertIn("session wrap-up", self.repo.git("log", "-1", "--format=%s"))
        self.assertIn("work the turn did", self.repo.git("show", "HEAD:src.txt"))
        self.assertGreaterEqual(self.repo.verify_calls(), 1, "the wrap-up is verify-gated")
        self.assertEqual(self.repo.porcelain(), [], "the turn's work landed; nothing is stranded")
        wd = self.repo.artifact("logs/deadline-watchdog.log").read_text()
        self.assertIn("took control at T-12s", wd, "a third of a short lead stays the model's (review round 2)")
        ledger = json.loads(self.repo.artifact("logs/cost-ledger.json").read_text())
        self.assertEqual(ledger["sessions"][-1]["exit"], "took-control")

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.repo.calls(), 1, "the ended turn is not re-invoked\n" + out)
        self.assertNotIn("retrying in continue mode", out)
        self.assertNotIn("asking once for a verdict", out)


STUB_CLAUDE_SILENT_THEN_STUCK = """#!/usr/bin/env bash
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
echo "work of call $n" >> src.txt
if [ "$n" -ge 2 ]; then
  sleep 120 >/dev/null 2>&1 </dev/null &
  echo $! > "$STUB_DIR/sleeper.pid"
  wait $!
fi
"""


class TakeControlDuringTheVerdictRequest(TakeControl):
    """Review round 2 (MAJOR): the turn ended at take-control was the
    no-verdict retry's; the run ended exit 1 with a dirty tree."""

    def setUp(self):
        super().setUp()
        (self.repo.stub / "claude").write_text(STUB_CLAUDE_SILENT_THEN_STUCK)

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("the verdict request's turn was ended", out)
        self.assertEqual(self.repo.porcelain(), [], out)

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        self.assertEqual(self.repo.calls(), 2, r.stdout + r.stderr)


STUB_CLAUDE_LIGHT = r"""#!/usr/bin/env bash
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
if [ "$PHASEKIT_ITER" = light-review ]; then
  sleep 1
  echo '{"done": true, "summary": "light task done (reviewed)"}' > artifacts/project-complete.json
  exit 0
fi
echo "work of the light build" >> src.txt
echo '{"done": true, "summary": "light task done"}' > artifacts/project-complete.json
sleep 120 >/dev/null 2>&1 </dev/null &
echo $! > "$STUB_DIR/sleeper.pid"
wait $!
"""


class TakeControlThenAFinishedReview(TakeControl):
    """Review round 2 (MAJOR): after the BUILD turn was ended, a light review
    that finished on its own was never landed (a sticky flag)."""

    def setUp(self):
        super().setUp()
        (self.repo.stub / "claude").write_text(STUB_CLAUDE_LIGHT)

    def _run(self):
        import time as _t
        return self.repo.run(env={
            "PHASEKIT_SESSION_DEADLINE": str(int(_t.time()) + 40),
            "PHASEKIT_WRAPUP_LEAD_SECONDS": "30",
            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0",
            "PHASEKIT_PACING_FLOOR_SECONDS": "1",
            "PHASEKIT_ITERATION_MODE": "light",
            "MAX_ITERATIONS": "3",
        }, timeout=60)

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("took control", out)
        self.assertIn("Run finished successfully.", out)
        self.assertTrue(self.repo.tracked("artifacts/project-complete.json", "HEAD"), out)
        self.assertIn("reviewed", self.repo.git("show", "HEAD:artifacts/project-complete.json"))

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        self.assertEqual(self.repo.calls(), 2, r.stdout + r.stderr)   # the build turn + its review


STUB_CLAUDE_REVIEW_STUCK = r"""#!/usr/bin/env bash
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
if [ "$PHASEKIT_ITER" = light-review ]; then
  sleep 120 >/dev/null 2>&1 </dev/null &
  echo $! > "$STUB_DIR/sleeper.pid"
  wait $!
  exit 0
fi
echo "work of the light build" >> src.txt
jq -n '{done: true, summary: "light task done", closes: ["OLD-1"],
        deferrals: [{item: "later", reason: "r", suggested_task: "t", key: "NEW-1"}]}' > artifacts/project-complete.json
"""


class TakeControlOfTheLightReview(TakeControl):
    """Review round 3 (MAJOR): an ENDED review never lands the completion,
    leaves no completion record at exit 0, and leaks none of its derived
    state (the ledger, the evidence) into the wrap-up."""

    def setUp(self):
        super().setUp()
        (self.repo.stub / "claude").write_text(STUB_CLAUDE_REVIEW_STUCK)

    def _run(self):
        import time as _t
        return self.repo.run(env={
            "PHASEKIT_SESSION_DEADLINE": str(int(_t.time()) + 25),
            "PHASEKIT_WRAPUP_LEAD_SECONDS": "18",
            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0",
            "PHASEKIT_PACING_FLOOR_SECONDS": "1",
            "PHASEKIT_ITERATION_MODE": "light",
            "MAX_ITERATIONS": "3",
        }, timeout=60)

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("the light review's turn was ended before it finished", out)
        self.assertIn("session wrap-up", self.repo.git("log", "-1", "--format=%s"))
        self.assertIn("work of the light build", self.repo.git("show", "HEAD:src.txt"))
        tracked = self.repo.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertNotIn("artifacts/project-complete.json", tracked)
        self.assertNotIn("artifacts/deferrals.json", tracked, "no derived state from an unlanded record")
        self.assertFalse(self.repo.artifact("project-complete.json").exists(), "no completion on disk at exit 0")

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        self.assertEqual(self.repo.calls(), 2, r.stdout + r.stderr)


class TakeControlThenARedLanding(TakeControlThenAFinishedReview):
    """Review round 9 (MAJOR): after a take-control the landing went red and
    the wrap-up gated the same work again with no turn between — a second
    breaker attempt (light mode: phase-blocked.json with zero repair turns)."""

    def setUp(self):
        super().setUp()
        self.repo.write("scripts/phasekit-verify.sh", _H.VERIFY_LOGGING, executable=True)
        self.repo.write("BAD", "red\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "a red gate")

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.repo.verify_calls(), 1, out)
        self.assertIn("not re-running it; the red stands", out)
        self.assertFalse(self.repo.artifact("phase-blocked.json").exists(), out)
        self.assertIn("UNVERIFIED", out)

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        self.assertEqual(self.repo.calls(), 2, r.stdout + r.stderr)


class TakeControlThenARedContractsGate(TakeControlThenAFinishedReview):
    """Review round 10 (MAJOR): the contracts gate's red returned before the
    loop recorded the red, so the wrap-up after a take-control re-gated the
    same tree with no turn between (breaker 2: phase-blocked.json)."""

    def setUp(self):
        super().setUp()
        self.repo.write("scripts/phasekit-verify.sh", _H.VERIFY_LOGGING, executable=True)
        # A declared contracts.yaml with no checker: the gate refuses (v0.7.1).
        self.repo.write("contracts.yaml", "depends_on: []\n")
        self.repo.git("rm", "-q", "--cached", "--ignore-unmatch", "scripts/phasekit-contracts.py")
        for c in pathlib.Path(self.repo.repo).glob("scripts/phasekit-contracts.py"):
            c.unlink()
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "declare contracts")

    def test_a_turn_that_has_not_yielded_is_ended_at_take_control_and_the_loop_wraps_up_verified(self):
        r = self._run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("not re-running it; the red stands", out)
        self.assertFalse(self.repo.artifact("phase-blocked.json").exists(), out)

    def test_a_deadline_yield_is_not_retried_as_a_cli_failure(self):
        r = self._run()
        self.assertEqual(self.repo.calls(), 2, r.stdout + r.stderr)


class CostLine(unittest.TestCase):
    def test_light_mode_keeps_a_window_before_the_build_turn_is_ended(self):
        # Review round 2 (MAJOR): the build turn was ended the same second the
        # sentinel armed whenever the cap bound.
        repo = _H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("scripts/phasekit-verify.sh", _H.VERIFY_LOGGING, executable=True)
        import time as _t
        r = repo.run(env={"PHASEKIT_SESSION_DEADLINE": str(int(_t.time()) + 3600), "MAX_ITERATIONS": "1",
                          "PHASEKIT_ITERATION_MODE": "light"})
        armed = [ln for ln in r.stdout.splitlines() if ln.startswith("deadline watchdog: armed")][0]
        lead = int(re.search(r"sentinel at T-(\d+)s", armed).group(1))
        build = int(re.search(r"build turn T-(\d+)s", armed).group(1))
        self.assertLessEqual(build, lead - 60, armed)

    def test_every_armed_session_prints_the_cost_line_and_the_take_control_point(self):
        repo = _H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        import time as _t
        r = repo.run(env={"PHASEKIT_SESSION_DEADLINE": str(int(_t.time()) + 7200), "MAX_ITERATIONS": "1"})
        out = r.stdout
        armed = [ln for ln in out.splitlines() if ln.startswith("deadline watchdog: armed")]
        self.assertEqual(len(armed), 1, out)
        self.assertRegex(armed[0], r"sentinel at T-\d+s, last-resort commit at T-60s \(span \d+s\), take control at T-\d+s")
        cost = [ln for ln in out.splitlines() if ln.startswith("phasekit-cost: {")]
        self.assertEqual(len(cost), 1, out)
        line = json.loads(cost[0][len("phasekit-cost: "):])
        for key in ("bound_s", "lead_s", "take_control_s", "lead_uncapped_s", "lead_cap_s",
                    "at_cap", "heavy", "heavy_in_previous_4", "p90_s", "samples"):
            self.assertIn(key, line)
        self.assertEqual(line["lead_cap_s"], line["bound_s"] * 25 // 100)
        self.assertFalse(line["heavy"], "priors are never evidence that a suite is too heavy")


if __name__ == "__main__":
    unittest.main()
