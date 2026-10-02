#!/usr/bin/env python3
"""Tests for v0.5.0 stack profiles (stack contract: verify seeding +
conventions doc).

Covers the acceptance surface of DESIGN-stack-profiles.md:
- profile resolution carries `stack:` (inherited, overridable, None default)
- enumerate_install_targets picks the stack verify template and adds
  docs/CONVENTIONS.md (project-owned since v0.17.0) for stack profiles only
- greenfield enrich under a stack profile seeds a CONFIGURED=1 gate
- --upgrade re-seeds the verify gate ONLY while it is still the stub
  (PHASEKIT_VERIFY_CONFIGURED=0); a configured gate is never overwritten
- the seeded gates actually work (docs-only link checker, static-web
  import-graph checker)

Run from the repo root: `python3 -m unittest tests.test_stack_profiles`
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

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

    def test_conventions_spec_is_project_owned(self):
        for name in STACKS:
            resolved = self.m.resolve_profile(self.profiles, name)
            targets = self.m.enumerate_install_targets(self.manifest, resolved)
            conv = next(s for s in targets if s["path"] == self.m.CONVENTIONS_DEST_PATH)
            # v0.17.0 (row 1140): seeded once, then the project's.
            self.assertEqual(conv["ownership"], "bootstrap-with-template-tracking")
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
        # The migrating upgrade (v0.17.0) hashes the template as if it were
        # the rendered output to tell an unedited copy from an amended one;
        # any {{PLACEHOLDER}} would break that identity.
        for name in STACKS:
            path = REPO_ROOT / "templates" / f"conventions.{name}.md"
            self.assertTrue(path.exists(), path)
            self.assertNotIn("{{", path.read_text())


    def test_stack_conventions_point_at_the_verify_budget(self):
        # Row 1140: the stale "fast (< ~30s)" budget is replaced by the
        # verify-budget doctrine (fast tier per commit, full suite at the
        # sprint and at completion). docs-only is unchanged.
        for name in ("python-uv", "static-web", "game-canvas"):
            text = (REPO_ROOT / "templates" / f"conventions.{name}.md").read_text()
            self.assertNotIn("< ~30s", text, name)
            self.assertIn('"Verify budget"', text, name)
            self.assertRegex(text, r"at\s+completion", name)

    def test_static_web_names_its_limit(self):
        text = (REPO_ROOT / "templates" / "conventions.static-web.md").read_text()
        self.assertIn("**Zero runtime dependencies.**", text)
        self.assertIn("has outgrown\n  this stack", text)

    def test_game_canvas_is_not_static_web_and_keeps_game_rules(self):
        text = (REPO_ROOT / "templates" / "conventions.game-canvas.md").read_text()
        self.assertNotIn("is a static-web project", text)
        self.assertNotIn("Zero runtime dependencies", text)
        self.assertIn("runtime-dependencies.json", text)
        self.assertIn("One ADR per addition", text)
        self.assertIn("A build step is allowed", text)
        for rule in ("deterministic core", "Fixed-timestep", "seedable RNG",
                     "Unit-test the deterministic core"):
            self.assertIn(rule, text)


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
        # --no-verify: `my-own-checks` is a placeholder that marks the gate as
        # configured, not a runnable gate. Since v0.16.0 upgrade RUNS a
        # configured gate before committing (tests/test_upgrade_gate.py), and
        # this test is about ownership — never overwriting it — not execution.
        result = _upgrade(fx.target, profile="python-uv", extra=["--no-verify"])
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

    def _game_canvas(self, tmp, files):
        dest = self._render(tmp, "game-canvas")
        for rel, text in files.items():
            f = Path(tmp) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return self._run(dest)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_game_canvas_gate_accepts_declared_dependencies(self):
        # Row 1140: an allowlist, not a prohibition — declared + ADR = green.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "workspaces": ["packages/*"], '
                                '"dependencies": {"howler": "^2"}}\n',
                "packages/render/package.json":
                    '{"name": "@g/render", "dependencies": {"pixi.js": "^8", "@g/core": "^0"}}\n',
                "packages/core/package.json": '{"name": "@g/core"}\n',
                "docs/adr/ADR-0001-audio.md": "# audio\n",
                "docs/adr/ADR-0002-pixi.md": "# pixi\n",
                "runtime-dependencies.json": json.dumps({
                    ".": {"howler": "docs/adr/ADR-0001-audio.md"},
                    "packages/render": {"pixi.js": "docs/adr/ADR-0002-pixi.md"},
                }),
            })
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("3 package.json checked", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_game_canvas_gate_rejects_undeclared_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No allowlist at all = an empty one: a fresh project stays dep-free.
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "dependencies": {"left-pad": "^1.0.0"}}\n'})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"left-pad"', result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            # Declared for another workspace is not declared for this one.
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "workspaces": ["server"]}\n',
                "server/package.json": '{"name": "s", "optionalDependencies": {"ws": "8"}}\n',
                "docs/adr/ADR-0001-ws.md": "# ws\n",
                "runtime-dependencies.json": '{".": {"ws": "docs/adr/ADR-0001-ws.md"}}',
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('server/package.json: runtime dependency "ws"', result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_game_canvas_gate_odd_inputs(self):
        # Review r1 MINOR 5: `**` workspaces are walked; an `npm:` alias under
        # a workspace's name is third-party; allowlist keys are normalised.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "workspaces": ["packages/**"]}\n',
                "packages/a/b/package.json": '{"name": "@g/b", "dependencies": {"left-pad": "1"}}\n',
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('packages/a/b/package.json: runtime dependency "left-pad"', result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "workspaces": ["w"]}\n',
                "w/package.json": '{"name": "w", "dependencies": {"g": "npm:evil@1"}}\n',
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('runtime dependency "g"', result.stderr)
        # Round 2: a git/URL source under a workspace's name is third-party;
        # a workspace:/file: link or a plain range is the workspace itself.
        for spec, ok in (("github:evil/core", False), ("https://x/core.tgz", False),
                         ("evil/core", False), ("workspace:*", True),
                         ("file:../core", True), ("^0.1.0", True)):
            with tempfile.TemporaryDirectory() as tmp:
                result = self._game_canvas(tmp, {
                    "package.json": '{"name": "g", "workspaces": ["core", "app"]}\n',
                    "core/package.json": '{"name": "@g/core"}\n',
                    "app/package.json": json.dumps(
                        {"name": "@g/app", "dependencies": {"@g/core": spec}}),
                })
                self.assertEqual(result.returncode == 0, ok, (spec, result.stderr))
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "workspaces": ["server"]}\n',
                "server/package.json": '{"name": "s", "dependencies": {"ws": "8"}}\n',
                "docs/adr/ADR-0001-ws.md": "# ws\n",
                "runtime-dependencies.json": '{"./server": {"ws": "docs/adr/ADR-0001-ws.md"}}',
            })
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_game_canvas_gate_requires_the_adr_to_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g", "dependencies": {"pixi.js": "^8"}}\n',
                "runtime-dependencies.json": '{".": {"pixi.js": "docs/adr/ADR-0009-nope.md"}}',
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ADR-0009-nope.md", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._game_canvas(tmp, {
                "package.json": '{"name": "g"}\n',
                "runtime-dependencies.json": '["not", "a", "map"]',
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime-dependencies.json: expected", result.stderr)

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
