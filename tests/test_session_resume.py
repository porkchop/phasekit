"""Resume by explicit session id, never `claude -c` (v0.18.8).

Every fleet session works in /workspace with one shared config volume, so
`claude -c` — "continue the most recent conversation in this directory" —
could resume ANOTHER project's conversation: measured 2026-10-06, 15 shared
transcripts held two projects' turns, and three xmeo-v3 turns had resumed
foundry-orchestrator sessions. The loop now runs one conversation per run: the
first turn's id is recorded (artifacts/logs/claude-session-id), and every
later turn, CLI retry and verdict request resumes THAT id. No usable id, or a
resume the CLI cannot honour, starts a NEW session with a re-anchoring prompt.

The real loop and the real run-phase.sh run in a fixture repo against a fake
`claude` that keeps the CLI's session semantics as probed live on Claude Code
2.1.289: `system`/`init` carries `session_id`; `--session-id` names a new
session; `--resume <id>` continues it under the same id; an unknown id prints
"No conversation found", emits a `result` with no turn and no `init`, exits 1;
`-c` takes the most recent session of ANY project (the shared directory).

Run from the repo root: `python3 -m unittest tests.test_session_resume`
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)
from _layout import Layout

try:
    from test_run_until_done_v060 import LoopHarness, VERIFY_OK
except ImportError:  # pragma: no cover
    from tests.test_run_until_done_v060 import LoopHarness, VERIFY_OK

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_PHASE = os.path.join(REPO_ROOT, "scripts", "run-phase.sh")
LOOP = os.path.join(REPO_ROOT, "scripts", "run-until-done.sh")
FORMATTER = os.path.join(REPO_ROOT, "scripts", "phasekit-log-fmt.sh")
SESSION_FILE = "artifacts/logs/claude-session-id"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REANCHOR = "NOTE FROM THE LOOP"


def _read(path):
    with open(path) as f:
        return f.read()

# The fake CLI. $FAKE_CLAUDE_STATE/sessions/<id> = one conversation (the calls
# that ran in it); latest = the most recent conversation in the shared
# directory, which is what -c would pick. $FAKE_CLAUDE_STATE/preinit-fail-<n>
# makes call n die before its session starts (no output at all);
# exit-<n> sets call n's exit code after its turn ran.
FAKE_CLAUDE = r"""#!/usr/bin/env bash
st="${FAKE_CLAUDE_STATE:?}"
mkdir -p "$st/sessions"
n=$(( $(cat "$st/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$st/calls"
printf '%s\n' "$@" > "$st/argv-$n"
mode=new; sid=""; prompt=""
while (( $# )); do
  case "$1" in
    -c|--continue) mode=continue ;;
    -r|--resume) mode=resume; sid="$2"; shift ;;
    --session-id) sid="$2"; shift ;;
    --model|--permission-mode|--output-format) shift ;;
    -p|--print) prompt="$2"; shift ;;
  esac
  shift
done
printf '%s' "$prompt" > "$st/prompt-$n"
if [[ -f "$st/preinit-fail-$n" ]]; then exit 1; fi
echo "fake-claude: a stderr line" >&2
case "$mode" in
  continue) sid="$(cat "$st/latest" 2>/dev/null)" ;;
  resume)
    if [[ ! -f "$st/sessions/$sid" ]]; then
      echo "No conversation found with session ID: $sid"
      echo '{"type":"result","subtype":"error_during_execution","is_error":true,"num_turns":0,"session_id":"'"$sid"'"}'
      exit 1
    fi ;;
esac
[[ -n "$sid" ]] || sid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
echo "$n" >> "$st/sessions/$sid"
echo "$sid" > "$st/latest"
echo '{"type":"system","subtype":"init","cwd":"/workspace","session_id":"'"$sid"'"}'
rc=0
if [[ -f scripts/scenario.sh ]]; then CALL_N="$n" bash scripts/scenario.sh || rc=$?; fi
echo '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"session_id":"'"$sid"'"}'
if [[ -f "$st/exit-$n" ]]; then exit "$(cat "$st/exit-$n")"; fi
exit "$rc"
"""

UPDATE = ("jq -n '{suggested_commit_message: \"phase-1: step '\"$CALL_N\"'\"}'"
          " > artifacts/phase-update.json\n")


class _FakeClaude:
    """The fake CLI's state directory, read back."""

    def _fake_setup(self, root):
        self.fake_dir = os.path.join(root, "fake-claude-state")
        self.fake_bin = os.path.join(root, "fake-bin")
        os.makedirs(self.fake_dir, exist_ok=True)
        os.makedirs(self.fake_bin, exist_ok=True)
        path = os.path.join(self.fake_bin, "claude")
        with open(path, "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(path, 0o755)

    def fake_env(self):
        return {"FAKE_CLAUDE_STATE": self.fake_dir,
                "PATH": self.layout.claude_path(self.fake_bin + os.pathsep + os.environ["PATH"])}

    def argv(self, n):
        with open(os.path.join(self.fake_dir, f"argv-{n}")) as f:
            return f.read().splitlines()

    def prompt(self, n):
        with open(os.path.join(self.fake_dir, f"prompt-{n}")) as f:
            return f.read()

    def fake_calls(self):
        p = os.path.join(self.fake_dir, "calls")
        return int(_read(p)) if os.path.exists(p) else 0

    def flag(self, n, name):
        a = self.argv(n)
        return a[a.index(name) + 1] if name in a else None

    def assert_never_continue(self):
        for n in range(1, self.fake_calls() + 1):
            a = self.argv(n)
            self.assertNotIn("-c", a, f"call {n} used `claude -c`: {a}")
            self.assertNotIn("--continue", a, f"call {n} used --continue: {a}")

    def mark(self, kind, n, value="1"):
        with open(os.path.join(self.fake_dir, f"{kind}-{n}"), "w") as f:
            f.write(value)

    def seed_foreign_session(self):
        """Another project's conversation, the most recent one in the shared
        directory — what `claude -c` would resume."""
        os.makedirs(os.path.join(self.fake_dir, "sessions"), exist_ok=True)
        foreign = "99999999-9999-4999-8999-999999999999"
        open(os.path.join(self.fake_dir, "sessions", foreign), "w").close()
        with open(os.path.join(self.fake_dir, "latest"), "w") as f:
            f.write(foreign + "\n")
        return foreign


class LoopResumesBySessionId(_FakeClaude, LoopHarness):
    """The real loop + the real run-phase.sh + the fake CLI."""

    def setUp(self):
        super().setUp()
        self._write("scripts/phasekit-verify.sh", VERIFY_OK, executable=True)
        self.layout.put("scripts/run-phase.sh", src=RUN_PHASE, executable=True)
        if os.path.exists(FORMATTER):
            self.layout.put("scripts/phasekit-log-fmt.sh", src=FORMATTER, executable=True)
        self._git("add", "-A")
        self._git("commit", "--allow-empty", "-qm", "real run-phase")
        self._fake_setup(self.tmp)
        self.foreign = self.seed_foreign_session()

    def run_loop(self, scenario, env=None):
        e = self.fake_env()
        e.update(env or {})
        return self._run_loop(scenario, env=e)

    def recorded(self):
        p = os.path.join(self.repo, SESSION_FILE)
        return _read(p).strip() if os.path.exists(p) else None

    def test_later_iterations_resume_the_first_turns_session_by_id(self):
        r = self.run_loop(UPDATE, env={"MAX_ITERATIONS": "3"})
        self.assertEqual(self.fake_calls(), 3, r.stdout + r.stderr)
        first = self.flag(1, "--session-id")
        self.assertRegex(first or "", UUID_RE)
        self.assertIsNone(self.flag(1, "--resume"))
        self.assertEqual(self.flag(2, "--resume"), first)
        self.assertEqual(self.flag(3, "--resume"), first)
        self.assert_never_continue()
        self.assertEqual(self.recorded(), first)
        self.assertNotIn(REANCHOR, self.prompt(2))

    def test_the_verdict_request_resumes_the_same_session(self):
        scenario = ('case "$CALL_N" in\n'
                    '  1) echo w1 >> src.txt ;;\n'
                    "  2) " + UPDATE + " ;;\n"
                    "esac\n")
        r = self.run_loop(scenario, env={"MAX_ITERATIONS": "1"})
        self.assertIn("asking once for a verdict", r.stdout + r.stderr)
        self.assertEqual(self.fake_calls(), 2)
        self.assertIn("without writing a verdict artifact", self.prompt(2))
        self.assertEqual(self.flag(2, "--resume"), self.flag(1, "--session-id"))
        self.assert_never_continue()

    def test_a_cli_retry_resumes_the_same_session(self):
        self.mark("exit", 1, "1")
        r = self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1", "PHASEKIT_ITER_RETRY": "1"})
        self.assertIn("retrying in continue mode", r.stdout + r.stderr)
        self.assertEqual(self.fake_calls(), 2)
        self.assertEqual(self.flag(2, "--resume"), self.flag(1, "--session-id"))
        self.assert_never_continue()

    def test_a_turn_that_died_before_its_session_started_is_retried_fresh(self):
        # Call 1 dies before the CLI started a session: the pre-chosen id names
        # nothing. The retry's resume cannot be honoured, so it starts a NEW
        # session that says so — never the foreign "most recent" one.
        self.mark("preinit-fail", 1)
        r = self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1", "PHASEKIT_ITER_RETRY": "1"})
        self.assertEqual(self.fake_calls(), 3, r.stdout + r.stderr)
        dead = self.flag(1, "--session-id")
        self.assertEqual(self.flag(2, "--resume"), dead)
        fresh = self.flag(3, "--session-id")
        self.assertRegex(fresh or "", UUID_RE)
        self.assertNotEqual(fresh, dead)
        self.assertNotEqual(fresh, self.foreign)
        self.assertIn(REANCHOR, self.prompt(3))
        self.assertIn("could not resume", r.stdout + r.stderr)
        self.assertEqual(self.recorded(), fresh)
        self.assert_never_continue()
        self.assertIn("phase-1: step 3", self._messages())

    def test_continue_without_a_recorded_id_starts_new_and_re_anchors(self):
        r = self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1", "CLAUDE_MODE": "continue"})
        self.assertEqual(self.fake_calls(), 1, r.stdout + r.stderr)
        self.assertIsNone(self.flag(1, "--resume"))
        self.assertRegex(self.flag(1, "--session-id") or "", UUID_RE)
        self.assertTrue(self.prompt(1).startswith(REANCHOR), self.prompt(1)[:200])
        self.assertIn("standard continue prompt", self.prompt(1))
        self.assertIn("no recorded session id", r.stdout + r.stderr)
        self.assert_never_continue()

    def test_operator_continue_resumes_the_last_runs_conversation(self):
        self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1"})
        first = self.flag(1, "--session-id")
        self.assertEqual(self.recorded(), first)
        self.seed_foreign_session()  # another project ran since
        self._prepare_next_session(UPDATE + "# the next session\n")
        r = self.run_loop(None, env={"MAX_ITERATIONS": "1", "CLAUDE_MODE": "continue"})
        self.assertEqual(self.fake_calls(), 2, r.stdout + r.stderr)
        self.assertEqual(self.flag(2, "--resume"), first)
        self.assertNotIn(REANCHOR, self.prompt(2))
        self.assert_never_continue()

    def test_a_new_run_starts_a_new_conversation(self):
        self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1"})
        self._prepare_next_session(UPDATE + "# the next session\n")
        self.run_loop(None, env={"MAX_ITERATIONS": "1"})
        a, b = self.flag(1, "--session-id"), self.flag(2, "--session-id")
        self.assertIsNone(self.flag(2, "--resume"))
        self.assertNotEqual(a, b)
        self.assertEqual(self.recorded(), b)

    def test_an_id_the_cli_cannot_resume_is_replaced_never_redirected(self):
        # A recorded id from elsewhere (another project's tree, a transcript
        # directory that was lost) names no conversation here.
        os.makedirs(os.path.join(self.repo, "artifacts", "logs"), exist_ok=True)
        stray = "12345678-1234-4234-8234-123456789abc"
        with open(os.path.join(self.repo, SESSION_FILE), "w") as f:
            f.write(stray + "\n")
        r = self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1", "CLAUDE_MODE": "continue"})
        self.assertEqual(self.fake_calls(), 2, r.stdout + r.stderr)
        self.assertEqual(self.flag(1, "--resume"), stray)
        fresh = self.flag(2, "--session-id")
        self.assertNotIn(fresh, (stray, self.foreign))
        self.assertIn(REANCHOR, self.prompt(2))
        self.assertEqual(self.recorded(), fresh)
        self.assert_never_continue()

    def test_a_malformed_recorded_id_never_reaches_the_cli(self):
        os.makedirs(os.path.join(self.repo, "artifacts", "logs"), exist_ok=True)
        with open(os.path.join(self.repo, SESSION_FILE), "w") as f:
            f.write("--dangerously-skip-permissions\n")
        self.run_loop(UPDATE, env={"MAX_ITERATIONS": "1", "CLAUDE_MODE": "continue"})
        a = self.argv(1)
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertIsNone(self.flag(1, "--resume"))
        self.assertIn(REANCHOR, self.prompt(1))

    def test_the_session_id_is_never_committed_nor_in_git_status(self):
        self.run_loop(UPDATE, env={"MAX_ITERATIONS": "2"})
        self.assertIsNotNone(self.recorded())
        self.assertNotIn("claude-session-id", self._git("status", "--porcelain",
                                                         "--untracked-files=all"))
        self.assertNotIn("claude-session-id", self._git("log", "--all", "--name-only",
                                                         "--format="))


class RunPhaseDirect(_FakeClaude, unittest.TestCase):
    """run-phase.sh on its own: the light review, exit codes, bookkeeping."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pk-resume-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.layout = Layout(self.repo)
        self.addCleanup(self.layout.cleanup)
        self.layout.put("scripts/run-phase.sh", src=RUN_PHASE, executable=True)
        with open(os.path.join(self.repo, "prompt.txt"), "w") as f:
            f.write("the prompt\n")
        self._fake_setup(self.tmp)
        self.foreign = self.seed_foreign_session()

    def run_phase(self, mode, it="1", extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_MODE", "PHASEKIT_ITER", "ANTHROPIC_MODEL",
                            "PHASEKIT_RETRY_ATTEMPT", "PHASEKIT_TRACE")}
        env.update(self.fake_env())
        env.update({"CLAUDE_MODE": mode, "PHASEKIT_ITER": it})
        env.update(extra or {})
        env = self.layout.env(env)
        return subprocess.run(["bash", str(self.layout.path("scripts/run-phase.sh")),
                               os.path.join(self.repo, "prompt.txt")],
                              cwd=self.repo, env=env, capture_output=True, text=True,
                              timeout=60)

    def recorded(self):
        p = os.path.join(self.repo, SESSION_FILE)
        return _read(p).strip() if os.path.exists(p) else None

    def test_the_light_review_never_replaces_the_runs_conversation(self):
        self.run_phase("new", "1")
        main = self.recorded()
        self.assertRegex(main or "", UUID_RE)
        r = self.run_phase("new", "light-review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.flag(2, "--session-id"))
        self.assertIsNone(self.flag(2, "--resume"))
        self.assertEqual(self.recorded(), main)
        self.run_phase("continue", "2")
        self.assertEqual(self.flag(3, "--resume"), main)
        self.assert_never_continue()

    def test_the_new_id_is_recorded_before_the_model_starts(self):
        # A turn that dies at once still names its conversation.
        self.mark("preinit-fail", 1)
        r = self.run_phase("new", "1")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(self.recorded() or "", UUID_RE)
        self.assertEqual(self.recorded(), self.flag(1, "--session-id"))

    def test_the_cli_exit_code_passes_through(self):
        self.mark("exit", 1, "7")
        self.assertEqual(self.run_phase("new", "1").returncode, 7)
        self.mark("exit", 2, "5")
        self.assertEqual(self.run_phase("continue", "2").returncode, 5)
        self.assertEqual(self.flag(2, "--resume"), self.flag(1, "--session-id"))

    def test_the_reported_id_wins(self):
        # A CLI that ran the turn under another id than the one asked for (a
        # fork): the id it reports is the conversation to resume.
        os.makedirs(os.path.join(self.repo, "artifacts", "logs"), exist_ok=True)
        fake = os.path.join(self.fake_bin, "claude")
        with open(fake) as f:
            body = f.read()
        with open(fake, "w") as f:
            f.write(body.replace('--session-id) sid="$2"; shift ;;', '--session-id) shift ;;'))
        self.run_phase("new", "1")
        asked = self.flag(1, "--session-id")
        got = self.recorded()
        self.assertRegex(got or "", UUID_RE)
        self.assertNotEqual(got, asked)
        self.run_phase("continue", "2")
        self.assertEqual(self.flag(2, "--resume"), got)

    def test_an_unwritable_record_never_fails_the_turn(self):
        os.makedirs(os.path.join(self.repo, "artifacts", "logs", "claude-session-id",
                                 "blocker"))
        r = self.run_phase("new", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(REANCHOR, self.prompt(2))
        self.assert_never_continue()

    def test_a_failed_resume_keeps_both_attempts_in_the_logs(self):
        os.makedirs(os.path.join(self.repo, "artifacts", "logs"), exist_ok=True)
        with open(os.path.join(self.repo, SESSION_FILE), "w") as f:
            f.write("12345678-1234-4234-8234-123456789abc\n")
        r = self.run_phase("continue", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = _read(os.path.join(self.repo, "artifacts", "logs", "claude-iter-4.jsonl"))
        self.assertIn("No conversation found", raw)
        self.assertIn('"subtype":"init"', raw)

    def test_a_turn_killed_mid_way_is_not_read_as_an_unresumable_session(self):
        # No `result` at all (a signal): the exit code propagates, no new session.
        self.run_phase("new", "1")
        fake = os.path.join(self.fake_bin, "claude")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'n=$(( $(cat "$FAKE_CLAUDE_STATE/calls") + 1 )); '
                    'echo "$n" > "$FAKE_CLAUDE_STATE/calls"; '
                    'printf "%s\\n" "$@" > "$FAKE_CLAUDE_STATE/argv-$n"; exit 143\n')
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 143)
        self.assertEqual(self.fake_calls(), 2)


class ResumeRefusalIsNarrow(RunPhaseDirect):
    """Review M1/m1/m2 (v0.18.8): the fallback starts a new session ONLY when
    the CLI said it has no such conversation — never after a signal, an auth
    or credit failure, or once the loop is taking the session back."""

    REAL = "11111111-2222-4333-8444-555555555555"

    # the inherited run-phase tests run once, in the parent class
    for _n in [n for n in dir(RunPhaseDirect) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.repo, "artifacts", "logs"), exist_ok=True)
        with open(os.path.join(self.repo, SESSION_FILE), "w") as f:
            f.write(self.REAL + "\n")

    def first_call_is(self, body):
        """Call 1 behaves as `body`; later calls are the normal fake."""
        fake = os.path.join(self.fake_bin, "claude")
        normal = os.path.join(self.fake_bin, "claude-normal")
        os.rename(fake, normal)
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'st="$FAKE_CLAUDE_STATE"\n'
                    'if [[ ! -f "$st/calls" ]]; then echo 1 > "$st/calls"; '
                    'printf "%s\\n" "$@" > "$st/argv-1"\n' + body + "\nfi\n"
                    f'exec "{normal}" "$@"\n')
        os.chmod(fake, 0o755)

    ZERO_TURN = ("echo '{\"type\":\"result\",\"subtype\":\"error_during_execution\","
                 "\"is_error\":true,\"num_turns\":0,\"session_id\":\"x\"}'")

    def test_a_signalled_resume_with_a_zero_turn_result_is_not_replaced(self):
        self.first_call_is("echo 'No conversation found with session ID: x'; "
                           + self.ZERO_TURN + "; exit 143")
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 143)
        self.assertEqual(self.fake_calls(), 1)
        self.assertEqual(self.recorded(), self.REAL)

    def test_an_auth_failure_keeps_the_runs_conversation(self):
        self.first_call_is("echo 'Invalid API key · Please run /login' >&2; "
                           + self.ZERO_TURN + "; exit 1")
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.fake_calls(), 1)
        self.assertEqual(self.recorded(), self.REAL)

    def test_a_refusal_on_stderr_alone_is_still_a_refusal(self):
        self.first_call_is(f"echo 'No conversation found with session ID: {self.REAL}' >&2; "
                           "exit 1")
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake_calls(), 2)
        self.assertIn(REANCHOR, self.prompt(2))
        self.assertNotEqual(self.recorded(), self.REAL)

    def test_no_new_session_once_the_loop_is_taking_the_session_back(self):
        refusal = (f"echo 'No conversation found with session ID: {self.REAL}'; "
                   + self.ZERO_TURN)
        for marker in ("artifacts/wrapup-requested", "artifacts/logs/.deadline-yield",
                       "artifacts/logs/.completion-yield"):
            with self.subTest(marker=marker):
                shutil.rmtree(self.fake_dir)
                os.makedirs(self.fake_dir)
                self.first_call_is(refusal + f"; touch '{self.repo}/{marker}'; exit 1")
                normal = os.path.join(self.fake_bin, "claude-normal")
                try:
                    r = self.run_phase("continue", "2")
                    self.assertEqual(r.returncode, 1)
                    self.assertEqual(self.fake_calls(), 1)
                finally:
                    os.remove(os.path.join(self.repo, marker))
                    os.replace(normal, os.path.join(self.fake_bin, "claude"))

    def test_a_stale_yield_from_an_earlier_turn_does_not_block_the_fallback(self):
        y = os.path.join(self.repo, "artifacts", "logs", ".deadline-yield")
        open(y, "w").close()
        old = time.time() - 600
        os.utime(y, (old, old))
        self.first_call_is(f"echo 'No conversation found with session ID: {self.REAL}'; "
                           + self.ZERO_TURN + "; exit 1")
        r = self.run_phase("continue", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake_calls(), 2)

    def test_the_refused_resumes_pid_is_not_left_for_the_watchdog(self):
        pidfile = os.path.join(self.repo, "artifacts", "logs", "claude.pid")
        self.first_call_is(f"echo 'No conversation found with session ID: {self.REAL}'; "
                           + self.ZERO_TURN + "; exit 1")
        fake = os.path.join(self.fake_bin, "claude-normal")
        with open(fake) as f:
            body = f.read()
        with open(fake, "w") as f:
            f.write(body.replace('mkdir -p "$st/sessions"',
                                 'mkdir -p "$st/sessions"\n'
                                 f'cp "{pidfile}" "$st/pid-seen" 2>/dev/null || true', 1))
        self.run_phase("continue", "2")
        seen = _read(os.path.join(self.fake_dir, "pid-seen")).split()
        self.assertEqual(seen[1], "2")
        self.assertFalse(os.path.exists(pidfile))

    def test_an_id_path_that_is_a_directory_collects_no_junk(self):
        os.remove(os.path.join(self.repo, SESSION_FILE))
        d = os.path.join(self.repo, SESSION_FILE)
        os.makedirs(d)
        r = self.run_phase("new", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(d), [])
        self.assertIn("is a directory", r.stderr)


class LightReviewThroughTheLoop(LoopResumesBySessionId):
    """The light review runs through the real loop and never replaces the run's
    conversation."""

    def test_the_light_review_is_a_side_conversation(self):
        from test_run_until_done_v060 import VERIFY_OK as _ok  # noqa: F401
        scenario = ('case "$CALL_N" in\n'
                    "  1) jq -n '{summary: \"done\"}' > artifacts/project-complete.json ;;\n"
                    "esac\n")
        r = self.run_loop(scenario, env={"PHASEKIT_ITERATION_MODE": "light",
                                         "MAX_ITERATIONS": "2"})
        out = r.stdout + r.stderr
        self.assertIn("final review pass", out)
        self.assertGreaterEqual(self.fake_calls(), 2, out)
        first = self.flag(1, "--session-id")
        self.assertIsNone(self.flag(2, "--session-id"))
        self.assertIsNone(self.flag(2, "--resume"))
        self.assertEqual(self.recorded(), first)
        self.assert_never_continue()

    # the inherited loop tests run once, in the parent class
    for _n in [n for n in dir(LoopResumesBySessionId) if n.startswith("test_")]:
        locals()[_n] = None
    del _n


class NoContinueAnywhere(unittest.TestCase):
    """Structural: no loop path builds `claude -c`."""

    def test_run_phase_never_passes_continue(self):
        text = _read(RUN_PHASE)
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\+=\(\s*(-c|--continue)\b")
        self.assertNotRegex(code, r"\bclaude\b[^\n]*\s(-c|--continue)\b")
        self.assertIn("--resume", code)
        self.assertIn("--session-id", code)

    def test_the_loop_invokes_claude_only_through_run_phase(self):
        text = _read(LOOP)
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(?m)(^|[;&|(])\s*(exec\s+)?claude\s+-")
        self.assertNotRegex(code, r"\bclaude\b[^\n]*\s(-c|--continue)\b")


if __name__ == "__main__":
    unittest.main()
