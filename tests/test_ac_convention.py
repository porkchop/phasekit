#!/usr/bin/env python3
"""The append-only acceptance-criteria convention is a cross-repo contract.

Source pattern: the long-running-harness doctrine (an agent must not quietly
weaken its own goalposts). Adopted 2026-08-21 via the field-scan steal-list;
the supervisor half (#187, spec-change detector keys on the marker) holds on
this convention being live fleet-wide, so the load-bearing property here is
that the MARKER LITERAL cannot drift between its three homes:

  contracts/interface.json  — the pin the supervisor reads
  docs/QUALITY_GATES.md     — the rule the session follows
  CONTINUE_PROMPT.txt       — the reminder at every session start

Run from the repo root: `python3 -m unittest tests.test_ac_convention`
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
GATES = REPO_ROOT / "docs" / "QUALITY_GATES.md"
PROMPT = REPO_ROOT / "CONTINUE_PROMPT.txt"

CONVENTION = "spec-acceptance-criteria-append-only"


def _entry():
    data = json.loads(MANIFEST.read_text())
    matches = [c for c in data.get("conventions", []) if c["name"] == CONVENTION]
    assert len(matches) == 1, f"exactly one {CONVENTION} entry expected"
    return matches[0]


class MarkerContract(unittest.TestCase):
    def test_the_pin_exists_with_both_consumers(self):
        entry = _entry()
        self.assertEqual(set(entry["consumers"]), {"session", "supervisor"})
        self.assertTrue(entry["marker"])

    def test_the_marker_literal_is_identical_in_all_three_homes(self):
        marker = _entry()["marker"]
        # The docs state the full spelled form `SUPERSEDED by AC#<n>: `; the
        # pin carries the fixed prefix the detector greps. The prefix must
        # appear VERBATIM in both human-facing homes — paraphrase is drift.
        for path in (GATES, PROMPT):
            self.assertIn(marker, path.read_text(), path)

    def test_the_docs_state_the_full_form_with_placeholder_and_separator(self):
        # `SUPERSEDED by AC#<n>: ` — the number placeholder and the
        # colon-space separator are part of the contract, or two repos will
        # implement two different lines.
        full = "SUPERSEDED by AC#<n>: "
        for path in (GATES, PROMPT):
            self.assertIn(full, path.read_text(), path)

    def test_the_rule_names_all_three_motions(self):
        gates = GATES.read_text()
        section = gates[gates.index("## Spec integrity gate"):]
        section = section[:section.index("\n## ", 1)]
        flat = " ".join(section.split()).lower()
        for phrase in ("append-only", "renumber", "change-request",
                       "full replacement text", "permanent identifiers"):
            self.assertIn(phrase, flat, phrase)

    def test_declared_in_lists_real_files(self):
        for rel in _entry()["declared_in"]:
            self.assertTrue((REPO_ROOT / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
