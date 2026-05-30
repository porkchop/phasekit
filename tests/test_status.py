#!/usr/bin/env python3
"""Tests for `phasekit status` / `--status` — a derived view of the workflow
artifacts (never a second source of truth).

Run from the repo root: `python3 -m unittest tests.test_status`
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "enrich-project.py"
WRAPPER = REPO_ROOT / "scripts" / "phasekit.sh"


def _status(target):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--status", str(target)],
        capture_output=True, text=True,
    )


def _write(target, name, obj):
    art = Path(target) / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / name).write_text(json.dumps(obj) + "\n")


class StatusCommand(unittest.TestCase):
    def test_missing_target_errors(self):
        res = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--status", "/no/such/dir"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 1)

    def test_empty_project(self):
        with tempfile.TemporaryDirectory() as d:
            res = _status(d)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("no phase artifacts yet", res.stdout)

    def test_approved_phase(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "phase-approval.json",
                   {"phase": "phase-3", "approved": True, "summary": "did the thing"})
            res = _status(d)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("approved through: phase-3", res.stdout)
            self.assertIn("next: start the next unapproved phase", res.stdout)

    def test_blocked_takes_precedence_over_approval(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "phase-approval.json", {"phase": "phase-6", "approved": True})
            _write(d, "phase-blocked.json",
                   {"blocked": True, "blocker_kind": "external-observation-required",
                    "reason": "needs a 72h soak", "next_step": "run the soak"})
            res = _status(d)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("approved through: phase-6", res.stdout)
            self.assertIn("BLOCKED [external-observation-required]", res.stdout)
            self.assertIn("needs a 72h soak", res.stdout)
            self.assertIn("run the soak", res.stdout)

    def test_project_complete(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "phase-approval.json", {"phase": "phase-9", "approved": True})
            _write(d, "project-complete.json", {"summary": "all phases done"})
            res = _status(d)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("PROJECT COMPLETE", res.stdout)

    def test_status_verb_uses_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "phase-approval.json", {"phase": "phase-1", "approved": True})
            res = subprocess.run(
                ["bash", str(WRAPPER), "status"], cwd=d, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("approved through: phase-1", res.stdout)


if __name__ == "__main__":
    unittest.main()
