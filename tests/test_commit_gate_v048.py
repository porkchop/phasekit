"""v0.4.8 commit-gate checks: scope containment + SPEC attestation.

Structural pins on run-until-done.sh (the gate is bash embedded in the loop;
these pin the load-bearing behaviors so deleting them fails tests) plus a
functional test of the scaffold-class scope scanner's python core.
"""

import json
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "run-until-done.sh")


class CommitGateStructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(SCRIPT) as f:
            self.text = f.read()

    def test_parses(self) -> None:
        r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hard_refusal_pair_and_explanation_artifact(self) -> None:
        self.assertIn(r"^\.claude/settings\.json$|^\.github/workflows/", self.text)
        self.assertIn("scope-refusal.json", self.text)
        # Refusal must explain recovery, not just refuse.
        self.assertIn("git restore --staged", self.text)

    def test_scaffold_warning_proceeds_not_blocks(self) -> None:
        self.assertIn("scope-warning.json", self.text)
        self.assertIn("Proceeding", self.text)

    def test_spec_attestation_records_numstat(self) -> None:
        self.assertIn("spec-change.json", self.text)
        self.assertIn("--numstat -- docs/SPEC.md", self.text)
        # Attestation must never gate: no return-nonzero in its branch.
        # (Anchor on the comment, not on first mention of the filename — since
        # v0.6.5 the transient-signal family list names it earlier in the file.)
        parts = self.text.split("SPEC change attestation", 1)
        self.assertEqual(len(parts), 2, "attestation comment anchor present")
        self.assertIn("spec-change.json", parts[1])


class ScopeScannerFunctionalTest(unittest.TestCase):
    def test_scaffold_intersection(self) -> None:
        # Reproduce the embedded python core against a fixture manifest.
        with tempfile.TemporaryDirectory() as root:
            manifest = os.path.join(root, "manifest.json")
            with open(manifest, "w") as f:
                json.dump({"files": [
                    {"path": "scripts/phasekit.sh", "ownership": "scaffold"},
                    {"path": "docs/SPEC.md", "ownership": "bootstrap-frozen"},
                ]}, f)
            code = (
                "import json, os, sys\n"
                "manifest = json.load(open(sys.argv[1]))\n"
                "scaffold = {f['path'] for f in manifest.get('files', [])\n"
                "            if f.get('ownership') == 'scaffold'}\n"
                "hits = sorted(set(os.environ.get('STAGED_FILES', '').split()) & scaffold)\n"
                "print(json.dumps(hits))\n"
            )
            env = dict(os.environ)
            env["STAGED_FILES"] = "scripts/phasekit.sh docs/SPEC.md src/app.js"
            r = subprocess.run(["python3", "-c", code, manifest],
                               capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(r.stdout), ["scripts/phasekit.sh"])


if __name__ == "__main__":
    unittest.main()
