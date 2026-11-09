"""v0.29.0 (audit D3): phase-close evidence files are opt-in by the tree, and
`phasekit evidence untrack` stops tracking them at rest, gated.

Pinned here:
  * evidence_files_enabled: PHASEKIT_EVIDENCE_FILES=1 forces, =0 disables;
    unset, a tree whose .gitignore ignores artifacts/iterations/ gets no file
    and every other tree gets one (the pre-v0.29.0 behaviour, unchanged);
  * a landing in an ignoring tree writes no evidence file into the commit and
    still stamps the record (plan_paths); =1 writes it even there (git add -f);
  * `phasekit init` seeds the ignore line, so a new project never tracks one;
  * `phasekit evidence untrack`: tracked evidence files leave the index and
    stay on disk, .gitignore gains the line, ONE commit, the gate ran; a red
    gate restores everything (exit 4); --dry-run changes nothing; a dirty tree
    or a loop session is refused (exit 2); a second run is a no-op.

Run from the repo root: python3 -m unittest tests.test_evidence_optin
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"

_spec = importlib.util.spec_from_file_location(
    "pk_iteration_facts_for_evidence", Path(__file__).resolve().parent / "test_iteration_facts.py")
IF = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(IF)
_eo = importlib.util.spec_from_file_location(
    "pk_engine_outside_for_evidence", Path(__file__).resolve().parent / "test_engine_outside.py")
EO = importlib.util.module_from_spec(_eo)
_eo.loader.exec_module(EO)


def _extract(name: str) -> str:
    text = LOOP.read_text()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, name
    return m.group(0)


class EvidenceFilesEnabled(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-ev-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)

    def enabled(self, **env):
        script = f'ROOT_DIR="{self.root}"\n' + _extract("evidence_files_enabled") + "\nevidence_files_enabled"
        e = {k: v for k, v in os.environ.items() if not k.startswith("PHASEKIT_")}
        e.update(env)
        return subprocess.run(["bash", "-c", script], env=e, capture_output=True).returncode == 0

    def test_a_plain_tree_writes_evidence_as_before(self):
        self.assertTrue(self.enabled())

    def test_an_ignoring_tree_does_not(self):
        (self.root / ".gitignore").write_text("artifacts/iterations/\n")
        self.assertFalse(self.enabled())

    def test_the_env_overrides_the_tree_both_ways(self):
        (self.root / ".gitignore").write_text("artifacts/iterations/\n")
        self.assertTrue(self.enabled(PHASEKIT_EVIDENCE_FILES="1"))
        (self.root / ".gitignore").unlink()
        self.assertFalse(self.enabled(PHASEKIT_EVIDENCE_FILES="0"))


class LandingInAnIgnoringTree(unittest.TestCase):
    def _repo(self):
        repo = IF.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write(".gitignore", "artifacts/iterations/\n")
        repo.git("add", ".gitignore")
        repo.git("commit", "-qm", "ignore evidence")
        return repo

    def test_no_evidence_file_rides_and_the_record_is_still_stamped(self):
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.scenario(IF.APPROVE_188)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("boundary-state: rested (step 7)", out)
        tracked = repo.git("ls-files", "artifacts/iterations")
        self.assertEqual(tracked, "", out)
        self.assertFalse((repo.repo / "artifacts" / "iterations").exists(), "nothing written to disk either")
        record = json.loads(repo.git("show", "HEAD:artifacts/phase-approval.json"))
        self.assertIn("plan_paths", record, "the record stamps are not the file")
        self.assertEqual(str(record["iteration"]), "88")

    def test_the_env_forces_the_file_even_there(self):
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.scenario(IF.APPROVE_188)
        r = repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_EVIDENCE_FILES": "1"})
        out = r.stdout + r.stderr
        self.assertIn("boundary-state: rested (step 7)", out)
        ev = json.loads(repo.git("show", "HEAD:artifacts/iterations/88/188.json"))
        self.assertEqual(ev["phase"], "188")


class UntrackCommand(EO.EngineFixture):
    """`phasekit evidence untrack` on a pinned project (the fixture's init)."""

    def _tracking_project(self):
        proj = self.new_project()
        gi = proj / ".gitignore"
        self.assertIn("artifacts/iterations/", gi.read_text(), "init seeds the ignore line")
        # make it a pre-v0.29.0 project: tracked evidence, no ignore line
        gi.write_text("\n".join(ln for ln in gi.read_text().splitlines()
                                if "artifacts/iterations/" not in ln and "phasekit v0.29.0" not in ln) + "\n")
        for n, p in ((1, "1"), (2, "3"), (2, "complete")):
            f = proj / "artifacts" / "iterations" / str(n) / f"{p}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"schema": 1, "iteration": str(n), "phase": p}) + "\n")
        EO._git(proj, "add", "-A")
        EO._git(proj, "commit", "-qm", "tracked evidence, no ignore")
        self.assertEqual(len(EO._git(proj, "ls-files", "artifacts/iterations").splitlines()), 3)
        return proj

    def test_init_seeds_the_ignore_line(self):
        proj = self.new_project()
        self.assertIn("artifacts/iterations/", (proj / ".gitignore").read_text())
        self.assertIn(".gitignore", EO._git(proj, "ls-files").splitlines())
        self.assertEqual(len(EO._git(proj, "log", "--format=%H").splitlines()), 1, "in init's one commit")

    def test_untrack_leaves_the_files_on_disk_ignored_in_one_gated_commit(self):
        proj = self._tracking_project()
        before = EO._git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "evidence", "untrack")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gate:", r.stdout + r.stderr, "the project's gate ran")
        self.assertEqual(EO._git(proj, "ls-files", "artifacts/iterations"), "")
        self.assertTrue((proj / "artifacts" / "iterations" / "2" / "complete.json").is_file())
        self.assertIn("artifacts/iterations/", (proj / ".gitignore").read_text())
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "", "clean at rest")
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD^"), before, "exactly one commit")
        self.assertEqual(EO._git(proj, "log", "-1", "--format=%s"),
                         "chore(phasekit): untrack phase evidence (artifacts/iterations/)")
        # a second run is a no-op
        r = self.cli(proj, "evidence", "untrack")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to do", r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD^"), before)

    def test_dry_run_changes_nothing(self):
        proj = self._tracking_project()
        before = EO._git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "evidence", "untrack", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("untrack 3 file(s)", r.stdout)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD"), before)
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        self.assertEqual(len(EO._git(proj, "ls-files", "artifacts/iterations").splitlines()), 3)

    def test_a_red_gate_restores_the_tree_and_exits_4(self):
        proj = self._tracking_project()
        gate = proj / "scripts" / "phasekit-verify.sh"
        gate.write_text("#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\nexit 1\n")
        EO._git(proj, "add", "-A"); EO._git(proj, "commit", "-qm", "red gate")
        before = EO._git(proj, "rev-parse", "HEAD")
        gi_before = (proj / ".gitignore").read_text()
        r = self.cli(proj, "evidence", "untrack")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("nothing changed", r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD"), before)
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        self.assertEqual((proj / ".gitignore").read_text(), gi_before)
        self.assertEqual(len(EO._git(proj, "ls-files", "artifacts/iterations").splitlines()), 3)

    def test_a_dirty_tree_and_a_loop_session_are_refused(self):
        proj = self._tracking_project()
        (proj / "scratch.txt").write_text("wip\n")
        r = self.cli(proj, "evidence", "untrack")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not clean", r.stderr)
        (proj / "scratch.txt").unlink()
        r = self.cli(proj, "evidence", "untrack", PHASEKIT_ITER_MARKER="1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("operator's command", r.stderr)
        self.assertEqual(len(EO._git(proj, "ls-files", "artifacts/iterations").splitlines()), 3)

    def test_an_extra_path_after_the_evidence_is_already_untracked(self):
        # review M3: `git rm` is fatal on a pathspec matching nothing; only the
        # paths with tracked files may reach it
        proj = self.new_project()      # already ignores artifacts/iterations/, tracks none
        shots = proj / "artifacts" / "phase3-qa"
        shots.mkdir()
        (shots / "a.png").write_bytes(b"\x89PNG")
        EO._git(proj, "add", "-A"); EO._git(proj, "commit", "-qm", "shots")
        r = self.cli(proj, "evidence", "untrack", "--path", "artifacts/phase3-qa")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "ls-files", "artifacts/phase3-qa"), "")
        self.assertIn("artifacts/phase3-qa/", (proj / ".gitignore").read_text())
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")

    def test_an_extra_path_is_untracked_too(self):
        proj = self._tracking_project()
        shots = proj / "artifacts" / "phase3-qa"
        shots.mkdir()
        (shots / "a.png").write_bytes(b"\x89PNG")
        EO._git(proj, "add", "-A"); EO._git(proj, "commit", "-qm", "shots")
        r = self.cli(proj, "evidence", "untrack", "--path", "artifacts/phase3-qa")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "ls-files", "artifacts/phase3-qa"), "")
        self.assertTrue((shots / "a.png").is_file())
        self.assertIn("artifacts/phase3-qa/", (proj / ".gitignore").read_text(),
                      "ignored too, or the loop's next landing would sweep it back in")
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
