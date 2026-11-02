"""Push credentials only with AUTO_PUSH=1 (v0.24.1, row 1300 option B).

Until v0.24.0 container-setup.sh forwarded the host's ssh-agent socket and
GH_TOKEN / GITHUB_TOKEN into every session whenever the host had them, even
with AUTO_PUSH unset, when the loop never pushes. A session that never pushes
still held a credential that could, and the command guard that stands between
the model and a push matches patterns. Now every door a phasekit container is
started through keeps one rule: a push credential crosses only beside
AUTO_PUSH=1, and what is withheld is named on stderr, never its value.

The bash is exercised for real: push_credential_args is extracted from
scripts/container-setup.sh by its own delimiters and run in a scratch shell,
and the whole script runs against a stub `docker` that records argv, so these
tests break iff the shipped code does. The runner (scripts/enrich-project.py)
and the launcher module (scripts/phasekit-launch.py) are imported and called.

Run from the repo root: `python3 -m unittest tests.test_push_credentials`
"""
from __future__ import annotations
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = REPO_ROOT / "scripts" / "container-setup.sh"
LAUNCH_PY = REPO_ROOT / "scripts" / "phasekit-launch.py"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
SOURCE = CONTAINER.read_text()

TOKEN_GH = "ghp_" + "G" * 30
TOKEN_GITHUB = "ghp_" + "H" * 30
TOKENS = {"GH_TOKEN": TOKEN_GH, "GITHUB_TOKEN": TOKEN_GITHUB}
PUSH_NAMES = ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load("pk_launch_push", LAUNCH_PY)


def _extract(pattern_start, pattern_end):
    out, taking = [], False
    for line in SOURCE.splitlines():
        if not taking and re.match(pattern_start, line):
            taking = True
        if taking:
            out.append(line)
            if len(out) > 1 and re.match(pattern_end, line):
                return "\n".join(out)
    raise AssertionError(f"could not extract {pattern_start!r} from container-setup.sh")


PUSH_FN = _extract(r"^push_credential_args\(\) \{", r"^\}")
NAMES_LINE = next(line for line in SOURCE.splitlines()
                  if line.startswith("PUSH_CREDENTIAL_NAMES=("))


def _env_args(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e" and i + 1 < len(argv)]


def _clean_env(tmp):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PHASEKIT_", "CLAUDE_", "ANTHROPIC_", "GH_", "GITHUB_"))
           and k not in ("SSH_AUTH_SOCK", "AUTO_PUSH", "MAX_ITERATIONS")}
    env["HOME"] = str(tmp / "home")
    (tmp / "home").mkdir(exist_ok=True)
    return env


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-push-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A real unix socket: the script forwards SSH_AUTH_SOCK only when it is one.
        self.sock_path = self.tmp / "agent.sock"
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(str(self.sock_path))
        self.addCleanup(self.srv.close)

    def all_set(self, **extra):
        return {"SSH_AUTH_SOCK": str(self.sock_path), **TOKENS, **extra}


class TheExtractedFunction(Fixture):
    """push_credential_args alone: its array and its one stderr line."""

    def run_fn(self, extra):
        env = _clean_env(self.tmp)
        env.update(extra)
        body = "\n".join([
            "set -euo pipefail",
            NAMES_LINE,
            PUSH_FN,
            "push_credential_args",
            'printf "%s\\0" ${PUSH_CREDENTIAL_ARGS[@]+"${PUSH_CREDENTIAL_ARGS[@]}"}',
        ])
        p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p, [a for a in p.stdout.split("\0") if a]

    def test_auto_push_unset_forwards_no_credential_and_names_what_it_withheld(self):
        p, argv = self.run_fn(self.all_set())
        self.assertEqual(argv, [])
        self.assertIn("push credentials withheld: SSH_AUTH_SOCK GH_TOKEN GITHUB_TOKEN", p.stderr)
        self.assertEqual(p.stderr.count("\n"), 1, "one stderr line")

    def test_any_value_but_1_withholds(self):
        for value in ("", "0", "true", "yes", " 1", "1 ", "01"):
            with self.subTest(value=value):
                p, argv = self.run_fn(self.all_set(AUTO_PUSH=value))
                self.assertEqual(argv, [])
                self.assertIn("push credentials withheld", p.stderr)

    def test_auto_push_1_forwards_the_socket_and_both_tokens_by_name(self):
        p, argv = self.run_fn(self.all_set(AUTO_PUSH="1"))
        self.assertEqual(argv,
                         ["-v", f"{self.sock_path}:/ssh-agent", "-e", "SSH_AUTH_SOCK=/ssh-agent",
                          "-e", "GH_TOKEN", "-e", "GITHUB_TOKEN"])
        self.assertNotIn("withheld", p.stderr)

    def test_nothing_set_says_nothing(self):
        p, argv = self.run_fn({})
        self.assertEqual(argv, [])
        self.assertEqual(p.stderr, "")

    def test_a_socket_path_that_is_not_a_socket_is_neither_forwarded_nor_named(self):
        bogus = self.tmp / "not-a-socket"
        bogus.write_text("")
        for auto in ("", "1"):
            with self.subTest(auto=auto):
                p, argv = self.run_fn({"SSH_AUTH_SOCK": str(bogus), "AUTO_PUSH": auto})
                self.assertNotIn("SSH_AUTH_SOCK=/ssh-agent", argv)
                self.assertNotIn("SSH_AUTH_SOCK", p.stderr)

    def test_no_value_is_ever_printed(self):
        for auto in ("", "1"):
            with self.subTest(auto=auto):
                p, argv = self.run_fn(self.all_set(AUTO_PUSH=auto))
                for value in TOKENS.values():
                    self.assertNotIn(value, p.stdout + p.stderr)


STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
exit 0
"""


class TheWholeSession(Fixture):
    """The real script, `shell`, against a stub docker: the session's argv."""

    def run_script(self, extra, command="shell"):
        (self.tmp / "bin").mkdir(exist_ok=True)
        stub = self.tmp / "bin" / "docker"
        stub.write_text(STUB_DOCKER)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        log = self.tmp / "docker-args.log"
        log.write_text("")
        env = _clean_env(self.tmp)
        env.update({"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                    "DOCKER_ARGS_LOG": str(log)}, **extra)
        p = subprocess.run(["bash", str(CONTAINER), command], capture_output=True, text=True,
                           env=env)
        runs = [b.strip().splitlines() for b in log.read_text().split("---\n") if b.strip()]
        runs = [c for c in runs
                if c and c[0] == "run" and any(a.endswith(":/workspace") for a in c)]
        self.assertEqual(len(runs), 1, p.stdout + p.stderr)
        return p, runs[0]

    def test_auto_push_unset_no_credential_flags_in_the_argv(self):
        p, argv = self.run_script(self.all_set())
        joined = "\n".join(argv)
        self.assertNotIn("/ssh-agent", joined)
        for name in PUSH_NAMES:
            self.assertFalse(any(e == name or e.startswith(name + "=") for e in _env_args(argv)),
                             name)
        self.assertIn("push credentials withheld: SSH_AUTH_SOCK GH_TOKEN GITHUB_TOKEN", p.stderr)

    def test_auto_push_1_credential_flags_present(self):
        p, argv = self.run_script(self.all_set(AUTO_PUSH="1"))
        self.assertIn(f"{self.sock_path}:/ssh-agent", argv)
        envs = _env_args(argv)
        for want in ("SSH_AUTH_SOCK=/ssh-agent", "GH_TOKEN", "GITHUB_TOKEN", "AUTO_PUSH=1"):
            self.assertIn(want, envs)
        self.assertNotIn("withheld", p.stderr)

    def test_no_value_ever_printed_or_on_the_command_line(self):
        for auto in ("", "1"):
            with self.subTest(auto=auto):
                p, argv = self.run_script(self.all_set(AUTO_PUSH=auto))
                for value in TOKENS.values():
                    self.assertNotIn(value, p.stdout + p.stderr)
                    self.assertNotIn(value, "\n".join(argv))

    def test_the_forward_list_cannot_carry_a_push_credential(self):
        for auto in ("", "1"):
            with self.subTest(auto=auto):
                p, argv = self.run_script(self.all_set(
                    AUTO_PUSH=auto, PHASEKIT_FORWARD_ENV="GH_TOKEN,GITHUB_TOKEN,SSH_AUTH_SOCK"))
                for name in PUSH_NAMES:
                    self.assertIn(f"refusing '{name}'", p.stderr)
                envs = _env_args(argv)
                expected = 1 if auto == "1" else 0  # only the AUTO_PUSH door, never twice
                self.assertEqual(envs.count("GH_TOKEN"), expected)
                self.assertEqual(envs.count("GITHUB_TOKEN"), expected)
                self.assertFalse(any(e == "SSH_AUTH_SOCK" or e == f"SSH_AUTH_SOCK={self.sock_path}"
                                     for e in envs))


class TheRunner(Fixture):
    """scripts/enrich-project.py: the gate, the landing and their forward list."""

    def setUp(self):
        super().setUp()
        self.e = _load("pk_enrich_push", ENRICH)
        self.project = self.tmp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.saved = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def argv(self, extra, **kw):
        os.environ.clear()
        os.environ.update(_clean_env(self.tmp))
        os.environ.update({"PHASEKIT_CONTAINER_USER": "0:0", "GIT_USER_NAME": "t",
                           "GIT_USER_EMAIL": "t@example.invalid"})
        os.environ.update(extra)
        return self.e.runner_argv(self.project, "img", "N", "SCRIPT", None, **kw)

    def test_the_landing_without_auto_push_1_carries_no_credential(self):
        for env in ([], [("AUTO_PUSH", "0")], [("AUTO_PUSH", "1"), ("AUTO_PUSH", "0")]):
            with self.subTest(env=env):
                argv = self.argv(self.all_set(), env=env, names=self.e.LAND_PUSH_SECRETS,
                                 forward_ssh=True)
                joined = "\n".join(argv)
                self.assertNotIn("/ssh-agent", joined)
                self.assertNotIn("GH_TOKEN", _env_args(argv))
                self.assertNotIn("GITHUB_TOKEN", _env_args(argv))

    def test_the_landing_with_auto_push_1_carries_them_by_name(self):
        argv = self.argv(self.all_set(), env=[("AUTO_PUSH", "1")],
                         names=self.e.LAND_PUSH_SECRETS, forward_ssh=True)
        joined = "\n".join(argv)
        self.assertIn("dst=/ssh-agent", joined)
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent", _env_args(argv))
        self.assertIn("GH_TOKEN", _env_args(argv))
        self.assertIn("GITHUB_TOKEN", _env_args(argv))
        for value in TOKENS.values():
            self.assertNotIn(value, joined)

    def test_the_gate_and_the_forward_list_never_carry_one(self):
        with mock.patch("sys.stderr") as err:
            argv = self.argv(self.all_set(
                PHASEKIT_FORWARD_ENV="GH_TOKEN,GITHUB_TOKEN,SSH_AUTH_SOCK"))
        envs = _env_args(argv)
        for name in PUSH_NAMES:
            self.assertFalse(any(e == name or e.startswith(name + "=") for e in envs), name)
        written = "".join(c.args[0] for c in err.write.call_args_list)
        for name in PUSH_NAMES:
            self.assertIn(f"refusing '{name}'", written)
        for value in TOKENS.values():
            self.assertNotIn(value, written + "\n".join(argv))

    def test_land_push_secrets_are_the_launchers_tokens(self):
        self.assertEqual(set(self.e.LAND_PUSH_SECRETS) | {"SSH_AUTH_SOCK"},
                         set(L.PUSH_CREDENTIAL_NAMES))


class TheLauncher(unittest.TestCase):
    """scripts/phasekit-launch.py compose(): --env NAME of a push credential."""

    def compose(self, **kw):
        with mock.patch.object(L, "volume_entries", lambda volume: set()):
            return L.compose("oneshot", "pk-test", ["true"], user="0:0", environ=TOKENS, **kw)

    def test_without_auto_push_1_a_push_credential_is_withheld_and_named(self):
        for sets in ([], ["AUTO_PUSH=0"], ["AUTO_PUSH=1", "AUTO_PUSH=0"], ["AUTO_PUSH=true"]):
            with self.subTest(sets=sets), mock.patch("sys.stderr") as err:
                argv = self.compose(secrets=["GH_TOKEN", "GITHUB_TOKEN"], sets=sets)
                written = "".join(c.args[0] for c in err.write.call_args_list)
                self.assertNotIn("GH_TOKEN", _env_args(argv))
                self.assertNotIn("GITHUB_TOKEN", _env_args(argv))
                self.assertIn("push credentials withheld: GH_TOKEN GITHUB_TOKEN", written)
                for value in TOKENS.values():
                    self.assertNotIn(value, written + " ".join(argv))

    def test_beside_auto_push_1_it_crosses_by_name(self):
        argv = self.compose(secrets=["GH_TOKEN", "GITHUB_TOKEN"], sets=["AUTO_PUSH=1"])
        self.assertIn("GH_TOKEN", _env_args(argv))
        self.assertIn("GITHUB_TOKEN", _env_args(argv))
        for value in TOKENS.values():
            self.assertNotIn(value, " ".join(argv))

    def test_set_and_mount_cannot_carry_a_push_credential(self):
        """Review MAJOR-1: --env was the only gated door of the composer."""
        for kw in ({"sets": ["GH_TOKEN=ghp_x"]}, {"sets": ["GITHUB_TOKEN=x", "AUTO_PUSH=1"]},
                   {"sets": ["SSH_AUTH_SOCK=/ssh-agent"]},
                   {"mounts": ["/tmp/agent.sock:/ssh-agent"]},
                   {"mounts": ["/tmp/agent.sock:/elsewhere"]},
                   {"mounts": ["/tmp/agent.sock:/ssh-agent"], "sets": ["AUTO_PUSH=0"]}):
            with self.subTest(kw=kw), self.assertRaises(L.LaunchError):
                with mock.patch.object(L, "volume_entries", lambda volume: set()):
                    L.compose("oneshot", "pk-test", ["true"], user="0:0",
                              environ={**TOKENS, "SSH_AUTH_SOCK": "/tmp/agent.sock"}, **kw)

    def test_beside_auto_push_1_the_agent_socket_may_be_mounted(self):
        argv = self.compose(mounts=["/tmp/agent.sock:/ssh-agent"],
                            sets=["AUTO_PUSH=1", "SSH_AUTH_SOCK=/ssh-agent"])
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent", _env_args(argv))
        self.assertIn("dst=/ssh-agent", " ".join(argv))

    def test_auto_push_cannot_come_from_env(self):
        """Review MINOR-2: `--set AUTO_PUSH=1 --env AUTO_PUSH` with a host AUTO_PUSH=0
        gave the container AUTO_PUSH=0 (docker's last -e wins) and the tokens."""
        with self.assertRaises(L.LaunchError):
            self.compose(secrets=["GH_TOKEN", "AUTO_PUSH"], sets=["AUTO_PUSH=1"])

    def test_the_cli_prints_names_only(self):
        env = {**os.environ, **TOKENS, "PHASEKIT_CONTAINER_USER": "0:0"}
        p = subprocess.run(["python3", str(LAUNCH_PY), "argv", "--role", "oneshot", "--name", "x",
                            "--credentials", "none", "--env", "GH_TOKEN", "--json", "--", "true"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("GH_TOKEN", _env_args(json.loads(p.stdout)))
        self.assertIn("push credentials withheld: GH_TOKEN", p.stderr)
        for value in TOKENS.values():
            self.assertNotIn(value, p.stdout + p.stderr)


class OneRule(unittest.TestCase):
    """The names exist in three places that must agree; the contract declares them."""

    def test_the_bash_list_the_module_and_the_contract_agree(self):
        bash_names = tuple(NAMES_LINE[len("PUSH_CREDENTIAL_NAMES=("):-1].split())
        fact = json.loads(MANIFEST.read_text())["facts"]["container"]["push_credentials"]
        self.assertEqual(bash_names, L.PUSH_CREDENTIAL_NAMES)
        self.assertEqual(tuple(fact["names"]), L.PUSH_CREDENTIAL_NAMES)
        self.assertEqual(fact["only_when"], "AUTO_PUSH=1")
        self.assertIn(fact["withheld_note"], PUSH_FN)
        self.assertIn(fact["withheld_note"], L.withheld_note(["X"]))
        self.assertTrue(fact["refused_by_forward_env"])

    def test_the_bash_rule_is_driven_by_the_list(self):
        """Review MINOR-3: the array must decide behaviour, not just sit beside it.
        A name added to the array is withheld by the function and refused by
        the forward list, with no other edit."""
        helper = _extract(r"^is_push_credential\(\) \{", r"^\}")
        body = "\n".join(["set -euo pipefail",
                          "PUSH_CREDENTIAL_NAMES=(SSH_AUTH_SOCK GH_TOKEN GITHUB_TOKEN "
                          "EXTRA_PUSH_TOKEN)",
                          helper, PUSH_FN, "push_credential_args",
                          "is_push_credential EXTRA_PUSH_TOKEN && echo REFUSED",
                          "is_push_credential XMEO_URL || echo NOT-A-CREDENTIAL"])
        tmp = Path(tempfile.mkdtemp(prefix="pk-push-l-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**_clean_env(tmp), "EXTRA_PUSH_TOKEN": "v" * 20}
        p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("push credentials withheld: EXTRA_PUSH_TOKEN", p.stderr)
        self.assertIn("REFUSED", p.stdout)
        self.assertIn("NOT-A-CREDENTIAL", p.stdout)
        self.assertIn('if is_push_credential "$_fwd_name"; then', SOURCE)

    def test_the_session_forwards_push_credentials_only_through_the_function(self):
        def code(text):
            return "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith("#"))
        outside = code(SOURCE).replace(code(PUSH_FN), "")
        self.assertNotEqual(outside, code(SOURCE), "the function was found and cut out")
        self.assertNotIn("/ssh-agent", outside)
        self.assertNotRegex(outside, r"-e\s+\"?(GH_TOKEN|GITHUB_TOKEN)\b")
        self.assertIn("push_credential_args\n", outside)
        self.assertIn('"${PUSH_CREDENTIAL_ARGS[@]}"', outside)

    def test_the_rule_mirrors_the_loops_push_condition(self):
        loop = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()
        self.assertIn('if [[ "${AUTO_PUSH:-}" != "1" ]]; then', loop)
        self.assertIn('if [[ "${AUTO_PUSH:-}" != "1" ]]; then', PUSH_FN)
        self.assertEqual(L.AUTO_PUSH_ON, "1")


if __name__ == "__main__":
    unittest.main()
