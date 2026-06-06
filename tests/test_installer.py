#!/usr/bin/env python3
"""Tests for the one-line installer and the `phasekit` CLI verb layer.

- Syntax checks for install.sh and scripts/phasekit.sh.
- Verb dispatch (adopt/check-version) and raw-flag backward compatibility.
- An end-to-end install integration: run install.sh against a LOCAL clone of
  this repo (PHASEKIT_URL=repo root, PHASEKIT_REF=HEAD so it exercises the code
  under test rather than the latest tag), then drive the generated shim. The
  pip/venv step needs network for pyyaml; the test skips cleanly if unavailable
  so it stays green offline.

Run from the repo root: `python3 -m unittest tests.test_installer`
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "phasekit.sh"
INSTALLER = REPO_ROOT / "install.sh"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _git(repo, *args):
    return _run(["git", "-C", str(repo), *args])


class InstallerBranchFastForward(unittest.TestCase):
    """Re-running the installer on a branch ref must advance to the remote tip.

    Regression for: `PHASEKIT_REF=master` did a bare `git checkout master`,
    stranding an existing install on a stale local branch (the `fetch` only
    moves remote-tracking refs). Uses a throwaway local "remote" so it runs
    fully offline — the fast-forward happens before the network-dependent venv
    step, so we assert on git state regardless of the installer's exit code.
    """

    def test_existing_install_fast_forwards_to_remote_tip(self):
        with tempfile.TemporaryDirectory() as d:
            remote = Path(d) / "remote"
            home = Path(d) / "share" / "phasekit"
            ident = ["-c", "user.email=t@t", "-c", "user.name=t"]

            # Build a remote with two commits on the default branch.
            remote.mkdir(parents=True)
            self.assertEqual(_git(remote, "init", "-q", "-b", "master").returncode, 0)
            (remote / "a").write_text("1")
            _git(remote, "add", "-A")
            _run(["git", "-C", str(remote), *ident, "commit", "-q", "-m", "A"])
            (remote / "b").write_text("2")
            _git(remote, "add", "-A")
            _run(["git", "-C", str(remote), *ident, "commit", "-q", "-m", "B"])
            tip = _git(remote, "rev-parse", "master").stdout.strip()

            # Existing install: clone, then rewind local master one commit so it
            # is behind origin/master — exactly the stranded-install state.
            home.parent.mkdir(parents=True)
            self.assertEqual(_git(remote, "clone", "-q", ".", str(home)).returncode, 0)
            _git(home, "reset", "--hard", "-q", "HEAD~1")
            self.assertNotEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), tip)

            # Re-run the installer targeting the branch. The venv step may fail
            # offline; we only assert the repo advanced to the remote tip.
            env = {
                **os.environ,
                "PHASEKIT_URL": str(remote),
                "PHASEKIT_REF": "master",
                "PHASEKIT_HOME": str(home),
                "PHASEKIT_BIN": str(Path(d) / "bin"),
            }
            _run(["bash", str(INSTALLER)], env=env)

            self.assertEqual(
                _git(home, "rev-parse", "HEAD").stdout.strip(), tip,
                "installer left the branch behind the remote tip",
            )


class SyntaxChecks(unittest.TestCase):
    def test_install_sh_parses(self):
        self.assertEqual(_run(["bash", "-n", str(INSTALLER)]).returncode, 0)

    def test_wrapper_parses(self):
        self.assertEqual(_run(["bash", "-n", str(WRAPPER)]).returncode, 0)


class VerbDispatch(unittest.TestCase):
    """Verbs operate on the current directory; raw flags still forward."""

    def test_adopt_then_check_version_in_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            adopt = _run(["bash", str(WRAPPER), "adopt"], cwd=d)
            self.assertEqual(adopt.returncode, 0, msg=adopt.stderr)
            self.assertTrue((Path(d) / ".scaffold" / "manifest.json").exists())

            cv = _run(["bash", str(WRAPPER), "check-version"], cwd=d)
            self.assertEqual(cv.returncode, 0, msg=cv.stderr)
            self.assertIn("enriched from", cv.stdout)

    def test_raw_flag_form_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            _run(["bash", str(WRAPPER), "adopt"], cwd=d)
            # Backward compat: explicit flag + path target, run from anywhere.
            cv = _run(["bash", str(WRAPPER), "--check-version", d])
            self.assertEqual(cv.returncode, 0, msg=cv.stderr)
            self.assertIn("enriched from", cv.stdout)

    def test_self_update_refuses_outside_a_phasekit_checkout(self):
        # Copy just the wrapper into a non-phasekit dir; self-update must refuse.
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "scripts"
            fake.mkdir()
            (fake / "phasekit.sh").write_text(WRAPPER.read_text())
            res = _run(["bash", str(fake / "phasekit.sh"), "self-update"])
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("not a phasekit checkout", res.stderr)


class InstallerIntegration(unittest.TestCase):
    """End-to-end: install from a local clone, then drive the shim."""

    def _skip_if_no_network(self, result):
        blob = (result.stdout + result.stderr).lower()
        if result.returncode != 0 and any(
            s in blob for s in ("pyyaml", "network", "ensurepip", "venv", "could not")
        ):
            self.skipTest(f"installer needs network/venv support: {blob.strip()[-200:]}")

    def test_install_and_run_shim(self):
        head = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip()
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "share" / "phasekit"
            bindir = Path(d) / "bin"
            env = {
                **os.environ,
                "PHASEKIT_URL": str(REPO_ROOT),
                "PHASEKIT_REF": head,
                "PHASEKIT_HOME": str(home),
                "PHASEKIT_BIN": str(bindir),
            }
            result = _run(["bash", str(INSTALLER)], env=env)
            self._skip_if_no_network(result)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            venv_py = home / ".venv" / "bin" / "python"
            shim = bindir / "phasekit"
            self.assertTrue(venv_py.exists(), "venv python missing")
            self.assertTrue(os.access(shim, os.X_OK), "shim not executable")
            self.assertEqual(
                _run([str(venv_py), "-c", "import yaml"]).returncode, 0,
                "venv cannot import yaml",
            )

            # Drive the installed shim end-to-end against a fresh project.
            with tempfile.TemporaryDirectory() as proj:
                adopt = _run([str(shim), "adopt"], cwd=proj)
                self.assertEqual(adopt.returncode, 0, msg=adopt.stderr)
                self.assertTrue((Path(proj) / ".scaffold" / "manifest.json").exists())
                cv = _run([str(shim), "check-version"], cwd=proj)
                self.assertEqual(cv.returncode, 0, msg=cv.stderr)
                self.assertIn("up to date", cv.stdout)


class Channels(unittest.TestCase):
    """ADR-0002 self-update channels: the `channel` verb and install.sh
    persistence/honoring. Fully offline — the channel is written before the
    network-dependent venv step, so we assert on git/file state regardless of
    the installer's exit code."""

    IDENT = ["-c", "user.email=t@t", "-c", "user.name=t"]

    def _commit(self, repo, name, content="x"):
        (Path(repo) / name).write_text(content)
        _git(repo, "add", "-A")
        _run(["git", "-C", str(repo), *self.IDENT, "commit", "-q", "-m", name])
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    def _make_remote(self, d):
        """A remote that is a minimal but canonical phasekit clone source: the
        real channel library + wrapper, guard stubs, a v0.1.0 tag on an early
        commit, and a later master tip (so stable != edge)."""
        remote = Path(d) / "remote"
        (remote / "scripts").mkdir(parents=True)
        (remote / "capabilities").mkdir(parents=True)
        for s in ("phasekit-channel.sh", "phasekit.sh"):
            shutil.copy(REPO_ROOT / "scripts" / s, remote / "scripts" / s)
        (remote / "scripts" / "enrich-project.py").write_text("# stub\n")
        (remote / "capabilities" / "project-capabilities.yaml").write_text("# stub\n")
        _git(remote, "init", "-q", "-b", "master")
        _git(remote, "add", "-A")
        _run(["git", "-C", str(remote), *self.IDENT, "commit", "-q", "-m", "A"])
        a = _git(remote, "rev-parse", "HEAD").stdout.strip()
        _git(remote, "tag", "v0.1.0")
        b = self._commit(remote, "b.txt")
        return remote, a, b

    def _install(self, url, home, ref=None):
        env = {**os.environ, "PHASEKIT_URL": str(url), "PHASEKIT_HOME": str(home),
               "PHASEKIT_BIN": str(Path(home).parent / "bin")}
        if ref is not None:
            env["PHASEKIT_REF"] = ref
        return _run(["bash", str(INSTALLER)], env=env)

    def test_install_defaults_to_stable_then_edge_persists(self):
        with tempfile.TemporaryDirectory() as d:
            remote, tag_commit, tip = self._make_remote(d)
            home = Path(d) / "share" / "phasekit"
            chan = home / ".phasekit-channel"

            # Fresh install, no ref → stable → latest tag (v0.1.0 commit).
            self._install(remote, home)
            self.assertEqual(chan.read_text().strip(), "stable")
            self.assertEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), tag_commit)

            # Explicit PHASEKIT_REF=master → edge channel, master tip.
            self._install(remote, home, ref="master")
            self.assertEqual(chan.read_text().strip(), "edge")
            self.assertEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), tip)

            # Append to remote; re-run with NO ref must honor persisted edge and advance.
            new_tip = self._commit(remote, "c.txt")
            self._install(remote, home)
            self.assertEqual(chan.read_text().strip(), "edge")
            self.assertEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), new_tip)

    def test_self_update_follows_channel(self):
        with tempfile.TemporaryDirectory() as d:
            remote, tag_commit, tip = self._make_remote(d)
            home = Path(d) / "home"
            _run(["git", "clone", "-q", str(remote), str(home)])
            shim = home / "scripts" / "phasekit.sh"

            # edge → advance to master tip, with the unreleased warning.
            _run(["bash", str(shim), "channel", "edge"])
            up = _run(["bash", str(shim), "self-update"])
            self.assertEqual(up.returncode, 0, msg=up.stderr)
            self.assertEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), tip)
            self.assertIn("UNRELEASED", up.stdout + up.stderr)

            # stable → move to the latest tag, no warning.
            _run(["bash", str(shim), "channel", "stable"])
            up = _run(["bash", str(shim), "self-update"])
            self.assertEqual(up.returncode, 0, msg=up.stderr)
            self.assertEqual(_git(home, "rev-parse", "HEAD").stdout.strip(), tag_commit)
            self.assertNotIn("UNRELEASED", up.stdout + up.stderr)

    def _fake_clone(self, d, *, canonical):
        """Minimal layout to drive scripts/phasekit.sh. With canonical=True it
        satisfies require_canonical_clone; otherwise the guard files are absent."""
        repo = Path(d) / "clone"
        (repo / "scripts").mkdir(parents=True)
        for s in ("phasekit.sh", "phasekit-channel.sh"):
            shutil.copy(REPO_ROOT / "scripts" / s, repo / "scripts" / s)
        _git(repo, "init", "-q")
        if canonical:
            (repo / "scripts" / "enrich-project.py").write_text("# stub\n")
            (repo / "capabilities").mkdir()
            (repo / "capabilities" / "project-capabilities.yaml").write_text("# stub\n")
        return repo / "scripts" / "phasekit.sh"

    def test_channel_verb_reads_default_and_sets(self):
        with tempfile.TemporaryDirectory() as d:
            shim = self._fake_clone(d, canonical=True)
            self.assertEqual(_run(["bash", str(shim), "channel"]).stdout.strip(), "stable")
            set_r = _run(["bash", str(shim), "channel", "edge"])
            self.assertEqual(set_r.returncode, 0, msg=set_r.stderr)
            self.assertEqual((shim.parent.parent / ".phasekit-channel").read_text().strip(), "edge")
            self.assertEqual(_run(["bash", str(shim), "channel"]).stdout.strip(), "edge")

    def test_channel_verb_refuses_outside_canonical_clone(self):
        with tempfile.TemporaryDirectory() as d:
            shim = self._fake_clone(d, canonical=False)
            r = _run(["bash", str(shim), "channel", "edge"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("refusing", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
