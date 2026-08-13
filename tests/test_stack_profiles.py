#!/usr/bin/env python3
"""Tests for v0.5.0 stack profiles (stack contract: verify seeding +
conventions doc).

Covers the acceptance surface of DESIGN-stack-profiles.md:
- profile resolution carries `stack:` (inherited, overridable, None default)
- enumerate_install_targets picks the stack verify template and adds
  docs/CONVENTIONS.md (scaffold class) for stack profiles only
- greenfield enrich under a stack profile seeds a CONFIGURED=1 gate
- --upgrade re-seeds the verify gate ONLY while it is still the stub
  (PHASEKIT_VERIFY_CONFIGURED=0); a configured gate is never overwritten
- the seeded gates actually work (docs-only link checker, static-web
  import-graph checker)

Run from the repo root: `python3 -m unittest tests.test_stack_profiles`
"""

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "enrich-project.py"

STACKS = ("python-uv", "static-web", "game-canvas", "docs-only")


def _load_module():
    spec = importlib.util.spec_from_file_location("enrich_project_stack_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enrich(target, profile=None):
    cmd = [sys.executable, str(SCRIPT_PATH), str(target)]
    if profile:
        cmd += ["--profile", profile]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _upgrade(target, profile=None, extra=()):
    cmd = [sys.executable, str(SCRIPT_PATH), "--upgrade", str(target), "--yes"]
    if profile:
        cmd += ["--profile", profile]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


class StackProfileResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load_module()
        cls.manifest = cls.m.load_manifest()
        cls.profiles = cls.manifest["profiles"]

    def test_stack_profiles_carry_stack(self):
        for name in STACKS:
            resolved = self.m.resolve_profile(self.profiles, name)
            self.assertEqual(resolved["stack"], name)

    def test_non_stack_profiles_have_no_stack(self):
        for name in ("default", "game-project", "saas-project", "with-design"):
            resolved = self.m.resolve_profile(self.profiles, name)
            self.assertIsNone(resolved["stack"])

    def test_game_canvas_inherits_game_agents(self):
        resolved = self.m.resolve_profile(self.profiles, "game-canvas")
        self.assertIn("engine-builder", resolved["include_agents"])

    def test_stack_verify_template_selected(self):
        for name in STACKS:
            resolved = self.m.resolve_profile(self.profiles, name)
            targets = self.m.enumerate_install_targets(self.manifest, resolved)
            verify = next(s for s in targets if s["path"] == self.m.VERIFY_DEST_PATH)
            self.assertEqual(verify["rendered_from"], self.m.STACK_VERIFY_TEMPLATES[name])
            self.assertEqual(verify["ownership"], "bootstrap-with-template-tracking")

    def test_default_profile_keeps_stub_and_no_conventions(self):
        resolved = self.m.resolve_profile(self.profiles, "default")
        targets = self.m.enumerate_install_targets(self.manifest, resolved)
        verify = next(s for s in targets if s["path"] == self.m.VERIFY_DEST_PATH)
        self.assertEqual(verify["rendered_from"], self.m.DEFAULT_VERIFY_TEMPLATE)
        self.assertFalse(
            [s for s in targets if s["path"] == self.m.CONVENTIONS_DEST_PATH])

    def test_conventions_spec_is_scaffold_class(self):
        for name in STACKS:
            resolved = self.m.resolve_profile(self.profiles, name)
            targets = self.m.enumerate_install_targets(self.manifest, resolved)
            conv = next(s for s in targets if s["path"] == self.m.CONVENTIONS_DEST_PATH)
            self.assertEqual(conv["ownership"], "scaffold")
            self.assertEqual(conv["rendered_from"], self.m.STACK_CONVENTIONS_TEMPLATES[name])


class TemplateHygiene(unittest.TestCase):
    """The template files themselves must hold the invariants the engine
    relies on."""

    def test_verify_templates_are_valid_bash_and_configured(self):
        for name in STACKS:
            path = REPO_ROOT / "templates" / f"phasekit-verify.template.{name}.sh"
            self.assertTrue(path.exists(), path)
            subprocess.run(["bash", "-n", str(path)], check=True)
            text = path.read_text()
            self.assertIn("PHASEKIT_VERIFY_CONFIGURED=1", text)
            self.assertNotRegex(text, r"(?m)^PHASEKIT_VERIFY_CONFIGURED=0")

    def test_python_uv_gate_runs_fast_tier_with_full_suite_at_completion(self):
        # v0.6.4 verify budget: the seeded python-uv gate excludes `slow`-marked
        # tests per-commit, and runs the complete suite once the completion
        # record exists (fast tier per-commit; full suite at sprint AND
        # completion). Other stack templates (node --test) have no marker idiom
        # worth forcing — this pin is python-uv only.
        text = (REPO_ROOT / "templates" / "phasekit-verify.template.python-uv.sh").read_text()
        self.assertIn('pytest -q -m "not slow"', text)
        self.assertIn("artifacts/project-complete.json", text)
        self.assertIn("--durations", text)

    def test_conventions_templates_are_placeholder_free(self):
        # scaffold-class update detection hashes the template as if it were
        # the rendered output; any {{PLACEHOLDER}} would break that identity.
        for name in STACKS:
            path = REPO_ROOT / "templates" / f"conventions.{name}.md"
            self.assertTrue(path.exists(), path)
            self.assertNotIn("{{", path.read_text())


class _ProjectFixture:
    def __init__(self, profile=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "project"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.target, check=True)
        _enrich(self.target, profile=profile)

    @property
    def verify(self):
        return self.target / "scripts" / "phasekit-verify.sh"

    @property
    def conventions(self):
        return self.target / "docs" / "CONVENTIONS.md"

    def cleanup(self):
        self._tmp.cleanup()


class GreenfieldSeeding(unittest.TestCase):
    def test_stack_enrich_seeds_configured_gate_and_conventions(self):
        fx = _ProjectFixture(profile="static-web")
        self.addCleanup(fx.cleanup)
        text = fx.verify.read_text()
        self.assertIn("PHASEKIT_VERIFY_CONFIGURED=1", text)
        self.assertIn("node --test", text)
        self.assertTrue(fx.conventions.exists())
        self.assertIn("static-web", fx.conventions.read_text())

    def test_default_enrich_still_seeds_stub(self):
        fx = _ProjectFixture()
        self.addCleanup(fx.cleanup)
        self.assertRegex(fx.verify.read_text(), r"(?m)^PHASEKIT_VERIFY_CONFIGURED=0")
        self.assertFalse(fx.conventions.exists())


class UpgradeReseeding(unittest.TestCase):
    def test_upgrade_reseeds_stub_gate(self):
        fx = _ProjectFixture()  # default profile → stub gate
        self.addCleanup(fx.cleanup)
        result = _upgrade(fx.target, profile="docs-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stub mode; seeding", result.stdout)
        text = fx.verify.read_text()
        self.assertIn("PHASEKIT_VERIFY_CONFIGURED=1", text)
        self.assertIn("docs-only", text)
        self.assertTrue(fx.conventions.exists())
        # Manifest records the new profile.
        m = _load_module()
        manifest = m.load_downstream_manifest(fx.target)
        self.assertEqual(manifest["profile"], "docs-only")

    def test_upgrade_never_overwrites_configured_gate(self):
        fx = _ProjectFixture()
        self.addCleanup(fx.cleanup)
        custom = "#!/usr/bin/env bash\nset -euo pipefail\nmy-own-checks\nPHASEKIT_VERIFY_CONFIGURED=1\n"
        fx.verify.write_text(custom)
        result = _upgrade(fx.target, profile="python-uv")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fx.verify.read_text(), custom)

    def test_keep_local_overrides_reseed(self):
        fx = _ProjectFixture()
        self.addCleanup(fx.cleanup)
        before = fx.verify.read_text()
        result = _upgrade(fx.target, profile="python-uv",
                          extra=["--keep-local", "scripts/phasekit-verify.sh"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fx.verify.read_text(), before)

    def test_stack_upgrade_is_idempotent(self):
        fx = _ProjectFixture(profile="static-web")
        self.addCleanup(fx.cleanup)
        result = _upgrade(fx.target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Seeded (configured) gate is not re-seeded on the second pass.
        self.assertNotIn("stub-reseed", result.stdout.replace("stub-reseed: 0", ""))
        self.assertIn("PHASEKIT_VERIFY_CONFIGURED=1", fx.verify.read_text())


class SeededGatesWork(unittest.TestCase):
    """Run the rendered verify scripts against minimal fixture projects."""

    def _render(self, tmp, stack):
        m = _load_module()
        scripts = Path(tmp) / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        dest = scripts / "phasekit-verify.sh"
        template = REPO_ROOT / m.STACK_VERIFY_TEMPLATES[stack]
        dest.write_text(m.render_template_text(template, "fixture-project"))
        return dest

    def _run(self, dest):
        return subprocess.run(["bash", str(dest)], capture_output=True, text=True)

    def test_docs_only_gate_catches_broken_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._render(tmp, "docs-only")
            readme = Path(tmp) / "README.md"
            readme.write_text("# Fixture\n\nSee [other](OTHER.md) and [gone](MISSING.md).\n")
            (Path(tmp) / "OTHER.md").write_text("# Other\n\nBack to [readme](README.md#fixture).\n")
            result = self._run(dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MISSING.md", result.stderr)

            readme.write_text("# Fixture\n\nSee [other](OTHER.md).\n")
            result = self._run(dest)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_docs_only_gate_catches_dangling_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._render(tmp, "docs-only")
            (Path(tmp) / "A.md").write_text("# Alpha\n\n[bad](B.md#no-such-heading)\n")
            (Path(tmp) / "B.md").write_text("# Beta\n\n## Real heading\n")
            result = self._run(dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no-such-heading", result.stderr)

            (Path(tmp) / "A.md").write_text("# Alpha\n\n[ok](B.md#real-heading)\n")
            result = self._run(dest)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_docs_only_gate_ignores_external_and_code_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._render(tmp, "docs-only")
            (Path(tmp) / "A.md").write_text(
                "# Alpha\n\n[ext](https://example.com/x)\n\n"
                "```\n[fenced](NOPE.md)\n```\n\nand `[span](ALSO-NOPE.md)` inline.\n"
            )
            result = self._run(dest)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_static_web_gate_catches_broken_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._render(tmp, "static-web")
            (Path(tmp) / "app.js").write_text("import { x } from './missing.js';\n")
            result = self._run(dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing.js", result.stderr)

            (Path(tmp) / "missing.js").write_text("export const x = 1;\n")
            result = self._run(dest)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_static_web_gate_rejects_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._render(tmp, "static-web")
            (Path(tmp) / "package.json").write_text(
                '{"name": "fixture", "dependencies": {"left-pad": "^1.0.0"}}\n')
            result = self._run(dest)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("left-pad", result.stderr)


if __name__ == "__main__":
    unittest.main()
