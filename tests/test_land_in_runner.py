#!/usr/bin/env python3
"""`phasekit land --in-runner` (v0.21.0): the loop's recovery-only pass in the
runner image, through THE harness verify --in-runner uses.

The gap (foundry-orchestrator deferral resolver-recovery-runs-its-own-docker-
argv, queue row 1837): after v0.20.0 moved the orchestrator's verify onto
`phasekit verify --in-runner`, one private runner invocation remained — its
auto-resolver's run_recovery composed its own `docker run` of a target's
`run-until-done.sh` with MAX_ITERATIONS=0 to land a stranded verdict artifact.
phasekit now declares that entry point; the orchestrator moves onto it next.

Pinned here, in the layout tests/_layout.py selects (LAYOUT_MODULES: vendored
AND pinned), with the SHIPPED loop run inside a docker stand-in that executes
the runner's command on the host with the container's paths mapped:
  * a stranded approval (and a stranded completion, branch-per-iteration
    included) is LANDED by the loop's own verify-gated landing, no model turn;
  * a clean tree is NOTHING to land (exit 3), a red gate is FAILED (exit 4)
    with the artifact still stranded and the work untouched;
  * what it may change is declared, and nothing else changes;
  * it REFUSES (exit 7) while a container works on the tree, while git is
    mid-operation, while another landing runs, and a loop without the witness;
  * a timeout removes the runner, and the next landing finishes the job;
  * the runner call is the gate's (one composer), with the loop's env last.

Run: `python3 -m unittest tests.test_land_in_runner`
"""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)
from _layout import PINNED

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
PIN_TOOL = REPO_ROOT / "scripts" / "phasekit-pin.py"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
PIN_TAG = "v0.19.0"  # what tests/_layout.py mark_pinned writes


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


H = load("pk_boundary_harness_land", Path(__file__).resolve().parent / "test_boundary_state.py")

# A docker stand-in: info/image by FAKE_DOCKER_MODE; `ps`/`inspect` report the
# containers FAKE_DOCKER_PS lists ("name=src" = a writable bind mount, or
# "name=type|rw|src"; comma-separated; every container also carries a tmpfs
# and a volume, which name no host path); `run`
# is logged (one JSON argv per line) and then EXECUTED on the host — each
# --mount dst mapped to its src in the -e values and the command, -w the cwd,
# HOME a scratch dir; a later -e wins, as docker's does.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import csv, json, os, re, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_DOCKER_MODE", "ready")
verb = args[0] if args else ""
listed = [e.split("=", 1) for e in os.environ.get("FAKE_DOCKER_PS", "").split(",") if "=" in e]
if verb == "info":
    if mode == "down":
        sys.exit(1)
    print("[]" if "SecurityOptions" in " ".join(args) else "27.0")
    sys.exit(0)
if verb == "image":
    sys.exit(0 if mode == "ready" else 1)
if verb == "ps":
    if os.environ.get("FAKE_DOCKER_PS_SLEEP"):
        import time
        time.sleep(float(os.environ["FAKE_DOCKER_PS_SLEEP"]))
    if mode == "psfail":
        sys.exit(1)
    for i, _ in enumerate(listed):
        print(f"id{i}")
    sys.exit(0)
if verb == "inspect":
    for i, (name, src) in enumerate(listed):
        mount = src if "|" in src else f"bind|true|{src}"
        print(f"/{name}\ttmpfs|true|\tvolume|true|\t{mount}")
    sys.exit(0)
if verb != "run":
    sys.exit(0)
if os.environ.get("FAKE_DOCKER_RUN_RC"):
    sys.exit(int(os.environ["FAKE_DOCKER_RUN_RC"]))
env, mounts, cwd, i = {}, {}, None, 1
while i < len(args):
    a = args[i]
    if a == "-e":
        k, eq, v = args[i + 1].partition("=")
        env[k] = v if eq else os.environ.get(k, "")
        i += 2
    elif a == "--mount":
        fields = dict(f.split("=", 1) for f in next(csv.reader([args[i + 1]])) if "=" in f)
        mounts[fields["dst"]] = fields["src"]
        i += 2
    elif a in ("--name", "--entrypoint", "--user", "-w"):
        if a == "-w":
            cwd = args[i + 1]
        i += 2
    elif a.startswith("-"):
        i += 1
    else:
        break
rest = args[i + 1:]
def host(text):
    for dst in sorted(mounts, key=len, reverse=True):
        text = re.sub(r"(?<![\w/.-])" + re.escape(dst) + r"(?![\w.-])",
                      lambda m, d=dst: mounts[d], text)
    return text
env = {k: host(v) for k, v in env.items()}
env["HOME"] = os.environ["FAKE_DOCKER_HOME"]
env["PATH"] = os.environ["PATH"]
env["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")
rest = [host(a) for a in rest]
sys.exit(subprocess.run(["bash"] + rest, cwd=host(cwd or "/"), env=env).returncode)
'''

APPROVAL = {"phase": "phase-1", "approved": True, "summary": "built it",
            "suggested_commit_message": "Phase 1 (APPROVED): built it"}


class Fixture(unittest.TestCase):
    """A boundary-harness project (the shipped loop, a stub model, a logging
    gate) in this run's layout, plus a docker stand-in and an engine store."""

    SQUASH = False

    def setUp(self):
        self.repo = H.Repo(squash=self.SQUASH)
        self.addCleanup(self.repo.cleanup)
        self.project = self.repo.repo
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-land-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.docker_log = self.tmp / "docker.log"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.engine = None
        if PINNED:
            self.engine = self.repo.layout.engine
            (self.engine / ".engine-version").write_text(PIN_TAG + "\n")
            self.repo.layout.seal()
            os.symlink(self.engine, self.store / PIN_TAG)

    # -- the tree ----------------------------------------------------------
    def strand_approval(self, **extra):
        self.repo.write("src.txt", "base\nwork by a dead session\n")
        self.repo.write("artifacts/phase-approval.json", json.dumps({**APPROVAL, **extra}) + "\n")

    def git(self, *args, check=True):
        return self.repo.git(*args, check=check)

    # -- running -----------------------------------------------------------
    def env(self, docker="ready", extra=None):
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("PHASEKIT_") or k in ("GIT_USER_NAME", "GIT_USER_EMAIL",
                                                  "MAX_ITERATIONS", "AUTO_PUSH", "SSH_AUTH_SOCK",
                                                  "GH_TOKEN", "GITHUB_TOKEN"):
                del e[k]
        e.update({"PHASEKIT_ENGINE_STORE": str(self.store), "PHASEKIT_NO_AUTO_FETCH": "1",
                  "FAKE_DOCKER_LOG": str(self.docker_log), "FAKE_DOCKER_HOME": str(self.home),
                  "FAKE_DOCKER_MODE": docker, "PHASEKIT_CONTAINER_USER": "root",
                  "STUB_DIR": str(self.repo.stub), "PHASEKIT_FORWARD_ENV": "STUB_DIR",
                  "GIT_CONFIG_NOSYSTEM": "1",
                  "PATH": f"{self.bin}{os.pathsep}{e.get('PATH', '')}"})
        e.update(extra or {})
        return e

    def land(self, *args, docker="ready", env=None, timeout=180):
        return subprocess.run(["bash", str(CLI), "land", "--in-runner", *args],
                              cwd=str(self.project), capture_output=True, text=True,
                              env=self.env(docker, env), timeout=timeout)

    def land_json(self, *args, **kw):
        r = self.land("--json", *args, **kw)
        try:
            return r, json.loads(r.stdout)
        except ValueError:
            self.fail(f"stdout is not one JSON object (rc {r.returncode}):\n{r.stdout}\n{r.stderr}")

    def runs(self):
        if not self.docker_log.exists():
            return []
        calls = [json.loads(ln) for ln in self.docker_log.read_text().splitlines()]
        return [c for c in calls if c and c[0] == "run"]

    def assert_no_model(self):
        self.assertEqual(self.repo.calls(), 0, "the landing pass never starts a model turn")


class StrandedApprovalLands(Fixture):
    def test_the_loops_own_landing_commits_it_verify_gated(self):
        self.strand_approval()
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["schema"], "phasekit-land-in-runner/1")
        self.assertEqual(rep["status"], "landed", rep)
        self.assertEqual(rep["recovery"]["open"], True)
        self.assertEqual(rep["recovery"]["walk_rc"], 0)
        self.assertEqual(rep["exit_code"], 3, "the loop's own code: the limit (an approval, "
                                              "not a completion)")
        self.assertEqual(rep["stranded_before"], ["artifacts/phase-approval.json"])
        self.assertEqual(rep["stranded_after"], [])
        self.assertEqual(rep["unexpected_changes"], [])
        self.assertFalse(rep["blocked"])
        self.assertEqual(len(rep["commits"]), 1, rep["commits"])
        self.assertEqual(rep["head_after"], rep["commits"][-1])
        self.assertIn("built it", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.git("show", "HEAD:src.txt"), "base\nwork by a dead session")
        self.assertGreaterEqual(self.repo.verify_calls(), 1, "verify-gated")
        self.assert_no_model()
        self.assertEqual(rep["layout"], "pinned" if PINNED else "vendored")
        self.assertEqual(rep["engine"], PIN_TAG if PINNED else None)
        self.assertNotIn("phasekit-land ", r.stderr + rep["tail"],
                         "the witness lines are the harness's, never the output's")

    def test_the_runner_is_a_sessions_with_the_loop_env_last(self):
        self.strand_approval()
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (argv,) = self.runs()
        joined = " ".join(argv)
        self.assertTrue(argv[argv.index("--name") + 1].startswith("phasekit-land-runner-"))
        self.assertIn(f'type=bind,"src={self.project.resolve()}",dst=/workspace', joined)
        self.assertIn("IS_SANDBOX=1", argv)
        self.assertIn("HOME=/home/node", argv)
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")
        self.assertIn("CLAUDE_CONFIG_DIR=/home/node/.claude", argv)
        self.assertNotIn(".claude,", joined.replace("dst=/home/node/.claude", ""),
                         "no claude config volume: no login reaches the landing pass")
        self.assertNotIn("scaffold-claude-config", joined)
        self.assertNotIn("/ssh-agent", joined, "no push asked, no agent")
        self.assertIn("AUTO_PUSH=0", argv)
        self.assertIn("-e", argv)
        self.assertIn("STUB_DIR", argv, "PHASEKIT_FORWARD_ENV names cross by name")
        image = argv.index("scaffold-runner")
        envs = [argv[i + 1] for i in range(image) if argv[i] == "-e"]
        self.assertEqual(envs.index("MAX_ITERATIONS=0") > envs.index("STUB_DIR"), True,
                         "the loop's pairs come after every forwarded key")
        if PINNED:
            self.assertIn(f'type=bind,"src={self.engine.resolve()}",dst=/opt/phasekit,readonly',
                          joined)
            self.assertIn("PHASEKIT_PROJECT_DIR=/workspace", argv)
            self.assertIn("PHASEKIT_CONTRACT=/opt/phasekit/contracts/interface.json", argv)
            self.assertIn("exec bash /opt/phasekit/scripts/run-until-done.sh", argv[-1])
        else:
            self.assertNotIn("/opt/phasekit", joined.replace('PATH="/opt', ""))
            self.assertIn("exec bash scripts/run-until-done.sh", argv[-1])

    def test_text_mode_says_landed_and_names_the_commit(self):
        self.strand_approval()
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("phasekit land --in-runner: LANDED", r.stdout)
        self.assertIn("  commit ", r.stdout)


class NothingToLand(Fixture):
    def test_a_rested_tree_is_nothing_and_exit_3(self):
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "nothing")
        self.assertEqual(rep["recovery"]["open"], False)
        self.assertEqual(rep["commits"], [])
        self.assertEqual(rep["head_after"], rep["head_before"])
        self.assert_no_model()

    def test_unrelated_dirt_is_left_exactly_as_found(self):
        self.repo.write("src.txt", "an operator's edit\n")
        self.repo.write("notes.txt", "untracked\n")
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(rep["unexpected_changes"], [])
        self.assertEqual((self.project / "src.txt").read_text(), "an operator's edit\n")
        self.assertEqual((self.project / "notes.txt").read_text(), "untracked\n")

    def test_a_forwarded_key_cannot_reopen_the_loop(self):
        """MAX_ITERATIONS named in PHASEKIT_FORWARD_ENV crosses — and loses:
        the landing's own pair is set last."""
        r, rep = self.land_json(env={"PHASEKIT_FORWARD_ENV": "STUB_DIR,MAX_ITERATIONS",
                                     "MAX_ITERATIONS": "5"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assert_no_model()


class RedGateFails(Fixture):
    def test_red_is_failed_and_the_work_stays_on_disk(self):
        self.strand_approval()
        self.repo.write("BAD", "red\n")
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(rep["recovery"]["walk_rc"], 1)
        self.assertEqual(rep["stranded_after"], ["artifacts/phase-approval.json"])
        self.assertEqual(rep["commits"], [])
        self.assertEqual(rep["unexpected_changes"], [])
        self.assertEqual((self.project / "src.txt").read_text(), "base\nwork by a dead session\n")
        self.assertIn("never landed", rep["detail"])
        self.assert_no_model()


class TheWitnessCannotBeForged(Fixture):
    """Review MAJOR 1: the nonce was exported, so a gate could print a line
    and a failed landing read as nothing. The loop now keeps it to itself."""

    def test_a_gate_cannot_see_the_nonce_or_pass_for_a_line(self):
        gate = self.project / "scripts" / "phasekit-verify.sh"
        gate.write_text(H.VERIFY_LOGGING.replace(
            "PHASEKIT_VERIFY_CONFIGURED=1\n",
            'PHASEKIT_VERIFY_CONFIGURED=1\necho "gate sees nonce=[${PHASEKIT_LAND_NONCE:-}]"\n'
            'echo "phasekit-land ${PHASEKIT_LAND_NONCE:-x} none"\n'))
        self.git("add", "-A")
        self.git("commit", "-qm", "a gate that tries")
        self.strand_approval()
        self.repo.write("BAD", "red\n")
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "failed")
        self.assertIn("gate sees nonce=[]", r.stderr)


class StrandedCompletionLandsOnTheTarget(Fixture):
    """Branch-per-iteration: the supervisor names the target and the work
    branch (flags, or the session runner's env names), as a session gets them."""

    SQUASH = True

    def test_the_completion_lands_and_reaches_main(self):
        self.strand_approval(final_phase=True)
        self.repo.write("artifacts/project-complete.json", json.dumps(
            {"done": True, "summary": "complete",
             "suggested_commit_message": "Project complete: v0 shipped"}) + "\n")
        r, rep = self.land_json("--squash-target", "main", "--work-branch", "iter/1-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "landed", rep)
        self.assertEqual(rep["exit_code"], 0, "the loop's own code: the completion landed")
        self.assertEqual(rep["stranded_after"], [])
        self.assertEqual(rep["unexpected_changes"], [])
        self.assertEqual(self.git("show", "main:src.txt"), "base\nwork by a dead session")
        (argv,) = self.runs()
        self.assertIn("PHASEKIT_SQUASH_TARGET=main", argv)
        self.assertIn("PHASEKIT_WORK_BRANCH=iter/1-test", argv)
        self.assert_no_model()

    def test_the_env_names_are_the_default(self):
        self.strand_approval()
        r, rep = self.land_json(env={"PHASEKIT_SQUASH_TARGET": "main",
                                     "PHASEKIT_WORK_BRANCH": "iter/1-test"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (argv,) = self.runs()
        self.assertIn("PHASEKIT_SQUASH_TARGET=main", argv)

    def test_a_bad_branch_name_is_usage(self):
        r = self.land("--squash-target", "no..pe")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        r = self.land("--work-branch", "iter/1-test")
        self.assertEqual(r.returncode, 2, "a work branch without its target")
        self.assertEqual(self.runs(), [])


class Refusals(Fixture):
    def test_a_container_working_on_the_tree(self):
        self.strand_approval()
        r, rep = self.land_json(env={
            "FAKE_DOCKER_PS": f"unrelated=/elsewhere,phasekit-proj-123={self.project}"})
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "refused")
        self.assertEqual(rep["running_containers"], ["phasekit-proj-123"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.repo.porcelain_all().count("?? artifacts/phase-approval.json"), 1)

    def test_a_container_mounting_a_path_inside_it(self):
        r, rep = self.land_json(env={"FAKE_DOCKER_PS": f"x={self.project / 'artifacts'}"})
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)

    def test_a_writable_parent_mount_counts_a_read_only_one_does_not(self):
        r, rep = self.land_json(env={"FAKE_DOCKER_PS": f"dev=bind|true|{self.project.parent}"})
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        r, rep = self.land_json(env={"FAKE_DOCKER_PS": "monitor=bind|false|/"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_tmpfs_and_volume_mounts_name_no_tree(self):
        """Review m2: an empty Source once resolved to the CLI's cwd — the
        project — so any container with a tmpfs refused every landing."""
        r, rep = self.land_json(env={"FAKE_DOCKER_PS": "other=tmpfs|true|,vol=volume|true|"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "nothing")

    def test_a_signal_before_the_runner_is_reported_not_raised(self):
        """Review m5: SIGTERM during the pre-checks is a JSON not-run, exit 5."""
        import signal
        import time
        proc = subprocess.Popen(["bash", str(CLI), "land", "--in-runner", "--json"],
                                cwd=str(self.project), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=self.env(extra={"FAKE_DOCKER_PS_SLEEP": "5"}))
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 5, out + err)
        rep = json.loads(out)
        self.assertEqual(rep["status"], "not-run")
        self.assertIn("interrupted", rep["detail"])
        self.assertEqual(self.runs(), [])

    def test_docker_that_cannot_list_containers_is_not_run(self):
        r, rep = self.land_json(docker="psfail")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "not-run")
        self.assertEqual(self.runs(), [])

    def test_git_mid_operation(self):
        self.strand_approval()
        lock = self.project / ".git" / "index.lock"
        lock.write_text("")
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("index.lock", rep["detail"])
        self.assertTrue(lock.exists(), "a lock it did not take is never removed")
        self.assertEqual(self.runs(), [])

    def test_another_landing(self):
        import fcntl
        fd = os.open(str(self.project / ".git" / "phasekit-land.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r, rep = self.land_json()
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("another `phasekit land`", rep["detail"])

    def test_inside_a_loop_session(self):
        r = self.land(env={"PHASEKIT_ITER_MARKER": "/tmp/x"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.runs(), [])

    def test_no_runner_is_not_run_never_the_host(self):
        self.strand_approval()
        r, rep = self.land_json(docker="noimage")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("never runs on the host", rep["detail"])
        self.assertEqual(rep["stranded_after"], [])  # not observed: nothing ran
        self.assertEqual(self.runs(), [])

    def test_a_runner_that_cannot_start(self):
        self.strand_approval()
        r, rep = self.land_json(env={"FAKE_DOCKER_RUN_RC": "125"})
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "not-run")

    def test_only_in_the_runner(self):
        r = subprocess.run(["bash", str(CLI), "land"], cwd=str(self.project),
                           capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 2)
        self.assertIn("--in-runner", r.stderr)


class TimeoutThenFinish(Fixture):
    """A runner stopped mid-landing (the gate hangs) is failed and removed;
    the loop's boundary record lets the next landing finish it."""

    def test_timeout_is_failed_then_the_next_land_lands(self):
        gate = self.project / "scripts" / "phasekit-verify.sh"
        gate.write_text(H.VERIFY_LOGGING.replace(
            "exit 0\n", 'if [ -f "$ROOT/SLOW" ]; then sleep 60; fi\nexit 0\n'))
        self.git("add", "-A")
        self.git("commit", "-qm", "a gate that can hang")
        self.strand_approval()
        self.repo.write("SLOW", "x\n")
        self.git("add", "SLOW")
        self.git("commit", "-qm", "slow")  # SLOW is not the stranded work
        r, rep = self.land_json("--timeout", "5")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertTrue(rep["timed_out"])
        self.assertIsNone(rep["exit_code"])
        self.assertIn("timed out after 5s", rep["detail"])
        rm = [c for c in [json.loads(ln) for ln in self.docker_log.read_text().splitlines()]
              if c[:2] == ["rm", "-f"]]
        self.assertTrue(rm and rm[0][2].startswith("phasekit-land-runner-"),
                        "the runner is removed, not orphaned")
        self.git("rm", "-q", "SLOW")
        self.git("commit", "-qm", "fast again")
        r2, rep2 = self.land_json()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(rep2["status"], "landed", rep2)
        self.assertEqual(self.git("show", "HEAD:src.txt"), "base\nwork by a dead session")
        self.assert_no_model()


class OwnStaleLock(Fixture):
    def test_only_a_lock_no_container_explains_is_released(self):
        e = load("pk_enrich_land_l", ENRICH)
        lock = self.project / ".git" / "index.lock"
        saved = dict(os.environ)
        try:
            cases = [(self.env(extra={"FAKE_DOCKER_PS": f"s={self.project}"}), False),
                     (self.env(), True)]
            for env, gone in cases:
                lock.write_text("")
                os.environ.clear()
                os.environ.update(env)
                self.assertEqual(bool(e._land_release_own_lock(self.project)), gone)
                self.assertEqual(lock.exists(), not gone)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class Push(Fixture):
    def test_push_crosses_the_agent_socket_known_hosts_and_tokens_by_name(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "main")
        sock_path = self.tmp / "agent.sock"
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(str(sock_path))
        self.addCleanup(srv.close)
        home = self.tmp / "operator-home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "known_hosts").write_text("host key\n")
        self.strand_approval()
        r, rep = self.land_json("--push", env={"SSH_AUTH_SOCK": str(sock_path),
                                               "HOME": str(home), "GH_TOKEN": "not-a-secret-x"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(rep["push_requested"])
        (argv,) = self.runs()
        joined = " ".join(argv)
        self.assertIn("AUTO_PUSH=1", argv)
        self.assertIn(f'type=bind,"src={sock_path}",dst=/ssh-agent', joined)
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent", argv)
        self.assertIn("dst=/home/node/.ssh/known_hosts,readonly", joined)
        self.assertIn("GH_TOKEN", argv)
        self.assertNotIn("not-a-secret-x", joined, "a token crosses by name, never by value")
        self.assertEqual(subprocess.run(["git", "-C", str(remote), "rev-parse", "main"],
                                        capture_output=True, text=True).stdout.strip(),
                         rep["head_after"], "the landing was pushed")


class OneComposer(Fixture):
    def test_the_landing_and_the_gate_share_one_runner_call(self):
        e = load("pk_enrich_land_c", ENRICH)
        saved, env = dict(os.environ), self.env()
        try:
            os.environ.clear()
            os.environ.update(env)
            gate = e.runner_argv(self.project, "img", "N", "SCRIPT", self.engine)
            land = e.runner_argv(self.project, "img", "N", "SCRIPT", self.engine,
                                 env=[("MAX_ITERATIONS", "0")])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        i = land.index("img")
        self.assertEqual(land[:i - 2] + land[i:], gate, "the same call plus the loop's pair")
        self.assertEqual(land[i - 2:i], ["-e", "MAX_ITERATIONS=0"])
        src = ENRICH.read_text()
        self.assertEqual(src.count('"docker", "run"'), 1, "runner_argv is the one composer")
        body = src[src.index("def _run_land("):src.index("def _dirty_state(")]
        self.assertIn("runner_argv(", body)
        self.assertIn("_supervise(", body)
        self.assertNotIn('"docker", "run"', PIN_TOOL.read_text())


class Units(unittest.TestCase):
    def setUp(self):
        self.e = load("pk_enrich_land_u", ENRICH)
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-land-u-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_witness_lines_are_split_off_and_a_spoofed_nonce_is_output(self):
        out = ("a\nphasekit-land n1 open 1\nb\nphasekit-land other walked 0\n"
               "phasekit-land n1 walked 2\n")
        text, rec = self.e._split_land_witness(out, "n1")
        self.assertEqual(rec, {"open": True, "from_step": 1, "walk_rc": 2, "complete": False,
                               "coherent": True})
        self.assertEqual(text, "a\nb\nphasekit-land other walked 0\n")
        self.assertEqual(self.e._split_land_witness("phasekit-land n1 none\n", "n1")[1],
                         {"open": False, "from_step": None, "walk_rc": None, "complete": False,
                          "coherent": True})
        for bad in ("phasekit-land n1 open 1\nphasekit-land n1 walked 1\nphasekit-land n1 none\n",
                    "phasekit-land n1 none\nphasekit-land n1 none\n",
                    "phasekit-land n1 walked 0\n"):
            rec = self.e._split_land_witness(bad, "n1")[1]
            self.assertFalse(rec["coherent"], bad)
            self.assertEqual(self.e.land_verdict(3, False, False, "", rec, [], 9)[0], "failed")

    def test_the_verdict_table(self):
        v = self.e.land_verdict
        rec = lambda o, w=None, c=False: {"open": o, "from_step": 1 if o else None,  # noqa: E731
                                          "walk_rc": w, "complete": c, "coherent": True}
        self.assertEqual(v(3, False, False, "", rec(False), [], 9)[0], "nothing")
        self.assertEqual(v(3, False, False, "", rec(True, 0), [], 9)[0], "landed")
        self.assertEqual(v(0, False, False, "", rec(True, 0), [], 9)[0], "landed")
        self.assertEqual(v(3, False, False, "", rec(True, 2), [], 9)[0], "nothing")
        self.assertEqual(v(3, False, False, "", rec(True, 1), ["a"], 9)[0], "failed")
        self.assertEqual(v(3, False, False, "", rec(True, 0), ["a"], 9)[0], "failed")
        self.assertEqual(v(2, False, False, "", rec(True, 1), [], 9)[0], "failed")
        self.assertEqual(v(2, False, False, "", rec(None), [], 9)[0], "failed")
        self.assertEqual(v(137, False, False, "", rec(True), [], 9)[0], "failed")
        self.assertEqual(v(None, True, False, "", rec(True), [], 9)[0], "failed")
        self.assertEqual(v(None, False, True, "", rec(None), [], 9)[0], "failed")
        self.assertEqual(v(125, False, False, "", rec(None), [], 9)[0], "not-run")
        self.assertEqual(v(0, False, False, "", rec(True, 1, True), [], 9)[0], "landed",
                         "finish_complete exits 0 whatever the walk's rc")
        self.assertEqual(v(0, False, False, "", rec(True, 2, True), [], 9)[0], "nothing")
        self.assertEqual(v(0, False, False, "", rec(True, 1, True), ["x"], 9)[0], "failed")
        mark = self.e.LAND_NO_MODEL_MARK
        self.assertEqual(v(3, False, False, mark, rec(False), [], 9)[0], "failed")

    def test_the_stub_refuses_a_model_call(self):
        script = self.e.land_script("x").replace("exec bash x", "claude -p hello")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, self.e.LAND_NO_MODEL_RC, r.stderr)
        self.assertIn(self.e.LAND_NO_MODEL_MARK + " (claude -p hello)", r.stderr)

    def test_a_loop_without_the_witness_is_refused(self):
        engine = self.tmp / "old-engine"
        (engine / "scripts").mkdir(parents=True)
        (engine / "scripts" / "run-until-done.sh").write_text("#!/bin/bash\nexit 3\n")
        proj = self.tmp / "p"
        proj.mkdir()
        subprocess.run(["git", "init", "-q", str(proj)], check=True)
        res = self.e.run_project_land(proj, engine=engine)
        self.assertEqual(res["status"], "refused")
        self.assertIn("predates the landing witness", res["detail"])
        self.assertTrue(self.e.land_witness_supported(LOOP))

    def test_what_a_landing_may_change(self):
        proj = self.tmp / "q"
        (proj / "artifacts").mkdir(parents=True)
        (proj / "docs").mkdir()

        def g(*a):
            return subprocess.run(["git", "-C", str(proj), *a], capture_output=True, text=True,
                                  check=True).stdout.strip()
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        for rel in ("a.txt", "b.txt", "artifacts/x.json", "docs/LEARNINGS.md"):
            (proj / rel).write_text("one\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        head0 = g("rev-parse", "HEAD")
        (proj / "a.txt").write_text("dirty payload\n")
        (proj / "r1.txt").write_text("renamed\n")
        g("add", "r1.txt")
        g("commit", "-qm", "r1")
        head0 = g("rev-parse", "HEAD")
        g("mv", "r1.txt", "r2.txt")  # an operator's staged rename (review m4)
        before = self.e._land_snapshot(proj)
        # the landing: the payload committed as found, records and learnings
        # rewritten — allowed; b.txt rewritten and committed — not allowed
        (proj / "artifacts" / "x.json").write_text("two\n")
        (proj / "docs" / "LEARNINGS.md").write_text("rotated\n")
        (proj / "b.txt").write_text("changed by the run\n")
        (proj / "new.txt").write_text("appeared\n")
        g("add", "a.txt", "b.txt", "artifacts/x.json", "docs/LEARNINGS.md")  # + the staged rename
        g("commit", "-qm", "landing")
        self.assertEqual(self.e._land_unexpected(proj, before, head0, g("rev-parse", "HEAD")),
                         ["b.txt", "new.txt"], "a rename committed as found is not a change")


class TheSurfaceIsDeclared(unittest.TestCase):
    def test_the_fact_names_the_cli_its_keys_and_codes(self):
        facts = json.loads(MANIFEST.read_text())["facts"]
        fact = facts["land_in_runner"]
        self.assertEqual(fact["cli"], "phasekit land --in-runner")
        self.assertEqual(fact["schema"], "phasekit-land-in-runner/1")
        pin = load("pk_pin_land", PIN_TOOL)
        e = load("pk_enrich_land_s", ENRICH)
        self.assertEqual(list(fact["json_keys"]), list(pin.LAND_RUNNER_KEYS))
        self.assertEqual(fact["status"], list(e.LAND_STATUSES))
        self.assertEqual({k: int(v) for k, v in fact["status_exit"].items()},
                         dict(pin.LAND_EXIT))
        self.assertEqual({int(k) for k in fact["exit_codes"]}, {0, 2, 3, 4, 5, 6, 7})
        self.assertEqual(fact["container_name_prefix"], e.LAND_RUNNER_NAME + "-")
        self.assertEqual(fact["may_change"], list(e.LAND_MAY_CHANGE))
        self.assertEqual(fact["env_set_last"]["MAX_ITERATIONS"], "0")
        self.assertEqual(fact["witness_env"], e.LAND_NONCE_ENV)
        self.assertEqual(facts["recovery_only_run"]["env"], "MAX_ITERATIONS")
        self.assertIn(e.LAND_NONCE_ENV, LOOP.read_text())


if __name__ == "__main__":
    unittest.main()
