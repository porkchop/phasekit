#!/usr/bin/env python3
"""Tests for branch-per-iteration + squash-to-target (v0.14.0).

The mechanism (scripts/run-until-done.sh, the block above
staged_touches_security_pair): with PHASEKIT_SQUASH_TARGET set the loop works
on a work branch, and every approval-class commit is squashed onto the target
as ONE commit followed by a merge-back on the branch. Nothing is rewritten or
force-pushed; the target moves only through the squash, and any other movement
fails the next squash closed (phase-blocked.json, blocker_kind
branch-integrity).

The bash is exercised for real: the whole function block is extracted from the
shipped script by its own delimiters and run against scratch git repositories,
so these tests break iff the shipped code does. `run_verify_gate` is stubbed
(its own tests live elsewhere); everything else is the real thing.

Run from the repo root: `python3 -m unittest tests.test_branch_squash`
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
CONTAINER_SETUP = REPO_ROOT / "scripts" / "container-setup.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
EXEC_MODES = REPO_ROOT / "docs" / "EXECUTION_MODES.md"

SOURCE = LOOP_SCRIPT.read_text()


def _extract_block(start_re, end_re):
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


# Everything from the block header up to (not including) the next function
# that is not ours.
FUNCTIONS = _extract_block(r"^# --- Branch-per-iteration \+ squash-to-target",
                           r"^staged_touches_security_pair\(\) \{")

VERIFY_STUB = '''
run_verify_gate() {
  echo "verify-stub called" >> "$ARTIFACTS_DIR/logs/verify-calls"
  [[ "${VERIFY_STUB_RC:-0}" == "0" ]]
}
print_json_summary() { cat "$1"; }
'''


class Fixture(unittest.TestCase):
    """A scratch repo on `main` with one base commit and an artifacts dir."""

    target = "main"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-squash-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", f"refs/heads/{self.target}")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.artifacts = self.repo / "artifacts"
        (self.artifacts / "logs").mkdir(parents=True)
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    # -- helpers ---------------------------------------------------------
    def git(self, *args, check=True, repo=None):
        r = subprocess.run(["git", "-C", str(repo or self.repo), *args],
                           capture_output=True, text=True)
        if check:
            self.assertEqual(r.returncode, 0, f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def bash(self, body, env=None, target=None):
        prelude = [
            f'cd "{self.repo}"',
            f'ARTIFACTS_DIR="{self.artifacts}"',
            f'SQUASH_TARGET="{self.target if target is None else target}"',
            'ITERATION_MODE="standard"',
            'VERIFY_MAX_ATTEMPTS=3',
            'BRANCH_INTEGRITY_BLOCKED=0',
        ]
        script = "\n".join(prelude) + "\n" + VERIFY_STUB + "\n" + FUNCTIONS + "\n" + body
        full_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
        full_env.pop("PHASEKIT_WORK_BRANCH", None)
        full_env.pop("PHASEKIT_SQUASH_TARGET", None)
        if env:
            full_env.update(env)
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=60, env=full_env)

    def commit(self, name, content="x\n", msg=None):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-qm", msg or f"edit {name}")
        return self.git("rev-parse", "HEAD")

    def write_artifact(self, name, message):
        (self.artifacts / name).write_text(json.dumps(
            {"phase": "phase-1", "suggested_commit_message": message}) + "\n")

    def head_branch(self):
        return self.git("symbolic-ref", "-q", "--short", "HEAD", check=False) or "HEAD"

    def tree(self, ref):
        return self.git("rev-parse", f"{ref}^{{tree}}")

    def count(self, ref):
        return int(self.git("rev-list", "--count", ref))

    def parents(self, ref):
        return self.git("rev-list", "--parents", "-n", "1", ref).split()[1:]

    def blocked(self):
        f = self.artifacts / "phase-blocked.json"
        return json.loads(f.read_text()) if f.exists() else None

    def start_work_branch(self, name="iter/1-test"):
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": name})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.head_branch(), name)
        return name


class FlagOff(Fixture):
    def test_unset_target_makes_every_function_a_no_op(self):
        head = self.git("rev-parse", "HEAD")
        r = self.bash(
            'ensure_work_branch; a=$?; squash_applies_to artifacts/phase-approval.json; b=$?; '
            'squash_pending; c=$?; squash_to_target "m" 1; d=$?; ensure_squashed_or_block 0 completion; e=$?; '
            'echo "$a $b $c $d $e"', target="")
        self.assertEqual(r.stdout.strip(), "0 1 1 0 0", r.stderr)
        self.assertEqual(self.head_branch(), "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("branch", "--list").split(), ["*", "main"])
        self.assertIsNone(self.blocked())

    def test_flag_off_push_path_is_the_original_plain_push(self):
        block = _extract_block(r"^auto_push_if_enabled\(\) \{", r"^\}")
        self.assertIn("if squash_mode; then", block)
        self.assertIn("  if git push 2>&1; then", block)


class WorkBranch(Fixture):
    def test_standing_on_target_creates_the_named_work_branch(self):
        name = self.start_work_branch("iter/7-slug")
        self.assertEqual(self.git("rev-parse", name), self.git("rev-parse", "main"))

    def test_standing_on_target_without_a_name_creates_a_stamped_branch(self):
        r = self.bash("ensure_work_branch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(self.head_branch(), r"^iter/\d{8}T\d{6}Z$")

    def test_already_on_the_named_work_branch_is_a_no_op(self):
        self.git("checkout", "-q", "-b", "iter/3-x")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/3-x"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head_branch(), "iter/3-x")

    def test_on_a_different_branch_than_named_blocks(self):
        self.git("checkout", "-q", "-b", "iter/2-old")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/3-new"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.blocked()["blocker_kind"], "branch-integrity")
        self.assertIn("iter/2-old", self.blocked()["reason"])
        self.assertEqual(self.head_branch(), "iter/2-old")

    def test_missing_target_branch_blocks(self):
        r = self.bash("ensure_work_branch", target="develop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a local branch", self.blocked()["reason"])

    def test_detached_head_blocks(self):
        self.git("checkout", "-q", "--detach")
        r = self.bash("ensure_work_branch")
        self.assertEqual(r.returncode, 1)
        self.assertIn("detached", self.blocked()["reason"])

    def test_reentering_a_fully_merged_work_branch_from_target_is_allowed(self):
        self.git("branch", "iter/1-done", "main")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/1-done"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head_branch(), "iter/1-done")

    def test_reentering_a_finished_branch_after_the_target_advanced_is_allowed(self):
        # MINOR-4: after completion HEAD rests on the target and an intake
        # commit lands there; re-using the branch name must still be fine.
        self.git("branch", "iter/1-done", "main")
        self.commit("docs/PHASES.md", msg="iteration 2 intake")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/1-done"})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.head_branch(), "iter/1-done")
        self.assertEqual(self.git("diff", "--stat", "main", "HEAD"), "")

    def test_reentering_an_unmerged_work_branch_from_target_blocks(self):
        self.git("checkout", "-q", "-b", "iter/1-wip")
        self.commit("src/a.txt")
        self.git("checkout", "-q", "main")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/1-wip"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not carry", self.blocked()["reason"])
        self.assertEqual(self.head_branch(), "main")

    def test_target_moved_out_of_band_blocks_at_loop_start_before_any_token(self):
        self.git("checkout", "-q", "-b", "iter/1-x")
        self.commit("src/a.txt")
        self.git("checkout", "-q", "main")
        self.commit("hotfix.txt", msg="hand commit on main")
        self.git("checkout", "-q", "iter/1-x")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": "iter/1-x"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("out-of-band", self.blocked()["reason"])


class Squash(Fixture):
    def setUp(self):
        super().setUp()
        self.branch = self.start_work_branch()
        self.base_main = self.git("rev-parse", "main")

    def approve(self, files, message):
        for f in files:
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(f"{message}\n")
        self.write_artifact("phase-approval.json", message)
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def test_one_squash_commit_per_approval_with_message_trailer_and_merge_back(self):
        self.commit("src/checkpoint.txt", msg="phase-1 (in progress): checkpoint")
        self.approve(["src/a.txt"], "Phase 1 (APPROVED): the first phase")
        branch_tip = self.git("rev-parse", "HEAD")
        r = self.bash('squash_to_target "Phase 1 (APPROVED): the first phase" 1')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        # target: exactly one new commit, parented on the old tip (no rewrite)
        self.assertEqual(self.count("main"), 2)
        self.assertEqual(self.parents("main"), [self.base_main])
        body = self.git("log", "-1", "--format=%B", "main")
        self.assertTrue(body.startswith("Phase 1 (APPROVED): the first phase"))
        self.assertIn(f"phasekit-squash: {self.branch}@{branch_tip[:7]}", body)
        # trees identical; branch has a merge-back with the target as 2nd parent
        self.assertEqual(self.tree("main"), self.tree("HEAD"))
        self.assertEqual(self.git("diff", "--stat", "main", "HEAD"), "")
        self.assertEqual(self.parents("HEAD"), [branch_tip, self.git("rev-parse", "main")])
        self.assertIn("merge-back main after squash", self.git("log", "-1", "--format=%s"))
        # checkpoint history survives on the branch; tree clean; still on branch
        self.assertIn("checkpoint", self.git("log", "--format=%s", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.head_branch(), self.branch)
        self.assertIsNone(self.blocked())

    def test_second_squash_diffs_only_the_second_phase(self):
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        self.assertEqual(self.bash('squash_to_target "Phase 1 (APPROVED)" 1').returncode, 0)
        self.commit("src/b-wip.txt", msg="phase-2 checkpoint")
        self.approve(["src/b.txt"], "Phase 2 (APPROVED)")
        r = self.bash('squash_to_target "Phase 2 (APPROVED)" 1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count("main"), 3)
        touched = set(self.git("diff", "--name-only", "main^", "main").split())
        self.assertEqual(touched, {"src/b-wip.txt", "src/b.txt", "artifacts/phase-approval.json"})
        self.assertEqual(self.git("diff", "--stat", "main", "HEAD"), "")

    def test_already_squashed_tree_is_a_no_op(self):
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        self.assertEqual(self.bash('squash_to_target "m" 1').returncode, 0)
        head = self.git("rev-parse", "HEAD")
        r = self.bash('squash_to_target "m" 1')
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to squash", r.stdout)
        self.assertEqual(self.count("main"), 2)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_out_of_band_move_of_target_refuses_and_touches_nothing(self):
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        branch_tip = self.git("rev-parse", "HEAD")
        # a hand commit on main behind the loop's back
        self.git("checkout", "-q", "main")
        hand = self.commit("hotfix.txt", msg="hand commit")
        self.git("checkout", "-q", self.branch)
        r = self.bash('squash_to_target "Phase 1 (APPROVED)" 1')
        self.assertEqual(r.returncode, 1)
        b = self.blocked()
        self.assertEqual(b["blocker_kind"], "branch-integrity")
        self.assertIn("out-of-band", b["reason"])
        self.assertIn("git merge main", b["next_step"])
        self.assertEqual(self.git("rev-parse", "main"), hand)
        self.assertEqual(self.git("rev-parse", "HEAD"), branch_tip)

    def test_operator_merge_of_target_into_branch_unblocks_the_squash(self):
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        self.git("checkout", "-q", "main")
        hand = self.commit("hotfix.txt", msg="hand commit")
        self.git("checkout", "-q", self.branch)
        self.assertEqual(self.bash('squash_to_target "m" 1').returncode, 1)
        self.git("merge", "-q", "--no-edit", "main")
        r = self.bash('rm -f "$ARTIFACTS_DIR/phase-blocked.json"; squash_to_target "Phase 1 (APPROVED)" 1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.parents("main"), [hand])
        self.assertTrue((self.repo / "hotfix.txt").exists())
        self.assertEqual(self.git("diff", "--stat", "main", "HEAD"), "")

    def test_interrupted_squash_is_completed_not_mistaken_for_out_of_band(self):
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        branch_tip = self.git("rev-parse", "HEAD")
        # simulate the kill between the two ref updates: target at S, no merge-back
        # (the trailer is what makes S a distinct object from the branch commit —
        # same tree, parent and message in the same second would be the SAME sha)
        s = self.git("commit-tree", self.tree("HEAD"), "-p", "main", "-m", "Phase 1 (APPROVED)",
                     "-m", f"phasekit-squash: {self.branch}@{branch_tip[:7]}")
        self.git("update-ref", "refs/heads/main", s)
        r = self.bash('squash_to_target "Phase 1 (APPROVED)" 1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("completing an interrupted squash", r.stdout)
        self.assertEqual(self.git("rev-parse", "main"), s)  # target not moved again
        self.assertEqual(self.parents("HEAD"), [branch_tip, s])
        self.assertIsNone(self.blocked())

    def test_interrupted_squash_is_still_recognised_after_the_branch_gained_commits(self):
        # MINOR-1: an intake or strand commit after the kill must not turn the
        # repair into a false "moved out-of-band".
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        branch_tip = self.git("rev-parse", "HEAD")
        s = self.git("commit-tree", self.tree("HEAD"), "-p", "main", "-m", "Phase 1 (APPROVED)",
                     "-m", f"phasekit-squash: {self.branch}@{branch_tip[:7]}")
        self.git("update-ref", "refs/heads/main", s)
        later = self.commit("src/strand.txt", msg="wip: strand landed after the kill")
        r = self.bash("ensure_work_branch", env={"PHASEKIT_WORK_BRANCH": self.branch})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("completing an interrupted squash", r.stdout)
        self.assertIsNone(self.blocked())
        self.assertEqual(self.git("rev-parse", "main"), s)
        self.assertEqual(self.parents("HEAD"), [later, s])
        self.assertTrue((self.repo / "src" / "strand.txt").exists())
        self.assertIn("src/strand.txt", self.git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_unrelated_target_holding_the_same_tree_is_refused_not_reported_squashed(self):
        # MINOR-5: guard 1 runs before the nothing-to-squash short-circuit.
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        orphan = self.git("commit-tree", self.tree("HEAD"), "-m", "unrelated root with the same tree")
        self.git("update-ref", "refs/heads/main", orphan)
        r = self.bash('squash_to_target "Phase 1 (APPROVED)" 1')
        self.assertEqual(r.returncode, 1)
        self.assertIn("out-of-band", self.blocked()["reason"])
        self.assertNotIn("nothing to squash", r.stdout)

    def test_stale_block_from_last_session_does_not_masquerade_as_a_new_one(self):
        # MINOR-3: a red verify at catch-up writes no block; the loop-start
        # sites must branch on the in-process signal, not the stale file.
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="wip")
        (self.artifacts / "phase-blocked.json").write_text('{"blocked": true, "reason": "STALE"}')
        r = self.bash('ensure_squashed_or_block 0; rc=$?; echo "rc=$rc blocked=$BRANCH_INTEGRITY_BLOCKED"',
                      env={"VERIFY_STUB_RC": "1"})
        self.assertIn("rc=1 blocked=0", r.stdout)
        self.git("checkout", "-q", "main")
        self.commit("hotfix.txt", msg="hand commit")
        self.git("checkout", "-q", self.branch)
        r = self.bash('ensure_squashed_or_block 0; rc=$?; echo "rc=$rc blocked=$BRANCH_INTEGRITY_BLOCKED"')
        self.assertIn("rc=1 blocked=1", r.stdout)

    def test_merge_back_parent_and_tree_come_from_one_head_snapshot(self):
        # MINOR-2: structural — head is read once, tree derived from it.
        fn = _extract_block(r"^merge_back_from_target\(\) \{", r"^\}")
        self.assertLess(fn.index('head="$(git rev-parse HEAD)"'), fn.index('tree="$(git rev-parse "$head^{tree}")"'))
        sq = _extract_block(r"^squash_to_target\(\) \{", r"^\}")
        self.assertIn('head="$(git rev-parse HEAD)"', sq)
        self.assertNotIn('git rev-parse "HEAD^{tree}"', sq)
        self.assertIn('$(git rev-parse --short "$head")', sq)

    def test_remote_target_ahead_refuses_even_when_local_ancestry_is_fine(self):
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "origin", "main")
        # someone else pushes to origin/main
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(bare), str(other)], check=True)
        self.git("config", "user.email", "o@o", repo=other)
        self.git("config", "user.name", "o", repo=other)
        (other / "elsewhere.txt").write_text("x\n")
        self.git("add", "-A", repo=other)
        self.git("commit", "-qm", "pushed from elsewhere", repo=other)
        self.git("push", "-q", "origin", "HEAD:main", repo=other)
        self.approve(["src/a.txt"], "Phase 1 (APPROVED)")
        main_before = self.git("rev-parse", "main")
        r = self.bash('squash_to_target "Phase 1 (APPROVED)" 1')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("origin/main", self.blocked()["reason"])
        self.assertEqual(self.git("rev-parse", "main"), main_before)


class Pending(Fixture):
    def setUp(self):
        super().setUp()
        self.branch = self.start_work_branch()

    def pending(self):
        return self.bash("squash_pending").returncode == 0

    def test_checkpoints_alone_are_not_pending(self):
        self.commit("src/wip.txt", msg="checkpoint")
        self.assertFalse(self.pending())

    def test_committed_approval_the_target_lacks_is_pending_until_squashed(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="Phase 1 (APPROVED)")
        self.assertTrue(self.pending())
        self.assertEqual(self.bash('squash_to_target "Phase 1 (APPROVED)" 1').returncode, 0)
        self.assertFalse(self.pending())

    def test_catch_up_uses_the_artifacts_message_and_runs_verify_first(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED): landed via a strand commit")
        self.commit("src/a.txt", msg="wip: strand")
        r = self.bash("ensure_squashed_or_block 0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.artifacts / "logs" / "verify-calls").exists())
        self.assertTrue(self.git("log", "-1", "--format=%s", "main")
                        .startswith("Phase 1 (APPROVED): landed via a strand commit"))

    def test_catch_up_with_red_verify_defers_without_blocking(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="wip")
        main_before = self.git("rev-parse", "main")
        r = self.bash("ensure_squashed_or_block 0", env={"VERIFY_STUB_RC": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(self.blocked())
        self.assertEqual(self.git("rev-parse", "main"), main_before)

    def test_verified_commit_path_does_not_rerun_verify(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="Phase 1 (APPROVED)")
        r = self.bash("ensure_squashed_or_block 1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.artifacts / "logs" / "verify-calls").exists())

    def test_completion_squashes_and_rests_on_the_target_keeping_the_branch(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="Phase 1 (APPROVED)")
        self.assertEqual(self.bash("ensure_squashed_or_block 1").returncode, 0)
        self.write_artifact("project-complete.json", "Project complete: v0 shipped")
        self.commit("src/final.txt", msg="Project complete: v0 shipped")
        r = self.bash("ensure_squashed_or_block 1 completion")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head_branch(), "main")
        self.assertTrue(self.git("log", "-1", "--format=%s", "main").startswith("Project complete: v0 shipped"))
        self.assertEqual(self.git("diff", "--stat", "main", self.branch), "")
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_completion_with_nothing_pending_still_rests_on_the_target(self):
        self.write_artifact("phase-approval.json", "Phase 1 (APPROVED)")
        self.commit("src/a.txt", msg="Phase 1 (APPROVED)")
        self.assertEqual(self.bash("ensure_squashed_or_block 1").returncode, 0)
        r = self.bash("ensure_squashed_or_block 0 completion")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head_branch(), "main")


class StructuralPins(unittest.TestCase):
    def test_commit_path_squashes_approval_class_commits_before_push(self):
        block = _extract_block(r"^commit_from_artifact\(\) \{", r"^\}")
        i = block.index('git commit -m "$msg"')
        j = block.index('squash_applies_to "$file"')
        k = block.index("auto_push_if_enabled")
        self.assertTrue(i < j < k)
        self.assertIn('if ! squash_to_target "$msg" 1; then', block)
        # MINOR-10: a refused squash still pushes the branch commit under AUTO_PUSH
        refuse = block.index('if ! squash_to_target "$msg" 1; then')
        self.assertIn("auto_push_if_enabled", block[refuse:block.index("return 1", refuse)])

    def test_work_branch_is_ensured_before_stranded_recovery_and_catch_up_after(self):
        head = SOURCE.index("if ! ensure_work_branch; then")
        stranded = SOURCE.index('if artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"; then')
        catch_up = SOURCE.index("ensure_squashed_or_block 0; then", stranded)
        loop = SOURCE.index('while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do')
        self.assertTrue(head < stranded < catch_up < loop)

    def test_every_successful_finish_passes_through_the_completion_squash(self):
        finishes = [m.start() for m in re.finditer(r'echo "Run finished successfully\."', SOURCE)]
        self.assertEqual(len(finishes), 3)
        for pos in finishes:
            window = SOURCE[max(0, pos - 700):pos]
            self.assertIn("ensure_squashed_or_block", window)
            self.assertTrue("completion; then" in window or "rest_on_target" in window)

    def test_catch_up_of_a_completion_finishes_the_run_instead_of_entering_the_loop(self):
        # MAJOR-2 (v0.14.0 review): a caught-up completion must not fall into
        # the while loop, whose cleanup deletes project-complete.json.
        start = SOURCE.index("if [[ -z \"$PENDING_COMMIT_RETRY\" ]] && squash_pending; then")
        loop = SOURCE.index('while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do')
        block = SOURCE[start:loop]
        self.assertIn("completion_owed=1", block)
        self.assertIn("rest_on_target", block)
        self.assertIn('echo "Run finished successfully."', block)
        self.assertIn('[[ "$BRANCH_INTEGRITY_BLOCKED" -eq 1 ]]', block)
        self.assertNotIn('-f "$ARTIFACTS_DIR/phase-blocked.json"', block)

    def test_work_branch_is_ensured_before_the_heal_commit(self):
        # MINOR-6: the heal commit is a loop-made commit; it rides the branch.
        self.assertLess(SOURCE.index("if ! ensure_work_branch; then"),
                        SOURCE.index("heal_tracked_transients || true"))

    def test_light_mode_labels_a_branch_integrity_block_honestly(self):
        fn = _extract_block(r"^maybe_escalate_light_commit\(\) \{", r"^\}")
        self.assertIn('write_light_escalation "branch_integrity"', fn)
        self.assertLess(fn.index("branch_integrity"), fn.index("verify_failures"))

    def test_checkpoints_never_squash(self):
        fn = _extract_block(r"^squash_applies_to\(\) \{", r"^\}")
        self.assertIn("phase-approval.json|project-complete.json", fn)
        self.assertNotIn("phase-update", fn)

    def test_env_is_declared_and_forwarded(self):
        m = json.loads(MANIFEST.read_text())
        by_name = {e["name"]: e for e in m["env"]}
        for name in ("PHASEKIT_SQUASH_TARGET", "PHASEKIT_WORK_BRANCH"):
            self.assertIn(name, by_name)
            self.assertTrue(by_name[name]["container_forwarded"])
            self.assertIn(f'-e {name}="${name}"', CONTAINER_SETUP.read_text())
        conv = [c for c in m["conventions"] if c["name"] == "branch-per-iteration-squash"]
        self.assertEqual(len(conv), 1)
        self.assertIn(conv[0]["marker"], SOURCE)
        self.assertIn(conv[0]["marker"], EXEC_MODES.read_text())

    def test_only_the_squash_moves_the_target_ref(self):
        # every update-ref on the target lives inside squash_to_target
        hits = [m.start() for m in re.finditer(r'update-ref[^\n]*refs/heads/\$SQUASH_TARGET', SOURCE)]
        fn_start = SOURCE.index("squash_to_target() {")
        fn_end = SOURCE.index("\n}\n", fn_start)
        self.assertEqual(len(hits), 1)
        self.assertTrue(fn_start < hits[0] < fn_end)
        self.assertNotIn("push --force", SOURCE)
        self.assertNotIn("push -f", SOURCE)


if __name__ == "__main__":
    unittest.main()
