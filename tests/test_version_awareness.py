#!/usr/bin/env python3
"""Tests for the version-awareness feature (scaffold release version, origin_url,
--check-version) and, critically, that the repo-only CI/test infrastructure does
NOT leak into downstream enrichment.

Run from the repo root: `python3 -m unittest tests.test_version_awareness`
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "enrich-project.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("enrich_project_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enrich_greenfield():
    """Enrich a fresh temp project and return (TemporaryDirectory, target Path)."""
    tmp = tempfile.TemporaryDirectory()
    target = Path(tmp.name) / "project"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(target)],
        check=True, capture_output=True,
    )
    return tmp, target


def _write_manifest(target, **overrides):
    """Write a minimal .scaffold/manifest.json with overridable fields."""
    manifest = {
        "schema_version": 1,
        "scaffold_version": "0.0.0+git.deadbee",
        "scaffold_commit": "deadbee",
        "profile": "default",
        "enriched_at": "2026-01-01T00:00:00Z",
        "normalization": {"recipe": "lf-trim-trailing-ws-single-final-newline", "version": 1},
        "files": [],
    }
    manifest.update(overrides)
    scaffold = target / ".scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)
    (scaffold / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _check_version(target):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check-version", str(target)],
        capture_output=True, text=True,
    )


class VersionHelpers(unittest.TestCase):
    def test_get_scaffold_version_shape(self):
        """Returns a (version, commit) tuple of non-empty strings, regardless of
        whether the checkout has tags (git describe --always falls back to sha)."""
        mod = _load_module()
        version, commit = mod.get_scaffold_version()
        self.assertIsInstance(version, str)
        self.assertIsInstance(commit, str)
        self.assertTrue(version)
        self.assertTrue(commit)

    def test_get_scaffold_origin_url_does_not_raise(self):
        mod = _load_module()
        url = mod.get_scaffold_origin_url()
        self.assertTrue(url is None or isinstance(url, str))


class ManifestContract(unittest.TestCase):
    def test_manifest_records_origin_url_and_version(self):
        """A freshly enriched project records origin_url + version/commit."""
        tmp, target = _enrich_greenfield()
        try:
            manifest = json.loads((target / ".scaffold" / "manifest.json").read_text())
            self.assertIn("origin_url", manifest)
            self.assertIn("scaffold_version", manifest)
            self.assertIn("scaffold_commit", manifest)
            self.assertTrue(manifest["scaffold_version"])
        finally:
            tmp.cleanup()


class CheckVersionCommand(unittest.TestCase):
    def test_missing_manifest_errors(self):
        with tempfile.TemporaryDirectory() as d:
            result = _check_version(d)
            self.assertEqual(result.returncode, 1)

    def test_old_manifest_without_origin_url_is_handled(self):
        """Backward compat: a pre-feature manifest (no origin_url, old version
        format, unresolvable commit) must not crash; reports informationally."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_manifest(target)  # no origin_url, commit 'deadbee' won't resolve
            result = _check_version(target)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("enriched from", result.stdout)

    def test_up_to_date_when_commit_matches_head(self):
        mod = _load_module()
        _version, commit = mod.get_scaffold_version()
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_manifest(target, scaffold_commit=commit)
            result = _check_version(target)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("up to date", result.stdout)


class EnrichmentIsolation(unittest.TestCase):
    """The repo-only CI/test infrastructure must never be provisioned into a
    downstream project. This is the guard for "doesn't interact with the
    enrichment scripts": files under `.github/` and `tests/` are in the
    `ignore:` glob list, so enrichment never installs them."""

    def test_enrichment_ships_no_ci_or_test_files(self):
        tmp, target = _enrich_greenfield()
        try:
            self.assertFalse((target / ".github").exists(),
                             "enrichment must not create .github/ downstream")
            self.assertFalse((target / "tests").exists(),
                             "enrichment must not create tests/ downstream")
            manifest = json.loads((target / ".scaffold" / "manifest.json").read_text())
            leaked = [e["path"] for e in manifest["files"]
                      if e["path"].startswith((".github/", "tests/"))]
            self.assertEqual(leaked, [], f"CI/test files leaked into install set: {leaked}")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
