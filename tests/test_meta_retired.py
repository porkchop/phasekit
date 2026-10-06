#!/usr/bin/env python3
"""The META pair is retired (v0.18.6; Aaron 2026-10-06).

docs/META_SPEC.md + docs/META_PHASES.md (phasekit's self-improvement spec and
plan, last edited 2026-04-27) and docs/SELF_APPLICATION_EXAMPLE.md went
dormant once phasekit moved to hand-built releases; running phasekit as its
own phasekit project was considered and declined. They live in docs/archive/
for provenance. Nothing may point a reader or a session at them again, and
they never ship. TheMetaPairIsArchived is red on v0.18.5; NeverInstalled
guards what was already true (no profile ever installed them).

Run from the repo root: python3 -m unittest tests.test_meta_retired
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
RETIRED = ("META_SPEC.md", "META_PHASES.md", "SELF_APPLICATION_EXAMPLE.md")
NAMES = re.compile(r"META_SPEC|META_PHASES|SELF_APPLICATION_EXAMPLE")
# Where the names may still appear: the archive itself, phasekit's own dated
# records (artifacts/), the manifest's registration of the archive, and the
# tests that pin the retirement.
ALLOWED = ("docs/archive/", "artifacts/", "capabilities/project-capabilities.yaml",
           "tests/test_meta_retired.py")


def _tracked():
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True,
                         text=True, check=True).stdout.splitlines()
    return [p for p in out if (REPO_ROOT / p).is_file()]


class TheMetaPairIsArchived(unittest.TestCase):
    def test_moved_to_the_archive_with_a_readme(self):
        for name in RETIRED:
            with self.subTest(name=name):
                self.assertFalse((REPO_ROOT / "docs" / name).exists())
                self.assertTrue((REPO_ROOT / "docs" / "archive" / name).is_file())
        self.assertTrue((REPO_ROOT / "docs" / "archive" / "README.md").is_file())

    def test_nothing_outside_the_archive_names_them(self):
        hits = []
        for rel in _tracked():
            if rel.startswith(ALLOWED[:2]) or rel in ALLOWED[2:]:
                continue
            try:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits += [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1)
                     if NAMES.search(line)]
        self.assertEqual(hits, [])

    def test_the_loop_reads_one_plan(self):
        src = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()
        body = src.split("def plan_file():", 1)[1].split("\n\n", 1)[0]
        self.assertIn('"docs/PHASES.md"', body)
        self.assertNotIn("META", body)


class NeverInstalled(unittest.TestCase):
    def test_no_profile_installs_an_archived_doc(self):
        tmp = Path(tempfile.mkdtemp(prefix="pk-meta-retired-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for profile in ("default", "with-design", "with-mutation", "python-uv"):
            with self.subTest(profile=profile):
                target = tmp / profile
                target.mkdir()
                r = subprocess.run([sys.executable, str(ENRICH), str(target), "--profile", profile],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                found = [str(p.relative_to(target)) for p in target.rglob("*")
                         if p.name in RETIRED or "archive" in p.relative_to(target).parts]
                self.assertEqual(found, [])
                manifest = (target / ".scaffold" / "manifest.json").read_text()
                self.assertIsNone(NAMES.search(manifest))


if __name__ == "__main__":
    unittest.main()
