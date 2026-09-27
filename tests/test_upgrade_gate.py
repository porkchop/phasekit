#!/usr/bin/env python3
"""`phasekit upgrade` v0.16.0: the project's gate runs first, keep-local is
remembered, a failed stage is loud, and releases name the surfaces they moved.

Row 813's evidence, each pinned below:
  * an upgrade commit ran no gate, so xmeo-v3's master went red (9 tests) from
    one upgrade and nothing said so until a session blocked;
  * `--keep-local` was forgotten after one upgrade, so the next plain upgrade
    TOOK NEW and deleted xmeo's amended docs/CONVENTIONS.md — three times;
  * a stale .git/index.lock made every `git add` fail and the upgrade returned
    as if there were nothing to commit (round-clock, v0.15.0 and v0.15.1);
  * the red-verify advice offered VERIFY_SKIP=1 as a peer of fixing the gate;
  * v0.14.10 added three jq bindings to the loop without its release saying so.

Gate tests run in HOST mode so they are deterministic without docker; the
container branch is exercised through a runner image that cannot exist.

Run: `python3 -m unittest tests.test_upgrade_gate`
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
SURFACES = REPO_ROOT / "scripts" / "release-surfaces.py"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
REINSTALLED = ".claude/hooks/require-verdict.sh"
NO_SUCH_IMAGE = "phasekit-test-no-such-runner-image"


FAKE_DOCKER = """#!/usr/bin/env bash
# A docker stand-in: FAKE_DOCKER_MODE = down | noimage | ready.
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info)  [ "$FAKE_DOCKER_MODE" = down ] && exit 1; echo 27.0; exit 0 ;;
  image) [ "$FAKE_DOCKER_MODE" = ready ] && exit 0; exit 1 ;;
  run)   exit "${FAKE_DOCKER_RUN_RC:-0}" ;;
  rm)    exit 0 ;;
esac
exit 0
"""


def load_enrich():
    spec = importlib.util.spec_from_file_location("enrich_project", ENRICH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gate_script(body):
    return ("#!/usr/bin/env bash\nset -euo pipefail\nPHASEKIT_VERIFY_CONFIGURED=1\n"
            + body + "\n")


class Fixture(unittest.TestCase):
    """A committed project whose next upgrade has a real file to reinstall."""

    GATE = None  # None = keep the scaffold's unconfigured stub
    NAME = "project"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-upggate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.docker_log = self.tmp / "docker.log"
        self.project = self.tmp / self.NAME
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        subprocess.run([sys.executable, str(ENRICH), str(self.project)],
                       capture_output=True, text=True, check=True)
        if self.GATE is not None:
            (self.project / "scripts" / "phasekit-verify.sh").write_text(gate_script(self.GATE))
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        (self.project / REINSTALLED).unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "aged")

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self.project), *args],
                           capture_output=True, text=True)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def env(self, mode="host", docker=None, extra=None):
        e = dict(os.environ)
        for k in ("PHASEKIT_VERIFY_CMD", "PHASEKIT_UPGRADE_VERIFY",
                  "PHASEKIT_UPGRADE_VERIFY_TIMEOUT", "PHASEKIT_RUNNER_IMAGE",
                  "PHASEKIT_CONTAINER_USER", "PHASEKIT_ROOTLESS_DOCKER"):
            e.pop(k, None)
        e["XDG_STATE_HOME"] = str(self.state)
        if mode is not None:
            e["PHASEKIT_UPGRADE_VERIFY"] = mode
        if docker == "none":  # no docker CLI at all: a PATH of just what is needed
            nod = self.tmp / "nodocker"
            if not nod.exists():
                nod.mkdir()
                for tool in ("git", "bash", "sh", "env"):
                    os.symlink(shutil.which(tool), nod / tool)
            e["PATH"] = str(nod)
        elif docker is not None:  # the fake docker, in the given mode
            e["PATH"] = f"{self.bin}{os.pathsep}{e.get('PATH', '')}"
            e["FAKE_DOCKER_MODE"] = docker
            e["FAKE_DOCKER_LOG"] = str(self.docker_log)
        e.update(extra or {})
        return e

    def upgrade(self, *extra, mode="host", env=None, docker=None):
        return subprocess.run(
            [sys.executable, str(ENRICH), "--upgrade", str(self.project), "--yes", *extra],
            capture_output=True, text=True, env=self.env(mode, docker, env))

    def check(self):
        return subprocess.run([sys.executable, str(ENRICH), "--check", str(self.project)],
                              capture_output=True, text=True, env=self.env())

    def pending_dirs(self):
        root = self.state / "phasekit" / "upgrade-pending"
        return list(root.iterdir()) if root.exists() else []

    def docker_calls(self):
        return self.docker_log.read_text().splitlines() if self.docker_log.exists() else []

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def subject(self):
        return self.git("log", "-1", "--format=%s").stdout.strip()

    def porcelain(self):
        return self.git("status", "--porcelain").stdout.strip()

    def manifest_entry(self, path):
        m = json.loads((self.project / ".scaffold" / "manifest.json").read_text())
        return next(f for f in m["files"] if f["path"] == path)


class RedGateRestoresAndCommitsNothing(Fixture):
    GATE = 'echo "BOOM from the project gate"; exit 1'

    def test_red_gate_restores_the_tree_and_commits_nothing(self):
        before_head, manifest_before = self.head(), (
            self.project / ".scaffold" / "manifest.json").read_bytes()
        r = self.upgrade()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.head(), before_head, "a red gate must commit nothing")
        self.assertEqual(self.porcelain(), "", "the tree must be restored byte-for-byte")
        self.assertFalse((self.project / REINSTALLED).exists())
        self.assertEqual((self.project / ".scaffold" / "manifest.json").read_bytes(),
                         manifest_before)
        for needle in ("UPGRADE NOT APPLIED", "BOOM from the project gate", REINSTALLED,
                       "scripts/phasekit-verify.sh (host)"):
            self.assertIn(needle, r.stderr)

    def test_no_verify_commits_anyway_and_says_so(self):
        r = self.upgrade("--no-verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.subject().endswith("(unverified: --no-verify)"), self.subject())
        self.assertTrue((self.project / REINSTALLED).is_file())

    def test_no_commit_never_runs_the_gate(self):
        r = self.upgrade("--no-commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("gate:", r.stdout)
        self.assertTrue((self.project / REINSTALLED).is_file())

    def test_container_mode_without_the_image_fails_closed_and_never_runs_bare(self):
        r = self.upgrade(mode="container", env={"PHASEKIT_RUNNER_IMAGE": NO_SUCH_IMAGE})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("refusing to run the gate on the host", r.stderr)
        self.assertNotIn("BOOM", r.stderr, "the gate must not have run on the host")
        self.assertEqual(self.porcelain(), "")

    def test_auto_mode_with_docker_but_no_image_refuses_instead_of_running_bare(self):
        """The supervisor-host hazard: docker is there, the image is not — a
        silent host fallback would run a project's suite beside live runners."""
        r = self.upgrade(mode="auto", docker="noimage")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("could not run here", r.stderr)
        self.assertIn("container-setup.sh build", r.stderr)
        self.assertNotIn("BOOM", r.stderr)
        self.assertEqual(self.porcelain(), "")


class GreenGateCommits(Fixture):
    GATE = 'echo "all good"'

    def test_green_gate_commits_with_the_plain_subject(self):
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gate: scripts/phasekit-verify.sh passed (host)", r.stdout)
        self.assertNotIn("unverified", self.subject())
        self.assertEqual(self.porcelain(), "")

    def test_auto_mode_uses_the_host_only_when_there_is_no_docker_cli(self):
        r = self.upgrade(mode="auto", docker="none")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passed (host)", r.stdout)

    def test_auto_mode_refuses_when_the_daemon_does_not_answer(self):
        """A wedged daemon, or a shell without the rootless DOCKER_HOST, is the
        supervisor host on a bad day — never a licence to run bare."""
        r = self.upgrade(mode="auto", docker="down")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("the docker daemon did not answer", r.stderr)
        self.assertEqual(self.porcelain(), "")

    def test_auto_mode_uses_the_runner_when_it_is_ready(self):
        r = self.upgrade(mode="auto", docker="ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passed (container)", r.stdout)
        run = next(c for c in self.docker_calls() if c.startswith("run "))
        self.assertIn(f"--user {os.getuid()}:{os.getgid()}", run)
        self.assertIn("-eo pipefail -c bash scripts/phasekit-verify.sh", run)

    def test_a_manifest_only_re_run_runs_no_gate(self):
        self.assertEqual(self.upgrade().returncode, 0)
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("gate:", r.stdout, "nothing moved, so there is nothing to verify")

    def test_an_unknown_mode_is_an_error_not_lock_contention(self):
        r = self.upgrade(mode="sometimes")
        self.assertEqual(r.returncode, 1, "2 means another process holds the lock")
        self.assertIn("PHASEKIT_UPGRADE_VERIFY", r.stderr)
        self.assertFalse((self.project / REINSTALLED).exists())


class TheGateMatchesTheLoopsSemantics(Fixture):
    GATE = 'echo "script gate is fine"'

    def test_verify_cmd_overrides_the_script_and_runs_under_pipefail(self):
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "false | true"})
        self.assertEqual(r.returncode, 4, "`false | true` is red under the loop's pipefail")
        self.assertIn("PHASEKIT_VERIFY_CMD", r.stderr)

    def test_a_gate_past_its_timeout_is_red(self):
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "sleep 5",
                              "PHASEKIT_UPGRADE_VERIFY_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 4)
        self.assertIn("timed out after 1s", r.stderr)


class StubGateIsSkipped(Fixture):
    def test_an_unconfigured_gate_is_skipped_not_failed(self):
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gate: skipped — the verify gate was still the unconfigured stub",
                      r.stdout)

    def test_a_gate_the_upgrade_itself_seeds_is_not_run(self):
        """Stub before, real stack gate after: the project never configured a
        gate, so it must not be blocked by the one this upgrade just gave it."""
        r = self.upgrade("--profile", "python-uv")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PHASEKIT_VERIFY_CONFIGURED=1",
                      (self.project / "scripts" / "phasekit-verify.sh").read_text())
        self.assertIn("gate: skipped", r.stdout)
        self.assertEqual(self.porcelain(), "")


class KeepLocalIsRemembered(Fixture):
    """xmeo's CONVENTIONS.md, three times: keep-local once, then a plain upgrade
    found local == manifest with a newer scaffold version and took new."""

    PATH = "docs/QUALITY_GATES.md"

    def customise_and_keep(self):
        f = self.project / self.PATH
        f.write_text(f.read_text() + "\n## Project amendment\nKept by the project.\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "project amends a scaffold doc")
        r = self.upgrade("--keep-local", self.PATH)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.manifest_entry(self.PATH).get("local"), "kept")

    def test_a_plain_later_upgrade_keeps_the_projects_version(self):
        self.customise_and_keep()
        r = self.upgrade()  # no flags: before v0.16.0 this TOOK NEW
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Project amendment", (self.project / self.PATH).read_text())
        self.assertIn("standing keep-local", r.stdout)
        self.assertEqual(self.manifest_entry(self.PATH).get("local"), "kept")

    def test_take_new_releases_the_standing_decision(self):
        self.customise_and_keep()
        r = self.upgrade("--take-new", self.PATH)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Project amendment", (self.project / self.PATH).read_text())
        self.assertNotIn("local", self.manifest_entry(self.PATH))

    def test_a_default_bootstrap_keep_is_not_recorded_as_a_decision(self):
        f = self.project / "docs" / "SPEC.md"
        f.write_text(f.read_text() + "\nproject-owned edit\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "edit spec")
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("local", self.manifest_entry("docs/SPEC.md"))

    def test_reconcile_carries_the_decision(self):
        self.customise_and_keep()
        subprocess.run([sys.executable, str(ENRICH), "--reconcile", "--force", str(self.project)],
                       capture_output=True, text=True, check=True)
        self.assertEqual(self.manifest_entry(self.PATH).get("local"), "kept")


class AFailedStageIsLoud(Fixture):
    def test_a_stale_index_lock_exits_5_and_the_next_upgrade_commits_it(self):
        lock = self.project / ".git" / "index.lock"
        lock.write_text("")
        before = self.head()
        r = self.upgrade()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("could not stage the upgrade", r.stderr)
        self.assertIn("index.lock", r.stderr)
        self.assertTrue((self.project / REINSTALLED).is_file(), "files stay installed")
        self.assertEqual(self.head(), before)
        c = self.check()
        self.assertEqual(c.returncode, 3, "a pending upgrade is not a clean project")
        self.assertIn("PENDING UPGRADE", c.stderr)
        lock.unlink()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("completing an earlier upgrade", r.stdout)
        self.assertTrue(self.subject().startswith("chore(scaffold): phasekit upgrade"))
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(self.pending_dirs(), [])
        self.assertEqual(self.check().returncode, 0)


class TheGateIsReadOnly(Fixture):
    GATE = 'echo "fine"'

    def test_a_gate_that_writes_a_new_file_is_red_and_its_file_removed(self):
        (self.project / "mine.txt").write_text("the project's own untracked file\n")
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "mkdir -p out && echo x > out/gate.txt"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("wrote into the tree", r.stderr)
        self.assertIn("out/gate.txt", r.stderr)
        self.assertFalse((self.project / "out").exists())
        self.assertTrue((self.project / "mine.txt").is_file(), "pre-existing work is kept")
        self.assertEqual(self.porcelain(), "?? mine.txt")

    def test_a_gate_that_stages_is_red_and_the_index_restored(self):
        (self.project / "mine.txt").write_text("unstaged on purpose\n")
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "git add -A"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("(the git index)", r.stderr)
        self.assertEqual(self.porcelain(), "?? mine.txt")

    def test_a_gate_that_edits_and_stages_a_source_file_is_fully_restored(self):
        """lint-staged style: format a project file and `git add` it."""
        src = self.project / "src.txt"
        src.write_text("original\n")
        self.git("add", "src.txt")
        self.git("commit", "-qm", "src")
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD":
                              "echo GATE >> src.txt; echo x > new.txt; git add src.txt new.txt"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(src.read_text(), "original\n")
        self.assertFalse((self.project / "new.txt").exists())
        self.assertEqual(self.porcelain(), "")

    def test_a_tree_git_cannot_read_is_refused_not_passed(self):
        (self.project / ".git" / "HEAD").write_text("garbage\n")
        self.addCleanup((self.project / ".git" / "HEAD").write_text, "ref: refs/heads/master\n")
        r = self.upgrade()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("cannot observe the tree", r.stderr)

    def test_a_gate_that_edits_a_tracked_file_is_red_and_the_edit_restored(self):
        spec = (self.project / "docs" / "SPEC.md").read_bytes()
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "echo more >> docs/SPEC.md"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual((self.project / "docs" / "SPEC.md").read_bytes(), spec)
        self.assertEqual(self.porcelain(), "")


class AnInterruptedUpgradeIsSettled(Fixture):
    GATE = 'echo "fine"'

    def test_sigterm_mid_gate_restores_and_exits_130(self):
        started = time.monotonic()
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -TERM $PPID; sleep 30"})
        self.assertLess(time.monotonic() - started, 20, "the gate must be killed, not awaited")
        self.assertEqual(r.returncode, 130, r.stdout + r.stderr)
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(self.pending_dirs(), [])

    def test_sigkill_mid_gate_is_restored_by_the_next_upgrade(self):
        before = self.head()
        r = self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -KILL $PPID"})
        self.assertEqual(r.returncode, -9)
        self.assertNotEqual(self.porcelain(), "", "the killed run left its writes")
        self.assertEqual(len(self.pending_dirs()), 1)
        c = self.check()
        self.assertEqual(c.returncode, 3, c.stdout + c.stderr)
        self.assertEqual(self.head(), before)
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("interrupted before its verdict; restoring", r.stderr)
        self.assertIn("gate: scripts/phasekit-verify.sh passed", r.stdout)
        self.assertEqual(self.porcelain(), "")
        self.assertTrue((self.project / REINSTALLED).is_file())
        self.assertEqual(self.pending_dirs(), [])

    def test_recovery_never_clobbers_work_done_after_the_kill(self):
        """A session ran on the half-upgraded tree before anyone re-ran the
        upgrade: its commits and its uncommitted edits win."""
        self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -KILL $PPID"})
        settings = self.project / ".claude" / "settings.json"
        settings.write_text(settings.read_text() + "\n")  # the session's uncommitted edit
        mine = settings.read_bytes()
        r = self.upgrade("--no-commit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("left as they are: .claude/settings.json", r.stderr)
        self.assertEqual(settings.read_bytes(), mine)

    def test_a_failed_commit_exits_5_and_stays_pending(self):
        hook = self.project / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        r = self.upgrade()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(len(self.pending_dirs()), 1)
        hook.unlink()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.porcelain(), "")

    def test_an_in_flight_settings_edit_is_not_swept_into_the_commit(self):
        settings = self.project / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        data["projectInFlight"] = True
        settings.write_text(json.dumps(data, indent=2) + "\n")
        before = self.head()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        committed = self.git("log", "--name-only", "--format=", f"{before}..HEAD").stdout.split()
        self.assertNotIn(".claude/settings.json", committed)

    def test_check_reports_a_corrupt_record_as_pending(self):
        self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -KILL $PPID"})
        [d] = self.pending_dirs()
        (d / "pending.json").write_text("{not json")
        self.assertEqual(self.check().returncode, 3)

    def test_a_verified_recovery_commits_only_what_the_upgrade_wrote(self):
        lock = self.project / ".git" / "index.lock"
        lock.write_text("")
        self.assertEqual(self.upgrade().returncode, 5)
        before = self.head()
        spec = self.project / "docs" / "SPEC.md"
        spec.write_text(spec.read_text() + "\nthe project's own edit\n")
        lock.unlink()
        r = self.upgrade()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        committed = self.git("log", "--name-only", "--format=", f"{before}..HEAD").stdout.split()
        self.assertIn(REINSTALLED, committed)
        self.assertNotIn("docs/SPEC.md", committed)
        self.assertEqual(self.porcelain(), "M docs/SPEC.md")

    def test_no_commit_leaves_no_pending_record(self):
        r = self.upgrade("--no-commit",
                         env={"PHASEKIT_VERIFY_CMD": "true"})  # gate off under --no-commit
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.pending_dirs(), [])

    def test_an_unreadable_record_stops_and_is_never_discarded(self):
        self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -KILL $PPID"})
        [d] = self.pending_dirs()
        (d / "pending.json").write_text("{not json")
        r = self.upgrade()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unreadable", r.stderr)
        self.assertTrue((d / "pending.json").exists())

    def test_dry_run_refuses_while_an_upgrade_is_pending(self):
        self.upgrade(env={"PHASEKIT_VERIFY_CMD": "kill -KILL $PPID"})
        r = self.upgrade("--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("pending", r.stderr)


class TheRunnerInvocation(Fixture):
    GATE = 'echo "fine"'
    NAME = "pro:ject,x"

    def run_call(self):
        return next(c for c in self.docker_calls() if c.startswith("run "))

    def test_a_path_with_colon_and_comma_is_one_mount_field(self):
        r = self.upgrade(mode="container", docker="ready")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'type=bind,"src={self.project}",dst=/workspace', self.run_call())

    def test_container_user_is_honoured_and_root_normalised(self):
        r = self.upgrade(mode="container", docker="ready",
                         env={"PHASEKIT_CONTAINER_USER": "root"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--user 0:0", self.run_call())

    def test_a_runner_that_cannot_start_is_infra_not_the_projects_red(self):
        r = self.upgrade(mode="container", docker="ready", env={"FAKE_DOCKER_RUN_RC": "125"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("could not run here", r.stderr)
        self.assertIn("docker exit 125", r.stderr)
        self.assertEqual(self.porcelain(), "")


class TheExitCodesArePinned(unittest.TestCase):
    def test_the_upgrade_exit_constants_are_in_the_contract(self):
        mod = load_enrich()
        spec = json.loads((REPO_ROOT / "contracts" / "interface.json").read_text())
        codes = spec["exit_codes"]["scripts/enrich-project.py"]["codes"]
        for const in (mod.EXIT_UPGRADE_GATE_RED, mod.EXIT_UPGRADE_UNCOMMITTED, 130):
            self.assertIn(str(const), codes)


class TheRedVerifyAdviceFixesFirst(unittest.TestCase):
    def test_the_bypass_is_the_last_resort_not_a_peer(self):
        text = LOOP.read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "next_step:" in l and "verify" in l
                    and "VERIFY_SKIP" in l)
        self.assertLess(line.index("fix the failing verify"), line.index("VERIFY_SKIP"))
        self.assertIn("last resort", line)


class ReleaseSurfaces(unittest.TestCase):
    def have(self, *tags):
        return all(subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "-q", "--verify",
                                   f"refs/tags/{t}"], capture_output=True).returncode == 0
                   for t in tags)

    def test_a_function_ends_at_its_closing_brace(self):
        mod_spec = importlib.util.spec_from_file_location("surfaces", SURFACES)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        text = ("f() {\n  jq --arg a 1 .\n}\njq --slurpfile b x --rawfile c y .\n")
        _, binds = mod.loop_surfaces(text, "loop.sh")
        self.assertEqual(binds["f"], {"a"})
        self.assertEqual(binds["loop.sh (top level)"], {"b", "c"})

    def test_an_indented_function_and_an_inner_brace_group_are_scoped(self):
        mod_spec = importlib.util.spec_from_file_location("surfaces", SURFACES)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        text = ("  g() {\n    { jq --arg x 1 .; }\n    jq --arg y 1 .\n  }\n"
                "jq --arg z 1 .\n")
        _, binds = mod.loop_surfaces(text, "loop.sh")
        self.assertEqual(binds["g"], {"x", "y"})
        self.assertEqual(binds["loop.sh (top level)"], {"z"})

    def test_it_names_the_bindings_that_broke_a_downstream_pin(self):
        if not self.have("v0.14.9", "v0.14.10"):
            self.skipTest("release tags are not fetched in this clone")
        r = subprocess.run([sys.executable, str(SURFACES), "v0.14.9", "v0.14.10"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("record_verify_failure(): +[footprint, recipe, rule]", r.stdout)
        self.assertIn("contract conventions added: verify-gate-read-only", r.stdout)

    def test_an_unreadable_ref_exits_2(self):
        r = subprocess.run([sys.executable, str(SURFACES), "no-such-ref-xyz"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
