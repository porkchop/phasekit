#!/usr/bin/env python3
"""Tests for `phasekit upgrade` committing its own work (item 3).

Leaving the tree dirty caused two distinct failures in one day:

1. IDLE PROJECTS SELF-DEADLOCK. The upgrade dirties the tree; the
   orchestrator's on-ramp refuses a dirty tree. A project that gets no sessions
   can therefore never absorb its own upgrade: dirty tree -> no session ->
   still dirty. Hit hello-foundry, steelman-meta and xmeo-v3 on the v0.7.1
   rollout and had to be fixed by hand.
2. ACTIVE PROJECTS FILE FALSE DRIFT SIGNALS. The projects whose sessions did
   absorb it committed scaffold-class files, tripping scope containment four
   times (operator tasks #130-133).

The sharp edge being tested as hard as the feature: it must stage ONLY what the
upgrade wrote. Sweeping in whatever else was dirty would hand a project's
in-flight work a commit message about the scaffold — worse than the problem.

Run from the repo root: `python3 -m unittest tests.test_upgrade_commits`
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"


class UpgradeFixture(unittest.TestCase):
    """A project enriched, committed, then artificially aged so the next
    upgrade has real files to install."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-upgcommit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        subprocess.run([sys.executable, str(ENRICH), str(self.project)],
                       capture_output=True, text=True, check=True)
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        # Age the project: drop a scaffold file so the upgrade reinstalls it.
        (self.project / ".claude" / "hooks" / "require-verdict.sh").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "aged")

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self.project), *args],
                           capture_output=True, text=True)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def upgrade(self, *extra):
        return subprocess.run(
            [sys.executable, str(ENRICH), "--upgrade", str(self.project), "--yes", *extra],
            capture_output=True, text=True)

    def head_files(self):
        return set(self.git("show", "--name-only", "--format=", "HEAD").stdout.split())

    def head_subject(self):
        return self.git("log", "-1", "--format=%s").stdout.strip()

    def porcelain(self):
        return self.git("status", "--porcelain").stdout.strip()


class CommitsItsOwnWork(UpgradeFixture):
    def test_the_tree_is_clean_after_an_upgrade(self):
        """The property that breaks the idle-project deadlock."""
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.porcelain(), "", "upgrade left the tree dirty")

    def test_the_message_uses_the_established_convention(self):
        self.upgrade()
        subject = self.head_subject()
        self.assertTrue(subject.startswith("chore(scaffold): phasekit upgrade "), subject)
        self.assertIn(" -> ", subject)

    def test_the_reinstalled_file_is_in_the_commit(self):
        self.upgrade()
        self.assertIn(".claude/hooks/require-verdict.sh", self.head_files())

    def test_the_manifest_is_committed_when_it_changes(self):
        """Not asserted unconditionally: a re-upgrade at the same scaffold
        version regenerates a byte-identical manifest, and committing that
        would be the empty-commit churn the next test forbids."""
        self.git("rm", "-q", "--cached", ".scaffold/manifest.json")
        self.git("commit", "-qm", "drop manifest from the index")
        self.upgrade()
        self.assertIn(".scaffold/manifest.json", self.head_files())

    def test_a_second_upgrade_makes_no_empty_commit(self):
        self.upgrade()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)


class NeverSweepsInUnrelatedWork(UpgradeFixture):
    def test_an_untracked_file_is_left_alone(self):
        (self.project / "my-feature.txt").write_text("in-flight\n", encoding="utf-8")
        self.upgrade()
        self.assertNotIn("my-feature.txt", self.head_files())
        self.assertIn("my-feature.txt", self.porcelain())

    def test_a_modified_project_file_is_left_alone(self):
        readme = self.project / "docs" / "SPEC.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nin-flight edit\n",
                          encoding="utf-8")
        self.upgrade()
        self.assertNotIn("docs/SPEC.md", self.head_files())
        self.assertIn("docs/SPEC.md", self.porcelain())

    def test_a_staged_change_is_not_carried_into_the_scaffold_commit(self):
        (self.project / "staged.txt").write_text("mine\n", encoding="utf-8")
        self.git("add", "staged.txt")
        self.upgrade()
        self.assertNotIn("staged.txt", self.head_files())


class OptOutAndFailSafe(UpgradeFixture):
    def test_no_commit_leaves_the_tree_dirty(self):
        r = self.upgrade("--no-commit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.porcelain(), "")
        self.assertNotIn("phasekit upgrade", self.head_subject())

    def test_a_repo_with_no_git_identity_still_upgrades(self):
        """Non-fatal: the files are already installed on disk."""
        self.git("config", "--unset", "user.email")
        self.git("config", "--unset", "user.name")
        r = subprocess.run(
            [sys.executable, str(ENRICH), "--upgrade", str(self.project), "--yes"],
            capture_output=True, text=True,
            env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin",
                 "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.project / ".claude" / "hooks" / "require-verdict.sh").is_file())

    def test_a_project_with_no_remote_commits_locally_without_error(self):
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("push failed", r.stdout + r.stderr)
        self.assertTrue(self.head_subject().startswith("chore(scaffold):"))

    def test_a_non_git_directory_does_not_crash_the_upgrade(self):
        shutil.rmtree(self.project / ".git")
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.project / ".claude" / "hooks" / "require-verdict.sh").is_file())


class PushesWhenThereIsSomewhereToPush(UpgradeFixture):
    def test_it_pushes_to_a_configured_upstream(self):
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.git("push", "-q", "-u", "origin", branch)

        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("push: ok", r.stdout)

        local = self.git("rev-parse", "HEAD").stdout.strip()
        remote = subprocess.run(["git", "-C", str(bare), "rev-parse", branch],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(local, remote, "the upgrade commit was not pushed")

    def test_a_remote_with_no_upstream_stays_local_without_error(self):
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no upstream", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
