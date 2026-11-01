#!/usr/bin/env python3
"""The landing records a UI phase's baselines and design review (v0.24.0, queue row 2378).

phasekit v0.23.0 enforced a phase's `Visual changes:` declaration only when
`phasekit ui-check --update` ran, so a baseline replaced by hand was caught
only by review; and nothing noticed a UI-touching phase that landed without
the design-reviewer's record. Both are now checked at every approval-class
landing of a ui-check project (docs/project/ui-pages.json present), warn and
record only — never a refusal (the landing seam is sensitive; refusing is
Aaron's call, recorded as an open question in the release):

  1. changed or deleted paths under tests/ui-baselines/ must be within the
     phase's declaration (`id` covers its viewports; `id@viewport` one view;
     an invalid declaration covers nothing; a NEW baseline needs none);
  2. a phase whose changed paths match `ui_paths`, with
     docs/project/DESIGN_SYSTEM.md present and no
     artifacts/design-review/<iteration>-<phase>.json, prints one ADVISORY
     line.

Recorded as `ui_landing` in the landed record, the phase-close evidence file
and boundary-state.json. A project without ui-pages.json gets no key at all.

The shipped loop runs whole in a scratch repo with a stub model (the
boundary-state harness). Run from the repo root:
python3 -m unittest tests.test_ui_landing
"""

import importlib.util
import json
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
Repo = H.Repo

UI_PAGES = {"schema": 1, "root": ".", "ui_paths": ["index.html", "src/**"], "fonts": [],
            "pages": [{"id": "start", "path": "/", "viewports": ["phone", "desktop"]},
                      {"id": "stats", "path": "/stats", "viewports": ["phone"]}]}

APPROVE = r"""
cat > artifacts/phase-approval.json <<'JSON'
{"phase": "phase-1", "approved": true, "summary": "restyled",
 "suggested_commit_message": "Phase 1 (APPROVED): restyled"}
JSON
"""


class UiLanding(unittest.TestCase):
    def _repo(self, visual="start", ui=True, design_system=True):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("artifacts/iteration-mode.json", '{"mode": "standard", "iteration": 7}\n')
        repo.write("docs/PHASES.md", "# Phases\n\n## Phase 1 — Restyle the start page\n"
                   + (f"Visual changes: {visual}\n" if visual is not None else ""))
        if ui:
            repo.write("docs/project/ui-pages.json", json.dumps(UI_PAGES) + "\n")
        if design_system:
            repo.write("docs/project/DESIGN_SYSTEM.md", "# Design system\n")
        for name in ("start@phone", "start@desktop", "stats@phone"):
            repo.write(f"tests/ui-baselines/{name}.png", f"png {name}\n")
        repo.write("src/app.js", "v1\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "a ui-check project")
        return repo

    def _land(self, repo, body):
        repo.scenario(body + APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        return out, json.loads(repo.git("show", "HEAD:artifacts/phase-approval.json"))

    def test_a_baseline_changed_outside_the_declaration_is_named_and_recorded_not_refused(self):
        repo = self._repo(visual="start")
        out, rec = self._land(repo, "echo new >> tests/ui-baselines/start@phone.png\n"
                                    "echo new >> tests/ui-baselines/stats@phone.png\n")
        b = rec["ui_landing"]["baselines"]
        self.assertEqual(b["status"], "outside", out)
        self.assertEqual(b["undeclared"], ["tests/ui-baselines/stats@phone.png"])
        self.assertEqual(b["undeclared_count"], 1)
        self.assertEqual(b["pages"], ["start"])
        self.assertIn("OUTSIDE its `Visual changes:` declaration", out)
        self.assertEqual(repo.record()["ui_landing"], rec["ui_landing"])
        evidence = json.loads(repo.git("show", "HEAD:artifacts/iterations/7/1.json"))
        self.assertEqual(evidence["ui_landing"], rec["ui_landing"])

    def test_a_deleted_baseline_is_judged_and_a_new_one_needs_no_declaration(self):
        repo = self._repo(visual="stats@phone")
        out, rec = self._land(repo, "rm tests/ui-baselines/stats@phone.png\n"
                                    "echo first > tests/ui-baselines/newpage@phone.png\n")
        b = rec["ui_landing"]["baselines"]
        self.assertEqual(b["status"], "inside", out)
        self.assertEqual(b["changed"], [{"path": "tests/ui-baselines/stats@phone.png", "status": "D"}])
        self.assertIn("all within its `Visual changes:` declaration", out)

    def test_an_id_covers_its_viewports_and_an_id_at_viewport_only_that_one(self):
        repo = self._repo(visual="start@phone")
        _, rec = self._land(repo, "echo x >> tests/ui-baselines/start@phone.png\n"
                                  "echo x >> tests/ui-baselines/start@desktop.png\n")
        self.assertEqual(rec["ui_landing"]["baselines"]["undeclared"],
                         ["tests/ui-baselines/start@desktop.png"])

    def test_an_invalid_or_absent_declaration_covers_nothing(self):
        for visual in ("start and the header", None):
            with self.subTest(visual=visual):
                repo = self._repo(visual=visual)
                _, rec = self._land(repo, "echo x >> tests/ui-baselines/start@phone.png\n")
                b = rec["ui_landing"]["baselines"]
                self.assertEqual(b["status"], "outside")
                self.assertEqual(b["undeclared"], ["tests/ui-baselines/start@phone.png"])
                self.assertEqual(b["declared"], visual is not None)
                if visual:
                    # words that are valid ids but no configured page: ui-check
                    # refuses the whole declaration, so it covers nothing here
                    self.assertEqual(b["unknown"], ["and", "the", "header"])

    def test_a_viewport_the_page_does_not_list_refuses_the_declaration(self):
        repo = self._repo(visual="start, stats@desktop")
        _, rec = self._land(repo, "echo x >> tests/ui-baselines/start@phone.png\n")
        b = rec["ui_landing"]["baselines"]
        self.assertEqual(b["unknown"], ["stats@desktop"])
        self.assertEqual(b["status"], "outside")

    def test_no_baseline_touched_is_none(self):
        repo = self._repo()
        _, rec = self._land(repo, "echo x > README.md\n")
        self.assertEqual(rec["ui_landing"]["baselines"]["status"], "none")
        self.assertEqual(rec["ui_landing"]["design_review"]["status"], "not-applicable")

    def test_a_ui_phase_without_a_design_review_gets_one_advisory_line(self):
        repo = self._repo()
        out, rec = self._land(repo, "echo v2 > src/app.js\n")
        d = rec["ui_landing"]["design_review"]
        self.assertEqual(d["status"], "missing", out)
        self.assertEqual(d["record"], "artifacts/design-review/7-1.json")
        self.assertEqual(d["ui_changed"], ["src/app.js"])
        self.assertEqual(out.count("ADVISORY design-review: phase 1 changed 1 UI path(s)"), 1, out)
        self.assertEqual(repo.record()["ui_landing"]["design_review"]["status"], "missing")

    def test_a_design_review_record_satisfies_it(self):
        repo = self._repo()
        out, rec = self._land(repo, "echo v2 > src/app.js\nmkdir -p artifacts/design-review\n"
                                    "echo '{\"schema\": 1, \"verdict\": \"approve\"}' > artifacts/design-review/7-1.json\n")
        d = rec["ui_landing"]["design_review"]
        self.assertEqual(d["status"], "present", out)
        self.assertEqual(d["record"], "artifacts/design-review/7-1.json")
        self.assertNotIn("ADVISORY design-review", out)

    def test_a_spelled_out_record_name_counts_and_another_phases_does_not(self):
        for name, status in (("iteration-7-phase-1.json", "present"), ("7-2.json", "missing")):
            with self.subTest(name=name):
                repo = self._repo()
                _, rec = self._land(repo, "echo v2 > src/app.js\nmkdir -p artifacts/design-review\n"
                                          f"echo '{{}}' > artifacts/design-review/{name}\n")
                self.assertEqual(rec["ui_landing"]["design_review"]["status"], status)

    def test_without_a_design_system_there_is_nothing_to_review(self):
        repo = self._repo(design_system=False)
        out, rec = self._land(repo, "echo v2 > src/app.js\n")
        self.assertEqual(rec["ui_landing"]["design_review"]["status"], "not-applicable")
        self.assertNotIn("ADVISORY design-review", out)

    def test_a_project_without_ui_check_gets_no_key_at_all(self):
        repo = self._repo(ui=False)
        out, rec = self._land(repo, "echo x >> tests/ui-baselines/start@phone.png\necho v2 > src/app.js\n")
        self.assertNotIn("ui_landing", rec)
        self.assertNotIn("ui_landing", repo.record() or {})
        self.assertNotIn("ui_landing", json.loads(repo.git("show", "HEAD:artifacts/iterations/7/1.json")))
        self.assertNotIn("ADVISORY design-review", out)


if __name__ == "__main__":
    unittest.main()
