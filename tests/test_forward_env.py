"""PHASEKIT_FORWARD_ENV (v0.14.3): the one door for project-specific keys.

container-setup.sh composes `docker run -e` from a fixed allowlist, so a
supervisor that placed e.g. XMEO_SEQUENCER_URL in the script's process env used
to see it dropped at the container wall (foundry-orchestrator #561's audit,
2026-09-07). Now the supervisor names the keys in PHASEKIT_FORWARD_ENV and the
script forwards exactly those that are set — names travel as a list, values
travel only in the process env, and the witness names keys only.

Same harness as test_contracts_mount_v070: the real script against a stub
`docker` that records argv.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_SCRIPT = REPO_ROOT / "scripts" / "container-setup.sh"

STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
exit 0
"""

SECRET_VALUE = "wss://example-sequencer.invalid/ws?token=" + "z" * 12


class ForwardEnvTest(unittest.TestCase):
    def _run(self, extra_env):
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
                        "MAX_ITERATIONS", "ANTHROPIC_MODEL", "PHASEKIT_CONTRACTS_MOUNT",
                        "PHASEKIT_ITERATION_MODE", "PHASEKIT_SESSION_DEADLINE",
                        "PHASEKIT_CONTAINER_NAME", "PHASEKIT_TRACE", "CLAUDE_MODE",
                        "AUTO_PUSH", "PHASEKIT_ITER_RETRY", "SKIP_PLAYWRIGHT_MCP",
                        "PHASEKIT_FORWARD_ENV", "XMEO_SEQUENCER_URL", "FOO_UNSET"):
                env.pop(var, None)
            env["HOME"] = str(tmp / "home")
            (tmp / "home").mkdir()
            env.update(extra_env)
            proc = subprocess.run(["bash", str(CONTAINER_SCRIPT), "shell"],
                                  capture_output=True, text=True, env=env)
            calls = [block.strip().splitlines()
                     for block in log.read_text(encoding="utf-8").split("---\n")
                     if block.strip()] if log.exists() else []
            runs = [c for c in calls if c and c[0] == "run"]
            self.assertEqual(len(runs), 1, proc.stdout + proc.stderr)
            return proc, runs[0]

    @staticmethod
    def _env_args(argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-e" and i + 1 < len(argv)]

    def test_unset_forwards_nothing_beyond_the_allowlist(self):
        _, argv = self._run({"XMEO_SEQUENCER_URL": SECRET_VALUE})
        envs = self._env_args(argv)
        self.assertFalse(any(e.startswith("XMEO_SEQUENCER_URL=") for e in envs), envs)
        self.assertFalse(any(e.startswith("PHASEKIT_FORWARD_ENV=") for e in envs), envs)

    def test_listed_and_set_names_cross_the_wall_and_the_witness_names_keys_only(self):
        proc, argv = self._run({
            "PHASEKIT_FORWARD_ENV": "XMEO_SEQUENCER_URL, FOO_UNSET",
            "XMEO_SEQUENCER_URL": SECRET_VALUE,
        })
        envs = self._env_args(argv)
        self.assertIn(f"XMEO_SEQUENCER_URL={SECRET_VALUE}", envs)
        self.assertFalse(any(e.startswith("FOO_UNSET") for e in envs), "an unset name must not be forwarded")
        self.assertIn("PHASEKIT_FORWARD_ENV=XMEO_SEQUENCER_URL, FOO_UNSET", envs)
        self.assertIn("container: forwarding project env: XMEO_SEQUENCER_URL", proc.stdout)
        self.assertNotIn(SECRET_VALUE, proc.stdout + proc.stderr, "values must never be printed")

    def test_a_malformed_name_is_skipped_loudly_and_the_rest_still_cross(self):
        proc, argv = self._run({
            "PHASEKIT_FORWARD_ENV": "bad-name,XMEO_SEQUENCER_URL",
            "XMEO_SEQUENCER_URL": SECRET_VALUE,
        })
        envs = self._env_args(argv)
        self.assertIn(f"XMEO_SEQUENCER_URL={SECRET_VALUE}", envs)
        self.assertIn("skipping malformed name 'bad-name'", proc.stderr)

    def test_env_file_syntax_in_the_list_never_prints_the_value(self):
        """Review finding 1 (v0.14.3): the loud path must not be the leak."""
        proc, argv = self._run({"PHASEKIT_FORWARD_ENV": f"XMEO_SEQUENCER_URL={SECRET_VALUE},FOO_UNSET"})
        self.assertNotIn(SECRET_VALUE, proc.stdout + proc.stderr)
        self.assertNotIn("token=", proc.stdout + proc.stderr)
        self.assertIn("skipping malformed name 'XMEO_SEQUENCER_URL=…'", proc.stderr)
        self.assertFalse(any(e.startswith("XMEO_SEQUENCER_URL") for e in self._env_args(argv)))

    def test_names_the_script_owns_are_refused_loudly(self):
        """Review finding 2: docker's last -e wins, so HOME/PATH/... from a
        project file would silently override the container's own settings."""
        proc, argv = self._run({"PHASEKIT_FORWARD_ENV": "HOME,PATH,XMEO_SEQUENCER_URL",
                                "XMEO_SEQUENCER_URL": SECRET_VALUE})
        envs = self._env_args(argv)
        self.assertIn(f"XMEO_SEQUENCER_URL={SECRET_VALUE}", envs)
        # This harness runs the non-root branch, where the script sets no HOME
        # itself; the point is that the HOST's HOME/PATH never cross.
        self.assertFalse(any(e.startswith(("HOME=", "PATH=")) for e in envs), envs)
        self.assertIn("refusing 'HOME'", proc.stderr)
        self.assertIn("refusing 'PATH'", proc.stderr)

    def test_interior_whitespace_is_malformed_not_repaired(self):
        """Review finding 3: 'XMEO SEQUENCER_URL' must not become XMEOSEQUENCER_URL."""
        proc, argv = self._run({"PHASEKIT_FORWARD_ENV": "XMEO SEQUENCER_URL",
                                "XMEOSEQUENCER_URL": SECRET_VALUE})
        self.assertFalse(any(e.startswith("XMEOSEQUENCER_URL") for e in self._env_args(argv)))
        self.assertIn("skipping malformed name 'XMEO SEQUENCER_URL'", proc.stderr)

    def test_newlines_separate_names_too(self):
        """Review finding 4: a YAML block scalar must not lose the names after
        the first line."""
        proc, argv = self._run({"PHASEKIT_FORWARD_ENV": "A_ONE\nXMEO_SEQUENCER_URL",
                                "A_ONE": "1", "XMEO_SEQUENCER_URL": SECRET_VALUE})
        envs = self._env_args(argv)
        self.assertIn("A_ONE=1", envs)
        self.assertIn(f"XMEO_SEQUENCER_URL={SECRET_VALUE}", envs)
        self.assertIn("forwarding project env: A_ONE XMEO_SEQUENCER_URL", proc.stdout)

    def test_a_list_with_nothing_set_says_so(self):
        proc, argv = self._run({"PHASEKIT_FORWARD_ENV": "FOO_UNSET"})
        self.assertFalse(any(e.startswith("FOO_UNSET") for e in self._env_args(argv)))
        self.assertIn("none of its names are set", proc.stderr)


if __name__ == "__main__":
    unittest.main()
