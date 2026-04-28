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


class AtomicCopyAndOrphanSweep(unittest.TestCase):
    """F10 #5 — interruption between file copies leaves no `*.scaffold-tmp`
    after a clean re-run, and the manifest reflects the post-rerun state.

    We simulate interruption by monkey-patching `os.replace` to raise after
    a few successful copies; then we run again normally and assert recovery.
    """

    def test_partial_copy_recovers_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)

            module = _load_module()
            real_replace = module.os.replace

            calls = {"n": 0}

            def boom_after_three(src, dst):
                calls["n"] += 1
                if calls["n"] > 3:
                    raise OSError("simulated interruption")
                return real_replace(src, dst)

            # Run enrich with the broken os.replace; should fail mid-way.
            module.os.replace = boom_after_three
            try:
                with self.assertRaises(OSError):
                    args = type("Args", (), {
                        "target_dir": str(target),
                        "profile": "default",
                        "force": False,
                        "dry_run": False,
                        "no_lock": True,  # avoid lock interactions in test
                    })()
                    module.cmd_enrich(args)
            finally:
                module.os.replace = real_replace

            # At least one orphan .scaffold-tmp must exist (the one that was
            # being replaced when we exploded).
            tmps_before = list(target.rglob("*" + module.TMP_SUFFIX))
            self.assertGreater(len(tmps_before), 0, "expected at least one orphan tmp")

            # Re-run from a fresh subprocess; should sweep the orphans, complete,
            # and leave the project in a consistent state with a manifest.
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(target), "--no-lock"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0,
                             f"recovery rerun failed:\n{result.stderr}")
            tmps_after = list(target.rglob("*" + module.TMP_SUFFIX))
            self.assertEqual(tmps_after, [],
                             f"orphan tmp files remain after recovery: {tmps_after}")
            self.assertTrue((target / ".scaffold" / "manifest.json").exists(),
                            "manifest must exist after successful recovery rerun")
            # Sweep message must have been printed during recovery
            self.assertIn("Swept orphan", result.stderr,
                          f"expected sweep log in stderr:\n{result.stderr}")


class UpgradeKeepLocalRoundTrip(unittest.TestCase):
    """F10 #6 — enrich → modify scaffold-owned file → --upgrade --keep-local
    preserves the edit, updates only the manifest sha, --check exits 0 thereafter."""

    def test_round_trip_preserves_edits_and_clean_check(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            qg_path = target / "docs" / "QUALITY_GATES.md"
            original = qg_path.read_text()
            qg_path.write_text(original + "\nDRIFT MARKER\n")

            # Pre-flight: --check now reports drift (exit 3)
            check = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--check", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(check.returncode, 3)
            self.assertIn("DRIFT: docs/QUALITY_GATES.md", check.stdout)

            # --upgrade --keep-local succeeds
            up = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--upgrade", "--yes",
                 "--keep-local", "docs/QUALITY_GATES.md", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(up.returncode, 0,
                             f"--upgrade --keep-local failed:\n{up.stderr}")

            # File still has the DRIFT MARKER (local edit preserved)
            self.assertIn("DRIFT MARKER", qg_path.read_text())

            # --check now clean (manifest sha was updated to match local content)
            check2 = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--check", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(check2.returncode, 0,
                             f"--check after --upgrade --keep-local should be clean:\n{check2.stdout}")
        finally:
            fixture.cleanup()


class UpgradeCollisionNovelRefuses(unittest.TestCase):
    """F10 #4 — `--upgrade` on a synthetic project with one drifted scaffold-owned
    file plus one collision-novel file produces exit 3 and names both paths.

    We synthesize a scaffold_manifest that adds docs/RUNBOOK.md as `scaffold`,
    while the project already has its own docs/RUNBOOK.md (project-original).
    """

    def test_collision_novel_refuses_with_path_named(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target

            # Project-side: introduce drift and a collision-novel file
            (target / "docs" / "QUALITY_GATES.md").write_text(
                (target / "docs" / "QUALITY_GATES.md").read_text() + "\nDRIFT\n"
            )
            (target / "docs" / "RUNBOOK.md").write_text("project-original runbook\n")

            module = _load_module()

            # Synthesize a scaffold_manifest where docs/RUNBOOK.md is now declared
            scaffold_manifest = module.load_manifest()
            scaffold_manifest = json.loads(json.dumps(scaffold_manifest))
            scaffold_manifest["files"]["docs/RUNBOOK.md"] = {"ownership": "scaffold"}

            # Load existing downstream manifest, resolve profile, and plan
            existing = module.load_downstream_manifest(target)
            profiles = scaffold_manifest.get("profiles", {})
            resolved = module.resolve_profile(profiles, "default")

            # Note: enumerate_install_targets only adds paths it knows about
            # via typed sections + ALWAYS_INSTALLED_FILE_PATHS. Files in the
            # files: map are not auto-included unless explicitly enumerated.
            # We patch ALWAYS_INSTALLED_FILE_PATHS to include docs/RUNBOOK.md
            # for this test, then restore it.
            original_always = module.ALWAYS_INSTALLED_FILE_PATHS
            module.ALWAYS_INSTALLED_FILE_PATHS = original_always + ("docs/RUNBOOK.md",)
            try:
                plans = module.compute_upgrade_plan(
                    target, scaffold_manifest, existing, resolved
                )
            finally:
                module.ALWAYS_INSTALLED_FILE_PATHS = original_always

            # Find both refusal cases
            refusals = [p for p in plans if p["action"] == module.ACTION_REFUSE]
            paths_in_refusal = {p["path"] for p in refusals}
            self.assertIn("docs/QUALITY_GATES.md", paths_in_refusal,
                          f"drift refusal missing; refusals: {paths_in_refusal}")
            self.assertIn("docs/RUNBOOK.md", paths_in_refusal,
                          f"collision-novel refusal missing; refusals: {paths_in_refusal}")

            states = {p["path"]: p["state"] for p in plans}
            self.assertEqual(states["docs/QUALITY_GATES.md"], "drifted")
            self.assertEqual(states["docs/RUNBOOK.md"], "collision-novel")
        finally:
            fixture.cleanup()


class UninstallIncludeOnceRoundTrip(unittest.TestCase):
    """F10 #7 — --uninstall --include-once removes all `scaffold` and `bootstrap-*`
    files; non-manifest files are byte-identical before and after."""

    def test_uninstall_default_keeps_bootstrap(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            (target / "docs" / "QUALITY_GATES.md").exists()  # sanity
            project_path = target / "docs" / "PROJECT_README.md"
            project_path.write_text("project content\n")

            # Default uninstall removes only `scaffold` class
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--uninstall", "--yes", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, f"--uninstall failed:\n{r.stderr}")

            self.assertFalse((target / "docs" / "QUALITY_GATES.md").exists(),
                             "scaffold-class file should have been removed")
            self.assertTrue((target / "docs" / "SPEC.md").exists(),
                            "bootstrap-frozen should remain without --include-once")
            self.assertTrue(project_path.exists(),
                            "project-original file must not be touched")
            self.assertEqual(project_path.read_text(), "project content\n")
            # Recovery log must exist
            self.assertTrue((target / ".scaffold" / "uninstall.log").exists())
        finally:
            fixture.cleanup()

    def test_uninstall_include_once_removes_all_classes(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            project_path = target / "docs" / "PROJECT_README.md"
            project_content = "project file untouched\n"
            project_path.write_text(project_content)

            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--uninstall", "--include-once",
                 "--yes", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0,
                             f"--uninstall --include-once failed:\n{r.stderr}")

            # Both classes gone
            self.assertFalse((target / "docs" / "QUALITY_GATES.md").exists(),
                             "scaffold file should be gone")
            self.assertFalse((target / "docs" / "SPEC.md").exists(),
                             "bootstrap-frozen file should be gone (--include-once)")
            self.assertFalse((target / ".claude" / "settings.json").exists(),
                             "bootstrap-with-template-tracking file should be gone")
            self.assertFalse((target / ".claude" / "CLAUDE.md").exists(),
                             "bootstrap-with-template-tracking should be gone")

            # Project file byte-identical
            self.assertTrue(project_path.exists())
            self.assertEqual(project_path.read_text(), project_content)

            # Manifest removed (no scaffold files remain)
            self.assertFalse((target / ".scaffold" / "manifest.json").exists())

            # Recovery log retained
            self.assertTrue((target / ".scaffold" / "uninstall.log").exists())
        finally:
            fixture.cleanup()


class TemplateSourceAdvisoryDrift(unittest.TestCase):
    """F10 #11 — modify a scaffold template; --check --include-templates on a
    downstream project enriched before the change exits with an advisory
    code, names the affected path, and never auto-overwrites the rendered file.

    To avoid mutating the real scaffold templates we synthesize a manifest
    with a deliberately-wrong `template_sha` for a bootstrap-with-template-tracking
    entry, then verify --check --include-templates surfaces the advisory.
    """

    def test_template_sha_mismatch_surfaces_advisory(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target

            # Read manifest, corrupt the template_sha for .claude/CLAUDE.md
            manifest_path = target / ".scaffold" / "manifest.json"
            with open(manifest_path) as f:
                manifest = json.load(f)
            tampered = False
            original_content_before_render = None
            for entry in manifest["files"]:
                if entry.get("ownership") == "bootstrap-with-template-tracking" \
                   and entry.get("rendered_from") == "templates/CLAUDE.template.md":
                    entry["template_sha"] = "0" * 64  # deliberately wrong
                    tampered = True
                    break
            self.assertTrue(tampered, "manifest should have a CLAUDE template-tracked entry")
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            # Capture the rendered file's content; advisory must NOT auto-overwrite
            rendered = target / ".claude" / "CLAUDE.md"
            content_before = rendered.read_text()

            # --check (no --include-templates): should be clean
            r1 = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--check", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r1.returncode, 0,
                             f"--check without --include-templates should be clean:\n{r1.stdout}")

            # --check --include-templates: should report advisory and exit 3
            r2 = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--check", "--include-templates",
                 str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r2.returncode, 3,
                             f"--check --include-templates should detect template drift:\n{r2.stdout}")
            self.assertIn(".claude/CLAUDE.md", r2.stdout)
            self.assertIn("TEMPLATE DRIFT", r2.stdout)
            self.assertIn("templates/CLAUDE.template.md", r2.stdout)

            # Auto-overwrite must not have happened
            self.assertEqual(rendered.read_text(), content_before,
                             "advisory must NOT modify the rendered file")
        finally:
            fixture.cleanup()


class UpgradeKeepLocalAppliesToUpdateAvailable(unittest.TestCase):
    """Regression: when `--reconcile` snapshotted a project-customized version
    of an agent file, subsequent `--upgrade` will see current_sha == manifest_sha
    (clean) but scaffold_new_sha != manifest_sha (update-available). Without
    this regression test, a default --upgrade would silently overwrite the
    project's customizations because --keep-local was only honored in the
    `drifted` branch.
    """

    def test_keep_local_honored_in_update_available(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            module = _load_module()

            # Modify a scaffold-class agent file in the project, then
            # reconcile so the manifest snapshots the modified version.
            project_path = target / ".claude" / "agents" / "code-reviewer.md"
            project_path.write_text("PROJECT-CUSTOMIZED VERSION\n")
            subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--reconcile", "--force",
                 str(target)],
                check=True, capture_output=True,
            )

            # Compute the upgrade plan with --keep-local for the customized file
            existing = module.load_downstream_manifest(target)
            scaffold_manifest = module.load_manifest()
            resolved = module.resolve_profile(scaffold_manifest["profiles"], "default")
            plans = module.compute_upgrade_plan(
                target, scaffold_manifest, existing, resolved,
                keep_local=(".claude/agents/code-reviewer.md",),
            )
            entry = next(p for p in plans
                         if p["path"] == ".claude/agents/code-reviewer.md")
            self.assertEqual(entry["state"], "update-available",
                             "expected update-available state")
            self.assertEqual(entry["action"], module.ACTION_KEEP_LOCAL,
                             "--keep-local must be honored even when state is update-available")

            # Apply and verify content untouched
            rc = module.apply_upgrade_plan(target, scaffold_manifest, plans, "default")
            self.assertEqual(rc, 0)
            self.assertEqual(project_path.read_text(), "PROJECT-CUSTOMIZED VERSION\n",
                             "project customization must be preserved")
        finally:
            fixture.cleanup()


class UpgradeActionPositivePaths(unittest.TestCase):
    """F10 review F6 — exercise --adopt, --rename-local, --accept-removal,
    --interactive on the positive path. Each action must produce the
    documented on-disk effect AND the manifest must reflect the chosen action.
    """

    def _make_synthetic_collision_scenario(self, target):
        """Add docs/RUNBOOK.md to the project (project-original) and return
        a synthetic scaffold_manifest that adds it as scaffold-class."""
        (target / "docs" / "RUNBOOK.md").write_text("project-original runbook\n")
        module = _load_module()
        scaffold_manifest = json.loads(json.dumps(module.load_manifest()))
        scaffold_manifest["files"]["docs/RUNBOOK.md"] = {"ownership": "scaffold"}
        return module, scaffold_manifest

    def test_adopt_records_current_content_as_canonical(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            module, scaffold_manifest = self._make_synthetic_collision_scenario(target)
            existing = module.load_downstream_manifest(target)
            resolved = module.resolve_profile(scaffold_manifest["profiles"], "default")

            original_always = module.ALWAYS_INSTALLED_FILE_PATHS
            module.ALWAYS_INSTALLED_FILE_PATHS = original_always + ("docs/RUNBOOK.md",)
            try:
                plans = module.compute_upgrade_plan(
                    target, scaffold_manifest, existing, resolved,
                    adopt=("docs/RUNBOOK.md",),
                )
                rc = module.apply_upgrade_plan(
                    target, scaffold_manifest, plans, "default"
                )
            finally:
                module.ALWAYS_INSTALLED_FILE_PATHS = original_always

            self.assertEqual(rc, 0)
            # File is unchanged (project-original content)
            self.assertEqual(
                (target / "docs" / "RUNBOOK.md").read_text(),
                "project-original runbook\n",
            )
            # Manifest now records it as scaffold class
            with open(target / ".scaffold" / "manifest.json") as f:
                m = json.load(f)
            paths = {e["path"]: e for e in m["files"]}
            self.assertIn("docs/RUNBOOK.md", paths)
            self.assertEqual(paths["docs/RUNBOOK.md"]["ownership"], "scaffold")
        finally:
            fixture.cleanup()

    def test_rename_local_moves_file_aside(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            module, scaffold_manifest = self._make_synthetic_collision_scenario(target)
            # Synthesize a real scaffold-side source for docs/RUNBOOK.md so
            # the rename-local install has something to copy. Write it under
            # an in-memory override of REPO_ROOT-relative resolution by
            # placing a temp file we'll point compute_upgrade_plan at.
            # Simpler: skip the new install and verify the rename moved it.
            existing = module.load_downstream_manifest(target)
            resolved = module.resolve_profile(scaffold_manifest["profiles"], "default")

            # Create the would-be scaffold source so apply can copy it
            (REPO_ROOT / "docs" / "RUNBOOK.md").write_text("scaffold-canonical runbook\n")

            original_always = module.ALWAYS_INSTALLED_FILE_PATHS
            module.ALWAYS_INSTALLED_FILE_PATHS = original_always + ("docs/RUNBOOK.md",)
            try:
                plans = module.compute_upgrade_plan(
                    target, scaffold_manifest, existing, resolved,
                    rename_local=("docs/RUNBOOK.md=docs/RUNBOOK.project.md",),
                )
                rc = module.apply_upgrade_plan(
                    target, scaffold_manifest, plans, "default"
                )
            finally:
                module.ALWAYS_INSTALLED_FILE_PATHS = original_always
                try:
                    (REPO_ROOT / "docs" / "RUNBOOK.md").unlink()
                except FileNotFoundError:
                    pass

            self.assertEqual(rc, 0)
            # Project-original moved aside
            renamed = target / "docs" / "RUNBOOK.project.md"
            self.assertTrue(renamed.exists(), "renamed file missing")
            self.assertEqual(renamed.read_text(), "project-original runbook\n")
            # Scaffold-canonical installed at the original path
            self.assertEqual(
                (target / "docs" / "RUNBOOK.md").read_text(),
                "scaffold-canonical runbook\n",
            )
        finally:
            fixture.cleanup()

    def test_accept_removal_deletes_orphaned_path(self):
        fixture = _GreenfieldFixture()
        try:
            target = fixture.target
            module = _load_module()

            # Synthesize a scaffold_manifest where docs/QUALITY_GATES.md is
            # NO LONGER declared (removed across versions).
            scaffold_manifest = json.loads(json.dumps(module.load_manifest()))
            del scaffold_manifest["docs"]["QUALITY_GATES"]
            # Also remove from default profile's include_docs
            inc = list(scaffold_manifest["profiles"]["default"].get("include_docs", []))
            if "QUALITY_GATES" in inc:
                inc.remove("QUALITY_GATES")
                scaffold_manifest["profiles"]["default"]["include_docs"] = inc

            existing = module.load_downstream_manifest(target)
            resolved = module.resolve_profile(scaffold_manifest["profiles"], "default")
            self.assertTrue((target / "docs" / "QUALITY_GATES.md").exists())

            plans = module.compute_upgrade_plan(
                target, scaffold_manifest, existing, resolved,
                accept_removal=("docs/QUALITY_GATES.md",),
            )
            removed = [p for p in plans if p["action"] == module.ACTION_DELETE]
            self.assertTrue(removed, f"expected an ACTION_DELETE plan; got {plans}")

            rc = module.apply_upgrade_plan(
                target, scaffold_manifest, plans, "default"
            )
            self.assertEqual(rc, 0)
            self.assertFalse(
                (target / "docs" / "QUALITY_GATES.md").exists(),
                "file should have been deleted by --accept-removal",
            )
            with open(target / ".scaffold" / "manifest.json") as f:
                m = json.load(f)
            paths = {e["path"] for e in m["files"]}
            self.assertNotIn("docs/QUALITY_GATES.md", paths,
                             "manifest should no longer carry the removed file")
        finally:
            fixture.cleanup()


class PreInstallSafety(unittest.TestCase):
    """F11b (secrets scan) and F11a (symlink refusal). Pre-install checks
    prevent accidentally distributing leaked credentials and prevent symlink
    escapes from writing into a directory outside the target."""

    def test_secret_pattern_refused(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tainted = tmp / "src.txt"
            # AKIA + 16 uppercase alnum chars matches the AWS pattern
            tainted.write_text("Some text\nAKIAABCDEFGHIJKLMNOP\nmore\n")
            dest = tmp / "dest.txt"
            with self.assertRaises(RuntimeError) as ctx:
                module.safe_install(tainted, dest, tmp)
            self.assertIn("secret-shaped", str(ctx.exception))
            self.assertFalse(dest.exists(), "tainted file must not be installed")

    def test_placeholder_does_not_match(self):
        """Documented placeholder forms (`sk-ant-...`) must NOT match the
        live-key regex (`.` not in the allowed key char class)."""
        module = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            doc = tmp / "doc.md"
            doc.write_text("Use ANTHROPIC_API_KEY='sk-ant-...' to authenticate.\n")
            dest = tmp / "dest.md"
            module.safe_install(doc, dest, tmp)  # must not raise
            self.assertTrue(dest.exists())

    def test_symlink_escape_refused(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as outer:
            outer = Path(outer)
            target = outer / "target"
            target.mkdir()
            outside = outer / "outside"
            outside.mkdir()
            # Build target/sub as a symlink to outside (escape)
            (target / "sub").symlink_to(outside)

            src = outer / "src.txt"
            src.write_text("ok\n")
            dest = target / "sub" / "dest.txt"

            with self.assertRaises(RuntimeError) as ctx:
                module.safe_install(src, dest, target)
            self.assertIn("escape", str(ctx.exception).lower())


class ScaffoldInternalDenyFromManifest(unittest.TestCase):
    """The deny-list for scaffold-internal files now derives from the
    capability manifest (M9 retired the SCAFFOLD_INTERNAL_FILES constant)."""

    def test_assert_blocks_classified_scaffold_internal(self):
        module = _load_module()
        # LICENSE is classified scaffold-internal in the manifest's files: map
        with self.assertRaises(RuntimeError) as ctx:
            module.assert_not_scaffold_internal("LICENSE")
        self.assertIn("scaffold-internal", str(ctx.exception))

    def test_assert_allows_scaffold_class(self):
        module = _load_module()
        # docs/QUALITY_GATES.md is `scaffold`, not internal — should pass.
        module.assert_not_scaffold_internal("docs/QUALITY_GATES.md")  # no raise


class M10DesignArtifact(unittest.TestCase):
    """M10 — opt-in `docs/DESIGN.md` artifact via the `with-design` profile.

    Acceptance:
    - default profile produces a project WITHOUT DESIGN.md (opt-in, no
      regression for existing users)
    - with-design profile produces a project WITH DESIGN.md rendered from
      templates/design.template.md
    - the rendered template fits under 60 lines (one-screen constraint)
    - manifest entry has ownership: bootstrap-frozen
    """

    def test_default_profile_does_not_install_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(
                (target / "docs" / "DESIGN.md").exists(),
                "default profile must NOT install docs/DESIGN.md",
            )

    def test_with_design_profile_installs_rendered_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH),
                 "--profile", "with-design", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            design_path = target / "docs" / "DESIGN.md"
            self.assertTrue(design_path.exists(),
                            "with-design profile must install docs/DESIGN.md")
            # Project name substituted
            self.assertIn(target.name, design_path.read_text(),
                          "project name should be substituted in rendered DESIGN.md")
            # Under 60 lines (one-screen proxy)
            text = design_path.read_text()
            line_count = len(text.splitlines())
            self.assertLess(line_count, 60,
                            f"DESIGN.md should fit on one screen; got {line_count} lines")
            # Width sanity: no plain-prose line exceeds 100 cols (box-drawing
            # characters in the system-sketch diagram are allowed up to that).
            for n, line in enumerate(text.splitlines(), start=1):
                self.assertLessEqual(
                    len(line), 100,
                    f"DESIGN.md line {n} is {len(line)} cols; exceeds 100",
                )
            # Manifest reflects bootstrap-frozen ownership
            with open(target / ".scaffold" / "manifest.json") as f:
                manifest = json.load(f)
            entry = next((e for e in manifest["files"] if e["path"] == "docs/DESIGN.md"),
                         None)
            self.assertIsNotNone(entry, "manifest must list docs/DESIGN.md")
            self.assertEqual(entry["ownership"], "bootstrap-frozen",
                             "DESIGN.md should be bootstrap-frozen")

    def test_existing_project_can_opt_in_via_upgrade(self):
        """The acceptance criterion the M10 spec explicitly demands:
        a project enriched with `default` can adopt `docs/DESIGN.md` by
        editing its manifest's profile to `with-design` and running --upgrade."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)

            # Step 1: enrich with default profile; assert no DESIGN.md
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((target / "docs" / "DESIGN.md").exists())

            # Step 2: edit the on-disk manifest's profile to with-design
            manifest_path = target / ".scaffold" / "manifest.json"
            with open(manifest_path) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["profile"], "default")
            manifest["profile"] = "with-design"
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            # Step 3: run --upgrade --yes; expect DESIGN.md to be installed
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH),
                 "--upgrade", "--yes", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0,
                             f"--upgrade after profile switch failed:\n{r.stderr}")

            # Step 4: assert DESIGN.md exists, manifest entry is bootstrap-frozen
            self.assertTrue((target / "docs" / "DESIGN.md").exists(),
                            "DESIGN.md must be installed after profile switch + upgrade")
            with open(manifest_path) as f:
                manifest = json.load(f)
            entry = next((e for e in manifest["files"] if e["path"] == "docs/DESIGN.md"),
                         None)
            self.assertIsNotNone(entry, "manifest must list docs/DESIGN.md after upgrade")
            self.assertEqual(entry["ownership"], "bootstrap-frozen")

            # Step 5: --check is clean
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--check", str(target)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0,
                             f"--check should be clean after opt-in upgrade:\n{r.stdout}")


if __name__ == "__main__":
    unittest.main()
