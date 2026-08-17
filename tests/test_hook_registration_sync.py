#!/usr/bin/env python3
"""Tests for hook-registration sync on upgrade (session-lifetime batch).

Hook FILES are scaffold-class and install on every upgrade. But
`.claude/settings.json` is bootstrap-with-template-tracking — write-once, never
overwritten, because it also carries project-owned permissions. So shipping a
new hook delivered the file to every existing project and the REGISTRATION to
none of them: the hook lands and never fires.

That matters concretely here. The Stop hook exists to answer an incident that
happened in an EXISTING project; without this sync it would have shipped as a
believed no-op to all eight fleet repos, which is the producer-built /
consumer-missing failure this scaffold has spent two releases eliminating.

The sync is additive and narrow, and these tests pin that narrowness as hard as
they pin the behaviour: it must never edit anything a project owns.

Run from the repo root: `python3 -m unittest tests.test_hook_registration_sync`
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("enrich_hooksync_test", ENRICH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


enrich = _load_module()


class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-hooksync-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        (self.project / ".claude" / "hooks").mkdir(parents=True)
        self.settings = self.project / ".claude" / "settings.json"

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def sync(self):
        return enrich.sync_hook_registrations(self.project, quiet=True)


class AddsMissingRegistrations(SyncFixture):
    def test_a_pre_stop_hook_project_gains_the_stop_registration(self):
        """The exact shape of every fleet project before this release."""
        self.write_settings({
            "permissions": {"allow": ["Bash(ls *)"], "deny": ["Bash(rm -rf *)"]},
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command",
                               "command": "./.claude/hooks/deny-dangerous-commands.sh"}],
                }],
            },
        })
        added = self.sync()
        self.assertIn(("Stop", "./.claude/hooks/require-verdict.sh"), added)
        stop = self.read_settings()["hooks"]["Stop"]
        commands = [h["command"] for e in stop for h in e["hooks"]]
        self.assertIn("./.claude/hooks/require-verdict.sh", commands)

    def test_matchers_are_carried_over_not_invented(self):
        self.write_settings({"hooks": {}})
        self.sync()
        hooks = self.read_settings()["hooks"]
        session_start = hooks["SessionStart"][0]
        self.assertEqual(session_start["matcher"], "compact")

    def test_a_settings_file_with_no_hooks_key_at_all_is_handled(self):
        self.write_settings({"permissions": {"allow": [], "deny": []}})
        self.sync()
        self.assertIn("Stop", self.read_settings()["hooks"])


class NeverTouchesWhatTheProjectOwns(SyncFixture):
    def setUp(self):
        super().setUp()
        self.original = {
            "permissions": {
                "allow": ["Bash(ls *)", "Bash(cargo test)"],
                "deny": ["Bash(rm -rf *)"],
            },
            "env": {"PROJECT_SPECIFIC": "1"},
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command",
                               "command": "./.claude/hooks/deny-dangerous-commands.sh"}],
                }, {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "./scripts/my-own-hook.sh"}],
                }],
            },
        }
        self.write_settings(self.original)

    def test_project_permissions_and_env_survive_untouched(self):
        self.sync()
        after = self.read_settings()
        self.assertEqual(after["permissions"], self.original["permissions"])
        self.assertEqual(after["env"], self.original["env"])

    def test_a_projects_own_hook_is_preserved(self):
        self.sync()
        commands = [h["command"]
                    for e in self.read_settings()["hooks"]["PreToolUse"]
                    for h in e["hooks"]]
        self.assertIn("./scripts/my-own-hook.sh", commands)
        self.assertIn("./.claude/hooks/deny-dangerous-commands.sh", commands)

    def test_an_already_registered_hook_is_not_duplicated(self):
        self.sync()
        before = self.read_settings()
        added = self.sync()
        self.assertEqual(added, [])
        self.assertEqual(self.read_settings(), before)

    def test_a_customised_registration_of_our_hook_is_left_alone(self):
        """Registered with a different matcher/ordering still counts as
        registered — we check the (event, command) pair, not the shape."""
        self.original["hooks"]["Stop"] = [{
            "matcher": "something-custom",
            "hooks": [{"type": "command",
                       "command": "./.claude/hooks/require-verdict.sh"}],
        }]
        self.write_settings(self.original)
        added = self.sync()
        self.assertEqual([a for a in added if a[0] == "Stop"], [])
        self.assertEqual(self.read_settings()["hooks"]["Stop"],
                         self.original["hooks"]["Stop"])


class FailsSafe(SyncFixture):
    def test_malformed_settings_never_fails_the_upgrade(self):
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.sync(), [])
        # And it did not clobber the file it could not parse.
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_absent_settings_is_a_no_op(self):
        self.assertEqual(enrich.sync_hook_registrations(self.tmp / "nowhere", quiet=True), [])


class EndToEndUpgrade(unittest.TestCase):
    """The claim that matters: a project enriched before this release actually
    ends up with a firing hook after `phasekit upgrade`."""

    def test_upgrade_installs_the_file_and_registers_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run([sys.executable, str(ENRICH), str(project)],
                           capture_output=True, text=True, check=True)

            # Simulate a project provisioned before the Stop hook existed.
            settings_path = project / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["hooks"].pop("Stop", None)
            settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            (project / ".claude" / "hooks" / "require-verdict.sh").unlink()

            r = subprocess.run(
                [sys.executable, str(ENRICH), "--upgrade", str(project), "--yes"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            self.assertTrue((project / ".claude" / "hooks" / "require-verdict.sh").is_file(),
                            "hook file not installed")
            after = json.loads(settings_path.read_text(encoding="utf-8"))
            commands = [h["command"] for e in after["hooks"].get("Stop", [])
                        for h in e["hooks"]]
            self.assertIn("./.claude/hooks/require-verdict.sh", commands,
                          "hook installed but never registered — it would never fire")


if __name__ == "__main__":
    unittest.main()
