#!/usr/bin/env python3
"""Tests for M9 Slice B (manifest writer, --reconcile, fcntl.flock).

Covers F10 acceptance criteria:
- #3: --reconcile produces a manifest whose sha256 values are reproducible
  by an independent script computing sha256 over the same byte normalization recipe.
- #8: every manifest file entry includes an `overlays: []` reserved field.
- #10: concurrent enrichment of the same target results in exactly one success;
  the other exits non-zero (lock contention).

Criterion #6 (round-trip with --keep-local) and #9 (migration fixtures) are
covered by Slice C tests once those features land.

Run from the repo root: `python3 -m unittest tests.test_m9_manifest`
"""

import hashlib
import importlib.util
import json
import multiprocessing
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "enrich-project.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("enrich_project_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _independent_normalized_sha256(file_path):
    """Reimplement the normalization recipe independently (per F10 #3) so the
    test cannot share a bug with the production code."""
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    normalized = ("\n".join(lines) + "\n" if lines else "").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _independent_strict_sha256(file_path):
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


class _GreenfieldFixture:
    """Helper: enrich a fresh greenfield project for use as a test target."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "project"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.target, check=True)
        # Configure identity for git operations the engine may emit.
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.target, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.target, check=True)
        # Run enrich (writes a manifest)
        subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(self.target)],
            check=True, capture_output=True,
        )

    def cleanup(self):
        self._tmp.cleanup()


class ReconcileShaReproducibility(unittest.TestCase):
    """F10 #3 — reconcile produces a manifest whose sha256 values are
    reproducible by an independent script computing the same recipe."""

    def test_reconcile_shas_match_independent(self):
        fixture = _GreenfieldFixture()
        try:
            # Force reconcile to rewrite the manifest from disk state
            subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--reconcile", "--force", str(fixture.target)],
                check=True, capture_output=True,
            )
            with open(fixture.target / ".scaffold" / "manifest.json") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(
                manifest["normalization"]["recipe"],
                "lf-trim-trailing-ws-single-final-newline",
            )
            self.assertGreater(len(manifest["files"]), 0)
            for entry in manifest["files"]:
                file_path = fixture.target / entry["path"]
                self.assertTrue(file_path.exists(), f"missing: {entry['path']}")

                expected_strict = _independent_strict_sha256(file_path)
                self.assertEqual(
                    entry["sha256_strict"], expected_strict,
                    f"strict sha mismatch for {entry['path']}",
                )

                if entry.get("text", True):
                    expected_normalized = _independent_normalized_sha256(file_path)
                    self.assertEqual(
                        entry["sha256"], expected_normalized,
                        f"normalized sha mismatch for {entry['path']}",
                    )
                else:
                    self.assertEqual(
                        entry["sha256"], expected_strict,
                        f"binary file {entry['path']}: sha256 should equal sha256_strict",
                    )
        finally:
            fixture.cleanup()


class OverlaysReservedField(unittest.TestCase):
    """F10 #8 — every manifest file entry includes `overlays: []` even when unused."""

    def test_overlays_field_present(self):
        fixture = _GreenfieldFixture()
        try:
            with open(fixture.target / ".scaffold" / "manifest.json") as f:
                manifest = json.load(f)
            for entry in manifest["files"]:
                self.assertIn("overlays", entry,
                              f"{entry['path']} missing overlays field")
                self.assertEqual(entry["overlays"], [],
                                 f"{entry['path']} overlays should be [] (M9.4 reserved)")
        finally:
            fixture.cleanup()


def _hold_lock_then_signal(target, signal_path):
    """Worker for the concurrency test. Acquires the per-target lock and
    holds it briefly so the second process is forced to contend."""
    module = _load_module()
    try:
        with module.target_lock(target):
            Path(signal_path).touch()  # signal that we hold the lock
            time.sleep(0.5)             # hold long enough for a contender
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        sys.exit(99)


class ConcurrentEnrichLockContention(unittest.TestCase):
    """F10 #10 — concurrent enrichment of the same target results in exactly
    one success; the other exits with a non-zero usage error code."""

    def test_concurrent_run_produces_exactly_one_success(self):
        fixture = _GreenfieldFixture()
        try:
            signal_path = Path(fixture.target) / ".lock-acquired"

            # Process 1: hold the lock for 0.5s
            ctx = multiprocessing.get_context("spawn")
            p1 = ctx.Process(
                target=_hold_lock_then_signal,
                args=(str(fixture.target), str(signal_path)),
            )
            p1.start()

            # Wait for p1 to acquire the lock
            for _ in range(100):
                if signal_path.exists():
                    break
                time.sleep(0.01)
            else:
                p1.terminate()
                self.fail("p1 never acquired lock")

            # Process 2: try to reconcile while p1 holds the lock
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH),
                 "--reconcile", "--force", str(fixture.target)],
                capture_output=True, text=True,
            )
            p1.join(timeout=5)

            self.assertEqual(p1.exitcode, 0, "p1 should release the lock cleanly")
            self.assertNotEqual(
                result.returncode, 0,
                "p2 should fail while p1 holds the lock\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "another", result.stderr.lower(),
                f"stderr should mention contention:\n{result.stderr}",
            )
        finally:
            fixture.cleanup()


class MigrationFixture(unittest.TestCase):
    """F10 #9 — synthetic v0 manifest run through engine at v1 should produce
    a valid v1 manifest with content equivalent. Round-trip and idempotency
    assertions both pass."""

    SYNTHETIC_V0 = {
        "schema_version": 0,
        "scaffold_version": "0.0.0+git.synthetic",
        "scaffold_commit": "synthetic",
        "profile": "default",
        "enriched_at": "2026-04-25T00:00:00Z",
        "files": [
            {
                "path": "docs/QUALITY_GATES.md",
                "ownership": "scaffold",
                "text": True,
                "sha256": "deadbeef",
                "sha256_strict": "deadbeef",
                "installed_at": "2026-04-25T00:00:00Z",
                # NOTE: no `overlays` field — v1 adds it
            },
        ],
        # NOTE: no `normalization` block — v1 adds it
    }

    def test_v0_to_v1_migration_adds_required_fields(self):
        module = _load_module()
        migrated = module.migrate_manifest(self.SYNTHETIC_V0)
        self.assertEqual(migrated["schema_version"], 1)
        self.assertEqual(
            migrated["normalization"]["recipe"],
            "lf-trim-trailing-ws-single-final-newline",
        )
        self.assertEqual(migrated["normalization"]["version"], 1)
        for entry in migrated["files"]:
            self.assertIn("overlays", entry)
            self.assertEqual(entry["overlays"], [])

    def test_migration_is_idempotent(self):
        module = _load_module()
        once = module.migrate_manifest(self.SYNTHETIC_V0)
        twice = module.migrate_manifest(once)
        self.assertEqual(once, twice, "migrate(migrate(v0)) must equal migrate(v0)")

    def test_migration_preserves_existing_data(self):
        module = _load_module()
        migrated = module.migrate_manifest(self.SYNTHETIC_V0)
        # Original v0 fields should survive
        self.assertEqual(migrated["scaffold_version"], "0.0.0+git.synthetic")
        self.assertEqual(migrated["profile"], "default")
        self.assertEqual(len(migrated["files"]), 1)
        self.assertEqual(migrated["files"][0]["path"], "docs/QUALITY_GATES.md")
        self.assertEqual(migrated["files"][0]["sha256"], "deadbeef")

    def test_already_current_returns_unchanged(self):
        module = _load_module()
        already_v1 = {
            "schema_version": 1,
            "files": [],
            "normalization": {
                "recipe": "lf-trim-trailing-ws-single-final-newline",
                "version": 1,
            },
        }
        migrated = module.migrate_manifest(already_v1)
        self.assertEqual(migrated, already_v1)

    def test_migrate_only_command_rewrites_manifest(self):
        """End-to-end: synthetic v0 written to disk → --migrate-only → v1."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            (target / ".scaffold").mkdir(parents=True)
            (target / ".scaffold" / "manifest.json").write_text(
                json.dumps(self.SYNTHETIC_V0, indent=2)
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--migrate-only", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0,
                             f"--migrate-only failed:\n{result.stderr}")
            with open(target / ".scaffold" / "manifest.json") as f:
                migrated = json.load(f)
            self.assertEqual(migrated["schema_version"], 1)
            self.assertIn("normalization", migrated)
            self.assertEqual(migrated["files"][0]["overlays"], [])


class ManifestSchema(unittest.TestCase):
    """Sanity: manifest written by the engine has the documented top-level shape."""

    REQUIRED_TOP_LEVEL = {"schema_version", "scaffold_version", "scaffold_commit",
                          "profile", "enriched_at", "normalization", "files"}

    def test_top_level_keys_present(self):
        fixture = _GreenfieldFixture()
        try:
            with open(fixture.target / ".scaffold" / "manifest.json") as f:
                manifest = json.load(f)
            actual = set(manifest.keys())
            missing = self.REQUIRED_TOP_LEVEL - actual
            self.assertFalse(missing, f"manifest missing keys: {missing}")
            self.assertEqual(manifest["schema_version"], 1)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
