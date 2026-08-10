"""compact-reanchor.sh: fires only on source=compact, bounded, fail-open."""

import json
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, ".claude", "hooks", "compact-reanchor.sh")


def run_hook(payload: dict, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


def make_project(root: str) -> None:
    os.makedirs(os.path.join(root, "artifacts"))
    os.makedirs(os.path.join(root, "docs"))
    with open(os.path.join(root, "artifacts", "phase-approval.json"), "w") as f:
        json.dump({"phase": "phase-3"}, f)
    with open(os.path.join(root, "docs", "PHASES.md"), "w") as f:
        f.write("# Phase plan\n\n## Phase 3 — thing\n\n## Phase 4 — next thing\n")
    with open(os.path.join(root, "docs", "SPEC.md"), "w") as f:
        f.write("# Spec\n\n## Acceptance criteria\n\n1. AC one\n2. AC two\n")


class CompactReanchorTest(unittest.TestCase):
    def test_silent_on_non_compact_sources(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            make_project(root)
            for source in ("startup", "resume", "clear"):
                r = run_hook({"source": source}, root)
                self.assertEqual(r.returncode, 0, source)
                self.assertEqual(r.stdout, "", source)

    def test_compact_emits_bounded_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            make_project(root)
            r = run_hook({"source": "compact"}, root)
            self.assertEqual(r.returncode, 0)
            self.assertIn("compact-reanchor", r.stdout)
            self.assertIn("phase-3", r.stdout)
            self.assertIn("## Phase 4", r.stdout)
            self.assertIn("Acceptance criteria", r.stdout)
            self.assertLessEqual(len(r.stdout.encode("utf-8")), 2048)

    def test_fail_open_on_missing_docs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            r = run_hook({"source": "compact"}, root)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_fail_open_on_garbage_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            r = subprocess.run(
                ["bash", HOOK], input="not json", capture_output=True,
                text=True, cwd=root, timeout=30,
            )
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_truncation_marked_when_oversized(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            make_project(root)
            with open(os.path.join(root, "docs", "PHASES.md"), "w") as f:
                f.write("# Phase plan\n" + "".join(
                    f"## Phase {i} — {'x' * 120}\n" for i in range(40)))
            r = run_hook({"source": "compact"}, root)
            self.assertEqual(r.returncode, 0)
            self.assertLessEqual(len(r.stdout.encode("utf-8")), 2048)
            self.assertIn("truncated by compact-reanchor", r.stdout)


if __name__ == "__main__":
    unittest.main()
