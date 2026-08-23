#!/usr/bin/env python3
"""The session-output-discipline family is a set of cross-repo contracts.

Adopted 2026-08-23 from the xmeo iteration-4 retrospective (foundry-meta
`reviews/REVIEW-2026-08-22-xmeo-iteration4-determinism.md`): a recorded
deferral must be machine-readable so a supervisor can schedule it (the
review found a deferral that lost its slot because it lived only in
prose), review rounds must ratchet (pin-per-fix, non-convergence is a
verdict), and PHASES progress records are bounded instruments.

Load-bearing properties pinned here, mirroring tests/test_ac_convention.py:

  contracts/interface.json  — the `approval-deferrals` pin the supervisor reads
  docs/QUALITY_GATES.md     — the three gates the session follows
  CONTINUE_PROMPT.txt       — the reminder at every session start

Run from the repo root: `python3 -m unittest tests.test_output_discipline`
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
GATES = REPO_ROOT / "docs" / "QUALITY_GATES.md"
PROMPT = REPO_ROOT / "CONTINUE_PROMPT.txt"

CONVENTION = "approval-deferrals"


def _entry():
    data = json.loads(MANIFEST.read_text())
    matches = [c for c in data.get("conventions", []) if c["name"] == CONVENTION]
    assert len(matches) == 1, f"exactly one {CONVENTION} entry expected"
    return matches[0]


def _gate_section(title):
    gates = GATES.read_text()
    start = gates.index(f"## {title}")
    section = gates[start:]
    end = section.index("\n## ", 1)
    return section[:end]


class DeferralsContract(unittest.TestCase):
    def test_the_pin_exists_with_both_consumers(self):
        entry = _entry()
        self.assertEqual(set(entry["consumers"]), {"session", "supervisor"})
        self.assertEqual(entry["marker"], "deferrals")

    def test_the_field_literal_appears_in_both_human_facing_homes(self):
        marker = f"`{_entry()['marker']}`"
        for path in (GATES, PROMPT):
            self.assertIn(marker, path.read_text(), path)

    def test_the_entry_shape_is_the_three_documented_keys(self):
        # {item, reason, suggested_task} — the supervisor's task-filing leg
        # codes against these names; a rename here must fail loudly.
        flat = " ".join(_gate_section("Deferred-scope gate").split())
        for key in ('"item"', '"reason"', '"suggested_task"'):
            self.assertIn(key, flat, key)
        self.assertIn("suggested_task", _entry()["semantics"])

    def test_the_gate_names_the_violation_and_the_absence_claim(self):
        flat = " ".join(_gate_section("Deferred-scope gate").split()).lower()
        for phrase in ("machine-readable", "prose-only", "verdict artifact",
                       "everything in scope shipped"):
            self.assertIn(phrase, flat, phrase)

    def test_declared_in_lists_real_files(self):
        for rel in _entry()["declared_in"]:
            self.assertTrue((REPO_ROOT / rel).is_file(), rel)


class ReviewFixConvergence(unittest.TestCase):
    def test_the_gate_states_all_three_motions(self):
        flat = " ".join(_gate_section("Review-fix convergence gate").split()).lower()
        for phrase in ("pin-per-fix", "regression test that fails without it",
                       "opt-in per iteration", "review-nonconvergence",
                       "phase-blocked.json"):
            self.assertIn(phrase, flat, phrase)

    def test_nonconvergence_reason_is_a_stable_literal(self):
        # A supervisor triaging phase-blocked.json keys on this string.
        self.assertIn("`review-nonconvergence`",
                      _gate_section("Review-fix convergence gate"))


class ProgressRecordDiscipline(unittest.TestCase):
    def test_the_gate_states_budget_structure_and_prohibitions(self):
        flat = " ".join(_gate_section("Progress record discipline").split()).lower()
        for phrase in ("at most 80 lines", "file:symbol", "append-only",
                       "never rewrite past ones", "line numbers"):
            self.assertIn(phrase, flat, phrase)
        for heading in ("closed", "opened", "decisions", "admissions", "verify"):
            self.assertIn(f"**{heading}**", flat, heading)

    def test_the_prompt_points_at_all_three_gates(self):
        prompt = PROMPT.read_text()
        for ref in ("Progress record discipline", "regression test",
                    "Deferred-scope gate"):
            self.assertIn(ref, prompt, ref)


if __name__ == "__main__":
    unittest.main()
