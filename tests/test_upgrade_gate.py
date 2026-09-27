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
a="$*"; nl='\\n'; echo "${a//$'\n'/$nl}" >> "$FAKE_DOCKER_LOG"  # one line per call
case "$1" in
  info)  [ "$FAKE_DOCKER_MODE" = down ] && exit 1; echo 27.0; exit 0 ;;
  image) [ "$FAKE_DOCKER_MODE" = ready ] && exit 0; exit 1 ;;
  run)   [ -n "${FAKE_DOCKER_RUN_SAY:-}" ] && echo "$FAKE_DOCKER_RUN_SAY"
         exit "${FAKE_DOCKER_RUN_RC:-0}" ;;
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
        self.assertIn("-eo pipefail -c mkdir -p", run)  # the identity preamble, then the gate
        self.assertTrue(run.endswith("\\nbash scripts/phasekit-verify.sh"), run)

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


class TheGateRunsInASessionsEnvironment(Fixture):
    """Row 1137 (v0.16.2): foundry-orchestrator's gate was refused with 15 red
    on a green tree — no git identity and no contracts provider in the runner,
    both of which a session has. The gate now mirrors a session."""

    GATE = 'echo "fine"'

    def run_call(self):
        return next(c for c in self.docker_calls() if c.startswith("run "))

    def provider(self, name="provider"):
        d = self.tmp / name
        d.mkdir()
        (d / "index.json").write_text('{"contracts": []}\n')
        return d

    def no_identity_env(self, extra=None):
        """A host with no global or system git identity at all."""
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        e = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": ""}
        e.update(extra or {})
        return e

    def upgrade_clean(self, *extra, mode="host", env=None, docker=None):
        e = self.env(mode, docker)
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                  "GIT_COMMITTER_EMAIL", "GIT_USER_NAME", "GIT_USER_EMAIL",
                  "PHASEKIT_CONTRACTS_DIR", "PHASEKIT_CONTRACTS_MOUNT", "PHASEKIT_FORWARD_ENV"):
            e.pop(k, None)
        e.update(env or {})
        if e.get("GIT_CONFIG_GLOBAL") == "":
            del e["GIT_CONFIG_GLOBAL"]
        return subprocess.run(
            [sys.executable, str(ENRICH), "--upgrade", str(self.project), "--yes", *extra],
            capture_output=True, text=True, env=e)

    # --- the runner's argv (fake docker) ---------------------------------

    def test_the_runner_gets_the_repos_identity_written_as_a_session_writes_it(self):
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTAINER_USER": "root"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        call = self.run_call()
        self.assertIn("-e GIT_USER_NAME=t ", call)
        self.assertIn("-e GIT_USER_EMAIL=t@t ", call)
        self.assertIn('git config --global user.name "$GIT_USER_NAME"', call)
        # config, never env: env would outrank a test's own scratch-repo identity
        self.assertNotIn("GIT_AUTHOR_NAME", call)
        self.assertNotIn("GIT_COMMITTER_NAME", call)

    def test_git_user_name_overrides_like_container_setup(self):
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"GIT_USER_NAME": "Ops", "GIT_USER_EMAIL": "ops@x"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("-e GIT_USER_NAME=Ops ", self.run_call())
        self.assertIn("-e GIT_USER_EMAIL=ops@x ", self.run_call())

    def test_root_gets_the_sessions_home_and_sandbox_flag(self):
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTAINER_USER": "root"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        call = self.run_call()
        self.assertIn("-e HOME=/home/node ", call)
        self.assertIn("-e IS_SANDBOX=1 ", call)
        self.assertIn("-e CLAUDE_CONFIG_DIR=/home/node/.claude ", call)

    def test_a_uid_that_cannot_write_the_images_home_gets_a_throwaway_one(self):
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTAINER_USER": "4242:4242"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        call = self.run_call()
        self.assertIn("-e HOME=/tmp/phasekit-upgrade-home ", call)
        self.assertNotIn("IS_SANDBOX", call)

    def test_the_contracts_dir_is_mounted_read_only_at_slash_contracts(self):
        d = self.provider("pro:vi,der")
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTRACTS_DIR": str(d)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        call = self.run_call()
        self.assertIn(f'--mount type=bind,"src={d}",dst=/contracts,readonly ', call)
        self.assertIn("-e PHASEKIT_CONTRACTS_DIR=/contracts ", call)

    def test_the_mount_name_wins_over_the_dir_name(self):
        mount, other = self.provider("mount"), self.provider("other")
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTRACTS_MOUNT": str(mount),
                                    "PHASEKIT_CONTRACTS_DIR": str(other)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'"src={mount}",dst=/contracts,readonly', self.run_call())
        self.assertNotIn(f'"src={other}"', self.run_call())

    def test_no_provider_means_no_mount(self):
        r = self.upgrade_clean(mode="container", docker="ready")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("/contracts", self.run_call())

    def test_an_unusable_provider_refuses_before_any_run(self):
        empty = self.tmp / "empty-provider"
        empty.mkdir()
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTRACTS_DIR": str(empty)})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("no readable index.json", r.stderr)
        self.assertFalse([c for c in self.docker_calls() if c.startswith("run ")])
        self.assertEqual(self.porcelain(), "")

    def test_a_forwarded_key_cannot_repoint_the_provider(self):
        d = self.provider()
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTRACTS_DIR": str(d),
                                    "PHASEKIT_FORWARD_ENV": "PHASEKIT_CONTRACTS_DIR,GIT_USER_NAME"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        call = self.run_call()
        self.assertNotIn("-e PHASEKIT_CONTRACTS_DIR ", call)
        self.assertNotIn("-e GIT_USER_NAME ", call)

    # --- the host branch, for real ---------------------------------------

    COMMITS_IN_SCRATCH = ('d=$(mktemp -d); trap \'rm -rf "$d"\' EXIT; git -C "$d" init -q; '
                          'git -C "$d" commit -q --allow-empty -m gate; ')

    def test_a_host_gate_that_commits_passes_on_a_host_with_no_identity(self):
        r = self.upgrade_clean(env=self.no_identity_env({
            "PHASEKIT_VERIFY_CMD": self.COMMITS_IN_SCRATCH
            + 'test "$(git -C "$d" log -1 --format=%an/%ae)" = t/t@t'}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(".claude", self.porcelain())

    def test_a_scratch_repos_own_identity_still_wins_on_the_host(self):
        r = self.upgrade_clean(env=self.no_identity_env({
            "PHASEKIT_VERIFY_CMD": 'd=$(mktemp -d); trap \'rm -rf "$d"\' EXIT; '
            'git -C "$d" init -q; git -C "$d" config user.name mine; '
            'git -C "$d" config user.email mine@x; '
            'git -C "$d" commit -q --allow-empty -m gate; '
            'test "$(git -C "$d" log -1 --format=%an)" = mine'}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_host_that_has_an_identity_is_left_as_it_is(self):
        env = self.no_identity_env({"PHASEKIT_VERIFY_CMD": 'test -z "${GIT_CONFIG_GLOBAL:-}"'})
        (Path(env["HOME"]) / ".gitconfig").write_text("[user]\n\tname = h\n\temail = h@h\n")
        r = self.upgrade_clean(env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_host_keeps_its_other_global_settings(self):
        env = self.no_identity_env({"PHASEKIT_VERIFY_CMD":
                                    'test "$(git config --get pk.marker)" = kept'})
        (Path(env["HOME"]) / ".gitconfig").write_text("[pk]\n\tmarker = kept\n")
        r = self.upgrade_clean(env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_host_gate_sees_the_mount_as_the_contracts_dir(self):
        d = self.provider()
        r = self.upgrade_clean(env={"PHASEKIT_CONTRACTS_MOUNT": str(d),
                                    "PHASEKIT_VERIFY_CMD":
                                    f'test "$PHASEKIT_CONTRACTS_DIR" = "{d}"'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_fixed_identity_when_nothing_names_one(self):
        mod = load_enrich()
        bare = self.tmp / "bare"
        bare.mkdir()
        subprocess.run(["git", "-C", str(bare), "init", "-q"], check=True)
        saved = dict(os.environ)
        try:
            for k in ("GIT_USER_NAME", "GIT_USER_EMAIL", "GIT_CONFIG_GLOBAL"):
                os.environ.pop(k, None)
            os.environ.update(self.no_identity_env())
            del os.environ["GIT_CONFIG_GLOBAL"]
            self.assertEqual(mod._gate_git_identity(bare),
                             (mod.GATE_GIT_NAME_DEFAULT, mod.GATE_GIT_EMAIL_DEFAULT))
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_the_images_user_by_name_or_bare_uid_gets_the_sessions_home(self):
        mod = load_enrich()
        for user in ("node", "node:node", "0", "0:0", "1000", "1000:1000"):
            self.assertEqual(mod._runner_home(user), "/home/node", user)
        for user in ("4242:4242", "nobody"):
            self.assertEqual(mod._runner_home(user), mod.RUNNER_THROWAWAY_HOME, user)
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"PHASEKIT_CONTAINER_USER": "node"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("-e HOME=/home/node ", self.run_call())

    def test_a_preamble_failure_is_infra_not_the_projects_red(self):
        mod = load_enrich()
        ro = self.tmp / "ro-home"
        ro.mkdir()
        ro.chmod(0o500)
        self.addCleanup(ro.chmod, 0o700)
        env = {"PATH": os.environ["PATH"], "HOME": str(ro / "sub"),
               "GIT_USER_NAME": "n", "GIT_USER_EMAIL": "e@e", "GIT_CONFIG_NOSYSTEM": "1"}
        r = subprocess.run(["bash", "-eo", "pipefail", "-c",
                            mod.GATE_IDENTITY_PREAMBLE + "\necho REACHED"],
                           capture_output=True, text=True, env=env)
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        self.assertEqual(r.returncode, mod.RUNNER_START_FAILED_RC, r.stdout + r.stderr)
        self.assertIn(mod.GATE_SETUP_FAILED_MARK, r.stderr)
        self.assertNotIn("REACHED", r.stdout)
        # and a writable HOME writes the identity then runs the gate
        home = self.tmp / "okhome"
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", mod.GATE_IDENTITY_PREAMBLE
                            + '\ntest "$(git config --global user.name)" = n; echo REACHED'],
                           capture_output=True, text=True, env={**env, "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REACHED", r.stdout)

    def test_the_setup_mark_turns_a_125_into_setup_infra(self):
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"FAKE_DOCKER_RUN_RC": "125"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("docker exit 125", r.stderr)  # no mark: docker's own failure

    def test_a_marked_125_is_reported_as_the_gates_setup(self):
        mod = load_enrich()
        r = self.upgrade_clean(mode="container", docker="ready",
                               env={"FAKE_DOCKER_RUN_RC": "125",
                                    "FAKE_DOCKER_RUN_SAY": mod.GATE_SETUP_FAILED_MARK})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("session setup failed", r.stderr)
        self.assertEqual(self.porcelain(), "")

    def test_a_tmpdir_inside_the_tree_is_not_the_gates_footprint(self):
        tmpdir = self.project / "work-scratch"  # not ignored by the scaffold
        tmpdir.mkdir()
        r = self.upgrade_clean(env=self.no_identity_env({
            "TMPDIR": str(tmpdir), "PHASEKIT_VERIFY_CMD": "true"}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_tmpdir_in_another_repo_does_not_lend_its_identity(self):
        other = self.tmp / "other"
        other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "lender"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "l@l"], check=True)
        (other / "tmp").mkdir()
        r = self.upgrade_clean(env=self.no_identity_env({
            "TMPDIR": str(other / "tmp"),
            "PHASEKIT_VERIFY_CMD": 'd=$(mktemp -d /tmp/pkgate-XXXX); trap \'rm -rf "$d"\' EXIT; '
            'git -C "$d" init -q; git -C "$d" commit -q --allow-empty -m g; '
            'test "$(git -C "$d" log -1 --format=%an)" = t'}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_relative_provider_is_resolved_where_the_upgrade_ran(self):
        d = self.provider("relprov")
        e = self.env("host")
        for k in ("PHASEKIT_CONTRACTS_DIR", "PHASEKIT_CONTRACTS_MOUNT"):
            e.pop(k, None)
        e["PHASEKIT_CONTRACTS_MOUNT"] = "relprov"
        e["PHASEKIT_VERIFY_CMD"] = f'test "$PHASEKIT_CONTRACTS_DIR" = "{d}"'
        r = subprocess.run([sys.executable, str(ENRICH), "--upgrade", str(self.project), "--yes"],
                           capture_output=True, text=True, env=e, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_relative_git_config_global_is_still_included(self):
        env = self.no_identity_env({"PHASEKIT_VERIFY_CMD":
                                    'test "$(git config --get pk.marker)" = kept'})
        rel = self.tmp / "rel.cfg"
        rel.write_text("[pk]\n\tmarker = kept\n")
        env["GIT_CONFIG_GLOBAL"] = os.path.relpath(rel, os.getcwd())
        r = self.upgrade_clean(env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_host_gate_sees_the_mount_when_both_names_are_set(self):
        mount, other = self.provider("mount"), self.provider("other")
        r = self.upgrade_clean(env={"PHASEKIT_CONTRACTS_MOUNT": str(mount),
                                    "PHASEKIT_CONTRACTS_DIR": str(other),
                                    "PHASEKIT_VERIFY_CMD":
                                    f'test "$PHASEKIT_CONTRACTS_DIR" = "{mount}"'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_unusable_provider_refuses_in_host_mode_too(self):
        r = self.upgrade_clean(env={"PHASEKIT_CONTRACTS_DIR": str(self.tmp / "nope")})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("not a directory", r.stderr)
        self.assertEqual(self.porcelain(), "")

    def test_the_mirror_constants_match_container_setup(self):
        mod = load_enrich()
        setup = (REPO_ROOT / "scripts" / "container-setup.sh").read_text()
        self.assertIn(f'CONTRACTS_CONTAINER_DIR="{mod.CONTRACTS_CONTAINER_DIR}"', setup)
        self.assertIn(f"-e HOME={mod.RUNNER_HOME})", setup)
        contracts = (REPO_ROOT / "scripts" / "phasekit-contracts.py").read_text()
        self.assertIn(f'"{mod.CONTRACTS_CONTAINER_DIR}"', contracts)


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
