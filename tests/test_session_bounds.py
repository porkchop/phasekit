"""The session container is bounded (v0.21.2).

v0.21.1 bounded every runner container; the session container itself still ran
with no --memory, and on 2026-10-28 a test run inside one took about 25 GB plus
all host swap. container-setup.sh now passes --memory, --memory-swap (the same
value) and --pids-limit, from PHASEKIT_SESSION_MEMORY and
PHASEKIT_SESSION_PIDS_LIMIT, and names the memory bound when the session exits
137.

Same harness as test_forward_env: the real script against a stub `docker`
that records argv.
"""
from __future__ import annotations
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_SCRIPT = REPO_ROOT / "scripts" / "container-setup.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"

# The session's run is the one that mounts the project at /workspace; only it
# exits with FAKE_SESSION_RC.
STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
for a in "$@"; do
  case "$a" in *:/workspace) exit "${FAKE_SESSION_RC:-0}" ;; esac
done
exit 0
"""


class SessionBoundsTest(unittest.TestCase):
    def _run(self, extra_env=None):
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
            for var in ("ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
                        "MAX_ITERATIONS", "PHASEKIT_CONTRACTS_MOUNT", "PHASEKIT_CONTAINER_NAME",
                        "PHASEKIT_TRACE", "PHASEKIT_FORWARD_ENV", "PHASEKIT_SESSION_MEMORY",
                        "PHASEKIT_SESSION_PIDS_LIMIT", "FAKE_SESSION_RC"):
                env.pop(var, None)
            env["HOME"] = str(tmp / "home")
            (tmp / "home").mkdir()
            env.update(extra_env or {})
            proc = subprocess.run(["bash", str(CONTAINER_SCRIPT), "shell"],
                                  capture_output=True, text=True, env=env)
            calls = [block.strip().splitlines()
                     for block in log.read_text(encoding="utf-8").split("---\n")
                     if block.strip()] if log.exists() else []
            runs = [c for c in calls
                    if c and c[0] == "run" and any(a.endswith(":/workspace") for a in c)]
            self.assertEqual(len(runs), 1, proc.stdout + proc.stderr)
            return proc, runs[0]

    def bounds(self, extra_env=None):
        proc, argv = self._run(extra_env)
        image = argv.index("scaffold-runner")
        for flag in ("--memory", "--memory-swap", "--pids-limit"):
            self.assertLess(argv.index(flag), image, f"{flag} is a docker flag, before the image")
        return (argv[argv.index("--memory") + 1], argv[argv.index("--memory-swap") + 1],
                argv[argv.index("--pids-limit") + 1], proc.stderr)

    def test_default_bounds(self):
        mem, swap, pids, err = self.bounds()
        self.assertEqual((mem, swap, pids), ("10g", "10g", "8192"))
        self.assertNotIn("PHASEKIT_SESSION", err)

    def test_the_environment_sets_them_and_swap_follows_memory(self):
        mem, swap, pids, _ = self.bounds({"PHASEKIT_SESSION_MEMORY": "6g",
                                          "PHASEKIT_SESSION_PIDS_LIMIT": "512"})
        self.assertEqual((mem, swap, pids), ("6g", "6g", "512"))

    def test_dockers_own_size_forms_are_accepted(self):
        for good in ("16gb", "1.5g", "512M", "1t", "8589934592", " 10g "):
            with self.subTest(mem=good):
                mem, swap, _, err = self.bounds({"PHASEKIT_SESSION_MEMORY": good})
                self.assertEqual(mem, good.strip())
                self.assertEqual(swap, good.strip())
                self.assertNotIn("PHASEKIT_SESSION_MEMORY", err)

    def test_a_leading_zero_pid_limit_is_read_as_decimal(self):
        for raw, want in (("0100", "100"), ("08", "8"), ("999999999", "999999999"),
                          ("0000000000001", "1")):
            with self.subTest(raw=raw):
                _, _, pids, err = self.bounds({"PHASEKIT_SESSION_PIDS_LIMIT": raw})
                self.assertEqual(pids, want)
                self.assertNotIn("PHASEKIT_SESSION_PIDS_LIMIT", err)

    def test_the_smallest_size_docker_accepts_is_accepted(self):
        for good in ("6m", "6291456", "0.006t"):
            with self.subTest(mem=good):
                mem, _, _, err = self.bounds({"PHASEKIT_SESSION_MEMORY": good})
                self.assertEqual(mem, good)
                self.assertNotIn("PHASEKIT_SESSION_MEMORY", err)

    def test_a_bad_value_is_the_default_never_unbounded(self):
        for bad_mem, bad_pids in (("0", "0"), ("-1", "-1"), ("lots", "many"),
                                  ("0.0g", "4096.5"), ("8 g", "00"), ("+8g", "-0"),
                                  ("٨g", "0x10"), ("12g;rm", "1e3"),
                                  # review r1: docker truncates the first three to 0
                                  # bytes (no bound) or refuses sizes under its 6 MiB
                                  # minimum; the pid limits wrap to 0 or below in
                                  # bash's 64-bit arithmetic, or exceed 9 digits
                                  ("0.5", "18446744073709551616"),
                                  ("0.9b", "18446744073709551615"),
                                  ("0.0000001g", "9223372036854775808"),
                                  ("1m", "36893488147419103232"),
                                  ("99999999999999999999p", "1000000000"),
                                  ("9" * 400, "0" * 30)):
            with self.subTest(mem=bad_mem, pids=bad_pids):
                mem, swap, pids, err = self.bounds({"PHASEKIT_SESSION_MEMORY": bad_mem,
                                                    "PHASEKIT_SESSION_PIDS_LIMIT": bad_pids})
                self.assertEqual((mem, swap, pids), ("10g", "10g", "8192"))
                self.assertIn("PHASEKIT_SESSION_MEMORY", err)
                self.assertIn("PHASEKIT_SESSION_PIDS_LIMIT", err)

    def test_a_killed_session_names_the_memory_bound_and_keeps_its_exit_code(self):
        proc, _ = self._run({"FAKE_SESSION_RC": "137", "PHASEKIT_SESSION_MEMORY": "6g"})
        self.assertEqual(proc.returncode, 137)
        self.assertIn("SIGKILL", proc.stderr)
        self.assertIn("memory bound (6g, PHASEKIT_SESSION_MEMORY)", proc.stderr)
        # review r1: PID 1 is the loop, which a cgroup OOM spares, so the outside
        # kill (a supervisor's timeout) is named first
        self.assertLess(proc.stderr.index("outside docker kill"), proc.stderr.index("memory bound"))
        self.assertNotIn("PIDS_LIMIT", proc.stderr, "the pid bound never SIGKILLs")

    def test_any_other_exit_code_passes_through_unannotated(self):
        for rc in ("0", "1", "3"):
            with self.subTest(rc=rc):
                proc, _ = self._run({"FAKE_SESSION_RC": rc})
                self.assertEqual(proc.returncode, int(rc))
                self.assertNotIn("SIGKILL", proc.stderr)

    def test_the_bounds_are_declared(self):
        env = {v["name"]: v for v in json.loads(MANIFEST.read_text())["env"]}
        self.assertEqual(env["PHASEKIT_SESSION_MEMORY"]["default"], "10g")
        self.assertEqual(env["PHASEKIT_SESSION_PIDS_LIMIT"]["default"], "8192")
        for name in ("PHASEKIT_SESSION_MEMORY", "PHASEKIT_SESSION_PIDS_LIMIT"):
            self.assertTrue(env[name]["container_forwarded"])  # the manifest's flag: handled by container-setup.sh
            self.assertEqual(env[name]["read_by"], ["scripts/container-setup.sh"])


if __name__ == "__main__":
    unittest.main()
