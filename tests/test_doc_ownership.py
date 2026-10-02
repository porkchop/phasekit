#!/usr/bin/env python3
"""v0.17.0 (row 1140): which docs a project may edit.

Decided 2026-09-27 instead of merging amendments on upgrade (an amendment
often rewrites an inherited rule in place; a mechanical merge can leave two
contradicting rules):

1. docs/CONVENTIONS.md is PROJECT-OWNED (bootstrap-with-template-tracking):
   seeded once, never overwritten; a template change is advisory only
   (`--check --include-templates`). Existing projects migrate on upgrade —
   an unedited scaffold-era copy takes the release's text once, an edited or
   kept one is kept, a standing `local: kept` is cleared with a note.
2. Scaffold process docs stay scaffold-owned and name a project-owned
   COMPANION (docs/project/QUALITY_GATES.md): seeded once, never rewritten,
   adopted (not refused) when the project already has one.
3. The template base a project-owned file records is CARRIED across upgrades,
   so the advisory survives the next upgrade until the project acknowledges
   it (`--keep-local PATH`) or takes the template (`--take-new PATH`, clean
   or edited).
4. A re-profile: an unedited CONVENTIONS.md takes the new stack's template,
   an edited one is kept and reported; leaving a stack never lets a plain
   `--uninstall` delete it.

Run from the repo root: `python3 -m unittest tests.test_doc_ownership`
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
SCRIPT_PATH = REPO_ROOT / "scripts" / "enrich-project.py"
CONV = "docs/CONVENTIONS.md"
COMPANION = "docs/project/QUALITY_GATES.md"
PROJECT_OWNED = "bootstrap-with-template-tracking"


def _load_module():
    spec = importlib.util.spec_from_file_location("enrich_project_ownership_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_module()


def _env():
    env = dict(os.environ)
    # These tests are about ownership, not the gate: never reach for docker.
    env["PHASEKIT_UPGRADE_VERIFY"] = "off"
    return env


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT_PATH), *args],
                          capture_output=True, text=True, env=_env())


class _Project:
    """A game-canvas project, optionally rewound to its pre-v0.17.0 shape."""

    def __init__(self, testcase, profile="game-canvas"):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        r = _run(str(self.root), "--profile", profile)
        testcase.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.t = testcase

    # --- manifest helpers --------------------------------------------------
    @property
    def manifest_path(self):
        return self.root / ".scaffold" / "manifest.json"

    def manifest(self):
        return json.loads(self.manifest_path.read_text())

    def entry(self, path):
        return next((e for e in self.manifest()["files"] if e["path"] == path), None)

    def edit_manifest(self, fn):
        m = self.manifest()
        fn(m)
        self.manifest_path.write_text(json.dumps(m, indent=2) + "\n")

    def as_pre_v017(self, conventions_text=None, kept=False, companion=False):
        """Rewind to what a v0.16.x project looks like: CONVENTIONS.md
        scaffold-class (sha = its bytes, no template fields), optionally a
        standing keep-local; no companion unless asked."""
        conv = self.root / CONV
        if conventions_text is not None:
            conv.write_text(conventions_text)
        norm, strict = M.compute_file_shas(conv, True)
        if not companion:
            (self.root / COMPANION).unlink()

        def fn(m):
            files = []
            for e in m["files"]:
                if e["path"] == COMPANION and not companion:
                    continue
                if e["path"] == CONV:
                    e = {k: v for k, v in e.items()
                         if k not in ("rendered_from", "template_sha", "local")}
                    e.update({"ownership": "scaffold", "sha256": norm, "sha256_strict": strict})
                    if kept:
                        e["local"] = "kept"
                files.append(e)
            m["files"] = files
        self.edit_manifest(fn)

    def upgrade(self, *extra):
        return _run("--upgrade", str(self.root), "--yes", "--no-commit", *extra)

    def check(self, *extra):
        return _run("--check", str(self.root), *extra)

    def template_text(self, stack="game-canvas"):
        return (REPO_ROOT / M.STACK_CONVENTIONS_TEMPLATES[stack]).read_text()


class ConventionsIsProjectOwned(unittest.TestCase):
    def test_fresh_install_records_project_owned_class(self):
        p = _Project(self)
        e = p.entry(CONV)
        self.assertEqual(e["ownership"], PROJECT_OWNED)
        self.assertEqual(e["rendered_from"], "templates/conventions.game-canvas.md")
        self.assertEqual(e["template_sha"], M.sha256_strict(REPO_ROOT / e["rendered_from"]))

    def test_edited_conventions_survives_upgrade_and_drift_is_advisory_only(self):
        # The acceptance case: a project amended its conventions before v0.17.0
        # (drifted from its scaffold-class baseline, no flag at all).
        p = _Project(self)
        p.as_pre_v017()
        amended = p.template_text() + "\n## Project amendment\n\nOur own rule.\n"
        (p.root / CONV).write_text(amended)

        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("refuse", r.stdout.split("--upgrade plan:")[1].split("\n")[0])
        self.assertEqual((p.root / CONV).read_text(), amended)
        e = p.entry(CONV)
        self.assertEqual(e["ownership"], PROJECT_OWNED)
        self.assertNotIn("local", e)

        # Plain check: clean (the project owns the bytes it has).
        r = p.check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # --include-templates: the template differs from what the project has,
        # reported as ADVISORY — and still nothing overwritten.
        r = p.check("--include-templates")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(f"TEMPLATE DRIFT (advisory; never auto-overwritten): {CONV}", r.stdout)
        self.assertEqual((p.root / CONV).read_text(), amended)

    def test_advisory_survives_the_next_upgrade_until_acknowledged(self):
        p = _Project(self)
        p.as_pre_v017()
        amended = p.template_text() + "\nAmended.\n"
        (p.root / CONV).write_text(amended)
        self.assertEqual(p.upgrade().returncode, 0)
        # A second (e.g. weekly maintenance) upgrade must not re-stamp the
        # template base and so silence the advisory.
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(p.check("--include-templates").returncode, 3)
        # The project acknowledges: keep mine, I have seen the template.
        r = p.upgrade("--keep-local", CONV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = p.check("--include-templates")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), amended)
        # ...and the acknowledgement records no standing keep (moot here).
        self.assertNotIn("local", p.entry(CONV))

    def test_interactive_keep_acknowledges_the_template(self):
        # Round-2 MINOR 1: answering `k` on a project-owned drifted file is
        # the same decision as --keep-local PATH, even though keep was already
        # its default action.
        p = _Project(self)
        p.as_pre_v017()
        (p.root / CONV).write_text(p.template_text() + "\nOurs.\n")
        self.assertEqual(p.upgrade().returncode, 0)
        self.assertEqual(p.check("--include-templates").returncode, 3)
        (p.root / CONV).write_text(p.template_text() + "\nOurs, edited again.\n")
        r = subprocess.run([sys.executable, str(SCRIPT_PATH), "--upgrade", str(p.root),
                            "--interactive", "--no-commit"],
                           input="k\ny\n", capture_output=True, text=True, env=_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = p.check("--include-templates")
        self.assertNotIn(f"never auto-overwritten): {CONV}", r.stdout)
        self.assertNotIn("local", p.entry(CONV))

    def test_standing_keep_local_is_cleared_with_a_note(self):
        # xmeo-v3's shape: amended, re-baselined, `local: kept` since v0.16.0.
        p = _Project(self)
        amended = p.template_text().replace("Dependency policy", "Dependency policy (ours)")
        p.as_pre_v017(conventions_text=amended, kept=True)
        r = p.upgrade("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("standing keep-local cleared", r.stdout)

        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"note: {CONV}: standing keep-local cleared", r.stdout)
        self.assertEqual((p.root / CONV).read_text(), amended)
        e = p.entry(CONV)
        self.assertEqual(e["ownership"], PROJECT_OWNED)
        self.assertNotIn("local", e)
        self.assertEqual(p.check().returncode, 0)
        self.assertEqual(p.check("--include-templates").returncode, 3)
        # And later upgrades still never touch it, flag or no flag.
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), amended)

    def test_unedited_scaffold_era_copy_takes_the_release_text_once(self):
        # A project that never touched the file gets this release's template
        # (the last time the scaffold writes it), then owns it.
        p = _Project(self)
        old = "# Stack conventions — game-canvas\n\nThe v0.16 text.\n"
        p.as_pre_v017(conventions_text=old)
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), p.template_text())
        e = p.entry(CONV)
        self.assertEqual(e["ownership"], PROJECT_OWNED)
        self.assertEqual(p.check("--include-templates").returncode, 0)

    def test_unchanged_copy_just_changes_class(self):
        p = _Project(self)
        p.as_pre_v017()
        before = (p.root / CONV).read_text()
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), before)
        self.assertEqual(p.entry(CONV)["ownership"], PROJECT_OWNED)
        self.assertEqual(p.check("--include-templates").returncode, 0)

    def test_edits_after_migration_are_never_overwritten(self):
        p = _Project(self)
        mine = "# Our conventions\n\nWe rewrote all of it.\n"
        (p.root / CONV).write_text(mine)
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), mine)
        self.assertNotIn("local", p.entry(CONV))

    def test_take_new_still_rerenders_on_request(self):
        p = _Project(self)
        (p.root / CONV).write_text("# mine\n")
        r = p.upgrade("--take-new", CONV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), p.template_text())

    def test_take_new_rerenders_a_clean_file_too(self):
        # Review r1 MAJOR 1: an unedited file whose template moved on must be
        # adoptable with --take-new, not only an edited one.
        p = _Project(self)
        stale = "# stale conventions\n"
        (p.root / CONV).write_text(stale)
        self.assertEqual(p.upgrade().returncode, 0)  # re-baselined: clean now
        r = p.upgrade("--take-new", CONV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), p.template_text())
        self.assertEqual(p.check("--include-templates").returncode, 0)


class ReProfile(unittest.TestCase):
    """Review r1 MAJOR 2 / MINOR 3-4: a re-profile moves CONVENTIONS.md to
    another stack's template, and must neither strand stale text nor let a
    plain uninstall delete project content."""

    def test_unedited_copy_takes_the_new_stacks_text(self):
        p = _Project(self, profile="static-web")
        r = p.upgrade("--profile", "game-canvas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("profile changed its template", r.stdout)
        self.assertEqual((p.root / CONV).read_text(), p.template_text("game-canvas"))
        self.assertEqual(p.entry(CONV)["rendered_from"], "templates/conventions.game-canvas.md")
        # (The configured static-web verify gate is kept — never overwritten —
        # and correctly reported against the game-canvas gate template.)
        r = p.check("--include-templates")
        self.assertNotIn(f"TEMPLATE DRIFT (advisory; never auto-overwritten): {CONV}", r.stdout)
        self.assertIn("scripts/phasekit-verify.sh ← templates/phasekit-verify.template.game-canvas.sh",
                      r.stdout)

    def test_edited_copy_is_kept_and_reported(self):
        p = _Project(self, profile="static-web")
        mine = p.template_text("static-web") + "\nOur amendment.\n"
        (p.root / CONV).write_text(mine)
        r = p.upgrade("--profile", "game-canvas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), mine)
        r = p.check("--include-templates")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(f"{CONV} ← templates/conventions.game-canvas.md", r.stdout)

    def test_leaving_a_stack_keeps_the_file_the_projects(self):
        p = _Project(self, profile="static-web")
        mine = "# ours\n"
        (p.root / CONV).write_text(mine)
        r = p.upgrade("--profile", "default")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(p.entry(CONV)["ownership"], PROJECT_OWNED)
        r = _run("--uninstall", str(p.root), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), mine)

    def test_coming_back_from_an_old_orphan_reports_the_template(self):
        # A pre-v0.17 scaffold-orphan entry has no template base: the file is
        # based on its own bytes, so stale text is reported, not silenced.
        p = _Project(self)
        stale = "# old stack text\n"
        (p.root / CONV).write_text(stale)
        norm, strict = M.compute_file_shas(p.root / CONV, True)

        def fn(m):
            for e in m["files"]:
                if e["path"] == CONV:
                    for k in ("rendered_from", "template_sha"):
                        e.pop(k, None)
                    e.update({"ownership": "scaffold-orphan", "sha256": norm,
                              "sha256_strict": strict})
        p.edit_manifest(fn)
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((p.root / CONV).read_text(), stale)
        self.assertEqual(p.check("--include-templates").returncode, 3)


class EnrichOverAnOldManifest(unittest.TestCase):
    def test_enrich_rerecords_without_a_moot_keep_and_reports_the_template(self):
        # Review r1 MINOR 7: `enrich` (not upgrade) over a v0.16 project.
        p = _Project(self)
        amended = p.template_text() + "\nOurs.\n"
        p.as_pre_v017(conventions_text=amended, kept=True, companion=True)
        r = _run(str(p.root), "--profile", "game-canvas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        e = p.entry(CONV)
        self.assertEqual(e["ownership"], PROJECT_OWNED)
        self.assertNotIn("local", e)
        self.assertEqual((p.root / CONV).read_text(), amended)
        self.assertEqual(p.check("--include-templates").returncode, 3)


class CompanionDoc(unittest.TestCase):
    def test_fresh_install_seeds_companion_project_owned(self):
        p = _Project(self, profile="default")
        comp = p.root / COMPANION
        self.assertTrue(comp.is_file())
        text = comp.read_text()
        self.assertIn("Project-owned companion to `docs/QUALITY_GATES.md`", text)
        self.assertNotIn("{{", text)
        self.assertEqual(p.entry(COMPANION)["ownership"], "bootstrap-frozen")

    def test_companion_is_never_rewritten_by_upgrade(self):
        p = _Project(self, profile="default")
        mine = "# Our gates\n\n## Fidelity gate\n\nOurs.\n"
        (p.root / COMPANION).write_text(mine)
        for extra in ((), ("--profile", "python-uv")):
            r = p.upgrade(*extra)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((p.root / COMPANION).read_text(), mine)

    def test_existing_project_gets_companion_on_upgrade(self):
        p = _Project(self)
        p.as_pre_v017()
        self.assertFalse((p.root / COMPANION).exists())
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((p.root / COMPANION).is_file())
        self.assertEqual(p.entry(COMPANION)["ownership"], "bootstrap-frozen")

    def test_a_companion_the_project_already_has_is_adopted_not_refused(self):
        p = _Project(self)
        p.as_pre_v017()
        mine = "# Already ours\n"
        (p.root / COMPANION).parent.mkdir(parents=True, exist_ok=True)
        (p.root / COMPANION).write_text(mine)
        r = p.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("adopted as its own", r.stdout)
        self.assertEqual((p.root / COMPANION).read_text(), mine)
        self.assertEqual(p.entry(COMPANION)["ownership"], "bootstrap-frozen")

    def test_scaffold_collision_still_refuses(self):
        # The adopt default is for project-owned classes only.
        p = _Project(self)

        def drop(m):
            m["files"] = [e for e in m["files"] if e["path"] != "docs/EXECUTION_MODES.md"]
        p.edit_manifest(drop)
        (p.root / "docs/EXECUTION_MODES.md").write_text("# ours\n")
        r = p.upgrade()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)


class SessionsAreTold(unittest.TestCase):
    """The rule reaches sessions: in the scaffold doc itself (propagated to
    every project) and wherever the scaffold instructs sessions."""

    def test_quality_gates_header_names_the_companion(self):
        head = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text().split("## Anti-rationalization")[0]
        self.assertIn("Scaffold-owned: do not edit this file in a project", head)
        self.assertIn("`docs/project/QUALITY_GATES.md`", head)
        self.assertIn("upstream to phasekit", head)

    def test_instruction_surfaces_name_the_companion(self):
        for rel in ("CONTINUE_PROMPT.txt", "templates/CLAUDE.template.md",
                    "templates/AGENTS.template.md"):
            text = (REPO_ROOT / rel).read_text()
            self.assertIn("docs/project/QUALITY_GATES.md", text, rel)
            self.assertIn("upstream to phasekit", text, rel)

    def test_conventions_templates_say_project_owned(self):
        for stack in M.STACK_CONVENTIONS_TEMPLATES:
            text = (REPO_ROOT / M.STACK_CONVENTIONS_TEMPLATES[stack]).read_text()
            self.assertIn("**project-owned**", text, stack)
            self.assertNotIn("scaffold-owned", text, stack)


if __name__ == "__main__":
    unittest.main()
