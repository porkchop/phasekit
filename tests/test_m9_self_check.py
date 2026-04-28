#!/usr/bin/env python3
"""Tests for `enrich-project.py --self-check` (M9 Slice A, acceptance criterion #2).

- Positive control: assert three named files land in their expected ownership classes.
- Negative control: inject an unclassified file in a synthetic scaffold copy;
  assert exit 1 with the path named on stderr.
- End-to-end: the live scaffold's `--self-check` exits 0.

Run from the repo root: `python3 -m unittest tests.test_m9_self_check`
"""

import importlib.util
import shutil
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


class PositiveControl(unittest.TestCase):
    """Three named files must land in their expected ownership classes."""

    EXPECTED = {
        "docs/QUALITY_GATES.md": "scaffold",
        "templates/CLAUDE.template.md": "scaffold-template",
        "LICENSE": "scaffold-internal",
    }

    def test_named_files_have_expected_classes(self):
        module = _load_module()
        manifest = module.load_manifest()
        classified = module.collect_classified_paths(manifest)
        for path, expected in self.EXPECTED.items():
            with self.subTest(path=path):
                self.assertIn(path, classified, f"{path} not classified by manifest")
                actual_class, _section = classified[path]
                self.assertEqual(
                    actual_class, expected,
                    f"{path} expected {expected!r} got {actual_class!r}",
                )


class LiveScaffoldPasses(unittest.TestCase):
    """`--self-check` against the current scaffold must exit 0."""

    def test_self_check_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--self-check"],
            capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"--self-check exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


class NegativeControl(unittest.TestCase):
    """Injecting an unclassified file produces exit 1 and names the path on stderr.

    We build a synthetic scaffold copy in a temp dir with a minimal manifest
    classifying one file, then add an extra tracked file with no classification.
    """

    def test_unclassified_file_fails_with_path_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "synthetic-scaffold"
            (tmp_root / "scripts").mkdir(parents=True)
            (tmp_root / "capabilities").mkdir(parents=True)

            # Copy the real script under test.
            shutil.copy2(SCRIPT_PATH, tmp_root / "scripts" / "enrich-project.py")

            # Minimal manifest: classifies only `classified.md`.
            (tmp_root / "capabilities" / "project-capabilities.yaml").write_text(
                "version: 1\n"
                "project:\n"
                "  name: synthetic\n"
                "text_default: true\n"
                "ignore: []\n"
                "profiles:\n"
                "  default: {}\n"
                "agents: {}\n"
                "docs: {}\n"
                "hooks: {}\n"
                "scripts: {}\n"
                "skills: {}\n"
                "files:\n"
                "  classified.md:\n"
                "    ownership: scaffold\n"
            )
            (tmp_root / "classified.md").write_text("ok\n")
            (tmp_root / "DELIBERATELY_UNCLASSIFIED.md").write_text("uh oh\n")

            # Make it a git repo so `git ls-files` returns these.
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=tmp_root, check=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
                cwd=tmp_root, check=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "fixture"],
                cwd=tmp_root, check=True,
            )

            result = subprocess.run(
                [sys.executable, str(tmp_root / "scripts" / "enrich-project.py"), "--self-check"],
                capture_output=True, text=True,
            )

            self.assertEqual(
                result.returncode, 1,
                f"expected exit 1, got {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "DELIBERATELY_UNCLASSIFIED.md", result.stderr,
                f"stderr did not name the unclassified path:\n{result.stderr}",
            )


class IgnoreConstraintEnforced(unittest.TestCase):
    """A glob in `ignore:` matching a path under a protected prefix fails --self-check
    and names the offending glob and path (M9 §8 falsifiable check)."""

    def test_protected_prefix_violation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "synthetic-scaffold"
            (tmp_root / "scripts").mkdir(parents=True)
            (tmp_root / "capabilities").mkdir(parents=True)
            (tmp_root / "docs").mkdir(parents=True)

            shutil.copy2(SCRIPT_PATH, tmp_root / "scripts" / "enrich-project.py")

            # Manifest with a forbidden ignore glob (`docs/**`) and a single
            # tracked doc that the glob would match.
            (tmp_root / "capabilities" / "project-capabilities.yaml").write_text(
                "version: 1\n"
                "project:\n"
                "  name: synthetic\n"
                "text_default: true\n"
                "ignore:\n"
                '  - "docs/**"\n'
                "profiles:\n"
                "  default: {}\n"
                "agents: {}\n"
                "docs: {}\n"
                "hooks: {}\n"
                "scripts: {}\n"
                "skills: {}\n"
                "files: {}\n"
            )
            (tmp_root / "docs" / "SOMETHING.md").write_text("hi\n")

            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
                cwd=tmp_root, check=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "fixture"],
                cwd=tmp_root, check=True,
            )

            result = subprocess.run(
                [sys.executable, str(tmp_root / "scripts" / "enrich-project.py"), "--self-check"],
                capture_output=True, text=True,
            )

            self.assertEqual(result.returncode, 1, "expected exit 1")
            self.assertIn("docs/**", result.stderr)
            self.assertIn("docs/SOMETHING.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
