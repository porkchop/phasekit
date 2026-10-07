#!/usr/bin/env python3
"""The engine's lock file is runtime-only: never created by a read-only verb,
never staged, never committed, and untracked by an upgrade (v0.18.6).

`.scaffold/manifest.json.lock` is the empty flock target the engine takes
around a manifest write. docs/INSTALL_LIFECYCLE.md always said to ignore it,
but nothing did, so it went into history the ordinary way: `enrich` left it
on disk, the first `git add -A` (a person's, or the loop's) swept it in.
Measured 2026-10-06 (foundry-meta designs/DESIGN-phasekit-self-application.md,
§5.5): 8 of 9 fleet projects track it, and in a repository with no manifest
at all `phasekit upgrade --dry-run` created `.scaffold/` + the lock, which
`phasekit verify` then staged (`A .scaffold/manifest.json.lock`).

Three halves, each red on v0.18.5:

  1. a verb that finds no manifest leaves no `.scaffold/` behind;
  2. the engine excludes the lock where it creates it (.git/info/exclude,
     repo-local, ships nothing), and every loop commit path keeps a fresh
     lock out of the index;
  3. `phasekit upgrade` untracks an already-tracked lock (the file stays on
     disk) inside its own commit — once, and never by sweeping a project's
     staged work into that commit.

Run from the repo root: python3 -m unittest tests.test_scaffold_lock
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
LOCK = ".scaffold/manifest.json.lock"

_spec = importlib.util.spec_from_file_location("pk_boundary_harness_lock", TESTS / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r


def _engine(*args, env=None):
    return subprocess.run([sys.executable, str(ENRICH), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=300)


def _new_repo(prefix):
    root = Path(tempfile.mkdtemp(prefix=prefix))
    proj = root / "project"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "config", "commit.gpgsign", "false")
    return root, proj


def _in_head(proj, rel):
    return _git(proj, "cat-file", "-e", f"HEAD:{rel}", check=False).returncode == 0


def _porcelain(proj):
    return _git(proj, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()


# ---------------------------------------------------------------------------
# 1. no manifest -> no .scaffold/
# ---------------------------------------------------------------------------

class NoManifestNoScaffoldDir(unittest.TestCase):
    def test_no_verb_that_finds_no_manifest_leaves_scaffold_behind(self):
        # RED on v0.18.5: --upgrade took the lock (mkdir .scaffold + O_CREAT)
        # before it looked for the manifest — dry-run or not.
        verbs = (
            ["--upgrade", "{t}", "--dry-run"],
            ["--upgrade", "{t}", "--yes"],
            ["--status", "{t}"],
            ["--check", "{t}"],
            ["--check-version", "{t}"],
            ["--uninstall", "{t}", "--dry-run"],
            ["--migrate-only", "{t}"],
        )
        for verb in verbs:
            with self.subTest(verb=verb[0] + (" " + verb[2] if len(verb) > 2 else "")):
                root, proj = _new_repo("pk-lock-nomanifest-")
                self.addCleanup(shutil.rmtree, root, True)
                _engine(*[a.format(t=proj) for a in verb])
                self.assertFalse((proj / ".scaffold").exists(),
                                 f"{verb[0]} created .scaffold/ in a repository with no manifest")
                self.assertEqual(_porcelain(proj), [])

    def test_the_upgrade_still_says_what_is_missing(self):
        root, proj = _new_repo("pk-lock-nomanifest-")
        self.addCleanup(shutil.rmtree, root, True)
        r = _engine("--upgrade", str(proj), "--dry-run")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("No .scaffold/manifest.json", r.stderr)


# ---------------------------------------------------------------------------
# 2. never staged
# ---------------------------------------------------------------------------

class TheLockStaysOutOfTheIndex(unittest.TestCase):
    def test_a_fresh_install_leaves_the_lock_on_disk_and_out_of_git(self):
        # RED on v0.18.5: `?? .scaffold/manifest.json.lock`, and the first
        # `git add -A` tracked it — how 8 of 9 fleet projects came to.
        root, proj = _new_repo("pk-lock-enrich-")
        self.addCleanup(shutil.rmtree, root, True)
        r = _engine(str(proj))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((proj / LOCK).is_file(), "the flock target is still created")
        self.assertNotIn(f"?? {LOCK}", _porcelain(proj))
        _git(proj, "add", "-A")
        self.assertEqual(_git(proj, "ls-files", "--", LOCK).stdout.strip(), "")
        self.assertIn(".scaffold/manifest.json", _git(proj, "ls-files").stdout.split())

    def test_the_exclude_is_written_once(self):
        root, proj = _new_repo("pk-lock-enrich-")
        self.addCleanup(shutil.rmtree, root, True)
        _engine(str(proj))
        _engine(str(proj), "--reconcile", "--force")
        lines = (proj / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(lines.count(LOCK), 1, lines)

    def test_a_project_in_a_subdirectory_is_excluded_at_its_own_path(self):
        root, top = _new_repo("pk-lock-sub-")
        self.addCleanup(shutil.rmtree, root, True)
        proj = top / "apps" / "one"
        proj.mkdir(parents=True)
        r = _engine(str(proj))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(f"?? apps/one/{LOCK}", _porcelain(top))
        self.assertIn(f"apps/one/{LOCK}", (top / ".git" / "info" / "exclude").read_text().splitlines())

    def test_a_target_that_is_not_a_repository_is_fine(self):
        root = Path(tempfile.mkdtemp(prefix="pk-lock-nogit-"))
        self.addCleanup(shutil.rmtree, root, True)
        r = _engine(str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((root / ".scaffold" / "manifest.json").is_file())


class TheLoopNeverStagesTheLock(unittest.TestCase):
    """The loop's own commit paths, for a lock no engine run excluded in this
    clone (one made by an older engine, or copied in)."""

    def _repo(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write(LOCK, "")
        return repo

    def test_phasekit_verify_does_not_stage_it(self):
        # RED on v0.18.5: `A .scaffold/manifest.json.lock` after the run.
        repo = self._repo()
        r = repo.run_verb("verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(LOCK, repo.git("diff", "--cached", "--name-only").split())

    def test_a_landing_never_commits_it_and_leaves_the_tree_clean(self):
        # RED on v0.18.5: the approval commit carried the lock.
        repo = self._repo()
        repo.scenario("echo work >> src.txt\n" + H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotEqual(repo.git("rev-parse", "HEAD"), repo.base, out)
        self.assertFalse(_in_head(repo.repo, LOCK), out)
        self.assertNotIn(f"?? {LOCK}", _porcelain(repo.repo), out)

    def test_a_tracked_lock_is_left_to_the_upgrade(self):
        # The loop untracks nothing it did not add: a tracked copy is the
        # upgrade's to untrack (half 3), so a session commit never carries it.
        repo = self._repo()
        repo.git("add", "-f", LOCK)
        repo.git("commit", "-qm", "tracked lock")
        repo.scenario("echo work >> src.txt\n" + H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertTrue(_in_head(repo.repo, LOCK), r.stdout + r.stderr)

    def test_every_commit_path_unstages_it_and_the_loop_excludes_it(self):
        self.assertIn('".scaffold/manifest.json.lock"',
                      H._extract_block(r"^ensure_transients_excluded\(\) \{", r"^\}"))
        self.assertIn(".scaffold/manifest.json.lock",
                      H._extract_block(r"^unstage_transient_adds\(\) \{", r"^\}"))


# ---------------------------------------------------------------------------
# 3. an upgrade untracks a tracked lock
# ---------------------------------------------------------------------------

class TheUpgradeUntracksATrackedLock(unittest.TestCase):
    """The fleet's shape: enriched, the lock committed beside the manifest."""

    def setUp(self):
        root, self.proj = _new_repo("pk-lock-upgrade-")
        self.addCleanup(shutil.rmtree, root, True)
        r = _engine(str(self.proj))
        self.assertEqual(r.returncode, 0, r.stderr)
        _git(self.proj, "add", "-A")
        _git(self.proj, "add", "-f", LOCK)
        _git(self.proj, "commit", "-qm", "base, lock tracked as the fleet has it")
        self.assertTrue(_in_head(self.proj, LOCK))

    def age(self):
        (self.proj / ".claude" / "hooks" / "require-verdict.sh").unlink()
        _git(self.proj, "add", "-A")
        _git(self.proj, "commit", "-qm", "aged")

    def upgrade(self, *extra):
        return _engine("--upgrade", str(self.proj), "--yes", *extra)

    def head(self):
        return _git(self.proj, "rev-parse", "HEAD").stdout.strip()

    def test_untracked_in_the_upgrade_commit_and_kept_on_disk(self):
        # RED on v0.18.5: the lock stayed tracked.
        self.age()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(_in_head(self.proj, LOCK), r.stdout + r.stderr)
        self.assertTrue((self.proj / LOCK).is_file())
        self.assertEqual(_porcelain(self.proj), [], "the tree is clean after the upgrade")
        subject = _git(self.proj, "log", "-1", "--format=%s").stdout.strip()
        self.assertTrue(subject.startswith("chore(scaffold): phasekit upgrade "), subject)
        files = _git(self.proj, "show", "--name-status", "--format=", "HEAD").stdout.splitlines()
        self.assertIn(f"D\t{LOCK}", files)
        self.assertIn("A\t.claude/hooks/require-verdict.sh", files)

    def test_idempotent(self):
        self.age()
        self.upgrade()
        before = self.head()
        time.sleep(1.1)
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), before, "a second upgrade makes no commit")
        self.assertEqual(_porcelain(self.proj), [])

    def test_untracked_even_when_nothing_else_is_installed(self):
        # A project already on this release: the untracking is the commit.
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(_in_head(self.proj, LOCK), r.stdout + r.stderr)
        self.assertEqual(_porcelain(self.proj), [])
        before = self.head()
        self.assertEqual(self.upgrade().returncode, 0)
        self.assertEqual(self.head(), before)

    def test_a_clone_with_no_exclude_yet_is_excluded_before_the_untracking(self):
        # The untracked file must not surface as `??` — a dirty tree trips
        # every clean-tree guard downstream.
        (self.proj / ".git" / "info" / "exclude").write_text("")
        self.age()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(_in_head(self.proj, LOCK))
        self.assertEqual(_porcelain(self.proj), [])

    def test_a_projects_staged_work_is_never_swept_in(self):
        self.age()
        (self.proj / "work.txt").write_text("in flight\n")
        _git(self.proj, "add", "work.txt")
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head_files = _git(self.proj, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertNotIn("work.txt", head_files)
        self.assertIn("A  work.txt", _git(self.proj, "status", "--porcelain").stdout.splitlines(),
                      "the project's staged work stays staged")
        # The lock waits for an upgrade with a clean index; it is not left
        # half-untracked meanwhile.
        self.assertTrue(_in_head(self.proj, LOCK))
        self.assertEqual(_git(self.proj, "ls-files", "--", LOCK).stdout.strip(), LOCK)
        self.assertIn("lock", r.stderr)

    def test_no_commit_leaves_it_alone(self):
        self.age()
        r = self.upgrade("--no-commit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(_in_head(self.proj, LOCK))
        self.assertEqual(_git(self.proj, "ls-files", "--", LOCK).stdout.strip(), LOCK)

    def test_a_dry_run_changes_nothing(self):
        self.age()
        before = self.head()
        r = _engine("--upgrade", str(self.proj), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), before)
        self.assertEqual(_git(self.proj, "ls-files", "--", LOCK).stdout.strip(), LOCK)

    # --- review round 1 (v0.18.6, before the tag) ---------------------------

    def test_a_staged_deletion_hidden_by_rename_detection_is_never_swept(self):
        # F1: porcelain `git diff --cached --name-only` folded the project's
        # staged deletion of tools/rv.sh into a rename onto the hook the
        # upgrade re-adds, so the plain commit carried it.
        hook = ".claude/hooks/require-verdict.sh"
        (self.proj / "tools").mkdir()
        _git(self.proj, "mv", hook, "tools/rv.sh")
        _git(self.proj, "commit", "-qm", "move the hook")
        _git(self.proj, "rm", "-q", "tools/rv.sh")
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head = _git(self.proj, "show", "--name-status", "--no-renames", "--format=", "HEAD").stdout
        self.assertNotIn("tools/rv.sh", head)
        self.assertIn("D  tools/rv.sh", _git(self.proj, "status", "--porcelain").stdout.splitlines())
        self.assertTrue(_in_head(self.proj, LOCK), "the lock waits for a clean index")

    def _no_identity_env(self):
        return {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(self.proj.parent),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.useConfigOnly", "GIT_CONFIG_VALUE_0": "true"}

    def test_a_lock_only_change_that_cannot_commit_leaves_the_tree_clean(self):
        # F2: a current project with no identity, or a refusing hook, kept
        # `D lock` staged (dirty) and the hook case exited 5.
        _git(self.proj, "config", "--unset", "user.name")
        _git(self.proj, "config", "--unset", "user.email")
        env = {k: v for k, v in os.environ.items()
               if k not in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                            "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        r = subprocess.run([sys.executable, str(ENRICH), "--upgrade", str(self.proj), "--yes"],
                           capture_output=True, text=True, env={**env, **self._no_identity_env()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_porcelain(self.proj), [], r.stderr)
        self.assertTrue(_in_head(self.proj, LOCK))

    def test_a_refusing_hook_on_a_lock_only_change_is_not_exit_5(self):
        hook = self.proj / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_porcelain(self.proj), [], r.stderr)
        self.assertTrue(_in_head(self.proj, LOCK))

    def test_an_operators_merge_in_progress_is_never_concluded(self):
        # F3: the plain commit consumed MERGE_HEAD and made a two-parent
        # "phasekit upgrade" commit.
        main = _git(self.proj, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        _git(self.proj, "checkout", "-q", "-b", "side")
        (self.proj / "side.txt").write_text("side\n")
        _git(self.proj, "add", "side.txt")
        _git(self.proj, "commit", "-qm", "side")
        _git(self.proj, "checkout", "-q", main)
        _git(self.proj, "merge", "--no-commit", "--no-ff", "-s", "ours", "side")
        self.assertTrue((self.proj / ".git" / "MERGE_HEAD").exists())
        before = self.head()
        self.upgrade()
        self.assertTrue((self.proj / ".git" / "MERGE_HEAD").exists(), "the merge is still the operator's")
        self.assertEqual(self.head(), before)
        self.assertTrue(_in_head(self.proj, LOCK))

    def test_a_non_utf8_exclude_file_never_stops_the_engine(self):
        # F4: UnicodeDecodeError escaped `except OSError` -> traceback, exit 1.
        (self.proj / ".git" / "info" / "exclude").write_bytes(b"caf\xe9\n")
        self.age()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(_in_head(self.proj, LOCK))
        self.assertEqual(_porcelain(self.proj), [])


if __name__ == "__main__":
    unittest.main()
