#!/usr/bin/env python3
"""Tests for the opt-in mutation-testing protocol (item 4).

Scope decision recorded here because it is a product decision, not a build
detail: this ships as a capability-profile OPTION (`with-mutation`), never as a
universal gate. phasekit is a public `curl | bash` tool whose META_SPEC now
states it must work for someone with no orchestration layer; mandating a heavy
practice for every downstream user is a far bigger claim than "Foundry does
this". The tests below pin the opt-in-ness as hard as the behaviour.

The harness's two load-bearing refusals, both about FALSE GREENS:
  - a budget-skipped mutant is recorded as `not_run`, never as a pass
  - there is no result cache, so there is nothing to invalidate wrongly

Run from the repo root: `python3 -m unittest tests.test_mutation_run`
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mutation-run.py"
CAPS = REPO_ROOT / "capabilities" / "project-capabilities.yaml"
DOC = REPO_ROOT / "docs" / "MUTATION_TESTING.md"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"

# A tiny subject with ONE well-tested branch and one that no test discriminates.
SUBJECT = '''\
def classify(n):
    if n > 0:
        return "positive"
    return "other"


def unguarded(flag):
    if flag:
        return "on"
    return "off"
'''

# Only classify() is really asserted; unguarded() is "covered" but not checked.
SUITE = '''\
import unittest
import subject


class T(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(subject.classify(1), "positive")

    def test_zero_is_other(self):
        self.assertEqual(subject.classify(0), "other")

    def test_unguarded_runs(self):
        subject.unguarded(True)
        subject.unguarded(False)
'''

TEST_COMMAND = f"{sys.executable} -m unittest discover -s . -p 'suite.py' -q"


class HarnessFixture(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="pk-mutation-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.tmp = root / "repo"
        self.specs = root / "specs"
        self.tmp.mkdir()
        self.specs.mkdir()
        (self.tmp / "subject.py").write_text(SUBJECT, encoding="utf-8")
        (self.tmp / "suite.py").write_text(SUITE, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "-C", str(self.tmp), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.tmp), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "base"], check=True)

    def write_spec(self, mutants, name="spec.json"):
        # OUTSIDE the repo under test: the harness refuses a dirty tree, and a
        # spec file dropped into the repo would itself be the dirt.
        path = self.specs / name
        path.write_text(json.dumps({
            "version": 1, "test_command": TEST_COMMAND, "mutants": mutants,
        }), encoding="utf-8")
        return path

    def run_harness(self, spec, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--spec", str(spec), "--repo", str(self.tmp), *extra],
            capture_output=True, text=True, cwd=self.tmp)

    def audit(self):
        return json.loads((self.tmp / "artifacts" / "mutation-run.json")
                          .read_text(encoding="utf-8"))

    KILLED_MUTANT = {"id": "m-killed", "file": "subject.py",
                     "find": "if n > 0:", "replace": "if n >= 0:",
                     "rationale": "boundary the zero test should catch"}
    SURVIVING_MUTANT = {"id": "m-survives", "file": "subject.py",
                        "find": 'return "on"', "replace": 'return "ON"',
                        "rationale": "unguarded() is covered but never asserted"}


class DetectsWhatItShould(HarnessFixture):
    def test_a_discriminated_mutant_is_killed(self):
        r = self.run_harness(self.write_spec([self.KILLED_MUTANT]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.audit()["summary"]["killed"], 1)

    def test_a_mutant_the_tests_do_not_notice_survives(self):
        """The real finding: coverage without discrimination."""
        r = self.run_harness(self.write_spec([self.SURVIVING_MUTANT]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("SURVIVED", r.stdout)
        self.assertEqual(self.audit()["summary"]["survived"], 1)

    def test_the_survivor_message_says_what_to_do_about_it(self):
        r = self.run_harness(self.write_spec([self.SURVIVING_MUTANT]))
        self.assertIn("Strengthen the test", r.stderr)
        self.assertIn("never by deleting it", r.stderr)

    def test_the_file_is_always_restored(self):
        before = (self.tmp / "subject.py").read_text(encoding="utf-8")
        self.run_harness(self.write_spec([self.KILLED_MUTANT, self.SURVIVING_MUTANT]))
        self.assertEqual((self.tmp / "subject.py").read_text(encoding="utf-8"), before)
        self.assertEqual(
            subprocess.run(["git", "-C", str(self.tmp), "status", "--porcelain",
                            "subject.py"], capture_output=True, text=True).stdout, "")


class RefusesFalseGreens(HarnessFixture):
    def test_a_budget_skipped_mutant_is_not_run_not_a_pass(self):
        """An unrun mutant that looked like a pass is the false green this
        whole design refuses."""
        spec = self.write_spec([self.KILLED_MUTANT, self.SURVIVING_MUTANT])
        r = self.run_harness(spec, "--max-seconds", "0")
        outcomes = {x["id"]: x["outcome"] for x in self.audit()["results"]}
        self.assertEqual(set(outcomes.values()), {"not_run"})
        self.assertEqual(self.audit()["summary"]["killed"], 0)
        self.assertEqual(r.returncode, 0)  # nothing ran, so nothing survived

    def test_an_ambiguous_find_is_an_error_not_a_guess(self):
        (self.tmp / "subject.py").write_text(
            SUBJECT + '\n\ndef again(n):\n    if n > 0:\n        return 1\n    return 0\n',
            encoding="utf-8")
        subprocess.run(["git", "-C", str(self.tmp), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qam", "two sites"], check=True)
        r = self.run_harness(self.write_spec([self.KILLED_MUTANT]))
        self.assertEqual(r.returncode, 2)
        self.assertIn("matches 2 times", r.stdout + r.stderr)

    def test_a_missing_file_is_an_error(self):
        r = self.run_harness(self.write_spec([dict(self.KILLED_MUTANT, file="nope.py")]))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.audit()["summary"]["error"], 1)

    def test_a_declared_equivalent_mutant_is_recorded_not_silently_dropped(self):
        spec = self.write_spec([{"id": "m-eq", "file": "subject.py",
                                 "find": 'return "off"', "equivalent": True,
                                 "rationale": "no observable difference"}])
        r = self.run_harness(spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = self.audit()["results"][0]
        self.assertEqual(result["outcome"], "equivalent")
        self.assertEqual(result["rationale"], "no observable difference")

    def test_it_refuses_a_dirty_tree(self):
        (self.tmp / "subject.py").write_text(SUBJECT + "\n# uncommitted\n", encoding="utf-8")
        r = self.run_harness(self.write_spec([self.KILLED_MUTANT]))
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)

    def test_there_is_no_result_cache_to_invalidate(self):
        """A resumable cache was considered and rejected: its failure mode is a
        false green on a quality gate."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("REJECTED", text)
        for banned in ("--resume", "cache_path", "load_cache"):
            self.assertNotIn(banned, text)


class Chunking(HarnessFixture):
    def _many(self, n):
        return [dict(self.KILLED_MUTANT, id=f"m{i}",
                     find="if n > 0:" if i == 0 else 'return "other"',
                     replace="if n >= 0:" if i == 0 else 'return "OTHER"')
                for i in range(n)]

    def test_list_prints_a_plan_without_running_anything(self):
        r = self.run_harness(self.write_spec(self._many(2)), "--list", "--chunk", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chunk 0", r.stdout)
        self.assertIn("chunk 1", r.stdout)
        self.assertFalse((self.tmp / "artifacts" / "mutation-run.json").exists())

    def test_a_chunk_runs_only_its_own_mutants(self):
        spec = self.write_spec(self._many(2))
        r = self.run_harness(spec, "--chunk", "1", "--index", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x["id"] for x in self.audit()["results"]], ["m0"])

    def test_an_out_of_range_chunk_is_an_error(self):
        r = self.run_harness(self.write_spec(self._many(2)), "--chunk", "1", "--index", "9")
        self.assertEqual(r.returncode, 2)
        self.assertIn("out of range", r.stderr)


class SpecValidation(HarnessFixture):
    def _err(self, payload):
        path = self.specs / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        r = self.run_harness(path)
        self.assertEqual(r.returncode, 2)
        return r.stderr

    def test_wrong_version_is_rejected(self):
        self.assertIn("`version` must be 1",
                      self._err({"version": 9, "test_command": "true", "mutants": [{}]}))

    def test_missing_test_command_is_rejected(self):
        self.assertIn("`test_command`", self._err({"version": 1, "mutants": [{}]}))

    def test_a_mutant_without_a_rationale_is_rejected(self):
        """A mutant nobody can explain is a mutant nobody can review."""
        msg = self._err({"version": 1, "test_command": "true", "mutants": [
            {"id": "m", "file": "subject.py", "find": "x", "replace": "y"}]})
        self.assertIn("`rationale`", msg)

    def test_a_non_equivalent_mutant_without_replace_is_rejected(self):
        msg = self._err({"version": 1, "test_command": "true", "mutants": [
            {"id": "m", "file": "subject.py", "find": "x", "rationale": "r"}]})
        self.assertIn("needs `replace`", msg)

    def test_duplicate_ids_are_rejected(self):
        msg = self._err({"version": 1, "test_command": "true", "mutants": [
            {"id": "m", "file": "s", "find": "a", "replace": "b", "rationale": "r"},
            {"id": "m", "file": "s", "find": "c", "replace": "d", "rationale": "r"}]})
        self.assertIn("duplicate mutant id", msg)


class ItIsOptInNotAMandate(unittest.TestCase):
    """Product-scope pin. Making this universal would be a decision about a
    public tool, not a build detail — so the opt-in-ness is asserted, not
    assumed."""

    def test_the_doc_is_not_in_the_default_profile(self):
        caps = CAPS.read_text(encoding="utf-8")
        default_block = caps.split("  default:", 1)[1].split("  game-project:", 1)[0]
        self.assertNotIn("MUTATION_TESTING", default_block)
        self.assertNotIn("mutation-run", default_block)

    def test_the_with_mutation_profile_exists_and_extends_default(self):
        import yaml
        caps = yaml.safe_load(CAPS.read_text(encoding="utf-8"))
        profile = caps["profiles"]["with-mutation"]
        self.assertEqual(profile["extends"], "default")
        self.assertIn("MUTATION_TESTING", profile["include_docs"])
        self.assertIn("mutation-run", profile["include_scripts"])

    def test_a_default_enrich_installs_neither(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run([sys.executable, str(ENRICH), str(project)],
                           capture_output=True, text=True, check=True)
            self.assertFalse((project / "docs" / "MUTATION_TESTING.md").exists())
            self.assertFalse((project / "scripts" / "mutation-run.py").exists())

    def test_the_with_mutation_profile_installs_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            r = subprocess.run([sys.executable, str(ENRICH), str(project),
                                "--profile", "with-mutation"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((project / "docs" / "MUTATION_TESTING.md").is_file())
            self.assertTrue((project / "scripts" / "mutation-run.py").is_file())

    def test_the_doc_states_it_is_an_option_not_a_gate(self):
        doc = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn("an option phasekit offers, not a gate it imposes", doc)
        self.assertIn("Most projects should not enable it", doc)

    def test_the_doc_records_the_rejected_cache_and_the_division_of_labour(self):
        doc = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn("resumable on-disk cache was considered and REJECTED", doc)
        self.assertIn("false green", doc)
        self.assertIn("never invents a mutant", doc)


if __name__ == "__main__":
    unittest.main()
