#!/usr/bin/env python3
"""Tests for v0.7.0 cross-project contracts — phase 4: awareness + packaging.

A mount nobody is told about goes unread — half of why META_REPO_PATH dangled
for months — so the session prompt must name the declared dependencies and
state that the contract is authoritative and guessing is forbidden.

Also pins the packaging half, which is where phasekit silently breaks:
docs/CONTRACTS.md registered and profiled, the checker installed downstream,
and the seeded consumer check identical across every stack template (five
copies that could rot independently otherwise).

Run from the repo root:
`python3 -m unittest tests.test_contracts_awareness_v070`
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "phasekit-contracts.py"
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
CAPS = REPO_ROOT / "capabilities" / "project-capabilities.yaml"

try:
    from test_contracts_v070 import _load_module
except ImportError:  # pragma: no cover
    from tests.test_contracts_v070 import _load_module
try:
    from test_run_until_done_v060 import LoopHarness, VERIFY_OK
except ImportError:  # pragma: no cover
    from tests.test_run_until_done_v060 import LoopHarness, VERIFY_OK

contracts = _load_module()

STACK_TEMPLATES = ("", ".python-uv", ".static-web", ".game-canvas", ".docs-only")

NOOP_SCENARIO = "true\n"


class PromptAwarenessTest(LoopHarness):
    """Drive the real loop and read the prompt the stub actually received."""

    def setUp(self):
        super().setUp()
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        shutil.copy2(SCRIPT_PATH, os.path.join(self.repo, "scripts", "phasekit-contracts.py"))
        self.mount = Path(self.tmp) / "mount"
        (self.mount / "billing-api").mkdir(parents=True)
        (self.mount / "billing-api" / "openapi.json").write_text("{}\n", encoding="utf-8")
        (self.mount / contracts.INDEX_FILENAME).write_text(
            json.dumps({"version": 1, "entries": [{"slug": "billing-api"}]}), encoding="utf-8"
        )

    def _declare_and_vendor(self, body="version: 1\ndepends_on:\n  - billing-api\n"):
        self._write("contracts.yaml", body)
        dest = Path(self.repo) / "vendor" / "contracts" / "billing-api"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.mount / "billing-api", dest)

    def _first_prompt(self, env=None):
        run_env = {"MAX_ITERATIONS": "1", contracts.MOUNT_DIR_ENV: str(self.mount)}
        run_env.update(env or {})
        self._run_loop(NOOP_SCENARIO, env=run_env)
        return self._prompt(1)

    def test_a_repo_declaring_nothing_gets_the_v066_prompt_byte_for_byte(self):
        prompt = self._first_prompt()
        self.assertEqual(prompt, "standard continue prompt\n")

    def test_a_declaring_repo_is_told_the_contract_is_authoritative(self):
        self._declare_and_vendor()
        prompt = self._first_prompt()
        self.assertIn("CROSS-PROJECT CONTRACTS", prompt)
        self.assertIn("AUTHORITATIVE", prompt)
        self.assertIn("GUESSING IS FORBIDDEN", prompt)
        # The dependency is NAMED, with the path its copy lives at — a prompt
        # that only said "you have dependencies" would not be actionable.
        self.assertIn("billing-api", prompt)
        self.assertIn("vendor/contracts/billing-api", prompt)
        # And the original prompt is still there, after the block.
        self.assertIn("standard continue prompt", prompt)

    def test_the_prompt_forbids_editing_the_vendored_copy_and_names_the_fix(self):
        self._declare_and_vendor()
        prompt = self._first_prompt()
        self.assertIn("Do NOT edit a vendored contract", prompt)
        self.assertIn("python3 scripts/phasekit-contracts.py refresh", prompt)

    def test_a_zero_entry_declaration_adds_nothing_to_the_prompt(self):
        """Declaring none is declaring nothing; do not spend prompt on it."""
        self._write("contracts.yaml", "version: 1\ndepends_on: []\n")
        self.assertEqual(self._first_prompt(), "standard continue prompt\n")

    def test_a_malformed_declaration_does_not_wedge_the_prompt(self):
        """The gate is where a bad declaration must be loud. Prompt
        composition failing open keeps the session reaching that gate — where
        the error is reported properly — instead of dying before it starts."""
        self._write("contracts.yaml", "version: 99\n")
        self.assertEqual(self._first_prompt(), "standard continue prompt\n")

    def test_light_mode_also_gets_the_contracts_block(self):
        """A light task is exactly as capable of guessing a field name."""
        self._declare_and_vendor()
        prompt = self._first_prompt(env={"PHASEKIT_ITERATION_MODE": "light"})
        self.assertIn("PHASEKIT LIGHT MODE", prompt)
        self.assertIn("CROSS-PROJECT CONTRACTS", prompt)


class StackSeedingTest(unittest.TestCase):
    """Every stack profile's verify gate carries the consumer check."""

    def _template(self, suffix):
        return (REPO_ROOT / f"templates/phasekit-verify.template{suffix}.sh").read_text(
            encoding="utf-8"
        )

    def test_every_verify_template_seeds_the_check(self):
        for suffix in STACK_TEMPLATES:
            with self.subTest(template=suffix or "stub"):
                self.assertIn(
                    "python3 scripts/phasekit-contracts.py check", self._template(suffix)
                )

    def test_the_seeded_block_is_identical_across_templates(self):
        """Five copies that could rot independently — pinned instead."""
        blocks = {}
        for suffix in STACK_TEMPLATES:
            text = self._template(suffix)
            start = text.index("# --- Cross-project contracts (phasekit v0.7.0)")
            end = text.index("fi\n", text.index("phasekit-contracts.py check")) + 3
            blocks[suffix] = text[start:end]
        self.assertEqual(len(set(blocks.values())), 1, blocks)

    def test_the_seeded_check_is_inert_without_a_declaration(self):
        for suffix in STACK_TEMPLATES:
            with self.subTest(template=suffix or "stub"):
                self.assertIn(
                    "if [[ -f contracts.yaml && -f scripts/phasekit-contracts.py ]]; then",
                    self._template(suffix),
                )

    def test_the_check_runs_before_any_stack_specific_fail_open(self):
        """python-uv fail-opens when there is no pyproject.toml yet; several
        others exit early too. A contract violation is not something to skip
        because the stack's own preconditions are not met, so the check is
        seeded immediately after the `cd`, ahead of all of them."""
        for suffix in STACK_TEMPLATES:
            with self.subTest(template=suffix or "stub"):
                text = self._template(suffix)
                after_cd = text.index('cd "$ROOT_DIR"')
                check = text.index("phasekit-contracts.py check")
                self.assertGreater(check, after_cd)
                # Nothing may exit between the cd and the check.
                between = text[after_cd:check]
                self.assertNotIn("exit ", between)

    def test_templates_still_parse(self):
        for suffix in STACK_TEMPLATES:
            path = REPO_ROOT / f"templates/phasekit-verify.template{suffix}.sh"
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class PackagingTest(unittest.TestCase):
    """Unregistered files pass local tests and silently fail to provision."""

    def test_contracts_doc_is_registered_and_in_the_default_profile(self):
        caps = CAPS.read_text(encoding="utf-8")
        self.assertIn("docs/CONTRACTS.md", caps)
        include_docs = caps.split("include_docs:", 1)[1].split("include_hooks:", 1)[0]
        self.assertIn("- CONTRACTS", include_docs)

    def test_a_fresh_enrich_installs_the_doc_and_the_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            r = subprocess.run(
                [sys.executable, str(ENRICH), str(project)],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((project / "docs" / "CONTRACTS.md").is_file())
            self.assertTrue((project / "scripts" / "phasekit-contracts.py").is_file())
            # The installed checker runs, and is inert in a repo that declares
            # nothing — the fresh-project experience must be unchanged.
            proc = subprocess.run(
                [sys.executable, str(project / "scripts" / "phasekit-contracts.py"),
                 "--repo", str(project), "check"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout + proc.stderr, "")

    def test_the_seeded_gate_of_a_stack_profile_is_inert_in_a_fresh_project(self):
        """The seeded check must not break a project that declares nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(
                [sys.executable, str(ENRICH), str(project), "--profile", "docs-only"],
                capture_output=True, text=True, check=True,
            )
            gate = project / "scripts" / "phasekit-verify.sh"
            self.assertIn("phasekit-contracts.py check", gate.read_text(encoding="utf-8"))
            r = subprocess.run(["bash", str(gate)], capture_output=True, text=True, cwd=project)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class DocumentationTest(unittest.TestCase):
    """The documented boundary must ship, not be discovered later."""

    def setUp(self):
        raw = (REPO_ROOT / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
        self.doc = raw
        # Prose is hard-wrapped; collapse whitespace so these pins assert on
        # what the doc SAYS rather than on where the lines happen to break.
        self.flat = " ".join(raw.split())

    def test_documents_the_ci_boundary(self):
        self.assertIn("public CI can prove conformance but not freshness", self.flat)

    def test_documents_that_verify_skip_does_not_disable_the_gate(self):
        self.assertIn("PHASEKIT_CONTRACTS_SKIP", self.doc)
        self.assertIn("does **not** switch the contracts gate off", self.flat)

    def test_documents_the_standalone_constraint(self):
        self.assertIn("never the mount's absence", self.flat)

    def test_documents_every_environment_variable_the_code_reads(self):
        for var in ("PHASEKIT_CONTRACTS_MOUNT", "PHASEKIT_CONTRACTS_DIR",
                    "PHASEKIT_CONTRACTS_SKIP"):
            with self.subTest(var=var):
                self.assertIn(var, self.doc)

    def test_the_conventions_in_the_doc_match_the_code(self):
        self.assertIn(contracts.CONTRACTS_FILENAME, self.doc)
        self.assertIn(contracts.DEFAULT_VENDOR_ROOT, self.doc)
        self.assertIn(contracts.DEFAULT_MOUNT_DIR, self.doc)
        self.assertIn(contracts.INDEX_FILENAME, self.doc)

    def test_the_gate_is_cross_referenced_from_quality_gates(self):
        qg = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text(encoding="utf-8")
        self.assertIn("docs/CONTRACTS.md", qg)

    def test_the_mount_is_cross_referenced_from_containerization(self):
        cz = (REPO_ROOT / "docs" / "CONTAINERIZATION.md").read_text(encoding="utf-8")
        self.assertIn("PHASEKIT_CONTRACTS_MOUNT", cz)
        self.assertIn("docs/CONTRACTS.md", cz)


if __name__ == "__main__":
    unittest.main()
