"""v0.18.7: the dead @imports in .claude/CLAUDE.md.

Claude Code resolves an `@path` import relative to the file that holds it. The
template seeded `- @docs/SPEC.md` and five more into `.claude/CLAUDE.md`, so
every one resolved to `.claude/docs/...` and loaded nothing (probed on Claude
Code 2.1.289 in scaffold-runner: `@docs/X` absent from context, `@../docs/X`
and `@/abs/X` present). They are not made live — an import loads the whole file
into every session, and a fleet SPEC reached 3.3 MB — the template NAMES them,
and an upgrade rewrites exactly those lines in an existing project's own copy.
"""
from __future__ import annotations
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"


def _load():
    spec = importlib.util.spec_from_file_location("enrich_project_imports_test", ENRICH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()

# A fleet project's .claude/CLAUDE.md as v0.18.6 left it (round-clock, 2026-10-06).
FLEET_V0186 = """# round-clock

This repository uses the phasekit workflow.

## Core operating rules
- Work in audit-first mode
- Start from the earliest unapproved phase
- Prefer minimal, backward-compatible changes unless a rewrite is explicitly justified
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally

## Required references
- @docs/SPEC.md
- @docs/ARCHITECTURE.md
- @docs/PHASES.md
- @docs/QUALITY_GATES.md
- @docs/PROD_REQUIREMENTS.md

## Optional references
- @docs/DESIGN.md — steady-state system design (subsystems, data flows, hot spots, boundaries). Read first if present; not every project has one."""

# Claude Code's import syntax, as a reader of the file would apply it: `@path`
# at a word start, outside inline code and fenced blocks.
_LIVE_IMPORT = re.compile(r"(?<![\w`])@([A-Za-z0-9_./~][^\s`]*)")


def live_imports(text):
    out, fenced = [], False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if not fenced:
            out += _LIVE_IMPORT.findall(re.sub(r"`[^`]*`", "", line))
    return out


def migrate(root, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        changed = M.migrate_claude_md_imports(root, **kw)
    return changed, buf.getvalue()


class TheShippedFilesImportNothing(unittest.TestCase):

    def test_the_template_names_its_references_and_imports_none(self):
        text = (REPO_ROOT / "templates" / "CLAUDE.template.md").read_text()
        self.assertEqual(live_imports(text), [])
        for rel in M.DEAD_IMPORT_PATHS:
            self.assertIn(f"- `{rel}`", text)
        self.assertIn("not `@`-imported", text)

    def test_phasekits_own_claude_md_imports_none(self):
        text = (REPO_ROOT / ".claude" / "CLAUDE.md").read_text()
        self.assertEqual(live_imports(text), [])
        for rel in ("docs/RELEASING.md", "docs/QUALITY_GATES.md", "docs/CAPABILITY_MANIFEST.md",
                    "capabilities/project-capabilities.yaml", "docs/USAGE_PATTERNS.md"):
            self.assertIn(f"- `{rel}`", text)


class Migration(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-imports-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".claude").mkdir()
        (self.root / "docs").mkdir()
        self.f = self.root / ".claude" / "CLAUDE.md"

    def test_the_fleet_file_loses_exactly_its_dead_imports(self):
        self.f.write_text(FLEET_V0186)
        changed, out = migrate(self.root)
        self.assertEqual(changed, [13, 14, 15, 16, 17, 20])
        after = self.f.read_text()
        self.assertEqual(live_imports(after), [])
        expected = re.sub(r"^- @(docs/\S+)", r"- `\1`", FLEET_V0186, flags=re.M)
        self.assertEqual(after, expected)  # every other byte, the final newline's absence too
        self.assertIn("- `docs/DESIGN.md` — steady-state system design", after)
        self.assertIn("migrate: .claude/CLAUDE.md: rewrote 6 dead @docs import(s)", out)

    def test_it_is_idempotent(self):
        self.f.write_text(FLEET_V0186)
        migrate(self.root)
        once = self.f.read_bytes()
        changed, out = migrate(self.root)
        self.assertEqual(changed, [])
        self.assertEqual(out, "")
        self.assertEqual(self.f.read_bytes(), once)

    def test_project_lines_are_kept_and_only_the_seeded_forms_change(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "notes.md").write_text("n\n")
        (self.root / "docs" / "SPEC.md").write_text("s\n")
        text = ("# p\r\n"
                "Our own rule: never deploy on Fridays.\r\n"
                "- @docs/SPEC.md\r\n"
                "  * @docs/project/QUALITY_GATES.md   # ours\r\n"
                "- @docs/SPEC.md.bak\r\n"
                "- see @docs/PHASES.md for the plan\r\n"
                "- @src/notes.md\r\n"
                "```\r\n"
                "- @docs/SPEC.md\r\n"
                "```\r\n"
                "mail me@docs/SPEC.md\r\n")
        self.f.write_bytes(text.encode())
        changed, out = migrate(self.root)
        self.assertEqual(changed, [3, 4])
        self.assertEqual(self.f.read_bytes().decode(), text
                         .replace("- @docs/SPEC.md\r\n  *", "- `docs/SPEC.md`\r\n  *", 1)
                         .replace("* @docs/project/QUALITY_GATES.md",
                                  "* `docs/project/QUALITY_GATES.md`"))
        # the project's own import is noted, never rewritten
        self.assertIn("line 7: @src/notes.md resolves relative to .claude/", out)
        self.assertNotIn("line 9", out)

    def test_dry_run_reports_and_writes_nothing(self):
        self.f.write_text(FLEET_V0186)
        changed, out = migrate(self.root, dry_run=True)
        self.assertEqual(len(changed), 6)
        self.assertIn("would rewrite 6", out)
        self.assertEqual(self.f.read_text(), FLEET_V0186)

    def test_no_file_or_a_symlink_is_left_alone(self):
        self.assertEqual(migrate(self.root), ([], ""))
        real = self.root / "elsewhere.md"
        real.write_text(FLEET_V0186)
        self.f.symlink_to(real)
        self.assertEqual(migrate(self.root), ([], ""))
        self.assertEqual(real.read_text(), FLEET_V0186)

    def test_the_file_mode_is_kept(self):
        self.f.write_text(FLEET_V0186)
        os.chmod(self.f, 0o640)
        migrate(self.root)
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o640)


class UpgradeMigratesAndCommits(unittest.TestCase):
    """End to end: a committed project whose .claude/CLAUDE.md is the fleet's
    v0.18.6 file plus a project line."""

    EXTRA = "\n\n## Project notes\n- Deploys go through the QA gate.\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-imports-upg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = self.tmp / "project"
        self.p.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        r = self.enrich(str(self.p))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        claude = self.p / ".claude" / "CLAUDE.md"
        claude.write_text(FLEET_V0186 + self.EXTRA)
        norm, strict = M.compute_file_shas(claude, True)
        m = json.loads((self.p / ".scaffold" / "manifest.json").read_text())
        for e in m["files"]:
            if e["path"] == ".claude/CLAUDE.md":
                e.update({"sha256": norm, "sha256_strict": strict})
        (self.p / ".scaffold" / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.p), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def enrich(self, *args):
        env = dict(os.environ)
        env["PHASEKIT_UPGRADE_VERIFY"] = "off"
        env["XDG_STATE_HOME"] = str(self.tmp / "state")
        return subprocess.run([sys.executable, str(ENRICH), *args],
                              capture_output=True, text=True, env=env)

    def test_the_upgrade_rewrites_commits_and_rebaselines(self):
        r = self.enrich("--upgrade", str(self.p), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrate: .claude/CLAUDE.md: rewrote 6 dead @docs import(s)", r.stdout)
        text = (self.p / ".claude" / "CLAUDE.md").read_text()
        self.assertEqual(live_imports(text), [])
        self.assertTrue(text.endswith(self.EXTRA))
        self.assertIn(".claude/CLAUDE.md", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        c = self.enrich("--check", str(self.p))
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertIn("drifted: 0", c.stdout)
        # a second upgrade has nothing to migrate
        head = self.git("rev-parse", "HEAD")
        r2 = self.enrich("--upgrade", str(self.p), "--yes")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertNotIn("migrate:", r2.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_a_dry_run_says_what_it_would_do(self):
        r = self.enrich("--upgrade", str(self.p), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would rewrite 6", r.stdout)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_an_in_flight_edit_with_dead_imports_is_left_for_a_later_upgrade(self):
        claude = self.p / ".claude" / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- work in progress\n")
        wip = claude.read_text()
        r = self.enrich("--upgrade", str(self.p), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("has uncommitted changes", r.stdout)
        self.assertEqual(claude.read_text(), wip)
        self.assertNotIn(".claude/CLAUDE.md",
                         self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain").strip(), "M .claude/CLAUDE.md")
        # once committed, the next upgrade migrates it
        self.git("commit", "-qam", "wip landed")
        r = self.enrich("--upgrade", str(self.p), "--yes")
        self.assertIn("rewrote 6 dead @docs import(s)", r.stdout)
        self.assertEqual(live_imports(claude.read_text()), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_an_over_long_at_token_never_fails_the_upgrade(self):
        claude = self.p / ".claude" / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- @" + "x" * 300 + "/y.md\n")
        self.git("commit", "-qam", "long")
        changed, _ = migrate(self.p)
        self.assertEqual(len(changed), 6)

    def test_an_in_flight_edit_without_dead_imports_never_rides_the_commit(self):
        self.enrich("--upgrade", str(self.p), "--yes")
        claude = self.p / ".claude" / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- work in progress\n")
        self.enrich("--upgrade", str(self.p), "--yes")
        self.assertIn("work in progress", claude.read_text())
        self.assertEqual(self.git("status", "--porcelain").strip(), "M .claude/CLAUDE.md")


if __name__ == "__main__":
    unittest.main()
