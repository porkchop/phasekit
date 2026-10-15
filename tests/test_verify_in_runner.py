#!/usr/bin/env python3
"""`phasekit verify --in-runner` (v0.20.0): ONE harness runs a project's gate
in the runner image, for the upgrade gate, the migrate gate and a measurement.

The incident (2026-10-15): foundry-orchestrator's weekly verify-budget witness
timed the fast tier with its OWN hand-built `docker run`, which neither mounted
the pinned engine at /opt/phasekit nor exported PHASEKIT_CONTRACT, so 37 tests
were red in the runner on a green tree — the third drift of that copy in a
week. Its fix blocked because phasekit had no declared entry point: `phasekit
verify` runs where it is invoked and refuses `--tier fast` on a tree carrying
artifacts/project-complete.json, and the only runner composition
(run_upgrade_gate) was private and default-tier only.

Pinned here, in the layout tests/_layout.py selects (the module is in
LAYOUT_MODULES, so it runs vendored AND pinned):
  * the runner invocation is the upgrade gate's own, byte-for-byte (one
    composer — runner_argv — and every gate path calls run_project_gate);
  * the gate sees the engine and the contract it would see in a session;
  * an explicit tier reaches the gate, and the fast tier is measured on a
    completed tree that the loop's `phasekit verify --tier fast` refuses;
  * read-only over the tree: a footprint is restored and red;
  * the result is machine-readable: exit status, elapsed and gate seconds,
    and EVERY failing test id;
  * it never runs the gate bare: no runner is exit 5, never a host run.

The container is a stand-in (tests/test_verify_in_runner.py FAKE_DOCKER): it
records the argv and runs the gate on the host with the container's paths
mapped to their mounts, so the gate's view is the argv's, not the test's.

Run: `python3 -m unittest tests.test_verify_in_runner`
"""

import importlib.util
import json
import os
import shutil
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
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
PIN_TAG = "v0.19.6"

# A docker stand-in. info/image answer by FAKE_DOCKER_MODE (ready | noimage |
# down); `run` is logged as JSON (one argv per line) and then EXECUTED on the
# host: each --mount dst is mapped to its src in the -e values and the script,
# -w is the cwd, HOME is a scratch dir (the image's /home/node is not here).
FAKE_DOCKER = r'''#!/usr/bin/env python3
import csv, json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_DOCKER_MODE", "ready")
verb = args[0] if args else ""
if verb == "info":
    if mode == "down":
        sys.exit(1)
    print("[]" if "SecurityOptions" in " ".join(args) else "27.0")
    sys.exit(0)
if verb == "image":
    sys.exit(0 if mode == "ready" else 1)
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
import re
def host(text):
    for dst in sorted(mounts, key=len, reverse=True):
        text = re.sub(r"(?<![\w/.-])" + re.escape(dst) + r"(?![\w.-])",
                      lambda m, d=dst: mounts[d], text)
    return text
env = {k: host(v) for k, v in env.items()}
env["HOME"] = os.environ["FAKE_DOCKER_HOME"]
env["PATH"] = os.environ["PATH"]
rest = [host(a) for a in rest]
sys.exit(subprocess.run(["bash"] + rest, cwd=host(cwd or "/"), env=env).returncode)
'''


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def gate_script(body):
    return ("#!/usr/bin/env bash\nset -euo pipefail\nPHASEKIT_VERIFY_CONFIGURED=1\n"
            + body + "\n")


class Fixture(unittest.TestCase):
    """A committed project in this run's layout, with a configurable gate."""

    GATE = 'echo "gate args: $*"  # takes --fast | --full'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-vir-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.docker_log = self.tmp / "docker.log"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.store = self.tmp / "store"
        if PINNED:
            (self.project / ".phasekit-version").write_text(PIN_TAG + "\n")
            (self.project / "scripts").mkdir()
            self.engine = self.store / PIN_TAG
            for rel in ("scripts/run-until-done.sh", "contracts/interface.json",
                        "bin/phasekit", "scripts/phasekit.sh"):
                dest = self.engine / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO_ROOT / rel, dest)
            (self.engine / ".engine-version").write_text(PIN_TAG + "\n")
        else:
            subprocess.run([sys.executable, str(ENRICH), str(self.project)],
                           capture_output=True, text=True, check=True)
            self.engine = None
        (self.project / "scripts" / "phasekit-verify.sh").write_text(gate_script(self.GATE))
        (self.project / ".gitignore").write_text("ignored/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.project), *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def env(self, docker="ready", extra=None):
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("PHASEKIT_") or k in ("GIT_USER_NAME", "GIT_USER_EMAIL"):
                del e[k]
        e.update({"PHASEKIT_ENGINE_STORE": str(self.store), "PHASEKIT_NO_AUTO_FETCH": "1",
                  "FAKE_DOCKER_LOG": str(self.docker_log), "FAKE_DOCKER_HOME": str(self.home),
                  "PHASEKIT_CONTAINER_USER": "root"})
        if docker == "none":
            nod = self.tmp / "nodocker"
            if not nod.exists():
                nod.mkdir()
                for tool in ("git", "bash", "sh", "env", "dirname", "python3"):
                    os.symlink(shutil.which(tool), nod / tool)
            e["PATH"] = str(nod)
        else:
            e["PATH"] = f"{self.bin}{os.pathsep}{e.get('PATH', '')}"
            e["FAKE_DOCKER_MODE"] = docker
        e.update(extra or {})
        return e

    def verify(self, *args, docker="ready", env=None):
        return subprocess.run(["bash", str(CLI), "verify", "--in-runner", *args],
                              cwd=str(self.project), capture_output=True, text=True,
                              env=self.env(docker, env))

    def verify_json(self, *args, **kw):
        r = self.verify("--json", *args, **kw)
        try:
            return r, json.loads(r.stdout)
        except ValueError:
            self.fail(f"stdout is not one JSON object (rc {r.returncode}):\n{r.stdout}\n{r.stderr}")

    def runs(self):
        if not self.docker_log.exists():
            return []
        calls = [json.loads(ln) for ln in self.docker_log.read_text().splitlines()]
        return [c for c in calls if c and c[0] == "run"]

    def porcelain(self):
        return self.git("status", "--porcelain").stdout.strip()


class TheRunnerHasASessionsEnvironment(Fixture):
    GATE = ('echo "gate args: $*"\n'
            'test -r "$PHASEKIT_CONTRACT" || { echo "no contract at $PHASEKIT_CONTRACT"; exit 9; }\n'
            'if [ -f .phasekit-version ]; then command -v phasekit >/dev/null '
            '|| { echo "no phasekit on PATH"; exit 8; }; fi\n'
            'git config --global user.name')

    def test_green_gate_is_exit_0_with_a_full_record(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["schema"], "phasekit-verify-in-runner/1")
        self.assertEqual(rep["status"], "passed")
        self.assertEqual(rep["exit_code"], 0)
        self.assertFalse(rep["timed_out"])
        self.assertEqual(rep["tier"], "auto")
        self.assertEqual(rep["where"], "container")
        self.assertEqual(rep["label"], "scripts/phasekit-verify.sh")
        self.assertEqual(rep["layout"], "pinned" if PINNED else "vendored")
        self.assertEqual(rep["engine"], PIN_TAG if PINNED else None)
        self.assertIsInstance(rep["elapsed_seconds"], float)
        self.assertIsInstance(rep["gate_seconds"], float)
        self.assertLessEqual(rep["gate_seconds"], rep["elapsed_seconds"])
        self.assertEqual(rep["failing_tests"], [])
        self.assertEqual(rep["footprint"], [])
        self.assertIn("gate args: \n", r.stderr, "auto passes no tier: the gate decides")
        self.assertNotIn("phasekit-gate-time", r.stderr + json.dumps(rep),
                         "the timing line is the harness's, never the gate's output")

    def test_the_engine_and_the_contract_are_the_sessions(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (argv,) = self.runs()
        joined = " ".join(argv)
        self.assertIn(f'type=bind,"src={self.project.resolve()}",dst=/workspace', joined)
        if PINNED:
            self.assertIn(f'type=bind,"src={self.engine.resolve()}",dst=/opt/phasekit,readonly',
                          joined)
            self.assertIn("PHASEKIT_CONTRACT=/opt/phasekit/contracts/interface.json", argv)
            self.assertIn('export PATH="/opt/phasekit/bin:$PATH"', argv[-1])
        else:
            self.assertNotIn("/opt/phasekit", joined)
            self.assertIn("PHASEKIT_CONTRACT=/workspace/contracts/interface.json", argv)
        self.assertIn("IS_SANDBOX=1", argv)
        self.assertIn("HOME=/home/node", argv)
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")
        self.assertTrue(argv[argv.index("--name") + 1].startswith("phasekit-verify-gate-"))
        self.assertNotIn("/var/run/docker.sock", joined)

    def test_the_contracts_provider_is_handed_over_read_only(self):
        prov = self.tmp / "stage"
        prov.mkdir()
        (prov / "index.json").write_text('{"contracts": []}\n')
        r, rep = self.verify_json(env={"PHASEKIT_CONTRACTS_MOUNT": str(prov)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (argv,) = self.runs()
        self.assertIn(f'type=bind,"src={prov.resolve()}",dst=/contracts,readonly', argv)
        self.assertIn("PHASEKIT_CONTRACTS_DIR=/contracts", argv)

    def test_text_mode_says_green_and_measurement(self):
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("phasekit verify --in-runner: GREEN", r.stdout)
        self.assertIn("a measurement", r.stdout)


class OneComposer(Fixture):
    """The upgrade/migrate gate and the measurement are ONE invocation: the
    same argv but for the container's name and the tier flag."""

    def test_upgrade_gate_and_measurement_compose_the_same_runner_call(self):
        e = load("pk_enrich_vir", ENRICH)
        saved, env = dict(os.environ), self.env()
        try:
            os.environ.clear()
            os.environ.update(env)
            up = e.run_upgrade_gate(self.project, "container", engine=self.engine)
            me = e.run_project_gate(self.project, "container", engine=self.engine, tier="fast")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual((up["status"], me["status"]), ("passed", "passed"), (up, me))
        a, b = self.runs()

        def norm(argv):
            argv = list(argv)
            argv[argv.index("--name") + 1] = "NAME"
            argv[-1] = (argv[-1].replace(" --fast'\n", "'\n")
                        .split("phasekit-gate-time ")[0])  # the nonce differs per run
            return argv
        self.assertTrue(a[a.index("--name") + 1].startswith("phasekit-upgrade-gate-"))
        self.assertEqual(norm(a), norm(b))
        self.assertIn("__pk_cmd='bash scripts/phasekit-verify.sh --fast'\n", b[-1])

    def test_every_gate_path_goes_through_the_harness(self):
        src = ENRICH.read_text()
        self.assertEqual(src.count('"docker", "run"'), 1, "runner_argv is the one composer")
        body = src[src.index("def run_upgrade_gate("):src.index("def run_project_gate(")]
        self.assertIn("return run_project_gate(", body)
        pin = PIN_TOOL.read_text()
        self.assertNotIn('"docker", "run"', pin)
        self.assertIn("e.run_upgrade_gate(root, mode, engine=engine)", pin)  # upgrade + migrate
        self.assertIn("e.run_project_gate(", pin)  # verify --in-runner


class TheFastTierIsMeasuredOnACompletedTree(Fixture):
    GATE = ('echo "gate args: $*"; [ "${1:-}" = --fast ] && echo FAST-RAN || echo OTHER-RAN'
            '  # --fast | --full')

    def setUp(self):
        super().setUp()
        (self.project / "artifacts").mkdir(exist_ok=True)
        (self.project / "artifacts" / "project-complete.json").write_text('{"done": true}\n')
        self.git("add", "-A")
        self.git("commit", "-qm", "complete")

    def test_fast_on_a_committed_completion_record_runs(self):
        r, rep = self.verify_json("--tier", "fast")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep["tier"], "fast")
        self.assertIn("gate args: --fast", r.stderr)
        self.assertIn("FAST-RAN", r.stderr)
        self.assertEqual(self.porcelain(), "")

    def test_full_is_passed_as_full(self):
        r, rep = self.verify_json("--tier", "full")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gate args: --full", r.stderr)

    def test_the_loops_verify_still_refuses_fast_here(self):
        """The refusal is the loop's guard against approving on fast: a
        measurement is not an approval, so only the measurement path is
        exempt — `phasekit verify --tier fast` keeps exit 2."""
        r = subprocess.run(["bash", str(CLI), "verify", "--tier", "fast"],
                           cwd=str(self.project), capture_output=True, text=True,
                           env=self.env())
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("FULL tier", r.stderr)
        self.assertEqual(self.runs(), [])

    def test_a_one_shot_command_takes_no_tier(self):
        r = self.verify("--tier", "fast", env={"PHASEKIT_VERIFY_CMD": "true"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("a tier cannot be selected for PHASEKIT_VERIFY_CMD", r.stderr)
        self.assertEqual(self.runs(), [])
        r2, rep = self.verify_json(env={"PHASEKIT_VERIFY_CMD": "echo one-shot"})
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(rep["label"], "PHASEKIT_VERIFY_CMD")

    def test_a_gate_that_never_names_the_flag_is_refused_a_tier(self):
        """Review M1: four of the five profile gates take no arguments; a tier
        they would ignore must not label the measurement."""
        (self.project / "scripts" / "phasekit-verify.sh").write_text(gate_script("echo plain"))
        self.git("commit", "-qam", "a gate with no tier flags")
        r = self.verify("--tier", "fast")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("does not parse --fast", r.stderr)
        self.assertEqual(self.runs(), [])
        r2, rep = self.verify_json()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(rep["tier"], "auto")

    def test_a_bad_tier_is_usage(self):
        r = self.verify("--tier", "medium")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.runs(), [])


PYTEST_RED = r'''
echo "....F..E"
echo "=========================== short test summary info ============================"
echo "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2"
echo "FAILED tests/test_a.py::TestK::test_two"
echo "FAILED tests/test_b.py::test_p[a - b] - ValueError: x - y"
echo "ERROR tests/test_c.py - ImportError: no module"
echo "ERROR tests/test_d.py::test_fixture - RuntimeError"
echo "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2"
echo "3 failed, 10 passed, 2 errors in 1.23s"
exit 1'''


class RedGivesEveryFailingId(Fixture):
    GATE = PYTEST_RED

    def test_every_failing_id_and_the_gates_own_exit(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(rep["failing_tests_format"], "pytest")
        self.assertEqual(rep["failing_tests"], [
            "tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two",
            "tests/test_b.py::test_p[a - b]", "tests/test_c.py",
            "tests/test_d.py::test_fixture"])
        self.assertIn("3 failed, 10 passed", rep["tail"])
        self.assertIsInstance(rep["gate_seconds"], float)

    def test_text_mode_lists_them_all(self):
        r = self.verify()
        self.assertEqual(r.returncode, 4)
        self.assertIn("phasekit verify --in-runner: RED", r.stdout)
        self.assertIn("failing (5):", r.stdout)
        self.assertIn("    tests/test_d.py::test_fixture", r.stdout)


class UnittestIdsToo(Fixture):
    GATE = ('echo "FAIL: test_x (pkg.mod.Case.test_x)"\n'
            'echo "ERROR: test_y (pkg.mod.Case)"\nexit 1')

    def test_unittest_summary_lines(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 4)
        self.assertEqual(rep["failing_tests"], ["pkg.mod.Case.test_x", "pkg.mod.Case.test_y"])
        self.assertEqual(rep["failing_tests_format"], "unittest")


class ReadOnlyOverTheTree(Fixture):
    GATE = ('echo changed >> scripts/phasekit-verify.sh.note; echo x > stray.txt; '
            'mkdir -p ignored && echo ok > ignored/out.txt')

    def test_a_footprint_is_restored_and_red(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(rep["exit_code"], 0)
        self.assertIn("stray.txt", rep["footprint"])
        self.assertIn("read-only over the tree", rep["detail"])
        self.assertEqual(self.porcelain(), "", "the footprint is restored")
        self.assertTrue((self.project / "ignored" / "out.txt").exists(),
                        "an ignored path is where gate output belongs")


class NeverBare(Fixture):
    def test_no_runner_image_is_not_run(self):
        r, rep = self.verify_json(docker="noimage")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(rep["status"], "infra")
        self.assertIn("not present", rep["detail"])
        self.assertEqual(self.runs(), [])

    def test_no_docker_cli_is_not_a_host_run(self):
        r, rep = self.verify_json(docker="none")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(rep["where"], "container")
        self.assertIn("docker is not installed", rep["detail"])

    def test_a_runner_that_cannot_start_is_not_run(self):
        r, rep = self.verify_json(env={"FAKE_DOCKER_RUN_RC": "125"})
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("could not start", rep["detail"])

    def test_a_timeout_is_red_and_says_so(self):
        r, rep = self.verify_json("--timeout", "1", env={"PHASEKIT_VERIFY_CMD": "sleep 20"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertTrue(rep["timed_out"])
        self.assertIsNone(rep["exit_code"])
        self.assertIn("timed out after 1s", rep["detail"])


class OnlyACleanTree(Fixture):
    """Review M3: the footprint restore reads every change made during the run
    as the gate's, so a tree something else is writing is refused, never
    measured (and never restored)."""

    def test_untracked_or_modified_or_staged_is_refused(self):
        for make in (lambda: (self.project / "new.txt").write_text("x"),
                     lambda: (self.project / ".gitignore").write_text("other/\n"),
                     lambda: self.git("rm", "-q", "--cached", ".gitignore")):
            with self.subTest(make=make):
                make()
                r = self.verify()
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("not clean", r.stderr)
                self.git("reset", "-q", "--hard")
                self.git("clean", "-qfd")
        self.assertEqual(self.runs(), [])

    def test_inside_a_loop_session_is_refused(self):
        r = self.verify(env={"PHASEKIT_ITER_MARKER": "/tmp/x"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


class TheCommandMeansWhatItMeant(Fixture):
    """Review m7: the command runs in its own `bash -eo pipefail -c`, exactly
    as before the timing wrapper."""

    def test_compound_command_stops_at_first_failure(self):
        r, rep = self.verify_json(env={"PHASEKIT_VERIFY_CMD": "false; echo AFTER"})
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("AFTER", rep["tail"])

    def test_a_stray_paren_and_quotes_are_the_commands(self):
        r, rep = self.verify_json(env={"PHASEKIT_VERIFY_CMD": "echo 'a ) b' \"it's\""})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a ) b it's", rep["tail"])


class NoGate(Fixture):
    def setUp(self):
        super().setUp()
        (self.project / "scripts" / "phasekit-verify.sh").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "no gate")

    def test_nothing_to_measure_is_not_run(self):
        r, rep = self.verify_json()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("no gate to run", rep["detail"])
        self.assertEqual(self.runs(), [])


class Parsing(unittest.TestCase):
    def setUp(self):
        self.e = load("pk_enrich_vir_p", ENRICH)

    def test_the_timing_line_is_split_off_exactly(self):
        out, secs = self.e._split_gate_time("a\nb\n\nphasekit-gate-time n1 1234\n", "n1")
        self.assertEqual((out, secs), ("a\nb\n", 1.234))
        out, secs = self.e._split_gate_time("a\nb\nphasekit-gate-time n1 5\n", "n1")
        self.assertEqual((out, secs), ("a\nb", 0.005))

    def test_a_spoofed_timing_line_with_another_nonce_is_output(self):
        text = "phasekit-gate-time other 1\nreal\n"
        self.assertEqual(self.e._split_gate_time(text, "n1"), (text, None))

    def test_ids_are_deduplicated_in_order(self):
        ids, fmt = self.e.failing_test_ids("FAILED a::b\nFAILED a::b - x\nFAIL: t (m.C)\n")
        self.assertEqual(ids, ["a::b", "m.C.t"])
        self.assertEqual(fmt, "pytest+unittest")

    def test_real_unittest_output(self):
        """Review M2, from a real `python3 -m unittest` run: an import
        failure, a 3.11+ and a pre-3.11 FAIL, subTests, and the summary."""
        out = ("ERROR: tests.test_b (unittest.loader._FailedTest.tests.test_b)\n"
               "FAIL: test_plain (tests.test_a.A.test_plain)\n"
               "FAIL: test_sub (tests.test_a.A.test_sub) (i=1)\n"
               "FAIL: test_sub (tests.test_a.A.test_sub) [msg]\n"
               "FAIL: test_old (m.C)\n"
               "FAILED (failures=3, errors=1)\n")
        ids, fmt = self.e.failing_test_ids(out)
        self.assertEqual(ids, ["tests.test_b", "tests.test_a.A.test_plain",
                               "tests.test_a.A.test_sub", "m.C.test_old"])
        self.assertEqual(fmt, "unittest")

    def test_real_unittest_run(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "tests"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "test_a.py").write_text(
                "import unittest\nclass A(unittest.TestCase):\n"
                "    def test_plain(self): self.fail()\n"
                "    def test_sub(self):\n"
                "        for i in range(2):\n"
                "            with self.subTest(i=i): self.assertEqual(i, 9)\n")
            (pkg / "test_b.py").write_text("import no_such_module_x\n")
            r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_a", "tests.test_b"],
                               cwd=d, capture_output=True, text=True)
        ids, fmt = self.e.failing_test_ids(r.stdout + r.stderr)
        # an import failure is named as unittest names it (the module's name)
        self.assertEqual(sorted(i for i in ids if "test_a" in i),
                         ["tests.test_a.A.test_plain", "tests.test_a.A.test_sub"])
        self.assertEqual([i for i in ids if "test_a" not in i and i.endswith("test_b")],
                         [i for i in ids if "test_a" not in i])
        self.assertEqual(len(ids), 3, ids)
        self.assertEqual(fmt, "unittest")

    def test_tool_noise_is_not_an_id(self):
        ids, fmt = self.e.failing_test_ids(
            "ERROR: Could not find a version (from versions: none)\nFound 3 errors.\n"
            "FAILED (errors=1)\n")
        self.assertEqual((ids, fmt), ([], None))


class TheSurfaceIsDeclared(unittest.TestCase):
    def test_the_fact_names_the_cli_and_its_codes(self):
        fact = json.loads(MANIFEST.read_text())["facts"]["verify_in_runner"]
        self.assertEqual(fact["cli"], "phasekit verify --in-runner")
        self.assertEqual(fact["schema"], "phasekit-verify-in-runner/1")
        pin = load("pk_pin_vir", PIN_TOOL)
        self.assertEqual(list(fact["json_keys"]), list(pin.VERIFY_RUNNER_KEYS))
        self.assertEqual({int(k) for k in fact["exit_codes"]}, {0, 2, 4, 5, 6})
        self.assertEqual((pin.EXIT_OK, pin.EXIT_USAGE, pin.EXIT_GATE_RED, pin.EXIT_NOT_RUN,
                          pin.EXIT_NOT_INSTALLED), (0, 2, 4, 5, 6))
        e = load("pk_enrich_vir_s", ENRICH)
        self.assertEqual(fact["tiers"], ["auto", *e.GATE_TIERS])
        self.assertEqual(fact["container_name_prefix"], e.VERIFY_GATE_NAME + "-")


if __name__ == "__main__":
    unittest.main()
