#!/usr/bin/env python3
"""Tests for v0.7.0 cross-project contracts — phase 1: declaration + reader.

Phase 1 ships INERT: the declaration format and its parser exist, nothing
mounts and nothing refuses yet. The acceptance surface is therefore:

  - a repo with no `contracts.yaml` behaves exactly as v0.6.6 (inert)
  - NON-VACUITY: the reader is demonstrably *reached* when the file exists —
    an inert feature that is also unreachable would pass the first test
    trivially, which is the failure mode this pair is designed to exclude
  - the format is strict: unknown keys, bad versions, traversal-shaped
    vendor paths and duplicate slugs are hard errors, never silent no-ops
    (a typo'd key that quietly does nothing is the exact bug class this
    release exists to kill)

Run from the repo root: `python3 -m unittest tests.test_contracts_v070`
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "phasekit-contracts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phasekit_contracts_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is absent for a bare exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contracts = _load_module()


def _write(repo: Path, text: str) -> Path:
    path = repo / contracts.CONTRACTS_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


class TestScriptShipsAtAll(unittest.TestCase):
    def test_script_exists_and_is_registered_for_downstream_install(self):
        """Unregistered files pass local tests and silently fail to provision."""
        self.assertTrue(SCRIPT_PATH.is_file())

        caps = (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text(encoding="utf-8")
        self.assertIn("scripts/phasekit-contracts.py", caps)

        enrich = (REPO_ROOT / "scripts" / "enrich-project.py").read_text(encoding="utf-8")
        always = enrich.split("ALWAYS_INSTALLED_FILE_PATHS = (", 1)[1].split(")", 1)[0]
        self.assertIn("scripts/phasekit-contracts.py", always)


class TestAbsentDeclarationIsInert(unittest.TestCase):
    def test_no_contracts_yaml_reads_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            decl = contracts.load_declaration(Path(tmp))
        self.assertFalse(decl.present)
        self.assertFalse(decl.declares_dependencies)
        self.assertEqual(decl.dependencies, ())
        self.assertIsNone(decl.path)

    def test_status_on_a_v066_shaped_repo_says_nothing_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo", tmp, "status"],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no contracts.yaml", proc.stdout)
        self.assertIn("nothing is checked", proc.stdout)


class TestNonVacuity(unittest.TestCase):
    """Prove the reader is actually REACHED when the file exists.

    Without this, "absent behaves as v0.6.6" would also pass for a reader
    that is never called at all.
    """

    def test_present_declaration_changes_the_parsed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            absent = contracts.load_declaration(repo)
            _write(repo, "version: 1\ndepends_on:\n  - slug: foundry-orchestrator\n")
            present = contracts.load_declaration(repo)

        self.assertFalse(absent.present)
        self.assertTrue(present.present)
        self.assertNotEqual(absent.dependencies, present.dependencies)
        self.assertEqual(present.slugs(), ("foundry-orchestrator",))

    def test_present_declaration_changes_the_cli_output(self):
        """The reader is reached through the real entry point, not just the API."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo, "version: 1\ndepends_on:\n  - slug: foundry-orchestrator\n")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo", tmp, "status", "--json"],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["declaration_present"])
        self.assertEqual(
            payload["dependencies"],
            [{
                "slug": "foundry-orchestrator",
                "vendor": "vendor/contracts/foundry-orchestrator",
                "vendored": False,
                "description": "",
            }],
        )

    def test_a_malformed_declaration_is_a_loud_error_not_a_silent_pass(self):
        """The strongest non-vacuity proof: bad input must be *observed*."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo, "version: 99\ndepends_on: []\n")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo", tmp, "status"],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("`version` must be 1", proc.stderr)


class TestDeclarationFormat(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo, text)
            return contracts.load_declaration(repo)

    def _err(self, text):
        with self.assertRaises(contracts.ContractsError) as ctx:
            self._load(text)
        return str(ctx.exception)

    def test_zero_entries_is_a_valid_declaration_of_none(self):
        decl = self._load("version: 1\ndepends_on: []\n")
        self.assertTrue(decl.present)
        self.assertFalse(decl.declares_dependencies)

    def test_depends_on_may_be_omitted_entirely(self):
        decl = self._load("version: 1\n")
        self.assertTrue(decl.present)
        self.assertEqual(decl.dependencies, ())

    def test_bare_string_shorthand_equals_the_mapping_form(self):
        shorthand = self._load("version: 1\ndepends_on:\n  - foundry-orchestrator\n")
        mapping = self._load("version: 1\ndepends_on:\n  - slug: foundry-orchestrator\n")
        self.assertEqual(shorthand.dependencies, mapping.dependencies)

    def test_vendor_path_defaults_under_the_conventional_root(self):
        decl = self._load("version: 1\ndepends_on:\n  - api-svc\n")
        self.assertEqual(decl.dependencies[0].vendor_path, "vendor/contracts/api-svc")
        self.assertEqual(contracts.DEFAULT_VENDOR_ROOT, "vendor/contracts")

    def test_vendor_path_override_is_honoured_and_normalized(self):
        decl = self._load(
            "version: 1\n"
            "depends_on:\n"
            "  - slug: api-svc\n"
            "    vendor: ./third_party/./api-svc/\n"
        )
        self.assertEqual(decl.dependencies[0].vendor_path, "third_party/api-svc")

    def test_description_is_carried_through(self):
        decl = self._load(
            "version: 1\ndepends_on:\n  - slug: api-svc\n    description: the HTTP API\n"
        )
        self.assertEqual(decl.dependencies[0].description, "the HTTP API")

    def test_vendor_dir_resolves_against_the_repo_root(self):
        decl = self._load("version: 1\ndepends_on:\n  - api-svc\n")
        self.assertEqual(
            decl.dependencies[0].vendor_dir(Path("/repo")),
            Path("/repo/vendor/contracts/api-svc"),
        )

    # --- strictness: every one of these must be an error, not a no-op ---

    def test_empty_file_is_an_error(self):
        self.assertIn("is empty", self._err(""))

    def test_missing_version_is_an_error(self):
        self.assertIn("`version` must be 1", self._err("depends_on: []\n"))

    def test_unknown_top_level_key_is_an_error(self):
        msg = self._err("version: 1\ndependencies: []\n")
        self.assertIn("unknown top-level key(s)", msg)
        self.assertIn("dependencies", msg)

    def test_unknown_entry_key_is_an_error(self):
        msg = self._err("version: 1\ndepends_on:\n  - slug: a\n    vendored: x\n")
        self.assertIn("unknown key(s)", msg)

    def test_top_level_list_is_an_error(self):
        self.assertIn("must be a mapping", self._err("- foundry-orchestrator\n"))

    def test_depends_on_must_be_a_list(self):
        self.assertIn("must be a list", self._err("version: 1\ndepends_on: foundry\n"))

    def test_duplicate_slug_is_an_error(self):
        self.assertIn("duplicate", self._err("version: 1\ndepends_on:\n  - a\n  - a\n"))

    def test_slug_with_a_path_separator_is_rejected(self):
        self.assertIn("`slug` must match", self._err("version: 1\ndepends_on:\n  - a/b\n"))

    def test_uppercase_slug_is_rejected(self):
        self.assertIn("`slug` must match", self._err("version: 1\ndepends_on:\n  - Foo\n"))

    def test_traversal_vendor_path_is_rejected(self):
        msg = self._err(
            "version: 1\ndepends_on:\n  - slug: a\n    vendor: ../../etc\n"
        )
        self.assertIn("must not escape the repo", msg)

    def test_absolute_vendor_path_is_rejected(self):
        msg = self._err("version: 1\ndepends_on:\n  - slug: a\n    vendor: /etc/passwd\n")
        self.assertIn("repo-relative", msg)

    def test_home_relative_vendor_path_is_rejected(self):
        msg = self._err("version: 1\ndepends_on:\n  - slug: a\n    vendor: ~/x\n")
        self.assertIn("repo-relative", msg)

    def test_invalid_yaml_is_an_error(self):
        self.assertIn("not valid YAML", self._err("version: 1\n  bad: [\n"))


class TestMountConventions(unittest.TestCase):
    """The mount point is a documented, overridable input contract."""

    def test_default_mount_is_slash_contracts(self):
        self.assertEqual(contracts.DEFAULT_MOUNT_DIR, "/contracts")
        self.assertEqual(contracts.INDEX_FILENAME, "index.json")

    def test_mount_dir_is_overridable_by_env(self):
        import os
        prior = os.environ.get(contracts.MOUNT_DIR_ENV)
        try:
            os.environ[contracts.MOUNT_DIR_ENV] = "/tmp/elsewhere"
            self.assertEqual(contracts.mount_dir(), Path("/tmp/elsewhere"))
            del os.environ[contracts.MOUNT_DIR_ENV]
            self.assertEqual(contracts.mount_dir(), Path("/contracts"))
        finally:
            if prior is None:
                os.environ.pop(contracts.MOUNT_DIR_ENV, None)
            else:
                os.environ[contracts.MOUNT_DIR_ENV] = prior


if __name__ == "__main__":
    unittest.main()
