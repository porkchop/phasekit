#!/usr/bin/env python3
"""Tests for v0.7.0 cross-project contracts — phase 2: mount plumbing.

Acceptance:
  - with a provider, /contracts is present and readable in-container
  - with none, the container starts EXACTLY as today

The second half is tested the only way that actually proves it: run the real
container-setup.sh against a stub `docker`, capture the composed argv with and
without a provider, and assert the difference is precisely the two contracts
arguments and nothing else. A structural grep would pass for a script that
also reordered or dropped an unrelated flag.

The provider manifest is honoured including the zero-entry case: "no
dependencies" is a manifest asserting none, never an empty directory, because
an empty directory is indistinguishable from a broken bind mount.

Run from the repo root: `python3 -m unittest tests.test_contracts_mount_v070`
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "phasekit-contracts.py"
CONTAINER_SCRIPT = REPO_ROOT / "scripts" / "container-setup.sh"

try:  # discover-style (CI puts tests/ on sys.path) and package-style both work
    from test_contracts_v070 import _load_module
except ImportError:  # pragma: no cover
    from tests.test_contracts_v070 import _load_module

contracts = _load_module()

HAVE_BASH = shutil.which("bash") is not None

# A stub `docker` that records the argv of every invocation and succeeds.
# `build` and `volume inspect` must both succeed so container-setup reaches
# `docker run`, which is the call we actually inspect.
STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
exit 0
"""


def _write_provider(root: Path, entries) -> Path:
    """Materialize a provider mount: index.json plus one dir per entry."""
    root.mkdir(parents=True, exist_ok=True)
    (root / contracts.INDEX_FILENAME).write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2), encoding="utf-8"
    )
    for entry in entries:
        slug = entry if isinstance(entry, str) else entry["slug"]
        rel = slug if isinstance(entry, str) else entry.get("path", slug)
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / "openapi.json").write_text('{"openapi": "3.1.0"}', encoding="utf-8")
    return root


class ProviderIndexTest(unittest.TestCase):
    def test_absent_mount_is_not_an_error(self):
        """No provider is the ordinary standalone case, never a failure."""
        with tempfile.TemporaryDirectory() as tmp:
            index = contracts.load_provider_index(Path(tmp) / "nothing-here")
        self.assertFalse(index.present)
        self.assertEqual(index.entries, ())

    def test_empty_directory_is_reported_as_no_provider(self):
        """An empty dir must NOT read as 'a provider asserting zero deps'."""
        with tempfile.TemporaryDirectory() as tmp:
            index = contracts.load_provider_index(Path(tmp))
        self.assertFalse(index.present)

    def test_zero_entry_manifest_is_a_present_provider(self):
        """The positive statement: 'I checked; there are none.'"""
        with tempfile.TemporaryDirectory() as tmp:
            index = contracts.load_provider_index(_write_provider(Path(tmp), []))
        self.assertTrue(index.present)
        self.assertEqual(index.entries, ())
        self.assertEqual(index.slugs(), ())

    def test_zero_entry_and_empty_dir_are_distinguishable(self):
        """The whole point of shipping a manifest — pinned as one assertion."""
        with tempfile.TemporaryDirectory() as tmp:
            empty = contracts.load_provider_index(Path(tmp) / "empty")
            (Path(tmp) / "empty").mkdir()
            asserted = contracts.load_provider_index(_write_provider(Path(tmp) / "asserted", []))
        self.assertNotEqual(empty.present, asserted.present)

    def test_entries_resolve_to_readable_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _write_provider(Path(tmp), ["foundry-orchestrator"])
            index = contracts.load_provider_index(mount)
            self.assertTrue(index.present)
            self.assertEqual(index.slugs(), ("foundry-orchestrator",))
            entry = index.get("foundry-orchestrator")
            self.assertEqual(entry.rel_path, "foundry-orchestrator")
            self.assertTrue(entry.source_dir(mount).is_dir())
            self.assertIsNone(index.get("not-declared"))

    def test_entry_path_may_differ_from_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _write_provider(
                Path(tmp), [{"slug": "api", "path": "nested/api-svc"}]
            )
            index = contracts.load_provider_index(mount)
            self.assertEqual(index.get("api").rel_path, "nested/api-svc")
            self.assertTrue(index.get("api").source_dir(mount).is_dir())

    def test_unknown_keys_are_tolerated_across_the_repo_boundary(self):
        """index.json is a separate producer's format; additive fields must
        not break a consumer built before they existed."""
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / contracts.INDEX_FILENAME).write_text(json.dumps({
                "version": 1,
                "generated_at": "2026-08-16T00:00:00Z",
                "provider": "foundry-orchestrator",
                "some_future_field": {"a": 1},
                "entries": [{"slug": "api", "sha256": "deadbeef", "future": True}],
            }), encoding="utf-8")
            index = contracts.load_provider_index(mount)
        self.assertEqual(index.slugs(), ("api",))

    # --- a mounted-but-broken manifest is an error, never "no dependencies" ---

    def _err(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / contracts.INDEX_FILENAME).write_text(payload, encoding="utf-8")
            with self.assertRaises(contracts.ContractsError) as ctx:
                contracts.load_provider_index(mount)
        return str(ctx.exception)

    def test_invalid_json_is_an_error(self):
        self.assertIn("unreadable provider manifest", self._err("{not json"))

    def test_non_object_manifest_is_an_error(self):
        self.assertIn("must be a JSON object", self._err("[]"))

    def test_non_list_entries_is_an_error(self):
        self.assertIn("`entries` must be a list", self._err('{"entries": "api"}'))

    def test_bad_slug_is_an_error(self):
        self.assertIn("invalid `slug`", self._err('{"entries": [{"slug": "a/b"}]}'))

    def test_duplicate_entry_slug_is_an_error(self):
        self.assertIn("duplicate", self._err('{"entries": ["api", "api"]}'))

    def test_escaping_entry_path_is_an_error(self):
        self.assertIn(
            "must stay inside the mount",
            self._err('{"entries": [{"slug": "api", "path": "../../etc"}]}'),
        )


class ProviderCliTest(unittest.TestCase):
    def _run(self, mount, *args):
        env = dict(os.environ)
        env[contracts.MOUNT_DIR_ENV] = str(mount)
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "provider", *args],
            capture_output=True, text=True, env=env,
        )

    def test_reports_no_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(Path(tmp) / "absent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no provider mounted", proc.stdout)

    def test_reports_a_zero_entry_provider_distinctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(_write_provider(Path(tmp), []))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("asserting 0 available contracts", proc.stdout)

    def test_reports_available_contracts_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _write_provider(Path(tmp), ["foundry-orchestrator"])
            proc = self._run(mount, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["provider_present"])
        self.assertEqual(payload["entries"], [
            {"slug": "foundry-orchestrator", "path": "foundry-orchestrator", "readable": True}
        ])


class ContainerSetupStructuralTest(unittest.TestCase):
    def setUp(self):
        self.text = CONTAINER_SCRIPT.read_text(encoding="utf-8")

    def test_parses(self):
        r = subprocess.run(["bash", "-n", str(CONTAINER_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_container_side_mount_path_agrees_with_the_python_side(self):
        """The path is named in bash and in Python; they must not drift.

        Pinned rather than abstracted — there is no cheap way to share a
        constant across the bash/python boundary, so the invariant is enforced
        by this assertion instead of by intention.
        """
        self.assertIn(
            f'CONTRACTS_CONTAINER_DIR="{contracts.DEFAULT_MOUNT_DIR}"', self.text
        )
        # And exactly one definition of it, so a second copy can't rot.
        self.assertEqual(self.text.count("CONTRACTS_CONTAINER_DIR="), 1)

    def test_mount_is_read_only(self):
        self.assertIn('"$CONTRACTS_CONTAINER_DIR":ro', self.text)

    def test_announces_the_path_to_in_container_tooling(self):
        self.assertIn('-e PHASEKIT_CONTRACTS_DIR="$CONTRACTS_CONTAINER_DIR"', self.text)


@unittest.skipUnless(HAVE_BASH, "bash required")
class ContainerSetupArgvTest(unittest.TestCase):
    """Functional: run the real script against a stub docker and read argv."""

    def _docker_run_argv(self, extra_env):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            stub = bindir / "docker"
            stub.write_text(STUB_DOCKER, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            log = tmp / "docker-args.log"

            env = dict(os.environ)
            env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
            env["DOCKER_ARGS_LOG"] = str(log)
            # Keep the argv deterministic: drop host-dependent inputs the
            # script forwards conditionally.
            for var in ("ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN",
                        "SSH_AUTH_SOCK", "MAX_ITERATIONS", "ANTHROPIC_MODEL",
                        "PHASEKIT_CONTRACTS_MOUNT", "PHASEKIT_ITERATION_MODE",
                        "PHASEKIT_SESSION_DEADLINE", "PHASEKIT_CONTAINER_NAME",
                        "PHASEKIT_TRACE", "CLAUDE_MODE", "AUTO_PUSH",
                        "PHASEKIT_ITER_RETRY", "SKIP_PLAYWRIGHT_MCP"):
                env.pop(var, None)
            env["HOME"] = str(tmp / "home")  # no known_hosts -> no extra mount
            (tmp / "home").mkdir()
            env.update(extra_env)

            proc = subprocess.run(
                ["bash", str(CONTAINER_SCRIPT), "shell"],
                capture_output=True, text=True, env=env,
            )
            calls = [
                block.strip().splitlines()
                for block in log.read_text(encoding="utf-8").split("---\n")
                if block.strip()
            ] if log.exists() else []
            return proc, [c for c in calls if c and c[0] == "run"]

    def _baseline_argv(self):
        proc, runs = self._docker_run_argv({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        self.assertNotIn("/contracts", " ".join(argv))
        self.assertNotIn("PHASEKIT_CONTRACTS_DIR", " ".join(argv))
        return argv

    def test_no_provider_composes_exactly_the_v066_argv(self):
        self._baseline_argv()

    def test_provider_adds_exactly_the_mount_and_the_env_var(self):
        baseline = self._baseline_argv()
        with tempfile.TemporaryDirectory() as tmp:
            mount = _write_provider(Path(tmp), ["foundry-orchestrator"])
            proc, runs = self._docker_run_argv({"PHASEKIT_CONTRACTS_MOUNT": str(mount)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(runs), 1)
            argv = runs[0]

            # Read-only bind mount at the fixed container path.
            self.assertIn(f"{mount}:/contracts:ro", argv)
            self.assertIn("PHASEKIT_CONTRACTS_DIR=/contracts", argv)

            # And NOTHING else changed. This is the "starts exactly as today"
            # guarantee, stated as a set difference rather than a vibe.
            added = [a for a in argv if a not in baseline]
            self.assertEqual(
                sorted(added),
                sorted([f"{mount}:/contracts:ro", "PHASEKIT_CONTRACTS_DIR=/contracts"]),
            )
            self.assertEqual([a for a in baseline if a not in argv], [])

    def test_a_missing_mount_path_fails_loudly_before_spending_tokens(self):
        """A provider passing a path we silently drop IS the META_REPO_PATH bug."""
        proc, runs = self._docker_run_argv(
            {"PHASEKIT_CONTRACTS_MOUNT": "/nonexistent/contracts"}
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a directory", proc.stderr)
        self.assertEqual(runs, [])

    def test_a_mount_without_index_json_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, runs = self._docker_run_argv({"PHASEKIT_CONTRACTS_MOUNT": tmp})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no readable index.json", proc.stderr)
        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
