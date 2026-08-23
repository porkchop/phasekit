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

    def test_the_key_rule_names_default_explicit_and_dedupe(self):
        # v0.12.1 (review finding): without a stable key, the supervisor's
        # (source, source_key) dedupe has only item-text hashing — one
        # rewording refiles the task. Default = the AC number (composes with
        # v0.11.0's permanent AC identifiers); explicit kebab-case otherwise.
        flat = " ".join(_gate_section("Deferred-scope gate").split()).lower()
        for phrase in ('"key"', "kebab-case", "same deferral",
                       "must name the ac number"):
            self.assertIn(phrase, flat, phrase)
        semantics = _entry()["semantics"]
        for phrase in ("'key'", "kebab-case", "never refiled"):
            self.assertIn(phrase, semantics, phrase)

    def test_deferral_filed_tasks_land_held_never_preapproved(self):
        # v0.12.1 (review finding): a session's own deferral entry must not be
        # self-granted approval for follow-up work — the autonomy seam.
        flat = " ".join(_gate_section("Deferred-scope gate").split()).lower()
        self.assertIn("never pre-approved", flat)
        self.assertIn("never pre-approved", _entry()["semantics"].lower())

    def test_the_gate_binds_every_mode(self):
        flat = " ".join(_gate_section("Deferred-scope gate").split()).lower()
        self.assertIn("regardless of execution mode", flat)

    def test_the_manifest_artifact_keys_agree_with_the_convention(self):
        # v0.12.1 (review finding): the conventions entry said verdict
        # artifacts may carry deferrals while the artifacts section's keys
        # lists didn't — a consumer building from the artifact entry (the
        # natural place to look) would miss the field entirely.
        data = json.loads(MANIFEST.read_text())
        arts = {a["name"]: a for a in data["artifacts"]}
        for name in ("phase-approval.json", "project-complete.json"):
            self.assertIn("deferrals", arts[name]["keys"], name)

    def test_the_doc_example_is_valid_json_and_carries_the_key(self):
        # The fenced block is the template sessions copy; a doc edit must not
        # ship a broken example. Extract the block containing "deferrals".
        gates = GATES.read_text()
        blocks = []
        rest = gates
        while "```json" in rest:
            start = rest.index("```json") + len("```json")
            end = rest.index("```", start)
            blocks.append(rest[start:end])
            rest = rest[end + 3:]
        with_deferrals = [b for b in blocks if '"deferrals"' in b]
        self.assertEqual(len(with_deferrals), 1)
        parsed = json.loads(with_deferrals[0])
        entry = parsed["deferrals"][0]
        self.assertEqual(set(entry),
                         {"item", "key", "reason", "suggested_task"})
        self.assertTrue(entry["item"].startswith(entry["key"]))


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

    def test_nonconvergence_is_a_declared_contract_not_a_stealth_one(self):
        # v0.12.1 (review finding): pinned as a doc literal only, this reason
        # string was a stealth cross-repo contract. Declared like the
        # SUPERSEDED marker was; the doc and manifest literals must agree.
        data = json.loads(MANIFEST.read_text())
        matches = [c for c in data["conventions"]
                   if c["name"] == "blocked-reason-review-nonconvergence"]
        self.assertEqual(len(matches), 1)
        entry = matches[0]
        self.assertEqual(entry["marker"], "review-nonconvergence")
        self.assertEqual(set(entry["consumers"]), {"session", "supervisor"})
        self.assertIn(f"`{entry['marker']}`",
                      _gate_section("Review-fix convergence gate"))

    def test_the_severity_vocabulary_is_anchored(self):
        # v0.12.1 (review finding): the stop verdict keys on "MAJOR" and the
        # grader is the session being graded — the words need definitions.
        flat = " ".join(_gate_section("Review-fix convergence gate").split()).lower()
        for phrase in ("**blocker**", "**major**", "**minor**",
                       "grade findings before writing the fix list"):
            self.assertIn(phrase, flat, phrase)


class ProgressRecordDiscipline(unittest.TestCase):
    def test_the_gate_states_budget_structure_and_prohibitions(self):
        flat = " ".join(_gate_section("Progress record discipline").split()).lower()
        for phrase in ("at most 80 lines", "file:symbol", "append-only",
                       "never rewrite past ones", "line numbers"):
            self.assertIn(phrase, flat, phrase)
        for heading in ("closed", "opened", "decisions", "admissions", "verify"):
            self.assertIn(f"**{heading}**", flat, heading)

    def test_local_phases_header_rules_win(self):
        # v0.12.1 (review finding): foundry-dashboard's PHASES.md header
        # declares its own citation rules, which the line-number prohibition
        # contradicted. Project-owned wins, same precedent as verify seeding.
        flat = " ".join(_gate_section("Progress record discipline").split()).lower()
        self.assertIn("local wins for that file", flat)

    def test_the_prompt_points_at_all_three_gates(self):
        prompt = PROMPT.read_text()
        for ref in ("Progress record discipline", "regression test",
                    "Deferred-scope gate"):
            self.assertIn(ref, prompt, ref)


if __name__ == "__main__":
    unittest.main()
