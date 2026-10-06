"""Per-project session directory (v0.18.8; auto-memory alone since v0.18.7).

Every session mounted its project at /workspace with the one shared config
volume, so Claude Code's per-directory state (<config>/projects/-workspace/:
transcripts, per-session files, auto-memory) was one directory for the whole
fleet — and `claude -c` resumed "the most recent conversation" in it, which
could be another project's. Now container-setup.sh mounts the volume's own
`project-sessions/<key>` over that WHOLE path for each session (ONE docker
volume-subpath mount, replacing v0.18.7's memory-only one), after a throwaway
container has created it — its memory/ carried over from v0.18.7's
`project-memory/<key>`, else from the shared memory; nothing is moved or
deleted.

Two halves, the extraction idiom: the real script against a stub `docker`
(what each `docker run` is given), and the seed script itself — pulled out of
container-setup.sh by its own delimiters — run against a scratch volume.
"""
from __future__ import annotations
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_SCRIPT = REPO_ROOT / "scripts" / "container-setup.sh"
PROJECT_DIR = "/home/node/.claude/projects/-workspace"

# STUB_API: what `docker version` reports (client and server API versions); STUB_PREP_RC / STUB_PREP_OUT: the
# memory-preparing run (the one with `--entrypoint /bin/sh`).
STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
if [[ "$1" == version ]]; then
  [[ -n "${STUB_API:-}" ]] && echo "$STUB_API"
  exit 0
fi
for a in "$@"; do
  if [[ "$a" == /bin/sh ]]; then
    echo "${STUB_PREP_OUT:-exists}"
    exit "${STUB_PREP_RC:-0}"
  fi
done
exit 0
"""


def _seed_script():
    """PROJECT_SESSIONS_SEED_SH as bash evaluates it (it splices in the subdirs)."""
    text = CONTAINER_SCRIPT.read_text()
    subdirs = re.findall(r"^PROJECT_(?:SESSIONS|MEMORY)_SUBDIR=.*?$", text, re.M)
    s = re.search(r"^PROJECT_SESSIONS_SEED_SH='.*?^fi'$", text, re.M | re.S)
    if not (len(subdirs) == 2 and s):
        raise AssertionError("container-setup.sh carries no PROJECT_SESSIONS_SEED_SH")
    snippet = "\n".join(subdirs) + f"\n{s.group(0)}\nprintf '%s' \"$PROJECT_SESSIONS_SEED_SH\"\n"
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True,
                          check=True).stdout


class ContainerSetupSessionsMount(unittest.TestCase):

    def _run(self, project="round-clock", extra=None):
        tmp = Path(tempfile.mkdtemp(prefix="pk-ses-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / project
        (root / "scripts").mkdir(parents=True)
        shutil.copy(CONTAINER_SCRIPT, root / "scripts" / "container-setup.sh")
        (tmp / "bin").mkdir()
        (tmp / "home").mkdir()
        stub = tmp / "bin" / "docker"
        stub.write_text(STUB_DOCKER)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        log = tmp / "log"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PHASEKIT_", "CLAUDE_", "ANTHROPIC_", "GH_", "GITHUB_",
                                    "STUB_", "SSH_AUTH_SOCK", "IMAGE_NAME"))}
        env.update({"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                    "DOCKER_ARGS_LOG": str(log), "HOME": str(tmp / "home")})
        env.update(extra or {})
        p = subprocess.run(["bash", str(root / "scripts" / "container-setup.sh"), "shell"],
                           capture_output=True, text=True, env=env)
        calls = [b.strip().splitlines() for b in log.read_text().split("---\n")
                 if b.strip()] if log.exists() else []
        runs = [c for c in calls if c and c[0] == "run"]
        session = [c for c in runs if any(a.endswith(":/workspace") for a in c)]
        prep = [c for c in runs if "/bin/sh" in c]
        return p, runs, session, prep

    @staticmethod
    def _mounts(argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount" and i + 1 < len(argv)]

    def _subpath(self, key, volume="scaffold-claude-config"):
        return (f"type=volume,src={volume},dst={PROJECT_DIR},"
                f"volume-subpath=project-sessions/{key}")

    TMPFS = f"type=tmpfs,dst={PROJECT_DIR},tmpfs-mode=1777"

    def test_the_session_mounts_its_own_directory_over_the_whole_shared_path(self):
        p, runs, session, prep = self._run("round-clock")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(session), 1, p.stdout + p.stderr)
        argv = session[0]
        # ONE mount: transcripts and memory together (v0.18.7's memory-only
        # mount is replaced, not stacked)
        self.assertEqual(self._mounts(argv), [self._subpath("round-clock")])
        self.assertFalse(any("project-memory" in a for a in argv), argv)
        # the rest is unchanged: the whole volume, the config dir, /workspace
        self.assertIn("scaffold-claude-config:/home/node/.claude", argv)
        self.assertIn("CLAUDE_CONFIG_DIR=/home/node/.claude", argv)
        self.assertTrue(any(a.endswith("/round-clock:/workspace") for a in argv))
        self.assertIn("sessions: project-sessions/round-clock", p.stdout)

    def test_two_projects_get_two_directories(self):
        _, _, a, _ = self._run("xmeo-v3")
        _, _, b, _ = self._run("foundry-orchestrator")
        self.assertEqual(self._mounts(a[0]), [self._subpath("xmeo-v3")])
        self.assertEqual(self._mounts(b[0]), [self._subpath("foundry-orchestrator")])

    def test_the_key_is_sanitized_and_never_empty_or_a_dot_name(self):
        for project, key in (("we ird,name", "we_ird_name"), ("...", "_default"),
                             (".hidden", "hidden"), ("a:b=c", "a_b_c")):
            with self.subTest(project=project):
                _, _, session, prep = self._run(project)
                self.assertEqual(self._mounts(session[0]), [self._subpath(key)])
                self.assertEqual(prep[0][-3:], ["sh", key, "/home/node/.claude"])

    def test_the_directory_is_prepared_first_by_the_session_user(self):
        _, runs, session, prep = self._run("round-clock", {"PHASEKIT_ROOTLESS_DOCKER": "1"})
        self.assertEqual(len(prep), 1)
        self.assertLess(runs.index(prep[0]), runs.index(session[0]))
        argv = prep[0]
        for flag in ("--rm", "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")
        self.assertEqual(argv[argv.index("--user") + 1],
                         session[0][session[0].index("--user") + 1])
        # where the session mounts it: a fresh volume is populated and owned the same way
        self.assertIn("scaffold-claude-config:/home/node/.claude", argv)
        self.assertNotIn("--cap-add=NET_ADMIN", argv)
        # the image's own user (no override) prepares as itself
        _, _, _, prep2 = self._run("round-clock")
        self.assertNotIn("--user", prep2[0])
        self.assertNotIn("--cap-add=DAC_OVERRIDE", prep2[0])

    def test_a_failed_preparation_gives_an_empty_throwaway_directory_never_the_shared_one(self):
        p, _, session, _ = self._run("round-clock", {"STUB_PREP_RC": "1",
                                                     "STUB_PREP_OUT": "mv: boom"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._mounts(session[0]), [self.TMPFS])
        self.assertIn("could not prepare project-sessions/round-clock", p.stderr)
        self.assertIn("mv: boom", p.stderr)

    def test_a_docker_without_volume_subpath_gives_an_empty_throwaway_directory(self):
        for api in ("1.44 1.54", "1.54 1.44", "1.44"):
            with self.subTest(api=api):
                p, _, session, prep = self._run("round-clock", {"STUB_API": api})
                # still prepared: that also creates the tmpfs mountpoint as the session user
                self.assertEqual(len(prep), 1)
                self.assertEqual(self._mounts(session[0]), [self.TMPFS])
                self.assertIn("older than 1.45", p.stderr)
        for api in ("1.45 1.45", "1.54 1.54", "2.0", "garbage"):
            with self.subTest(api=api):
                _, _, session, prep = self._run("round-clock", {"STUB_API": api})
                self.assertEqual(len(prep), 1)
                self.assertEqual(self._mounts(session[0]), [self._subpath("round-clock")])

    def test_a_host_directory_config_volume_is_bound(self):
        _, _, session, prep = self._run("round-clock", {"CLAUDE_VOLUME": "/srv/claude"})
        self.assertIn("/srv/claude:/home/node/.claude", prep[0])
        self.assertEqual(self._mounts(session[0]), [
            f"type=bind,src=/srv/claude/project-sessions/round-clock,dst={PROJECT_DIR}"])

    def test_a_config_path_with_a_comma_gives_an_empty_throwaway_directory(self):
        p, _, session, _ = self._run("round-clock", {"CLAUDE_VOLUME": "/srv/a,b"})
        self.assertEqual(self._mounts(session[0]), [self.TMPFS])
        self.assertIn("contains a comma", p.stderr)

    def test_a_first_run_says_where_its_memory_came_from(self):
        for out, said in (("seeded:project-memory",
                           "memory carried over from project-memory/round-clock"),
                          ("seeded:shared", "memory seeded with a copy of the shared memory"),
                          ("seeded:none", "first run: empty")):
            with self.subTest(out=out):
                p, _, _, _ = self._run("round-clock", {"STUB_PREP_OUT": out})
                self.assertIn(said, p.stdout)

    def test_a_refused_session_prepares_nothing(self):
        p, runs, _, _ = self._run("round-clock",
                                  {"PHASEKIT_CONTRACTS_MOUNT": "/nonexistent/contracts"})
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(runs, [])


class SeedScript(unittest.TestCase):
    """The script the preparing container runs, against a scratch volume."""

    def setUp(self):
        self.script = _seed_script()
        self.vol = Path(tempfile.mkdtemp(prefix="pk-ses-vol-"))
        self.addCleanup(shutil.rmtree, self.vol, True)
        self.shared = self.vol / "projects" / "-workspace"
        self.legacy = self.shared / "memory"
        self.v187 = self.vol / "project-memory"
        self.base = self.vol / "project-sessions"

    def seed(self, key):
        r = subprocess.run(["sh", "-c", self.script, "sh", key, str(self.vol)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    @staticmethod
    def tree(d):
        return {str(p.relative_to(d)): p.read_text() for p in sorted(d.rglob("*")) if p.is_file()}

    def _shared_transcripts(self):
        self.shared.mkdir(parents=True, exist_ok=True)
        (self.shared / "aaaa.jsonl").write_text('{"gitBranch":"iter/1-other-project"}\n')
        (self.shared / "aaaa").mkdir()
        (self.shared / "aaaa" / "tool.txt").write_text("other project's file\n")

    def test_a_first_run_carries_the_v0187_memory_over_and_moves_nothing(self):
        self._shared_transcripts()
        self.legacy.mkdir(parents=True)
        (self.legacy / "MEMORY.md").write_text("shared, older\n")
        own = self.v187 / "round-clock"
        (own / "sub").mkdir(parents=True)
        (own / "MEMORY.md").write_text("- [a](a.md)\n")
        (own / "a.md").write_text("round-clock's own note\n")
        (own / "sub" / "b.md").write_text("note b\n")
        other = self.v187 / "xmeo-v3"
        other.mkdir()
        (other / "MEMORY.md").write_text("xmeo's\n")
        before = (self.tree(self.vol))
        self.assertEqual(self.seed("round-clock"), "seeded:project-memory")
        dst = self.base / "round-clock"
        self.assertEqual(self.tree(dst / "memory"), self.tree(own))
        # no transcript, no other project's anything
        self.assertEqual(sorted(p.name for p in dst.iterdir()), ["memory"])
        # nothing moved or changed: every file that was there is still there, unchanged
        after = self.tree(self.vol)
        for path, body in before.items():
            self.assertEqual(after.get(path), body, path)
        self.assertEqual(oct(dst.stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct((dst / "memory").stat().st_mode & 0o777), "0o755")
        self.assertFalse((self.base / ".seed" / "round-clock").exists())

    def test_without_a_v0187_memory_the_shared_memory_is_copied(self):
        self._shared_transcripts()
        self.legacy.mkdir(parents=True)
        (self.legacy / "MEMORY.md").write_text("shared\n")
        (self.v187 / "someone-else").mkdir(parents=True)
        (self.v187 / "someone-else" / "MEMORY.md").write_text("not mine\n")
        self.assertEqual(self.seed("round-clock"), "seeded:shared")
        dst = self.base / "round-clock"
        self.assertEqual(self.tree(dst), {"memory/MEMORY.md": "shared\n"})
        self.assertEqual(self.tree(self.legacy), {"MEMORY.md": "shared\n"})

    def test_later_runs_keep_the_directory_and_it_diverges(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "MEMORY.md").write_text("v1\n")
        self.assertEqual(self.seed("a"), "seeded:shared")
        (self.legacy / "MEMORY.md").write_text("v2 from an old-version project\n")
        (self.base / "a" / "memory" / "own.md").write_text("a's own\n")
        (self.base / "a" / "s1.jsonl").write_text("a's transcript\n")
        self.assertEqual(self.seed("a"), "exists")
        self.assertEqual(self.tree(self.base / "a"),
                         {"memory/MEMORY.md": "v1\n", "memory/own.md": "a's own\n",
                          "s1.jsonl": "a's transcript\n"})
        self.assertEqual(self.seed("b"), "seeded:shared")
        self.assertEqual(self.tree(self.base / "b"),
                         {"memory/MEMORY.md": "v2 from an old-version project\n"})

    def test_nothing_yet_creates_the_mountpoint_and_an_empty_directory(self):
        self.assertEqual(self.seed("fresh"), "seeded:none")
        self.assertTrue(self.shared.is_dir())
        self.assertEqual([p.name for p in (self.base / "fresh").iterdir()], ["memory"])
        self.assertEqual(list((self.base / "fresh" / "memory").iterdir()), [])

    def test_a_killed_seed_leaves_no_half_directory_and_is_redone(self):
        (self.v187 / "k").mkdir(parents=True)
        (self.v187 / "k" / "MEMORY.md").write_text("whole\n")
        work = self.base / ".seed" / "k"
        half = work / "XXabcd"
        half.mkdir(parents=True)
        (half / "partial.md").write_text("half\n")
        hour_ago = time.time() - 2 * 3600
        os.utime(half, (hour_ago, hour_ago))
        running = work / "XXlive"  # a seed of the same key still being filled
        running.mkdir()
        other = self.base / ".seed" / "k.other" / "XXefgh"
        other.mkdir(parents=True)
        self.assertEqual(self.seed("k"), "seeded:project-memory")
        self.assertEqual(self.tree(self.base / "k"), {"memory/MEMORY.md": "whole\n"})
        self.assertFalse(half.exists(), "a killed seed's leftovers are cleared")
        self.assertTrue(running.is_dir(), "a seed that may still be running is not touched")
        self.assertTrue(other.is_dir(), "another key's seed in flight is not touched")

    def test_an_existing_directory_is_never_reseeded(self):
        (self.v187 / "k").mkdir(parents=True)
        (self.v187 / "k" / "MEMORY.md").write_text("v187\n")
        own = self.base / "k"
        own.mkdir(parents=True)
        self.assertEqual(self.seed("k"), "exists")
        self.assertEqual(list(own.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
