"""The one launcher (v0.22.0, O5): scripts/phasekit-launch.py.

Twelve places composed a `docker run` of the runner image by hand; two of them
had no bounds at all, none of the orchestrator's bounded swap, and each had its
own constants and validator. This module is the one composer, and these tests
pin what every role gets from it: the bounds (swap = memory), the user and its
environment, the credentials mount, mount syntax, secrets by name only, and the
reading of an exit status. The last class pins that the rules exist ONCE.
"""
from __future__ import annotations
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

import importlib.util
import json
import os
import re
import subprocess
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCH = REPO_ROOT / "scripts" / "phasekit-launch.py"
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"

_spec = importlib.util.spec_from_file_location("phasekit_launch", LAUNCH)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)

ROOT = "0:0"
BAD_MEMORY = ("0", "-1", "lots", "0.0g", "8 g", "+8g", "٨g", "12g;rm", "0.5", "0.9b",
              "0.0000001g", "1m", "5.9m", "99999999999999999999p", "9" * 400)
BAD_PIDS = ("0", "-1", "many", "4096.5", "00", "-0", "0x10", "1e3",
            "18446744073709551616", "9223372036854775808", "1000000000", "0" * 30)


FLEET_VOLUME = {".credentials.json", "settings.json", "projects", "history.jsonl"}


def compose(role="oneshot", command=("true",), environ=None, entries=FLEET_VOLUME, **kw):
    """ENTRIES stands in for what the credentials volume holds (no docker probe)."""
    kw.setdefault("user", ROOT)
    with mock.patch.object(L, "volume_entries", lambda volume: entries):
        return L.compose(role, "pk-test", list(command), environ=environ or {}, **kw)


def flag(argv, name):
    return argv[argv.index(name) + 1]


def envs(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]



def mount_values(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]

class Bounds(unittest.TestCase):
    def test_every_role_is_bounded_with_swap_equal_to_memory(self):
        # the session's own argv is pinned by tests/test_session_bounds.py
        for role, (mem, pids, *_rest) in L.ROLES.items():
            if role in L.BOUNDS_ONLY_ROLES:
                self.assertEqual(L.bounds(role, environ={}), (mem, pids))
                continue
            with self.subTest(role=role):
                argv = compose(role)
                self.assertEqual(flag(argv, "--memory"), mem)
                self.assertEqual(flag(argv, "--memory-swap"), mem)
                self.assertEqual(flag(argv, "--pids-limit"), str(pids))
                self.assertLess(argv.index("--memory"), argv.index(L.IMAGE_DEFAULT))

    def test_the_declared_defaults(self):
        self.assertEqual({r: v[:2] for r, v in L.ROLES.items()},
                         {"session": ("10g", 8192), "runner": ("8g", 4096),
                          "oneshot": ("2g", 512), "repair": ("4g", 512)})

    def test_each_role_has_its_own_overrides(self):
        for role in L.ROLES:
            with self.subTest(role=role):
                env = {L.memory_env(role): "3g", L.pids_env(role): "0100"}
                if role in L.BOUNDS_ONLY_ROLES:
                    self.assertEqual(L.bounds(role, environ=env), ("3g", 100))
                    continue
                argv = compose(role, environ=env)
                self.assertEqual((flag(argv, "--memory"), flag(argv, "--memory-swap"),
                                  flag(argv, "--pids-limit")), ("3g", "3g", "100"))

    def test_a_bad_value_is_the_default_never_unbounded(self):
        for role, (mem, pids, *_rest) in L.ROLES.items():
            for bad_mem, bad_pids in zip(BAD_MEMORY, BAD_PIDS + BAD_PIDS[:3], strict=True):
                with self.subTest(role=role, mem=bad_mem, pids=bad_pids):
                    got = L.bounds(role, quiet=True,
                                   environ={L.memory_env(role): bad_mem,
                                            L.pids_env(role): bad_pids})
                    self.assertEqual(got, (mem, pids))

    def test_the_smallest_and_odd_sizes_docker_accepts(self):
        for good in ("6m", "6291456", "0.006t", "16gb", "1.5g", "512M", " 10g "):
            with self.subTest(good=good):
                self.assertEqual(L.bounds("oneshot", environ={"PHASEKIT_ONESHOT_MEMORY": good})[0],
                                 good.strip())

    def test_a_bad_value_says_so_on_stderr(self):
        proc = subprocess.run(["python3", str(LAUNCH), "bounds", "--role", "repair"],
                              capture_output=True, text=True,
                              env={**os.environ, "PHASEKIT_REPAIR_MEMORY": "0.5",
                                   "PHASEKIT_REPAIR_PIDS_LIMIT": "0"})
        self.assertEqual(proc.stdout.strip(), "4g 512")
        self.assertIn("PHASEKIT_REPAIR_MEMORY='0.5'", proc.stderr)
        self.assertIn("PHASEKIT_REPAIR_PIDS_LIMIT='0'", proc.stderr)


class Composition(unittest.TestCase):
    def test_root_is_sandboxed_and_another_user_is_not(self):
        root = envs(compose(user="0:0"))
        self.assertIn("IS_SANDBOX=1", root)
        self.assertIn("HOME=/home/node", root)
        other = envs(compose(user="1234:1234"))
        self.assertNotIn("IS_SANDBOX=1", other)
        self.assertIn(f"HOME={L.THROWAWAY_HOME}", other)

    def test_credentials_follow_the_role_and_the_caller(self):
        def creds(argv):
            return {m.split("volume-subpath=")[1].split(",")[0]: m.endswith(",readonly")
                    for m in mount_values(argv) if "type=volume" in m}
        self.assertEqual(creds(compose("runner")), {})
        # The login is writable by default: a refresh into a read-only login
        # strands the rotated token and logs the fleet out. Settings: read-only.
        for role in ("oneshot", "repair"):
            with self.subTest(role=role):
                self.assertEqual(creds(compose(role)),
                                 {".credentials.json": False, "settings.json": True})
        self.assertEqual(creds(compose("oneshot", credentials="ro")),
                         {".credentials.json": True, "settings.json": True})
        self.assertEqual(creds(compose("oneshot", credentials="none")), {})
        vol = [m for m in mount_values(compose("oneshot", environ={"CLAUDE_VOLUME": "other-vol"}))
               if "type=volume" in m]
        self.assertTrue(vol and all('"src=other-vol"' in m for m in vol))

    def test_no_role_mounts_the_shared_volume_whole(self):
        """v0.22.1: transcripts, history and .claude.json live in the config dir;
        a container that mounts the whole volume shares them with every other.
        Every composed role, every credentials mode: the config dir is a private
        tmpfs any user can write, and the volume reaches it one file at a time."""
        for role in set(L.ROLES) - set(L.BOUNDS_ONLY_ROLES):
            for cred in ("none", "ro", "rw"):
                with self.subTest(role=role, credentials=cred):
                    mounts = mount_values(compose(role, credentials=cred))
                    for m in mounts:
                        if "type=volume" in m and "scaffold-claude-config" in m:
                            self.assertIn(",volume-subpath=", m)
                    if cred != "none":
                        self.assertEqual(
                            mounts[0], f"type=tmpfs,dst={L.CLAUDE_CONFIG_DIR},tmpfs-mode=1777")
                        self.assertEqual(
                            sorted(m.split(",dst=")[1].split(",")[0] for m in mounts[1:3]),
                            [f"{L.CLAUDE_CONFIG_DIR}/{L.CREDENTIALS_FILE}",
                             f"{L.CLAUDE_CONFIG_DIR}/{L.SETTINGS_FILE}"])

    def test_a_caller_cannot_mount_the_credentials_volume_whole(self):
        for spec in ("scaffold-claude-config:/x", "scaffold-claude-config:/home/node/.claude:ro"):
            with self.subTest(spec=spec), self.assertRaises(L.LaunchError):
                compose(credentials="none", volumes=[spec])
        with self.assertRaises(L.LaunchError):
            compose(environ={"CLAUDE_VOLUME": "v2"}, volumes=["v2:/x"])
        self.assertTrue(compose(volumes=["other:/x"]))

    def test_a_file_the_volume_lacks_is_not_mounted_and_unknown_mounts_both(self):
        def subpaths(entries):
            return sorted(m.split("volume-subpath=")[1].split(",")[0]
                          for m in mount_values(compose(entries=entries)) if "type=volume" in m)
        self.assertEqual(subpaths({".credentials.json"}), [".credentials.json"])
        self.assertEqual(subpaths({"settings.json"}), ["settings.json"])  # an API-key login
        self.assertEqual(subpaths(set()), [])
        # unreadable volume: mount both, so a missing one fails the run, never widens it
        self.assertEqual(subpaths(None), [".credentials.json", "settings.json"])
        self.assertIn("volume-subpath", L.exit_reason("oneshot", 127))

    def test_the_session_is_bounds_only_here(self):
        with self.assertRaises(L.LaunchError) as cm:
            compose("session")
        self.assertIn("container-setup.sh", str(cm.exception))
        self.assertEqual(L.bounds("session", environ={}), ("10g", 8192))

    def test_a_volume_subpath_is_one_plain_entry(self):
        for bad in ("../x", "a/b", "..", ".", "a,b", ""):
            with self.subTest(subpath=bad), self.assertRaises(L.LaunchError):
                L.mount_arg("vol", "/d", kind="volume", subpath=bad)
        with self.assertRaises(L.LaunchError):
            L.mount_arg("/host", "/d", subpath="x")  # a bind has no subpath

    def test_mounts_are_one_field_each_and_split_at_the_last_colon(self):
        argv = compose(mounts=["/srv/a:b,c:/out", "/proj:/project:ro"])
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertIn('type=bind,"src=/srv/a:b,c",dst=/out', mounts)
        self.assertIn('type=bind,"src=/proj",dst=/project,readonly', mounts)
        for bad in ("/no-destination", "/x:relative", "/x:/y\nz"):
            with self.subTest(bad=bad), self.assertRaises(L.LaunchError):
                compose(mounts=[bad])

    def test_secrets_cross_by_name_only_and_only_when_set(self):
        argv = compose(secrets=["GH_TOKEN", "UNSET_ONE"], environ={"GH_TOKEN": "s3cr3t-value"})
        self.assertIn("GH_TOKEN", envs(argv))
        self.assertNotIn("UNSET_ONE", envs(argv))
        self.assertNotIn("s3cr3t-value", " ".join(argv))
        for bad in ("A=B", "1X", "HOME"):
            with self.subTest(bad=bad), self.assertRaises(L.LaunchError):
                compose(secrets=[bad])

    def test_the_launcher_owns_its_names(self):
        for owned in L.OWNED_NAMES:
            with self.subTest(name=owned), self.assertRaises(L.LaunchError):
                compose(sets=[f"{owned}=x"])
        self.assertIn("QA_URL=http://h:1/", envs(compose(sets=["QA_URL=http://h:1/"])))

    def test_the_image_the_command_and_the_order(self):
        argv = compose(command=["-c", "echo hi"], entrypoint="bash", workdir="/out",
                       add_hosts=["h:1.2.3.4"], environ={"PHASEKIT_RUNNER_IMAGE": "img:9"})
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--name"])
        self.assertEqual(argv[-3:], ["img:9", "-c", "echo hi"])
        self.assertEqual(flag(argv, "--entrypoint"), "bash")
        self.assertEqual(flag(argv, "-w"), "/out")
        self.assertEqual(flag(argv, "--add-host"), "h:1.2.3.4")

    def test_refusals(self):
        for kw in ({"role": "nope"}, {"command": ()}, {"credentials": "maybe"}):
            with self.subTest(kw=kw), self.assertRaises(L.LaunchError):
                compose(**kw)
        with self.assertRaises(L.LaunchError):
            L.compose("oneshot", "bad name", ["true"], user=ROOT, environ={})


class ReviewR1(unittest.TestCase):
    """The v0.22.0 review's confirmed cases."""

    def test_a_trailing_newline_is_never_accepted(self):
        for kw in ({"sets": ["HOME\n=evil"]}, {"sets": ["X\n=1"]}, {"secrets": ["GH_TOKEN\n"]}):
            with self.subTest(kw=kw), self.assertRaises(L.LaunchError):
                compose(**kw)
        with self.assertRaises(L.LaunchError):
            L.compose("oneshot", "n\n", ["true"], user=ROOT, environ={})
        self.assertIsNone(L.pids_value("100\n"))
        self.assertFalse(L.memory_ok("8g\n"))

    def test_a_destination_cannot_add_mount_options(self):
        for dst in ("/w,readonly=false", '/w"x', "/w x", "/w\tx"):
            with self.subTest(dst=dst), self.assertRaises(L.LaunchError):
                compose(mounts=[f"/a:{dst}"])

    def test_root_by_any_spelling_is_sandboxed(self):
        for user in ("0", "0:0", "root", "root:root", "0:1000"):
            with self.subTest(user=user):
                self.assertIn("IS_SANDBOX=1", envs(compose(user=user)))

    def test_loading_the_module_writes_no_bytecode(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "scripts"
            scripts.mkdir()
            for name in ("enrich-project.py", "phasekit-launch.py"):
                shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
            # run as a script, as the engine is: its own load caches nothing
            proc = subprocess.run(["python3", str(scripts / "enrich-project.py"), "--help"],
                                  capture_output=True, text=True, cwd=tmp,
                                  env={k: v for k, v in os.environ.items()
                                       if k != "PYTHONDONTWRITEBYTECODE"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((scripts / "__pycache__").exists())

    def test_a_missing_module_is_named(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(REPO_ROOT / "scripts" / "enrich-project.py", Path(tmp) / "e.py")
            proc = subprocess.run(["python3", str(Path(tmp) / "e.py"), "--help"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("cannot load the launcher module", proc.stderr)


class ExitReading(unittest.TestCase):
    def test_137_names_the_likely_cause_by_role(self):
        runner = L.exit_reason("runner", 137, environ={})
        self.assertLess(runner.index("memory bound (8g, PHASEKIT_RUNNER_MEMORY)"),
                        runner.index("outside docker kill"))
        for role in ("session", "oneshot", "repair"):
            with self.subTest(role=role):
                why = L.exit_reason(role, 137, environ={})
                self.assertLess(why.index("outside docker kill"), why.index("memory bound"))
                self.assertIn(L.memory_env(role), why)
                self.assertNotIn("PIDS", why)

    def test_docker_failures_and_ordinary_exits(self):
        self.assertIn("docker run itself failed", L.exit_reason("oneshot", 125))
        for rc in (0, 1, 3, 2):
            self.assertIsNone(L.exit_reason("oneshot", rc))


class Cli(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run(["bash", str(CLI), "launch-argv", *args], capture_output=True,
                              text=True, env={**os.environ, **(env or {})})

    def test_json_through_the_phasekit_verb(self):
        proc = self.run_cli("--role", "oneshot", "--name", "pk-scope-1", "--user", ROOT,
                            "--mount", "/w:/out", "--json", "--", "bash", "-c", "x y")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = json.loads(proc.stdout)
        self.assertEqual(argv[-3:], ["bash", "-c", "x y"])
        self.assertEqual(flag(argv, "--memory-swap"), "2g")

    def test_nul_separated_by_default(self):
        proc = self.run_cli("--role", "runner", "--name", "n", "--user", ROOT, "--", "true")
        self.assertEqual(proc.stdout.split("\0")[:2], ["docker", "run"])

    def test_a_refusal_is_exit_2_and_prints_no_argv(self):
        proc = self.run_cli("--role", "nope", "--name", "n", "--", "true")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("unknown role", proc.stderr)


class OneSource(unittest.TestCase):
    """The rules exist once: no other shipped script carries a memory
    validator, a role default, or the runner user rule."""

    def shipped(self):
        for p in sorted((REPO_ROOT / "scripts").glob("*")):
            if p.is_file() and p.name != LAUNCH.name and p.suffix in (".py", ".sh"):
                yield p

    def test_no_second_validator_or_default(self):
        patterns = [r"2\s*\*\*\s*20", r"2\^20", r'"10g"', r'"8g"', r"=10g\b", r'"2g"', r'"4g"',
                    r"SecurityOptions"]
        for p in self.shipped():
            text = p.read_text(encoding="utf-8")
            for pat in patterns:
                with self.subTest(file=p.name, pattern=pat):
                    self.assertIsNone(re.search(pat, text), f"{p.name} carries {pat}")

    def test_every_role_variable_is_declared(self):
        declared = {e["name"]: e for e in json.loads(MANIFEST.read_text())["env"]}
        for role, (mem, pids, *_rest) in L.ROLES.items():
            with self.subTest(role=role):
                self.assertEqual(declared[L.memory_env(role)]["default"], mem)
                self.assertEqual(declared[L.pids_env(role)]["default"], str(pids))
                self.assertIn("scripts/phasekit-launch.py",
                              declared[L.memory_env(role)]["read_by"])


if __name__ == "__main__":
    unittest.main()
