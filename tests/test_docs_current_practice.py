"""P7 (v0.19.0): the current-practice docs describe the engine OUTSIDE the
repository. A vendored-only form — running the vendored scripts from a
project, `phasekit bootstrap`/`adopt`, the `.scaffold/manifest.json`
lifecycle, the raw `--upgrade` flag — may appear only where the text says it
is the legacy path: on a line that says "legacy" or "vendored", or under a
heading (the nearest one before it, outside code fences) that does. Vendored
usage stays documented, as "legacy, supported until migrated"; it just can
never again read as the way things work.

Run from the repo root: python3 -m unittest tests.test_docs_current_practice
"""

import re
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent

VENDORED_ONLY = (
    "bash scripts/run-until-done.sh",
    "bash scripts/container-setup.sh",
    "bash scripts/phasekit.sh",
    "phasekit bootstrap",
    "phasekit adopt",
    ".scaffold/manifest.json",
    "phasekit --upgrade",
)
CURRENT_PRACTICE_DOCS = (
    "README.md",
    "docs/INSTALL_LIFECYCLE.md",
    "docs/USAGE_PATTERNS.md",
    "docs/CONTAINERIZATION.md",
    "docs/EXECUTION_MODES.md",
    "docs/RELEASING.md",
)
MARK = re.compile(r"legacy|vendored", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")


def vendored_mentions_outside_legacy(text):
    """[(line_no, line)] for every vendored-only form that neither says
    legacy/vendored itself nor sits under a heading that does. Lines in a
    fenced block count, under the enclosing section's heading."""
    bad, heading, in_fence = [], "", False
    for no, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = HEADING.match(line)
            if m:
                heading = m.group(1)
        if any(s in line for s in VENDORED_ONLY) and not (MARK.search(line) or MARK.search(heading)):
            bad.append((no, line))
    return bad


class CurrentPracticeDocs(unittest.TestCase):
    def test_vendored_forms_appear_only_as_legacy(self):
        for rel in CURRENT_PRACTICE_DOCS:
            with self.subTest(doc=rel):
                bad = vendored_mentions_outside_legacy((REPO_ROOT / rel).read_text(encoding="utf-8"))
                self.assertEqual(bad, [], f"{rel}: a vendored-only form outside a legacy section")

    def test_the_checker_is_not_vacuous(self):
        self.assertEqual(len(vendored_mentions_outside_legacy(
            "# Quick start\n\nRun `bash scripts/run-until-done.sh`.\n")), 1)
        self.assertEqual(vendored_mentions_outside_legacy(
            "# Legacy (vendored)\n\n```\nbash scripts/run-until-done.sh\n```\n"), [])

    def test_the_readme_quickstart_is_install_init_use_upgrade(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        positions = [text.find(s) for s in ("install.sh", "phasekit init", "phasekit loop", "phasekit upgrade")]
        self.assertTrue(all(p >= 0 for p in positions), positions)
        self.assertEqual(positions, sorted(positions), "the quickstart order: install, init, use, upgrade")
        self.assertIn("Migrating from vendored", text)
        self.assertIn("phasekit migrate", text)

    def test_the_lifecycle_doc_covers_the_pin_store_plugin_and_migrate(self):
        text = (REPO_ROOT / "docs" / "INSTALL_LIFECYCLE.md").read_text(encoding="utf-8")
        for s in (".phasekit-version", "engines install", "PHASEKIT_NO_AUTO_FETCH", "plugin", "phasekit migrate",
                  "phasekit check", "phasekit upgrade"):
            self.assertIn(s, text)


if __name__ == "__main__":
    unittest.main()
