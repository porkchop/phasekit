#!/usr/bin/env python3
"""The project rules register and its checker (v0.26.0).

`docs/project/RULES.md` is a project's list of the rules its incidents taught
it, each with the one place it is implemented and the test that pins it
(xmeo-v3, 2026-11-07: a rule held as code in two places drifted when a new mode
updated one). `scripts/phasekit-rules.py` is the one checker of its format.

Pinned here:
  * the format: an entry per level-2 heading, three fields once each, unique
    ids; a Test that names a missing file, or a test name not in the file, is a
    finding; `no test yet` is not; fenced and commented examples are not
    entries; an unclosed fence is a finding;
  * `rules_touched`: absent, null and [] pass; an unknown id, a non-array, a
    non-string item are findings;
  * an absent or empty register passes (exit 0); findings exit 3; a register
    that cannot be read exits 5, never 0 and never 3; a named test file that
    cannot be resolved or read is a finding, not exit 5;
  * code spans follow CommonMark (``double`` runs; a name may hold a backtick);
  * the `rules` verbs act on the project root, not the working directory;
  * the seeded template is a valid, empty register, and `init` never
    overwrites; enrich seeds it once, project-owned, and upgrade never rewrites
    it;
  * `phasekit rules check` and `phasekit check` (exit 8 on findings in an
    otherwise clean check);
  * the doctrine and the agent prompts carry the two review checks.

Run from the repo root: python3 -m unittest tests.test_rules_register
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-rules.py"
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
TEMPLATE = REPO_ROOT / "templates" / "project-rules.template.md"
REG = "docs/project/RULES.md"

ENTRY = """## RULE-1 — The ring and the pieces come from one board
- Incident: 2026-11-07, multiplayer halt drew the ring off the pieces
- Implemented in: `src/board.ts` (`placementBoard`)
- Test: `tests/board.test.ts` `ring and pieces share one board`
"""


def _load_enrich():
    spec = importlib.util.spec_from_file_location("enrich_rules_register_test", ENRICH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Tree:
    def __init__(self, testcase):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "demo"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "tests" / "board.test.ts").write_text(
            "it('ring and pieces share one board', () => {})\n")

    def register(self, text):
        p = self.root / REG
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def record(self, name, data):
        p = self.root / "artifacts" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data) + "\n")
        return p

    def check(self, *extra):
        return subprocess.run([sys.executable, str(TOOL), "check", "--repo", str(self.root),
                               *extra], capture_output=True, text=True, cwd=self.root)


class Format(unittest.TestCase):
    def setUp(self):
        self.t = _Tree(self)

    def _findings(self, text, *extra):
        self.t.register(text)
        r = self.t.check(*extra)
        return r, [x for x in r.stdout.splitlines() if x]

    def test_absent_register_passes(self):
        r = self.t.check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to check", r.stdout)

    def test_the_seeded_template_is_a_valid_empty_register(self):
        r, _ = self._findings(TEMPLATE.read_text().replace("{{PROJECT_NAME}}", "demo"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.t.check("--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["entries"], [], "the template's example is fenced, not an entry")
        self.assertFalse(data["absent"])

    def test_a_valid_entry_passes_and_is_reported(self):
        r, _ = self._findings("# demo — project rules\n\n" + ENTRY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(self.t.check("--json").stdout)
        self.assertEqual([(e["id"], e["line"]) for e in data["entries"]], [("RULE-1", 3)])

    def test_no_test_yet_passes(self):
        for marker in ("no test yet", "No test yet.", "`no test yet`"):
            with self.subTest(marker=marker):
                r, _ = self._findings(ENTRY.replace(
                    "`tests/board.test.ts` `ring and pieces share one board`", marker))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_test_file_that_does_not_exist_is_a_finding(self):
        r, f = self._findings(ENTRY.replace("tests/board.test.ts", "tests/gone.test.ts"))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(f, [f"{REG} line 4: RULE-1: test file `tests/gone.test.ts` does not exist"])

    def test_a_test_name_not_in_the_file_is_a_finding(self):
        r, f = self._findings(ENTRY.replace("ring and pieces share one board", "renamed away"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("RULE-1: test `renamed away` is not in `tests/board.test.ts`", f[0])

    def test_a_name_in_double_backticks_is_still_checked(self):
        r, f = self._findings(ENTRY.replace(
            "`tests/board.test.ts` `ring and pieces share one board`",
            "``tests/board.test.ts`` `` renamed away ``"))
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("RULE-1: test `renamed away` is not in `tests/board.test.ts`", f[0])

    def test_a_name_can_carry_a_backtick(self):
        (self.t.root / "tests" / "board.test.ts").write_text("it('reads `x`', () => {})\n")
        r, _ = self._findings(ENTRY.replace(
            "`ring and pieces share one board`", "``it('reads `x`'``"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_unresolvable_test_path_is_a_finding_and_keeps_the_others(self):
        """A symlink loop (RuntimeError on Python < 3.13) or a NUL byte (ValueError)
        once became exit 5, which the loop WARNs past, dropping every finding."""
        (self.t.root / "tests" / "loop.ts").symlink_to("loop.ts")
        dup = "## RULE-1 — again\n"
        for path in ("tests/loop.ts", "tests/a\x00b.ts"):
            with self.subTest(path=path):
                r, f = self._findings(ENTRY.replace(
                    "`tests/board.test.ts` `ring and pieces share one board`", f"`{path}`") + dup)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertTrue(any("duplicate id" in x for x in f), f)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_named_test_file_is_a_finding(self):
        target = self.t.root / "tests" / "board.test.ts"
        target.chmod(0)
        self.addCleanup(target.chmod, 0o644)
        r, f = self._findings(ENTRY)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("cannot be read", f[0])

    def test_a_file_without_a_name_passes(self):
        r, _ = self._findings(ENTRY.replace(" `ring and pieces share one board`", ""))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_test_path_outside_the_repository_is_a_finding(self):
        for path in ("../outside.ts", "/etc/hostname"):
            with self.subTest(path=path):
                r, f = self._findings(ENTRY.replace("tests/board.test.ts", path))
                self.assertEqual(r.returncode, 3)
                self.assertIn("must be a path from the repository root", f[0])

    def test_a_test_directory_is_a_finding(self):
        r, f = self._findings(ENTRY.replace("`tests/board.test.ts` `ring and pieces share one board`",
                                            "`tests`"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("is not a regular file", f[0])

    def test_a_bare_test_value_is_a_finding(self):
        r, f = self._findings(ENTRY.replace(
            "`tests/board.test.ts` `ring and pieces share one board`", "the board tests"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("must be a test file in backticks", f[0])

    def test_missing_empty_and_repeated_fields_are_findings(self):
        text = ("## RULE-1 — One\n- Incident: x\n- Test: no test yet\n- test: again\n\n"
                "## RULE-2 — Two\n- Incident:\n- Implemented in: y\n- Test: no test yet\n")
        r, f = self._findings(text)
        self.assertEqual(r.returncode, 3)
        joined = "\n".join(f)
        self.assertIn("line 1: RULE-1: missing `- Implemented in:`", joined)
        self.assertIn("line 4: RULE-1: `test:` appears twice (first on line 3)", joined)
        self.assertIn("line 7: RULE-2: `Incident:` is empty", joined)

    def test_a_duplicate_id_is_a_finding(self):
        r, f = self._findings(ENTRY + "\n" + ENTRY.replace("come from one board", "share a board"))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(f, [f"{REG} line 6: RULE-1: duplicate id (first on line 1); ids are unique "
                             "and never reused — give this rule a new id"])

    def test_a_level_two_heading_that_is_not_an_entry_is_a_finding(self):
        for title in ("Notes", "rule-1 — lower case id", "RULE-1 no separator", "1RULE — digit first"):
            with self.subTest(title=title):
                r, f = self._findings(f"## {title}\n")
                self.assertEqual(r.returncode, 3)
                self.assertIn("a level-2 heading that is not an entry", f[0])

    def test_separators_and_bullets_the_format_allows(self):
        text = ENTRY.replace(" — ", " – ").replace("- Incident", "* Incident").replace(
            "- Test", "   + Test")
        r, _ = self._findings(text)
        self.assertEqual(r.returncode, 0, r.stdout)
        r, _ = self._findings(ENTRY.replace(" — ", " - ").replace("RULE-1", "PLACEMENT-2"))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_fenced_and_commented_examples_are_not_entries(self):
        text = ("# t\n\n```markdown\n## Not an entry\n```\n\n<!--\n## Also not\n-->\n"
                "<!-- ## nor this -->\n\n" + ENTRY)
        r, _ = self._findings(text)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(json.loads(self.t.check("--json").stdout)["entries"]), 1)

    def test_an_unclosed_fence_or_comment_is_a_finding(self):
        for opener, word in (("```", "fenced block"), ("<!--", "HTML comment")):
            with self.subTest(opener=opener):
                r, f = self._findings(f"# t\n{opener}\n" + ENTRY)
                self.assertEqual(r.returncode, 3)
                self.assertIn(f"line 2: an {word}" if word.startswith("HTML") else
                              f"line 2: a {word}", f[0])

    def test_a_level_one_heading_ends_the_entry(self):
        r, f = self._findings("## RULE-1 — One\n- Incident: x\n# Elsewhere\n"
                              "- Implemented in: y\n- Test: no test yet\n")
        self.assertEqual(r.returncode, 3)
        self.assertIn("RULE-1: missing `- Implemented in:`, `- Test:`", f[0])

    def test_a_bom_and_crlf_are_tolerated(self):
        r, _ = self._findings("﻿" + ENTRY.replace("\n", "\r\n"))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_an_unreadable_register_exits_5_not_0_or_3(self):
        p = self.t.root / REG
        p.parent.mkdir(parents=True)
        p.write_bytes(b"\xff\xfe not utf-8\n")
        r = self.t.check()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("is not UTF-8", r.stderr)
        p.unlink()
        p.mkdir()
        self.assertEqual(self.t.check().returncode, 5)

    def test_a_repo_that_is_not_a_directory_exits_5(self):
        r = subprocess.run([sys.executable, str(TOOL), "check", "--repo", str(TOOL)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 5)

    def test_usage_errors_exit_2(self):
        r = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class RulesTouched(unittest.TestCase):
    def setUp(self):
        self.t = _Tree(self)
        self.t.register(ENTRY)

    def _check(self, data):
        p = self.t.record("phase-approval.json", data)
        r = self.t.check("--record", str(p))
        return r, [x for x in r.stdout.splitlines() if x]

    def test_absent_null_empty_and_known_pass(self):
        for data in ({"phase": "1"}, {"rules_touched": None}, {"rules_touched": []},
                     {"rules_touched": ["RULE-1", " RULE-1 "]}):
            with self.subTest(data=data):
                r, _ = self._check(data)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_unknown_id_is_a_finding(self):
        r, f = self._check({"rules_touched": ["RULE-1", "RULE-9"]})
        self.assertEqual(r.returncode, 3)
        self.assertEqual(f, ["artifacts/phase-approval.json: rules_touched names RULE-9, but "
                             f"{REG} has no entry `## RULE-9 — …`"])

    def test_a_non_array_and_bad_items_are_findings(self):
        r, f = self._check({"rules_touched": "RULE-1"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("must be an array of register ids", f[0])
        r, f = self._check({"rules_touched": [3, ""]})
        self.assertEqual(len(f), 2)
        self.assertIn("rules_touched[0] is not a register id", f[0])

    def test_without_a_register_every_id_is_unknown(self):
        (self.t.root / REG).unlink()
        r, f = self._check({"rules_touched": ["RULE-1"]})
        self.assertEqual(r.returncode, 3)
        self.assertIn(f"there is no {REG}", f[0])

    def test_an_entry_with_other_findings_still_has_its_id(self):
        self.t.register(ENTRY.replace("tests/board.test.ts", "tests/gone.ts"))
        r, f = self._check({"rules_touched": ["RULE-1"]})
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len(f), 1, f)
        self.assertIn("does not exist", f[0])

    def test_a_record_that_is_not_json_exits_5(self):
        p = self.t.root / "artifacts" / "phase-approval.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        r = self.t.check("--record", str(p))
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)


class Seeding(unittest.TestCase):
    def test_init_writes_the_template_once_and_never_overwrites(self):
        t = _Tree(self)
        r = subprocess.run([sys.executable, str(TOOL), "init", "--repo", str(t.root)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (t.root / REG).read_text()
        self.assertTrue(text.startswith("# demo — project rules"), text[:60])
        self.assertNotIn("{{", text)
        (t.root / REG).write_text("# mine\n")
        r = subprocess.run([sys.executable, str(TOOL), "init", "--repo", str(t.root)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        self.assertEqual((t.root / REG).read_text(), "# mine\n")

    def test_enrich_registers_it_project_owned_and_ships_the_checker(self):
        e = _load_enrich()
        self.assertEqual(e.DOC_TEMPLATE_MAP["PROJECT_RULES"], "templates/project-rules.template.md")
        self.assertIn("scripts/phasekit-rules.py", e.ALWAYS_INSTALLED_FILE_PATHS)
        manifest = e.load_manifest()
        self.assertEqual(manifest["docs"]["PROJECT_RULES"],
                         {"path": REG, "required": False, "ownership": "bootstrap-frozen"})
        self.assertIn("PROJECT_RULES", manifest["profiles"]["default"]["include_docs"])


def _enrich(*args):
    env = dict(os.environ, PHASEKIT_UPGRADE_VERIFY="off")
    return subprocess.run([sys.executable, str(ENRICH), *args], capture_output=True, text=True,
                          env=env)


class InAProject(unittest.TestCase):
    """A bootstrapped (vendored) project: seeded once, never rewritten, and
    `phasekit check` / `phasekit rules check` judge it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        r = _enrich(str(self.root), "--profile", "default")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _write_register(self, text):
        """The project edits its register; the manifest records the bytes as
        its own (a vendored `--check` reports an edited bootstrap-frozen file
        as DRIFT — pre-existing, and not what these tests are about)."""
        e = _load_enrich()
        path = self.root / REG
        mpath = self.root / ".scaffold" / "manifest.json"
        m = json.loads(mpath.read_text())
        if text is None:
            path.unlink()
            m["files"] = [x for x in m["files"] if x["path"] != REG]
        else:
            path.write_text(text)
            norm, strict = e.compute_file_shas(path, True)
            for x in m["files"]:
                if x["path"] == REG:
                    x.update({"sha256": norm, "sha256_strict": strict})
        mpath.write_text(json.dumps(m, indent=2) + "\n")

    def _cli(self, *args):
        env = dict(os.environ, PHASEKIT_UPGRADE_VERIFY="off", PHASEKIT_NO_UPDATE_CHECK="1")
        return subprocess.run(["bash", str(CLI), *args], cwd=self.root, capture_output=True,
                              text=True, env=env)

    def test_bootstrap_seeds_the_register_project_owned(self):
        text = (self.root / REG).read_text()
        self.assertIn("Project-owned rules register", text)
        self.assertNotIn("{{", text)
        m = json.loads((self.root / ".scaffold" / "manifest.json").read_text())
        entry = next(e for e in m["files"] if e["path"] == REG)
        self.assertEqual(entry["ownership"], "bootstrap-frozen")
        self.assertTrue((self.root / "scripts" / "phasekit-rules.py").is_file())

    def test_upgrade_never_rewrites_it(self):
        mine = "# demo — project rules\n\n" + ENTRY.replace(
            "`tests/board.test.ts` `ring and pieces share one board`", "no test yet")
        (self.root / REG).write_text(mine)
        r = _enrich("--upgrade", str(self.root), "--yes", "--no-commit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.root / REG).read_text(), mine)

    def test_phasekit_check_exits_8_on_findings_and_0_when_honest(self):
        r = self._cli("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._write_register(ENTRY)   # names tests/board.test.ts, which is not here
        r = self._cli("check")
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertIn("RULE-1: test file `tests/board.test.ts` does not exist", r.stdout)
        self.assertIn("RULES: docs/project/RULES.md has findings", r.stderr)
        r = self._cli("rules", "check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "board.test.ts").write_text("ring and pieces share one board\n")
        self.assertEqual(self._cli("check").returncode, 0)
        self.assertEqual(self._cli("rules", "check").returncode, 0)

    def test_rules_verbs_act_on_the_project_root_from_a_subdirectory(self):
        self._write_register(ENTRY)
        sub = self.root / "sub"
        sub.mkdir()
        env = dict(os.environ, PHASEKIT_UPGRADE_VERIFY="off", PHASEKIT_NO_UPDATE_CHECK="1")
        r = subprocess.run(["bash", str(CLI), "rules", "check"], cwd=sub, capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        r = subprocess.run(["bash", str(CLI), "rules", "init"], cwd=sub, capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertFalse((sub / REG).exists())

    def test_drift_wins_over_findings_and_both_are_printed(self):
        self._write_register(ENTRY)
        (self.root / "docs" / "QUALITY_GATES.md").write_text("# edited\n")
        r = self._cli("check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("does not exist", r.stdout)

    def test_no_register_is_clean(self):
        self._write_register(None)
        r = self._cli("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("RULES", r.stdout + r.stderr)


class SessionsAreTold(unittest.TestCase):
    """The register and the two review checks reach every surface that plans,
    builds or reviews."""

    def _read(self, rel):
        return (REPO_ROOT / rel).read_text()

    def test_code_reviewer_answers_both_checks_and_a_bare_no_is_major(self):
        text = self._read(".claude/agents/code-reviewer.md")
        self.assertIn("## Mandatory checks (answer both in every review)", text)
        self.assertIn("already exist elsewhere in", text)
        self.assertIn("docs/project/RULES.md", text)
        self.assertIn("A NO without a reason", text)
        self.assertIn("MAJOR", text)

    def test_the_fresh_context_round_and_the_light_review_ask_them_too(self):
        gates = self._read("docs/QUALITY_GATES.md")
        conv = gates[gates.index("## Review-fix convergence gate"):]
        conv = conv[:conv.index("\n## ", 5)]
        self.assertIn("Every round answers the reuse and rules checks", conv)
        loop = self._read("scripts/run-until-done.sh")
        review = loop[loop.index("You are the FINAL REVIEWER"):loop.index("REVIEW_EOF\n  echo")]
        self.assertIn("already exist elsewhere in the repo", review)
        self.assertIn("docs/project/RULES.md", review)

    def test_planners_read_the_register_and_records_carry_rules_touched(self):
        for rel in (".claude/agents/strategy-planner.md", ".claude/agents/project-lead.md",
                    "CONTINUE_PROMPT.txt"):
            self.assertIn("docs/project/RULES.md", self._read(rel), rel)
        self.assertIn("`rules_touched`", self._read(".claude/agents/project-lead.md"))
        for rel in ("engine-builder", "frontend-builder", "backend-builder"):
            self.assertIn("rules touched", self._read(f".claude/agents/{rel}.md"), rel)

    def test_the_plugin_serves_the_same_agents(self):
        self.assertEqual((REPO_ROOT / "plugin" / "agents").resolve(),
                         (REPO_ROOT / ".claude" / "agents").resolve())

    def test_the_doctrine_states_the_register_and_the_hygiene_rules(self):
        gates = self._read("docs/QUALITY_GATES.md")
        self.assertIn("## Project rules register (v0.26.0)", gates)
        self.assertIn("### Test-suite hygiene (v0.26.0)", gates)
        self.assertIn("drive time-dependent code with a controllable clock", gates.lower())
        self.assertIn("a supervisor turns an over-budget tier into a delegated work request", gates)

    def test_the_contract_pins_the_format_the_key_and_the_codes(self):
        m = json.loads(self._read("contracts/interface.json"))
        conv = next(c for c in m["conventions"] if c["name"] == "project-rules")
        self.assertEqual(conv["marker"], REG)
        for rel in conv["declared_in"]:
            with self.subTest(file=rel):
                self.assertIn(REG, self._read(rel))
        art = {a["name"]: a for a in m["artifacts"]}
        for name in ("phase-approval.json", "project-complete.json"):
            self.assertIn("rules_touched", art[name]["keys"], name)
        self.assertEqual(set(m["exit_codes"]["scripts/phasekit-rules.py"]["codes"]),
                         {"0", "2", "3", "4", "5"})
        self.assertIn("8", m["exit_codes"]["scripts/phasekit.sh"]["codes"])


if __name__ == "__main__":
    unittest.main()
