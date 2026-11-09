"""v0.29.0 (audit A3): the prompt is two parts, the guard probe runs once per
plugin + CLI pair, and the loop names oversized documents once per session.

Pinned here:
  * CONTINUE_PROMPT.txt carries exactly one `=== OPERATING RULES` marker line;
    above it the per-turn orientation (five read targets, none of the engine's
    process docs, no CLAUDE.md the harness loads itself), below it the rules;
  * run-phase.sh sends the orientation as the prompt (`-p`) and the rules as
    `--append-system-prompt`, byte for byte; the session clock line joins the
    prompt, never the rules; a prompt file without the marker is sent whole;
  * rewrite_engine_doc_paths rewrites engine doc paths the project lacks and
    leaves the project's own docs alone (both parts use it);
  * guard_probe_once probes on the first turn, skips a turn whose key matches,
    probes again when hooks.json, a hook script or the CLI binary changes or
    PHASEKIT_GUARD_PROBE_EVERY_TURN=1, and records nothing after a red probe;
  * docs_size_advisory names every oversized top-level doc once, skips
    LEARNINGS and archives, and is silent when disabled or nothing is over.

Run from the repo root: python3 -m unittest tests.test_prompt_split
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = REPO_ROOT / "CONTINUE_PROMPT.txt"
RUN_PHASE = REPO_ROOT / "scripts" / "run-phase.sh"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
MARKER = "=== OPERATING RULES"


def _extract(source: Path, name: str) -> str:
    text = source.read_text()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name} not found in {source.name}"
    return m.group(0)


def _split(text: str):
    lines = text.split("\n")
    idx = [i for i, ln in enumerate(lines) if ln.startswith(MARKER)]
    return idx, lines


class PromptFile(unittest.TestCase):
    def test_exactly_one_marker_line(self):
        idx, _ = _split(PROMPT.read_text())
        self.assertEqual(len(idx), 1)

    def test_orientation_reads_five_things_and_no_engine_doc(self):
        idx, lines = _split(PROMPT.read_text())
        orientation = "\n".join(lines[:idx[0]])
        for must in ("docs/PHASES.md", "docs/LEARNINGS.md", "docs/project/QUALITY_GATES.md",
                     "docs/project/RULES.md", "docs/SPEC.md"):
            self.assertIn(must, orientation, must)
        for gone in ("docs/CAPABILITY_MANIFEST.md", "capabilities/project-capabilities.yaml",
                     "docs/USAGE_PATTERNS.md", "docs/PROD_REQUIREMENTS.md", "- .claude/CLAUDE.md",
                     "Operating rules:"):
            self.assertNotIn(gone, orientation, gone)
        # the engine's gates doc is named as reference, never as a read target
        self.assertNotRegex(orientation, r"^- docs/QUALITY_GATES\.md", "")

    def test_rules_below_the_marker_carry_the_standing_rules(self):
        idx, lines = _split(PROMPT.read_text())
        rules = "\n".join(lines[idx[0] + 1:])
        for must in ("The loop owns every commit", "CLOSE OUT IN ONE ORDER", "Tests test the product.",
                     "SUPERSEDED by AC#<n>: ", "docs/acceptance.json", "BACKGROUND WORK DIES WITH YOUR TURN",
                     "Read it EARLY"):
            self.assertIn(must, rules, must)
        # the two-mode acceptance rule names both modes
        self.assertIn("LEDGER mode", rules)
        self.assertIn("APPEND-ONLY", rules)


FAKE_CLAUDE = r"""#!/usr/bin/env bash
out="${FAKE_ARGV:?}"
: > "$out"
for a in "$@"; do printf '%s\0' "$a" >> "$out"; done
echo '{"type":"system","subtype":"init","session_id":"11111111-2222-3333-4444-555555555555"}'
echo '{"type":"result","subtype":"success","is_error":false,"num_turns":1}'
exit 0
"""


class RunPhaseSplit(unittest.TestCase):
    """run-phase.sh in a vendored scratch project (no probe, no rendering)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-split-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        proj = self.tmp / "proj"
        (proj / "scripts").mkdir(parents=True)
        (proj / "artifacts").mkdir()
        shutil.copy2(RUN_PHASE, proj / "scripts" / "run-phase.sh")
        subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
        self.proj = proj
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "claude").write_text(FAKE_CLAUDE)
        os.chmod(self.bin / "claude", 0o755)
        self.argv_file = self.tmp / "argv"

    def run_phase(self, prompt_file: Path, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PHASEKIT_", "CLAUDE_"))}
        env.update({"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "FAKE_ARGV": str(self.argv_file)})
        env.update(extra)
        r = subprocess.run(["bash", str(self.proj / "scripts" / "run-phase.sh"), str(prompt_file)],
                           cwd=str(self.proj), capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.argv_file.read_bytes().split(b"\0")[:-1]
        argv = [a.decode() for a in argv]
        return argv

    @staticmethod
    def _flag(argv, flag):
        return argv[argv.index(flag) + 1]

    def test_orientation_is_the_prompt_and_rules_the_system_prompt(self):
        argv = self.run_phase(PROMPT)
        idx, lines = _split(PROMPT.read_text())
        orientation = "\n".join(lines[:idx[0]])
        rules = "\n".join(lines[idx[0] + 1:])
        self.assertEqual(self._flag(argv, "-p").rstrip("\n"), orientation.rstrip("\n"))
        self.assertEqual(self._flag(argv, "--append-system-prompt").rstrip("\n"), rules.rstrip("\n"))
        self.assertNotIn(MARKER, self._flag(argv, "-p"))
        self.assertNotIn(MARKER, self._flag(argv, "--append-system-prompt"))

    def test_the_clock_joins_the_prompt_never_the_rules(self):
        deadline = str(int(time.time()) + 1800)
        argv = self.run_phase(PROMPT, PHASEKIT_SESSION_DEADLINE=deadline)
        self.assertIn("Session clock:", self._flag(argv, "-p"))
        self.assertNotIn("Session clock:", self._flag(argv, "--append-system-prompt"))

    def test_a_prompt_file_without_the_marker_is_sent_whole(self):
        custom = self.tmp / "custom.txt"
        custom.write_text("Do the thing.\nThen stop.\n")
        argv = self.run_phase(custom)
        self.assertEqual(self._flag(argv, "-p"), "Do the thing.\nThen stop.")
        self.assertNotIn("--append-system-prompt", argv)


class RewriteEngineDocPaths(unittest.TestCase):
    def test_only_engine_docs_the_project_lacks_are_rewritten(self):
        tmp = Path(tempfile.mkdtemp(prefix="pk-rw-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = tmp / "engine"; (engine / "docs").mkdir(parents=True)
        (engine / "docs" / "QUALITY_GATES.md").write_text("g\n")
        (engine / "docs" / "USAGE_PATTERNS.md").write_text("u\n")
        proj = tmp / "proj"; (proj / "docs").mkdir(parents=True)
        (proj / "docs" / "USAGE_PATTERNS.md").write_text("mine\n")
        script = (f'ENGINE_DIR="{engine}"; ROOT_DIR="{proj}"; '
                  'ENGINE_PROCESS_DOCS="QUALITY_GATES USAGE_PATTERNS"\n'
                  + _extract(RUN_PHASE, "rewrite_engine_doc_paths")
                  + '\nrewrite_engine_doc_paths "see docs/QUALITY_GATES.md and docs/USAGE_PATTERNS.md and docs/SPEC.md"')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"see {engine}/docs/QUALITY_GATES.md and docs/USAGE_PATTERNS.md and docs/SPEC.md")


class GuardProbeOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-probe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        (self.plugin / "hooks" / "scripts").mkdir(parents=True)
        (self.plugin / "hooks" / "hooks.json").write_text("{}\n")
        (self.plugin / "hooks" / "run-hook.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.plugin / ".claude-plugin").mkdir()
        (self.plugin / ".claude-plugin" / "plugin.json").write_text("{}\n")
        for h in ("deny-dangerous-commands", "require-verdict", "wrapup-nudge"):
            (self.plugin / "hooks" / "scripts" / f"{h}.sh").write_text("#!/bin/sh\nexit 0\n")
        self.logs = self.tmp / "logs"; self.logs.mkdir()
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "claude").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin / "claude", 0o755)
        self.count = self.tmp / "count"

    def run_once(self, rc=0, **env):
        # the real guard_probe is replaced by a counter that returns RC
        script = (f'PLUGIN_DIR="{self.plugin}"; LOG_DIR="{self.logs}"\n'
                  f'guard_probe() {{ echo p >> "{self.count}"; return {rc}; }}\n'
                  f'GUARD_PROBE_MARK="$LOG_DIR/.guard-probe-ok"\n'
                  + _extract(RUN_PHASE, "guard_probe_key") + _extract(RUN_PHASE, "guard_probe_once")
                  + "\nguard_probe_once")
        e = {k: v for k, v in os.environ.items() if not k.startswith("PHASEKIT_")}
        e["PATH"] = str(self.bin) + os.pathsep + os.environ["PATH"]
        e.update(env)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
        return r.returncode

    def probes(self):
        return len(self.count.read_text().split()) if self.count.exists() else 0

    def test_first_turn_probes_and_the_next_skips(self):
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.probes(), 1)
        self.assertTrue((self.logs / ".guard-probe-ok").is_file())
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.probes(), 1)

    def test_a_changed_pair_probes_again(self):
        self.run_once(); self.run_once()
        self.assertEqual(self.probes(), 1)
        hj = self.plugin / "hooks" / "hooks.json"
        hj.write_text('{"hooks": {}}\n')      # size changes
        self.run_once()
        self.assertEqual(self.probes(), 2)
        self.run_once()
        self.assertEqual(self.probes(), 2)
        hook = self.plugin / "hooks" / "scripts" / "wrapup-nudge.sh"
        hook.write_text("#!/bin/sh\n# changed\nexit 0\n")
        self.run_once()
        self.assertEqual(self.probes(), 3)
        cli = self.bin / "claude"
        cli.write_text("#!/bin/sh\n# newer cli\nexit 0\n")
        self.run_once()
        self.assertEqual(self.probes(), 4)
        (self.plugin / "hooks" / "run-hook.sh").write_text("#!/bin/sh\n# changed dispatcher\nexit 0\n")
        self.run_once()
        self.assertEqual(self.probes(), 5)

    def test_every_turn_flag_probes_every_turn(self):
        self.run_once(PHASEKIT_GUARD_PROBE_EVERY_TURN="1")
        self.run_once(PHASEKIT_GUARD_PROBE_EVERY_TURN="1")
        self.assertEqual(self.probes(), 2)

    def test_a_red_probe_records_nothing_and_fails(self):
        self.assertEqual(self.run_once(rc=1), 1)
        self.assertFalse((self.logs / ".guard-probe-ok").exists())
        # and the next turn probes again (no stale green)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.probes(), 2)

    def test_the_pinned_path_calls_the_once_wrapper_and_the_key_names_every_proved_file(self):
        text = RUN_PHASE.read_text()
        self.assertIn('if ! guard_probe_once; then', text)
        # the raw probe is called only by the wrapper
        calls = [ln for ln in text.splitlines() if re.search(r"(^|[;&|\s])guard_probe(\s|$|\|\|)", ln)
                 and "guard_probe()" not in ln and "guard_probe_once" not in ln and "guard_probe_key" not in ln
                 and not ln.strip().startswith("#")]
        self.assertEqual(len(calls), 1, calls)
        key = _extract(RUN_PHASE, "guard_probe_key")
        for name in ("hooks/hooks.json", "hooks/run-hook.sh", ".claude-plugin/plugin.json",
                     "deny-dangerous-commands", "require-verdict", "wrapup-nudge"):
            self.assertIn(name, key, name)

    def test_the_marker_lives_in_the_logs_dir(self):
        self.run_once()
        key = (self.logs / ".guard-probe-ok").read_text().strip()
        self.assertTrue(key.startswith(str(self.plugin) + "|"), key)


class DocsSizeAdvisory(unittest.TestCase):
    def run_advisory(self, root: Path, **env):
        script = (f'ROOT_DIR="{root}"\n' + _extract(LOOP, "docs_size_advisory") + "\ndocs_size_advisory")
        e = {k: v for k, v in os.environ.items() if not k.startswith("PHASEKIT_")}
        e.update(env)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stderr

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-docs-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "docs" / "archive").mkdir(parents=True)
        big = "x" * (50 * 1024)
        (self.root / "docs" / "SPEC.md").write_text(big)
        (self.root / "docs" / "LEARNINGS.md").write_text(big)
        (self.root / "docs" / "LEARNINGS-ARCHIVE.md").write_text(big)
        (self.root / "docs" / "phases-archive.md").write_text(big)
        (self.root / "docs" / "archive" / "SPEC-2026-11-09.md").write_text(big)
        (self.root / "docs" / "PHASES.md").write_text("small\n")

    def test_names_only_the_oversized_live_documents(self):
        err = self.run_advisory(self.root)
        self.assertIn("docs/SPEC.md 50 KB", err)
        self.assertEqual(err.count("run-until-done: note"), 1)
        for skipped in ("LEARNINGS", "archive", "PHASES.md"):
            self.assertNotIn(skipped, err, skipped)

    def test_silent_when_nothing_is_over_or_disabled(self):
        self.assertEqual(self.run_advisory(self.root, PHASEKIT_DOC_WARN_KB="100"), "")
        self.assertEqual(self.run_advisory(self.root, PHASEKIT_DOC_WARN_KB="abc"), "")
        (self.root / "docs" / "SPEC.md").write_text("small\n")
        self.assertEqual(self.run_advisory(self.root), "")


if __name__ == "__main__":
    unittest.main()
