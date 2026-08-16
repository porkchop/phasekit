#!/usr/bin/env python3
"""Tests for v0.7.0 cross-project contracts — phase 3: the gate.

Acceptance:
  - a deliberately EDITED vendored copy fails the gate
  - a deliberately STALE vendored copy fails, with the refresh command in the
    message (without that, a stale contract becomes a wedge — the roughest
    edge in this design, so it is pinned rather than trusted)
  - a repo declaring nothing is unaffected

Plus the two invariants the rest of the release rests on:
  - the ordinary test suite never needs a provider (conformance reads the
    vendored copy; only the gate reads the mount)
  - `phasekit upgrade` never erases a project's contracts.yaml — the file is
    project-owned, and an upgrade that deleted it would silently switch the
    gate off for that repo

Run from the repo root: `python3 -m unittest tests.test_contracts_gate_v070`
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

try:
    from test_contracts_v070 import _load_module
except ImportError:  # pragma: no cover
    from tests.test_contracts_v070 import _load_module

contracts = _load_module()

CONTRACT_FILES = {
    "openapi.json": '{"openapi": "3.1.0", "paths": {"/health": {}}}\n',
    "schemas/iteration.json": '{"type": "object", "required": ["iteration"]}\n',
}


class GateFixture(unittest.TestCase):
    """A consumer repo declaring one dependency, plus a provider mount."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "consumer"
        self.mount = root / "mount"
        self.slug = "foundry-orchestrator"

        # Install the checker where it actually lives downstream, so the
        # refresh command the failure message names is the repo-relative one a
        # user would really type — not an absolute path that happens to work
        # only because the test drove the source tree's copy.
        (self.repo / "scripts").mkdir(parents=True)
        self.checker = self.repo / "scripts" / "phasekit-contracts.py"
        shutil.copy2(SCRIPT_PATH, self.checker)

        (self.repo / "vendor" / "contracts").mkdir(parents=True)
        self.declare([self.slug])
        self.write_provider(CONTRACT_FILES)
        self.vendor_from_provider()

    def tearDown(self):
        self._tmp.cleanup()

    # --- fixture helpers ---

    def declare(self, slugs):
        body = "version: 1\ndepends_on:\n" + "".join(f"  - {s}\n" for s in slugs)
        (self.repo / contracts.CONTRACTS_FILENAME).write_text(body, encoding="utf-8")

    def write_provider(self, files, slug=None):
        slug = slug or self.slug
        target = self.mount / slug
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for rel, text in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.mount / contracts.INDEX_FILENAME).write_text(
            json.dumps({"version": 1, "entries": [{"slug": slug}]}), encoding="utf-8"
        )

    def vendor_from_provider(self, slug=None):
        slug = slug or self.slug
        dest = self.repo / "vendor" / "contracts" / slug
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.mount / slug, dest)

    def run_cli(self, *args, mount=..., cwd=None):
        env = dict(os.environ)
        env.pop(contracts.MOUNT_DIR_ENV, None)
        if mount is ...:
            mount = self.mount
        if mount is not None:
            env[contracts.MOUNT_DIR_ENV] = str(mount)
        return subprocess.run(
            [sys.executable, str(self.checker), "--repo", str(self.repo), *args],
            capture_output=True, text=True, env=env, cwd=cwd,
        )


class CheckPassesWhenAuthentic(GateFixture):
    def test_matching_vendored_copy_passes(self):
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_OK, proc.stderr)
        self.assertIn("match the provider", proc.stdout)

    def test_nested_files_are_compared_not_just_the_top_level(self):
        """A shallow compare would pass on a changed schema file."""
        (self.repo / "vendor" / "contracts" / self.slug / "schemas" / "iteration.json").write_text(
            '{"type": "object", "required": ["id"]}\n', encoding="utf-8"
        )
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_DRIFT)
        self.assertIn("schemas/iteration.json", proc.stderr)


class DeclaringNothingIsUnaffected(GateFixture):
    def test_no_contracts_yaml_passes_with_no_provider(self):
        (self.repo / contracts.CONTRACTS_FILENAME).unlink()
        proc = self.run_cli("check", mount=None)
        self.assertEqual(proc.returncode, contracts.EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_zero_entry_declaration_passes_with_no_provider(self):
        """Declaring `depends_on: []` is declaring nothing — no refusal."""
        (self.repo / contracts.CONTRACTS_FILENAME).write_text(
            "version: 1\ndepends_on: []\n", encoding="utf-8"
        )
        proc = self.run_cli("check", mount=None)
        self.assertEqual(proc.returncode, contracts.EXIT_OK, proc.stderr)


class RefusesWhenUnobtainable(GateFixture):
    def test_declared_but_no_provider_refuses(self):
        proc = self.run_cli("check", mount=self.mount / "nowhere")
        self.assertEqual(proc.returncode, contracts.EXIT_UNOBTAINABLE)
        self.assertIn("REFUSING", proc.stderr)
        self.assertIn("no provider is mounted", proc.stderr)
        self.assertIn(self.slug, proc.stderr)

    def test_the_refusal_explains_that_the_test_suite_does_not_need_one(self):
        """The documented boundary must ship visible, not be discovered."""
        proc = self.run_cli("check", mount=self.mount / "nowhere")
        self.assertIn("ordinary test suite does NOT need a provider", proc.stderr)
        self.assertIn(contracts.MOUNT_DIR_ENV, proc.stderr)

    def test_provider_that_does_not_offer_the_slug_refuses(self):
        (self.mount / contracts.INDEX_FILENAME).write_text(
            json.dumps({"version": 1, "entries": [{"slug": "something-else"}]}),
            encoding="utf-8",
        )
        (self.mount / "something-else").mkdir()
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_UNOBTAINABLE)
        self.assertIn("not offered by the provider", proc.stderr)
        self.assertIn("something-else", proc.stderr)

    def test_zero_entry_provider_still_refuses_a_declared_dependency(self):
        """'I checked, there are none' is a present provider — and it still
        cannot satisfy a repo that declares one."""
        (self.mount / contracts.INDEX_FILENAME).write_text(
            json.dumps({"version": 1, "entries": []}), encoding="utf-8"
        )
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_UNOBTAINABLE)
        self.assertIn("not offered by the provider", proc.stderr)

    def test_index_lists_a_slug_whose_directory_is_missing(self):
        shutil.rmtree(self.mount / self.slug)
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_UNOBTAINABLE)
        self.assertIn("broken mount", proc.stderr)


class RefusesOnDrift(GateFixture):
    def _assert_drift(self, proc):
        self.assertEqual(proc.returncode, contracts.EXIT_DRIFT, proc.stdout + proc.stderr)
        self.assertIn("DRIFT", proc.stderr)
        # The exact command, spelled out. Without this the gate is a wedge.
        self.assertIn("python3 scripts/phasekit-contracts.py refresh", proc.stderr)

    def test_a_deliberately_edited_vendored_copy_fails(self):
        """The forgery case: a consumer edits its copy to go green."""
        (self.repo / "vendor" / "contracts" / self.slug / "openapi.json").write_text(
            '{"openapi": "3.1.0", "paths": {"/health": {}, "/forged": {}}}\n',
            encoding="utf-8",
        )
        proc = self.run_cli("check")
        self._assert_drift(proc)
        self.assertIn("differing content", proc.stderr)
        self.assertIn("openapi.json", proc.stderr)

    def test_a_deliberately_stale_vendored_copy_fails(self):
        """The producer moved on and the consumer did not refresh."""
        self.write_provider(dict(CONTRACT_FILES, **{
            "openapi.json": '{"openapi": "3.1.0", "paths": {"/health": {}, "/iterations": {}}}\n',
        }))
        proc = self.run_cli("check")
        self._assert_drift(proc)

    def test_a_file_the_producer_added_is_drift(self):
        self.write_provider(dict(CONTRACT_FILES, **{"events.json": "{}\n"}))
        proc = self.run_cli("check")
        self._assert_drift(proc)
        self.assertIn("only in the provider", proc.stderr)
        self.assertIn("events.json", proc.stderr)

    def test_a_file_the_producer_deleted_is_drift(self):
        self.write_provider({"openapi.json": CONTRACT_FILES["openapi.json"]})
        proc = self.run_cli("check")
        self._assert_drift(proc)
        self.assertIn("only in the vendored copy", proc.stderr)

    def test_declared_but_never_vendored_is_drift_with_the_refresh_command(self):
        shutil.rmtree(self.repo / "vendor" / "contracts" / self.slug)
        proc = self.run_cli("check")
        self._assert_drift(proc)
        self.assertIn("declared but not vendored", proc.stderr)

    def test_a_symlink_in_a_contract_tree_is_rejected_not_skipped(self):
        target = self.repo / "vendor" / "contracts" / self.slug / "link.json"
        target.symlink_to("openapi.json")
        proc = self.run_cli("check")
        self.assertEqual(proc.returncode, contracts.EXIT_CONFIG)
        self.assertIn("regular files only", proc.stderr)


class RefreshFixesDrift(GateFixture):
    def test_refresh_makes_a_stale_copy_pass(self):
        self.write_provider(dict(CONTRACT_FILES, **{"events.json": "{}\n"}))
        self.assertEqual(self.run_cli("check").returncode, contracts.EXIT_DRIFT)

        proc = self.run_cli("refresh")
        self.assertEqual(proc.returncode, contracts.EXIT_OK, proc.stderr)
        self.assertIn("refreshed", proc.stdout)
        self.assertEqual(self.run_cli("check").returncode, contracts.EXIT_OK)

    def test_refresh_removes_files_the_producer_deleted(self):
        """Replace, don't merge — else the copy becomes a silent superset."""
        self.write_provider({"openapi.json": CONTRACT_FILES["openapi.json"]})
        self.run_cli("refresh")
        self.assertFalse(
            (self.repo / "vendor" / "contracts" / self.slug / "schemas").exists()
        )
        self.assertEqual(self.run_cli("check").returncode, contracts.EXIT_OK)

    def test_refresh_creates_a_never_vendored_copy(self):
        shutil.rmtree(self.repo / "vendor" / "contracts" / self.slug)
        self.assertEqual(self.run_cli("refresh").returncode, contracts.EXIT_OK)
        self.assertEqual(self.run_cli("check").returncode, contracts.EXIT_OK)

    def test_refresh_is_idempotent(self):
        proc = self.run_cli("refresh")
        self.assertIn("already current", proc.stdout)
        self.assertEqual(proc.returncode, contracts.EXIT_OK)

    def test_refresh_without_a_provider_refuses(self):
        proc = self.run_cli("refresh", mount=self.mount / "nowhere")
        self.assertEqual(proc.returncode, contracts.EXIT_UNOBTAINABLE)
        self.assertIn("cannot refresh", proc.stderr)

    def test_the_command_named_by_the_failure_message_is_the_one_that_works(self):
        """Pin the message against the CLI so the two cannot drift apart."""
        self.write_provider(dict(CONTRACT_FILES, **{"events.json": "{}\n"}))
        message = self.run_cli("check").stderr
        named = [
            line.split("Fix with:", 1)[1].strip()
            for line in message.splitlines() if "Fix with:" in line
        ]
        self.assertTrue(named, message)
        command = named[0]
        self.assertTrue(command.startswith("python3 "))

        # Run exactly what the message told the user to run, from the repo.
        env = dict(os.environ)
        env[contracts.MOUNT_DIR_ENV] = str(self.mount)
        args = command.split()
        args[0] = sys.executable
        proc = subprocess.run(args, capture_output=True, text=True, env=env, cwd=self.repo)
        self.assertEqual(proc.returncode, contracts.EXIT_OK, proc.stderr)
        self.assertEqual(self.run_cli("check").returncode, contracts.EXIT_OK)


class ConformanceNeverNeedsAProvider(GateFixture):
    """The lockfile pattern's whole point: `git clone && <tests>` works."""

    def test_vendored_copy_is_readable_with_no_mount_at_all(self):
        env = dict(os.environ)
        env.pop(contracts.MOUNT_DIR_ENV, None)
        proc = subprocess.run(
            [sys.executable, str(self.checker), "--repo", str(self.repo), "status", "--json"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["dependencies"][0]["vendored"])
        # And the contract's actual content is there to test against.
        self.assertTrue(
            (self.repo / "vendor" / "contracts" / self.slug / "openapi.json").is_file()
        )


class LoopWiringTest(unittest.TestCase):
    """Structural pins on run-until-done.sh's integration of the gate."""

    def setUp(self):
        self.text = LOOP_SCRIPT.read_text(encoding="utf-8")

    def _fn(self, name):
        return self.text.split(name + "() {", 1)[1].split("\n}", 1)[0]

    def test_parses(self):
        r = subprocess.run(["bash", "-n", str(LOOP_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_contracts_gate_runs_inside_the_verify_gate(self):
        self.assertIn("run_contracts_gate() {", self.text)
        self.assertIn("run_contracts_gate", self._fn("run_verify_gate"))

    def test_contracts_gate_runs_before_the_verify_skip_bypass(self):
        """VERIFY_SKIP is set routinely for red TDD commits; if it also
        switched off contract authenticity, the gate would be off exactly when
        a red gate is applying the pressure to cheat."""
        body = self._fn("run_verify_gate")
        # Compare CODE positions, not comment text — a comment naming
        # VERIFY_SKIP earlier must not be able to satisfy this.
        self.assertLess(
            body.index("if ! run_contracts_gate; then"),
            body.index('if [[ "${VERIFY_SKIP:-}" == "1" ]]; then'),
        )

    def test_contracts_gate_is_inert_without_a_declaration(self):
        self.assertIn('[[ -f "$ROOT_DIR/contracts.yaml" ]] || return 0',
                      self._fn("run_contracts_gate"))

    def test_failure_capture_is_shared_by_both_gates(self):
        """A second copy of the capture logic is how wrapup_commit silently
        lost the post-verify gates before v0.6.6."""
        self.assertIn("record_verify_failure() {", self.text)
        self.assertIn("record_verify_failure", self._fn("run_contracts_gate"))
        self.assertIn("record_verify_failure", self._fn("run_verify_gate"))
        # Exactly one writer of the failure artifact's `verify_failed` field.
        self.assertEqual(self.text.count("verify_failed: true"), 1)

    def test_a_declaration_with_no_checker_warns_rather_than_passing_quietly(self):
        body = self._fn("run_contracts_gate")
        self.assertIn("WARN: contracts.yaml is present but", body)

    def test_the_skip_hatch_is_separate_from_verify_skip_and_loud(self):
        body = self._fn("run_contracts_gate")
        self.assertIn('if [[ "${PHASEKIT_CONTRACTS_SKIP:-}" == "1" ]]; then', body)
        # The bypass announces itself on stderr; a silent one is a trapdoor.
        bypass = body.split('if [[ "${PHASEKIT_CONTRACTS_SKIP:-}" == "1" ]]; then', 1)[1]
        self.assertIn(">&2", bypass[:400])
        # VERIFY_SKIP must not be what switches contracts off.
        self.assertNotIn('"${VERIFY_SKIP:-}"', body)


try:
    from test_run_until_done_v060 import LoopHarness, VERIFY_OK
except ImportError:  # pragma: no cover
    from tests.test_run_until_done_v060 import LoopHarness, VERIFY_OK

APPROVE = (
    "echo work >> src.txt\n"
    "jq -n '{suggested_commit_message: \"phase-1: work\"}'"
    " > artifacts/phase-approval.json\n"
)


class LoopFunctionalContractsTest(LoopHarness):
    """Drive the REAL loop. Structural pins prove the code is written; only
    this proves the gate actually fires (and, more importantly, that it stays
    silent for the repos that declare nothing)."""

    def setUp(self):
        super().setUp()
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        shutil.copy2(SCRIPT_PATH, os.path.join(self.repo, "scripts", "phasekit-contracts.py"))

    def _declare(self, slug="foundry-orchestrator"):
        self._write("contracts.yaml", f"version: 1\ndepends_on:\n  - {slug}\n")

    def _mount(self, slug="foundry-orchestrator", body='{"openapi": "3.1.0"}\n'):
        mount = Path(self.tmp) / "mount"
        (mount / slug).mkdir(parents=True, exist_ok=True)
        (mount / slug / "openapi.json").write_text(body, encoding="utf-8")
        (mount / contracts.INDEX_FILENAME).write_text(
            json.dumps({"version": 1, "entries": [{"slug": slug}]}), encoding="utf-8"
        )
        return mount

    def _vendor(self, mount, slug="foundry-orchestrator"):
        dest = Path(self.repo) / "vendor" / "contracts" / slug
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(mount / slug, dest)

    def _failure_artifact(self):
        path = Path(self.repo) / "artifacts" / "phase-verify-failed.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_a_repo_declaring_nothing_never_mentions_contracts(self):
        """The v0.6.6 experience, unchanged, for every existing project."""
        r = self._run_loop(APPROVE, env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("contracts", r.stdout.lower())
        self.assertIn("phase-1: work", self._messages())
        self.assertIsNone(self._failure_artifact())

    def test_a_declared_dependency_with_no_provider_blocks_the_commit(self):
        self._declare()
        self._prepare_scenario(APPROVE)
        before = self._messages()
        r = self._run_loop(None, env={"MAX_ITERATIONS": "1"})
        self.assertEqual(before, self._messages(), "the gate did not block the commit")
        self.assertIn("REFUSING", r.stdout + r.stderr)
        artifact = self._failure_artifact()
        self.assertEqual(artifact["label"], "contracts")
        self.assertEqual(artifact["exit_code"], contracts.EXIT_UNOBTAINABLE)

    def test_drift_blocks_the_commit_and_names_the_refresh_command(self):
        mount = self._mount()
        self._declare()
        self._vendor(mount)
        (Path(self.repo) / "vendor" / "contracts" / "foundry-orchestrator"
         / "openapi.json").write_text('{"openapi": "3.1.0", "forged": true}\n', encoding="utf-8")
        self._prepare_scenario(APPROVE)
        before = self._messages()
        r = self._run_loop(None, env={
            "MAX_ITERATIONS": "1", contracts.MOUNT_DIR_ENV: str(mount)})
        self.assertEqual(before, self._messages())
        self.assertIn("DRIFT", r.stdout + r.stderr)
        artifact = self._failure_artifact()
        self.assertEqual(artifact["exit_code"], contracts.EXIT_DRIFT)
        self.assertIn("python3 scripts/phasekit-contracts.py refresh", artifact["log_tail"])

    def test_an_authentic_vendored_copy_commits_normally(self):
        mount = self._mount()
        self._declare()
        self._vendor(mount)
        r = self._run_loop(APPROVE, env={
            "MAX_ITERATIONS": "1", contracts.MOUNT_DIR_ENV: str(mount)})
        self.assertIn("phase-1: work", self._messages(), r.stdout + r.stderr)
        self.assertIsNone(self._failure_artifact())

    def test_verify_skip_does_not_switch_the_contracts_gate_off(self):
        """The load-bearing scoping decision, proven end-to-end."""
        self._declare()
        self._prepare_scenario(APPROVE)
        before = self._messages()
        self._run_loop(None, env={"MAX_ITERATIONS": "1", "VERIFY_SKIP": "1"})
        self.assertEqual(before, self._messages())
        self.assertEqual(self._failure_artifact()["label"], "contracts")

    def test_the_dedicated_hatch_does_switch_it_off(self):
        self._declare()
        r = self._run_loop(APPROVE, env={
            "MAX_ITERATIONS": "1", "PHASEKIT_CONTRACTS_SKIP": "1"})
        self.assertIn("phase-1: work", self._messages(), r.stdout + r.stderr)
        self.assertIn("bypassing the cross-project contracts gate", r.stdout + r.stderr)


class UpgradePreservesTheDeclaration(unittest.TestCase):
    """contracts.yaml is PROJECT-owned. An upgrade that erased it would
    silently switch this repo's gate off — the exact trap called out in the
    release kickoff."""

    def test_contracts_yaml_is_not_registered_as_an_installed_file(self):
        caps = (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text(encoding="utf-8")
        self.assertNotIn("contracts.yaml:", caps)
        enrich = ENRICH.read_text(encoding="utf-8")
        always = enrich.split("ALWAYS_INSTALLED_FILE_PATHS = (", 1)[1].split(")", 1)[0]
        self.assertNotIn("contracts.yaml", always)

    def test_upgrade_leaves_an_existing_contracts_yaml_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(
                [sys.executable, str(ENRICH), str(project)],
                capture_output=True, text=True, check=True,
            )
            declaration = project / contracts.CONTRACTS_FILENAME
            body = "version: 1\ndepends_on:\n  - foundry-orchestrator\n"
            declaration.write_text(body, encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(ENRICH), "--upgrade", str(project), "--yes"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(declaration.is_file(), "upgrade deleted contracts.yaml")
            self.assertEqual(declaration.read_text(encoding="utf-8"), body)
            # And the checker that reads it did get installed.
            self.assertTrue((project / "scripts" / "phasekit-contracts.py").is_file())


if __name__ == "__main__":
    unittest.main()
