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
        committed = json.loads(LastResortCommit._git(self, "show", "HEAD:artifacts/phase-approval.json"))
        self.assertEqual(committed["deferrals"][0]["key"], "polish-the-lobby-animation-timing-later")
        self.assertEqual(committed["deferrals"][0]["key_derived"], "slug")
        on_disk = json.loads((self.artifacts / "phase-approval.json").read_text())
        self.assertEqual(on_disk["deferrals"][0]["key"], "polish-the-lobby-animation-timing-later")


if __name__ == "__main__":
    unittest.main()
