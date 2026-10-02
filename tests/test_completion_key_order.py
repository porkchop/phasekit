#!/usr/bin/env python3
"""The completion record's scalars come first (v0.18.5; Aaron 2026-10-02, rider 3).

artifacts/project-complete.json carries the full open deferral set (v0.18.0
composition). On xmeo-v3 it reached 382 KB, and the loop's stamp appended
`iteration` AFTER a ~218 KB `deferrals` array: the orchestrator's landing
reader, which read the first 256 KB, could not find `iteration` and stalled
xmeo for ~9 hours on 2026-10-01 (the reader now reads 4 MiB; this is the
other half). Every loop write of the record now orders its keys: `iteration`
first, then the other scalar keys, then the arrays and objects — keys and
values unchanged, only their order.

Every test here is red on v0.18.4.

Run from the repo root: python3 -m unittest tests.test_completion_key_order
"""

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"

_spec = importlib.util.spec_from_file_location(
    "pk_iteration_facts_ko", Path(__file__).resolve().parent / "test_iteration_facts.py")
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)
H, Repo, supervised = F.H, F.Repo, F.supervised


def ordered_keys(text):
    pairs = []
    json.loads(text, object_pairs_hook=lambda kv: pairs.append([k for k, _ in kv]) or dict(kv))
    return pairs[-1]  # the outermost object closes last


class ScalarsFirst(unittest.TestCase):
    def _repo(self, squash=False):
        repo = Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        return repo

    def assertScalarsFirst(self, text, msg=""):
        record = json.loads(text)
        keys = ordered_keys(text)
        self.assertEqual(keys[0], "iteration", f"{msg}\n{keys}")
        kinds = [isinstance(record[k], (list, dict)) for k in keys]
        self.assertEqual(kinds, sorted(kinds), f"every scalar before any array or object: {keys}")
        self.assertLess(text.index('"iteration"'), 64, "a prefix reader finds it at once")

    def test_the_xmeo_shape_a_large_open_set_never_pushes_iteration_back(self):
        # the 2026-10-01 stall at small scale: a session record that names no
        # iteration, a ~240 KB open set seeded from the previous record
        repo = self._repo()
        big = [{"item": f"Deferred thing {i}", "reason": "r" * 6000, "suggested_task": "t", "key": f"k{i}"}
               for i in range(40)]
        repo.write("artifacts/project-complete.json", json.dumps({"done": True, "deferrals": big}) + "\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "iteration 87 complete")
        repo.git("rm", "-q", "artifacts/project-complete.json")
        repo.git("commit", "-qm", "archive")
        supervised(repo, 88)
        repo.scenario('echo w >> src.txt\n'
                      'jq -n \'{done: true, deferrals: [{item: "NEW", reason: "r", suggested_task: "t", key: "new"}],'
                      ' summary: "s", closes: []}\' > artifacts/project-complete.json\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        text = repo.git("show", "HEAD:artifacts/project-complete.json")
        self.assertGreater(len(text), 200_000, r.stdout + r.stderr)
        self.assertScalarsFirst(text, r.stdout + r.stderr)
        record = json.loads(text)
        self.assertEqual(record["iteration"], 88)
        self.assertEqual(len(record["deferrals"]), 41, "the open set is unchanged, only reordered")
        self.assertEqual(record["summary"], "s")

    def test_a_session_record_written_arrays_first_lands_scalars_first(self):
        repo = self._repo(squash=True)
        supervised(repo, 88)
        repo.scenario(r"""
echo w >> src.txt
jq -n '{deferrals: [{item: "Tune the panel", reason: "r", suggested_task: "t", key: "c-key"}],
        closes: [], done: true, summary: "iteration 88 done",
        suggested_commit_message: "iteration 88: done"}' > artifacts/project-complete.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        for ref in ("main", "iter/1-test"):
            with self.subTest(ref=ref):
                self.assertScalarsFirst(repo.git("show", f"{ref}:artifacts/project-complete.json"), out)

    def test_a_record_the_loop_synthesizes_from_a_final_approval_is_scalars_first(self):
        repo = self._repo()
        supervised(repo, 88)
        repo.scenario(r"""
echo w >> src.txt
jq -n '{phase: "phase-1", approved: true, final_phase: true, summary: "last",
        suggested_commit_message: "iteration 88 phase 1: last",
        deferrals: [{item: "AC#3 integer math", reason: "later", suggested_task: "fixed point"}]}' \
  > artifacts/phase-approval.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("recorded artifacts/project-complete.json from it (step 3)", out)
        self.assertScalarsFirst(repo.git("show", "HEAD:artifacts/project-complete.json"), out)


class TheOrderingIsOneDefinition(unittest.TestCase):
    """The order is one jq definition the writers share, and it changes order only."""

    def _defs(self):
        r = subprocess.run(["bash", "-c", f'eval "$(sed -n "/^_completion_key_order_jq() {{/,/^}}/p" "{LOOP}")"; '
                           "_completion_key_order_jq"], capture_output=True, text=True, check=True)
        return r.stdout

    def test_values_and_keys_are_unchanged_and_nested_objects_keep_their_order(self):
        rec = {"deferrals": [{"z": 1, "a": 2}], "plan_paths": {"z": 1, "a": 2}, "done": True,
               "summary": "s", "iteration": None, "base": "abc", "ts": 3}
        r = subprocess.run(["jq", self._defs() + " completion_key_order"], input=json.dumps(rec),
                           capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(r.stdout), rec)
        self.assertEqual(ordered_keys(r.stdout),
                         ["iteration", "done", "summary", "base", "ts", "deferrals", "plan_paths"])
        self.assertIn('"z": 1,\n      "a": 2', r.stdout, "inside a value nothing moves")

    def test_a_record_without_iteration_gains_no_key(self):
        r = subprocess.run(["jq", "-c", self._defs() + " completion_key_order"],
                           input='{"deferrals": [], "done": true}', capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), '{"done":true,"deferrals":[]}')

    def test_every_writer_of_the_record_applies_it(self):
        src = LOOP.read_text()
        # the shared rewrite (stamp, keys, ledger, plan_paths) and the synthesizer
        body = src.split("_rewrite_json_keeping_mtime() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("completion_key_order", body)
        synth = src.split("_boundary_synthesize_completion() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("completion_key_order", synth)


if __name__ == "__main__":
    unittest.main()
