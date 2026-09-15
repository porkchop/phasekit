#!/usr/bin/env python3
"""Boundary state (v0.14.5): one record, one landing path, every kill point
covered — by construction, not by the next incident.

Four incidents in three days (2026-09-07/08) were each a SEAM between two
correct mechanisms, because "done" was DERIVED by eight accreted mechanisms
from different subsets of five files. The release replaces the derivation
with one record (artifacts/boundary-state.json, steps 0 idle → 7 rested) and
one landing function (land_boundary) that every entry point calls, each step
PROVEN from git/disk before it is recorded.

Aaron's bar (kickoff §3): "complete coverage of all cases so that we ideally
catch any errors we are not anticipating". So the centrepiece here is
GENERATED: for every entry point that can open a boundary and every kill
point in the sequence, a real session of the shipped loop is SIGKILLed at
that exact seam (PHASEKIT_BOUNDARY_KILL_PROBE, a fault-injection knob that is
inert unless set), then a second session runs with a stub model that writes
nothing, and the state is asserted against a small explicit model of what
the sequence owes:

  * the record reaches step 7 (green), or stays at the last proven step
    with the refusing gate's artifact on disk (red at the resume) — never
    dirty-and-silent;
  * the target carries exactly one squash per approved boundary, trailer
    included, and never two squashes of one tree;
  * the tree rests clean; a final boundary rests on the target with no
    consumed baton; a verdict artifact lands in exactly one commit;
  * the record's step is monotonic across the kill and every sha_at_step is
    reachable from HEAD or the target;
  * a green resume never re-runs the verify tier the same tree already
    passed (the verify memo — sessions 669/670's whole bound);
  * one of the known witness lines is printed.

Parametrised over {squash, plain} × {non-final, final via the approval's
final_phase flag, final with the session's own completion record} ×
{verify green, verify red at the resume}, for entry points: the iteration
commit (kill after every step, before and after the record advances), a
stranded fresh approval (no record at all — the fleet-upgrade shape), a
stale approval committed by hand (the catch-up squash), the deadline
watchdog's last-resort commit (a real kill at the bound), and the wrap-up
fall-through by soft stop and by deadline pacing.

The matrix runs in parallel (each case in its own scratch repo); failures
report per case via subTest. The bash is the shipped script, copied whole.

Run from the repo root: python3 -m unittest tests.test_boundary_state
"""

import base64
import concurrent.futures
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_SCRIPT = REPO_ROOT / "scripts" / "run-until-done.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
SOURCE = LOOP_SCRIPT.read_text()

STEP_NAMES = ["idle", "approved", "committed", "recorded", "squashed",
              "merged-back", "armed", "rested"]
RESTED = 7

WITNESS_LINES = (
    "boundary-state: rested (step 7)",
    "boundary-state: stopped at step",
    "boundary-state: landing from step",
    "Run finished successfully.",
)


def _extract_block(start_re, end_re):
    lines = SOURCE.splitlines()
    out, taking = [], False
    for line in lines:
        if not taking and re.match(start_re, line):
            taking = True
        if taking:
            if re.match(end_re, line):
                return "\n".join(out)
            out.append(line)
    raise AssertionError(f"could not extract {start_re!r} from the loop")


BOUNDARY_BLOCK = _extract_block(r"^# --- Boundary state \(v0\.14\.5\)", r"^# --- Deadline watchdog")


def _transient_array():
    m = re.search(r"^TRANSIENT_SIGNALS=\((.*?)^\)", SOURCE, re.S | re.M)
    names = re.findall(r'^\s*"([^"]+)"\s*$', m.group(1), re.M)
    return "TRANSIENT_SIGNALS=(" + " ".join('"%s"' % n for n in names) + ")"


# ---------------------------------------------------------------------------
# The stub model and verify scripts
# ---------------------------------------------------------------------------

STUB_RUN_PHASE = """#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
: "${STUB_DIR:?stub state dir must be set}"
n=$(( $(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/calls"
cp "$1" "$STUB_DIR/prompt-$n.txt"
export PHASEKIT_TEST_LOOP_PID="$PPID"
CALL_N="$n" bash "$STUB_DIR/scenario.sh"
"""

# Logs every run so a memo hit (no run) is observable.
VERIFY_LOGGING = """#!/usr/bin/env bash
PHASEKIT_VERIFY_CONFIGURED=1
echo run >> "${STUB_DIR:?}/verify-calls"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/BAD" ]; then exit 1; fi
exit 0
"""

# The model's approval-class output. FINAL_KIND: no | flag | both.
APPROVE_SCENARIO = r"""
echo "work by call $CALL_N" >> src.txt
jq -n --arg fk "${FINAL_KIND:-no}" '{
  phase: "phase-1", approved: true, summary: "built it",
  suggested_commit_message: "Phase 1 (APPROVED): built it",
  final_phase: ($fk != "no"),
  deferrals: [
    {item: "AC#3 integer math", reason: "later", suggested_task: "fixed point"},
    {item: "Polish the lobby animation timing curve", reason: "later", suggested_task: "polish"}
  ]
}' > artifacts/phase-approval.json
if [ "${FINAL_KIND:-no}" = both ]; then
  jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete: v0 shipped"}' > artifacts/project-complete.json
fi
"""

NOTHING_SCENARIO = ":\n"
# A stub model that writes nothing but ORIENTS like a real first turn
# (CONTINUE_PROMPT step 1: read the baton, then delete it).
ORIENT_SCENARIO = "rm -f artifacts/session-handoff.json\n"


class Repo:
    """A scratch repo on `main` with the shipped loop, a stub model, and a
    logging verify gate. squash=True runs branch-per-iteration."""

    def __init__(self, squash):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-boundary-"))
        self.repo = self.tmp / "repo"
        self.stub = self.tmp / "stub"
        self.squash = squash
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "artifacts").mkdir()
        (self.repo / "docs").mkdir()
        self.stub.mkdir()
        shutil.copy(LOOP_SCRIPT, self.repo / "scripts" / "run-until-done.sh")
        self.write("scripts/run-phase.sh", STUB_RUN_PHASE, executable=True)
        self.write("scripts/phasekit-verify.sh", VERIFY_LOGGING, executable=True)
        self.write("CONTINUE_PROMPT.txt", "prompt\n")
        self.write("docs/PHASES.md", "# Phases\n")
        self.write("src.txt", "base\n")
        self.scenario(NOTHING_SCENARIO)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        if squash:
            self.git("checkout", "-q", "-b", "iter/1-test")
        self.branch = "iter/1-test" if squash else "main"

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- plumbing -------------------------------------------------------
    def write(self, rel, content, executable=False):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            os.chmod(path, 0o755)

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def scenario(self, text):
        # Outside the tree on purpose: a scenario is the model's script, not
        # project content a commit may sweep.
        (self.stub / "scenario.sh").write_text(text)

    def reset_stub(self):
        for f in ("calls", "verify-calls"):
            p = self.stub / f
            if p.exists():
                p.unlink()

    def run(self, env=None, timeout=120):
        run_env = dict(os.environ)
        for var in ("ANTHROPIC_MODEL", "PHASEKIT_ITERATION_MODE", "MAX_ITERATIONS",
                    "VERIFY_MAX_ATTEMPTS", "CLAUDE_MODE", "AUTO_PUSH",
                    "PHASEKIT_VERIFY_CMD", "VERIFY_SKIP", "PHASEKIT_WRAPUP_SENTINEL",
                    "PHASEKIT_SESSION_DEADLINE", "PHASEKIT_PACING_FLOOR_SECONDS",
                    "PHASEKIT_VERIFY_BUDGET_SECONDS", "PHASEKIT_SQUASH_TARGET",
                    "PHASEKIT_WORK_BRANCH", "PHASEKIT_BOUNDARY_KILL_PROBE",
                    "PHASEKIT_WRAPUP_LEAD_SECONDS", "PHASEKIT_LASTRESORT_LEAD_SECONDS",
                    "FINAL_KIND"):
            run_env.pop(var, None)
        run_env.update({
            "PHASEKIT_NO_UPDATE_CHECK": "1",
            "PHASEKIT_ITER_RETRY": "0",
            "STUB_DIR": str(self.stub),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        if self.squash:
            run_env["PHASEKIT_SQUASH_TARGET"] = "main"
            run_env["PHASEKIT_WORK_BRANCH"] = "iter/1-test"
        run_env.update(env or {})
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "run-until-done.sh")],
            cwd=self.repo, env=run_env, capture_output=True, text=True, timeout=timeout)

    # -- observations ---------------------------------------------------
    def record(self):
        p = self.repo / "artifacts" / "boundary-state.json"
        return json.loads(p.read_text()) if p.exists() else None

    def calls(self):
        p = self.stub / "calls"
        return int(p.read_text().strip()) if p.exists() else 0

    def verify_calls(self):
        p = self.stub / "verify-calls"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def head_branch(self):
        return self.git("symbolic-ref", "-q", "--short", "HEAD", check=False) or "HEAD"

    def tree(self, ref):
        return self.git("rev-parse", f"{ref}^{{tree}}")

    def porcelain(self):
        return [ln for ln in self.git("status", "--porcelain").splitlines() if ln]

    def trailer_commits(self, ref="main"):
        out = self.git("log", "--format=%H%x00%B%x01", f"{self.base}..{ref}")
        commits = []
        for chunk in out.split("\x01"):
            if "\x00" not in chunk:
                continue
            sha, body = chunk.strip().split("\x00", 1)
            if re.search(r"^phasekit-squash: ", body, re.M):
                commits.append(sha)
        return commits

    def commits_touching(self, path, ref="HEAD"):
        out = self.git("log", "--no-merges", "--diff-filter=AM", "--format=%H",
                       f"{self.base}..{ref}", "--", path)
        return [ln for ln in out.splitlines() if ln]

    def tracked(self, path, ref="HEAD"):
        return self.git("cat-file", "-e", f"{ref}:{path}", check=False) == "" and \
            subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", f"{ref}:{path}"],
                           capture_output=True).returncode == 0

    def reachable(self, sha):
        for ref in ("HEAD", "main", self.branch):
            r = subprocess.run(["git", "-C", str(self.repo), "merge-base",
                                "--is-ancestor", sha, ref], capture_output=True)
            if r.returncode == 0:
                return True
        return False

    def artifact(self, name):
        return self.repo / "artifacts" / name


# ---------------------------------------------------------------------------
# The explicit model of what the sequence owes after a kill at step k
# ---------------------------------------------------------------------------

def expected_step(mode, k, red, verified_tree, needs_completion_commit):
    """k = the last step whose ACTION completed before the kill (0..7).
    red = the resume's verify gate is red. verified_tree = the tree the
    resume must land already passed a gate (a memo exists), so no
    verify-gated action remains for it. needs_completion_commit = a final
    boundary whose completion record is not yet committed (step 3 must
    make a verify-gated commit)."""
    squash = mode == "squash"
    if not red:
        return RESTED
    if k < 2:
        return 1                    # the phase commit is the first gated action
    if needs_completion_commit:
        return 2                    # the completion commit is the next gated action
    if squash and k < 4 and not verified_tree:
        return 3                    # the catch-up squash re-verifies an unverified tree
    return RESTED


def expected_verify_runs_green(entry, mode, k, needs_completion_commit):
    """How many times the RESUME's gate should actually run when green."""
    squash = mode == "squash"
    if entry in ("stranded-fresh", "wrapup", "pacing") or k < 2:
        return 1                    # ONE phase commit carries the (synthesized) completion; the squash inside re-uses verified=1
    if entry in ("catchup", "watchdog"):
        if squash:
            return 1                # an unverified tree: the completion commit OR the catch-up squash verifies it once
        return 1 if needs_completion_commit else 0
    return 0                        # iteration entry, k >= 2: the memo covers the tree


# ---------------------------------------------------------------------------
# Case construction: leave the tree exactly as the kill would
# ---------------------------------------------------------------------------

def build_case(entry, mode, final_kind, k, probe_phase, red):
    """Run (or construct) the killed session; return (repo, kill_step)."""
    repo = Repo(squash=(mode == "squash"))
    env1 = {"FINAL_KIND": final_kind}
    if entry == "iteration":
        repo.scenario(APPROVE_SCENARIO)
        env1["PHASEKIT_BOUNDARY_KILL_PROBE"] = f"{k}:{probe_phase}"
        env1["MAX_ITERATIONS"] = "1"
        r1 = repo.run(env=env1)
        if r1.returncode != -9:
            raise AssertionError(f"session 1 was not killed by the probe (rc {r1.returncode}):\n{r1.stdout}\n{r1.stderr}")
        return repo, k
    if entry == "stranded-fresh":
        # No session ran: the artifacts and work sit uncommitted, no record.
        subprocess.run(["bash", "-c", APPROVE_SCENARIO], cwd=repo.repo, check=True,
                       env={**os.environ, "CALL_N": "1", "FINAL_KIND": final_kind})
        return repo, 1
    if entry == "catchup":
        # Committed on the branch by hand (a strand commit, a hotfix): the
        # target lacks it and no record exists.
        subprocess.run(["bash", "-c", APPROVE_SCENARIO], cwd=repo.repo, check=True,
                       env={**os.environ, "CALL_N": "1", "FINAL_KIND": final_kind})
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: landed by hand, never squashed")
        return repo, 3 if final_kind != "no" else 2
    if entry == "watchdog":
        # The deadline watchdog's last-resort commit fires while the model is
        # still in its turn; the supervisor's kill follows. Real kill.
        now = int(time.time())
        # The turn waits for the watchdog's commit to land (bounded), then the
        # supervisor's kill arrives — the production order (commit at T-60s,
        # kill at T), made deterministic under a loaded test host.
        repo.scenario(APPROVE_SCENARIO + (
            'for _ in $(seq 1 125); do grep -q "last-resort commit landed" '
            '"$ROOT_DIR/artifacts/logs/deadline-watchdog.log" 2>/dev/null && break; sleep 0.2; done\n'
            'kill -KILL "$PHASEKIT_TEST_LOOP_PID"\nsleep 5\n'))
        env1.update({
            "PHASEKIT_SESSION_DEADLINE": str(now + 62),
            "PHASEKIT_WRAPUP_LEAD_SECONDS": "0",
            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "60",   # fires ~2s in, after the turn has dirtied the tree
            "PHASEKIT_PACING_FLOOR_SECONDS": "1",       # the 3-minute floor would refuse the turn
            "MAX_ITERATIONS": "1",
        })
        r1 = repo.run(env=env1)
        if r1.returncode != -9:
            raise AssertionError(f"watchdog session was not killed (rc {r1.returncode}):\n{r1.stdout}\n{r1.stderr}")
        log = repo.artifact("logs/deadline-watchdog.log")
        deadline = time.time() + 20
        while time.time() < deadline and not (log.exists() and "last-resort commit landed" in log.read_text()):
            time.sleep(0.2)
        if not (log.exists() and "last-resort commit landed" in log.read_text()):
            raise AssertionError(f"the watchdog never landed its commit:\n{log.read_text() if log.exists() else '(no log)'}")
        if "command not found" in log.read_text():
            # v0.14.6 (review MINOR-4): the one dynamic, real-fork guard — a
            # helper the forked watchdog cannot see (defined after the arm
            # site; run 682) shows up here, whatever the static pin thinks.
            raise AssertionError(f"the forked watchdog could not see a helper:\n{log.read_text()}")
        return repo, 3
    if entry in ("wrapup", "pacing"):
        # v0.14.2 shape: the approval's commit is RED at the boundary, the
        # session then wraps up (soft stop / pacing) with a red gate — the
        # standing work lands as a labelled wip, the approval stays on disk.
        stop = ("touch artifacts/wrapup-requested\n" if entry == "wrapup" else "sleep 4\n")
        repo.scenario("touch BAD\n" + APPROVE_SCENARIO + stop)
        env1["MAX_ITERATIONS"] = "3"
        if entry == "pacing":
            # The first pacing check (before iteration 1) sees ~7s remaining
            # against a 1s floor and starts the turn; the turn sleeps 4s, so
            # the second check sees <=3s against a 1.2x-average threshold of
            # >=4s (integer math) and wraps up — robust to load both ways.
            env1.update({
                "PHASEKIT_SESSION_DEADLINE": str(int(time.time()) + 7),
                "PHASEKIT_PACING_FLOOR_SECONDS": "1",
                "PHASEKIT_WRAPUP_LEAD_SECONDS": "0",
                "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0",
            })
        r1 = repo.run(env=env1)
        out = r1.stdout + r1.stderr
        if "UNVERIFIED work committed" not in out:
            raise AssertionError(f"the fall-through did not happen (rc {r1.returncode}):\n{out}")
        if not red:
            (repo.repo / "BAD").unlink()      # the next session's fix
        return repo, 1
    raise AssertionError(entry)


def run_case(case):
    entry, mode, final_kind, k, probe_phase, red = case
    repo, kill_step = build_case(entry, mode, final_kind, k, probe_phase, red)
    try:
        completion_landed = repo.tracked("artifacts/project-complete.json", "HEAD")
        rec_after_kill = repo.record()
        step_at_kill = rec_after_kill["step"] if rec_after_kill else 0
        repo.reset_stub()
        repo.scenario(ORIENT_SCENARIO)
        if entry == "iteration" and k == RESTED and probe_phase == "post":
            # Killed after the last advance: nothing is owed. Assert the
            # resting state directly (a resume would start an iteration on a
            # finished boundary, which is the supervisor's decision, not the
            # sequence's).
            return dict(repo_state=snapshot(repo), step_at_kill=step_at_kill,
                        rec=rec_after_kill, out="", rc=None, calls=0, verify_runs=0,
                        kill_step=kill_step, resumed=False, completion_landed=completion_landed)
        env2 = {"MAX_ITERATIONS": "1"}
        if red and entry not in ("wrapup", "pacing"):
            env2["PHASEKIT_VERIFY_CMD"] = "exit 1"
        r2 = repo.run(env=env2)
        return dict(repo_state=snapshot(repo), step_at_kill=step_at_kill,
                    rec=repo.record(), out=r2.stdout + r2.stderr, rc=r2.returncode,
                    calls=repo.calls(), verify_runs=repo.verify_calls(),
                    kill_step=kill_step, resumed=True, completion_landed=completion_landed)
    finally:
        repo.cleanup()


def snapshot(repo):
    return dict(
        head_branch=repo.head_branch(),
        porcelain=repo.porcelain(),
        trailers=repo.trailer_commits("main") if repo.squash else [],
        trailer_trees=[repo.tree(s) for s in (repo.trailer_commits("main") if repo.squash else [])],
        tree_head=repo.tree("HEAD"),
        tree_main=repo.tree("main"),
        approval_commits=repo.commits_touching("artifacts/phase-approval.json"),
        completion_commits=repo.commits_touching("artifacts/project-complete.json"),
        baton_tracked=any(repo.tracked(p, ref) for p in ("artifacts/session-handoff.json",
                                                          "artifacts/session-interrupted.json")
                          for ref in ("HEAD", "main")),
        handoff_on_disk=repo.artifact("session-handoff.json").exists(),
        interrupted_on_disk=repo.artifact("session-interrupted.json").exists(),
        verify_failed=repo.artifact("phase-verify-failed.json").exists(),
        blocked=repo.artifact("phase-blocked.json").exists(),
        completion_on_disk=repo.artifact("project-complete.json").exists(),
        approval_never_landed=any(ln.endswith("artifacts/phase-approval.json") for ln in repo.porcelain()),
        reachable={sha: repo.reachable(sha) for sha in
                   list(((repo.record() or {}).get("sha_at_step") or {}).values())
                   + list((((repo.record() or {}).get("previous") or {}).get("sha_at_step") or {}).values())},
        boundary_state_visible=any("boundary-state" in ln for ln in repo.porcelain()),
    )


def all_cases():
    modes = ("squash", "plain")
    finals = ("no", "flag", "both")
    reds = (False, True)
    cases = []
    for mode, fk, red in itertools.product(modes, finals, reds):
        for k in range(1, RESTED + 1):
            for phase in ("pre", "post"):
                cases.append(("iteration", mode, fk, k, phase, red))
        for entry in ("stranded-fresh", "catchup", "watchdog", "wrapup", "pacing"):
            if entry == "catchup" and mode == "plain" and fk != "no":
                # A completion record committed by hand in plain mode IS a
                # resting complete project; the loop's re-run semantics there
                # (the supervisor never dispatches into it) are unchanged and
                # not a boundary the sequence owes anything to.
                continue
            cases.append((entry, mode, fk, 0, "-", red))
    return cases


def case_name(case):
    entry, mode, fk, k, phase, red = case
    where = f"kill@{k}:{phase}" if entry == "iteration" else "-"
    return f"{entry}/{mode}/final={fk}/{where}/{'red' if red else 'green'}"


class KillPointMatrix(unittest.TestCase):
    """The generated coverage. One process-level fixture: every case runs in
    its own scratch repo, in parallel; assertions are made per case."""

    results = None

    @classmethod
    def setUpClass(cls):
        cases = all_cases()
        workers = min(8, (os.cpu_count() or 2))
        cls.results = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(run_case, c): c for c in cases}
            for fut, c in futs.items():
                try:
                    cls.results[c] = fut.result()
                except Exception as e:  # noqa: BLE001 — reported per case below
                    cls.results[c] = e

    def _assert_case(self, case, res):
        entry, mode, fk, k, phase, red = case
        if isinstance(res, Exception):
            raise AssertionError(f"case construction failed: {res}\n" +
                                 "".join(traceback.format_exception(res)))
        squash = mode == "squash"
        final = fk != "no"
        st = res["repo_state"]
        rec = res["rec"]
        self.assertIsNotNone(rec, "the record must exist after the sequence ran")
        kill_step = res["kill_step"]
        # A resume that lands a PHASE boundary then gives the model its turn
        # begins a new idle record; the landed boundary is carried as
        # `previous` — that is what a between-sessions reader sees.
        landed = rec
        if rec["step"] == 0 and rec.get("previous"):
            landed = rec["previous"]

        # verified_tree: the tree to land already has a green memo — true for
        # the iteration entry once the commit (step 2) ran in session 1.
        verified_tree = entry == "iteration" and kill_step >= 2
        needs_completion_commit = final and not res["completion_landed"]
        exp = expected_step(mode, kill_step, red, verified_tree, needs_completion_commit)
        if not res["resumed"]:
            exp = RESTED

        # (a) the record reaches step 7, or stops honestly.
        self.assertEqual(landed["step"], exp, f"record step {landed['step']} ({landed.get('step_name')}) != expected {exp}\n{res['out']}")
        self.assertEqual(landed.get("step_name"), STEP_NAMES[landed["step"]])
        self.assertEqual(landed.get("phase"), "phase-1")
        self.assertEqual(landed.get("final"), final)
        # (e) monotonic across the kill.
        self.assertGreaterEqual(landed["step"], res["step_at_kill"])
        # (f) every recorded sha is reachable from HEAD or the target.
        for sha, ok in st["reachable"].items():
            self.assertTrue(ok, f"sha_at_step {sha} unreachable")
        # the record itself never shows in git status (hidden transient)
        self.assertFalse(st["boundary_state_visible"])
        # (g) a witness line.
        if res["resumed"]:
            self.assertTrue(any(w in res["out"] for w in WITNESS_LINES), f"no witness line:\n{res['out']}")

        if exp == RESTED:
            # (b) exactly one squash per approved boundary, with the trailer,
            # and never two squashes of one tree.
            if squash:
                self.assertEqual(len(st["trailers"]), 1, f"squashes on main: {st['trailers']}\n{res['out']}")
                self.assertEqual(len(set(st["trailer_trees"])), len(st["trailer_trees"]))
                self.assertEqual(st["tree_main"], st["tree_head"], "the target must carry the branch tree")
            # (c) the tree rests clean; a final boundary rests on the target.
            allowed = {"?? artifacts/session-handoff.json"} if not final else set()
            dirt = [ln for ln in st["porcelain"] if ln not in allowed]
            self.assertEqual(dirt, [], f"tree not clean: {st['porcelain']}\n{res['out']}")
            if final:
                self.assertEqual(st["head_branch"], "main")
                self.assertFalse(st["handoff_on_disk"], "a consumed baton survived a final rest")
                self.assertTrue(st["completion_on_disk"])
            if final or not res["resumed"]:
                # step 7 disarms the dead-man baton: nothing is in flight. (A
                # resumed phase boundary then gives the model a turn, whose
                # own provisional baton is the EXIT trap's business.)
                self.assertFalse(st["interrupted_on_disk"], "a provisional baton survived a rest")
                if res["resumed"]:
                    self.assertEqual(res["rc"], 0, res["out"])
                    self.assertIn("Run finished successfully.", res["out"])
                    self.assertEqual(res["calls"], 0, "a final boundary lands with zero model turns")
            else:
                if res["resumed"]:
                    self.assertEqual(res["calls"], 1, "a phase boundary lands, then the model gets its turn")
            # (d) no verdict artifact lands twice.
            self.assertEqual(len(st["approval_commits"]), 1, f"approval landed in {st['approval_commits']}")
            if final:
                self.assertEqual(len(st["completion_commits"]), 1, f"completion landed in {st['completion_commits']}")
            # v0.14.4's rule is completion-scoped: a COMPLETION commit never
            # carries a baton; a phase commit may (the wrap-up doctrine
            # commits its own baton for the next session to read and delete).
            if final:
                self.assertFalse(st["baton_tracked"], "a completion commit carried a baton")
            self.assertFalse(st["verify_failed"])
            # (h) the memo: a green resume never re-runs a tier this tree passed.
            if res["resumed"] and not red:
                self.assertEqual(res["verify_runs"], expected_verify_runs_green(entry, mode, kill_step, needs_completion_commit),
                                 f"verify ran {res['verify_runs']}x\n{res['out']}")
        else:
            # Red at the resume: the refusing gate left its artifact, nothing
            # is silent, and nothing that needed the gate moved.
            self.assertTrue(st["verify_failed"] or "squash deferred" in res["out"], res["out"])
            self.assertIn("boundary-state: stopped at step", res["out"])
            if exp == 1:
                self.assertTrue(st["approval_never_landed"], "the approval must stay on disk, uncommitted")
                if squash:
                    self.assertEqual(st["trailers"], [], "nothing reached the target")
            if exp == 3:
                self.assertEqual(st["trailers"], [], "an unverified tree must not be squashed")
                self.assertFalse(st["approval_never_landed"])

    def test_every_entry_point_and_kill_point(self):
        self.assertTrue(self.results, "no cases ran")
        for case in all_cases():
            with self.subTest(case=case_name(case)):
                self._assert_case(case, self.results[case])

    def test_the_matrix_is_the_full_product(self):
        cases = all_cases()
        entries = {c[0] for c in cases}
        self.assertEqual(entries, {"iteration", "stranded-fresh", "catchup", "watchdog", "wrapup", "pacing"})
        it = [c for c in cases if c[0] == "iteration"]
        self.assertEqual(len(it), 2 * 3 * 2 * RESTED * 2)
        self.assertEqual({(c[3], c[4]) for c in it}, {(k, p) for k in range(1, 8) for p in ("pre", "post")})
        self.assertEqual({c[1] for c in cases}, {"squash", "plain"})
        self.assertEqual({c[2] for c in cases}, {"no", "flag", "both"})
        self.assertEqual({c[5] for c in cases}, {True, False})


# ---------------------------------------------------------------------------
# The primitives, extracted from the shipped script
# ---------------------------------------------------------------------------

STUBS = '''
squash_mode() { [[ -n "${SQUASH_TARGET:-}" ]]; }
current_branch() { git symbolic-ref -q --short HEAD 2>/dev/null || echo HEAD; }
squash_pending() { return 1; }
artifact_written_this_iteration() { return 1; }
'''
# v0.14.6: artifact_never_landed is the SHIPPED definition, never a stub — a
# stub here (and a `command -v`-skipped block in test_deadline_watchdog.py)
# masked the fork-visibility defect of run 682 from all 550 tests.
STUBS += _extract_block(r"^artifact_never_landed\(\) \{", r"^\}") + "\n}\n"


class Primitives(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-boundary-prim-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.artifacts = self.tmp / "artifacts"
        self.artifacts.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.tmp, check=True)
        (self.tmp / "f").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.tmp, check=True)

    def bash(self, body, env=None):
        prelude = [
            "set -uo pipefail",
            f'cd "{self.tmp}"', f'ROOT_DIR="{self.tmp}"', f'ARTIFACTS_DIR="{self.artifacts}"',
            'SQUASH_TARGET="${SQUASH_TARGET:-}"', 'ITERATION_MODE=standard',
            _transient_array(), STUBS, BOUNDARY_BLOCK,
        ]
        full = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
        full.pop("PHASEKIT_BOUNDARY_KILL_PROBE", None)
        full.update(env or {})
        return subprocess.run(["bash", "-c", "\n".join(prelude) + "\n" + body],
                              capture_output=True, text=True, timeout=60, env=full)

    def record(self):
        return json.loads((self.artifacts / "boundary-state.json").read_text())

    def test_begin_writes_an_idle_record_and_keeps_the_memo(self):
        r = self.bash('boundary_begin 3; verify_memo_record t1 fast lbl cmd; boundary_begin 4')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual((rec["step"], rec["step_name"], rec["pass"]), (0, "idle", 4))
        self.assertEqual(rec["verify_memo"]["tree_sha"], "t1")
        self.assertEqual(rec["sha_at_step"], {})

    # v0.14.8 (orchestrator #690, found by the landing_state() audit): the
    # record's top-level `iteration` was the MAX_ITERATIONS pass counter, so
    # every live record in the fleet read `iteration: 1` and a consumer that
    # read it as the supervising iteration was right only by accident. The
    # pass counter is now `pass`; `iteration` is the supervisor's label from
    # artifacts/iteration-mode.json, verbatim, else null; schema 1 -> 2.
    # Red on v0.14.7: KeyError 'pass', schema 1, iteration == the pass.
    def _marker(self, payload):
        (self.artifacts / "iteration-mode.json").write_text(payload)

    def test_a_supervised_record_names_the_supervising_iteration_not_the_pass(self):
        self._marker(json.dumps({"mode": "standard", "iteration": 129, "grade": "standard"}) + "\n")
        r = self.bash('boundary_begin 1')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual((rec["schema"], rec["pass"], rec["iteration"]), (2, 1, 129))
        r = self.bash('boundary_begin 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual((rec["schema"], rec["pass"], rec["iteration"]), (2, 2, 129))

    def test_a_standalone_record_carries_iteration_null(self):
        self.assertFalse((self.artifacts / "iteration-mode.json").exists())
        r = self.bash('boundary_begin 1')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertIn("iteration", rec)
        self.assertEqual((rec["schema"], rec["pass"], rec["iteration"]), (2, 1, None))

    def test_the_label_is_carried_verbatim_never_normalised_or_derived(self):
        # a string label rides as the string; a marker without the key, an
        # unparseable marker, a torn (empty) marker, a non-object marker and
        # a non-label value (object, array, boolean — review MAJOR 2: a
        # consumer that requires an int must never see a corrupt record) all
        # read as null — and none of them derails the write, under the
        # loop's own `set -e` (review MINOR 5): the record still begins.
        for payload, want in ((json.dumps({"iteration": "iteration-130"}), "iteration-130"),
                              (json.dumps({"iteration": 0}), 0),
                              (json.dumps({"mode": "light"}), None),
                              (json.dumps({"iteration": {"n": 1}}), None),
                              (json.dumps({"iteration": [1]}), None),
                              (json.dumps({"iteration": True}), None),
                              (json.dumps({"iteration": None}), None),
                              ("{not json", None),
                              ("", None),
                              ("[1, 2]", None)):
            with self.subTest(payload=payload):
                self._marker(payload)
                r = self.bash('set -e; boundary_begin 3; echo "rc-after=$?"')
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("rc-after=0", r.stdout)
                rec = self.record()
                self.assertEqual((rec["schema"], rec["pass"], rec["step_name"]), (2, 3, "idle"))
                self.assertEqual(rec["iteration"], want)
                # the branch name is never the source: the scratch repo's branch
                # carries no iter/<N> and the label still came from the marker
                self.assertNotIn("iter/", rec["branch"])

    def test_a_schema_1_record_is_archived_as_it_was_under_a_schema_2_record(self):
        # Review MAJOR 1 (the rollout shape): a v0.14.7 session rested with a
        # schema-1 record; the first v0.14.8 pass archives it verbatim — its
        # own `schema: 1`, its `iteration` still the old pass counter, no
        # `pass` — and it stays there across idle passes. A consumer branches
        # on the schema of the block it reads.
        (self.artifacts / "boundary-state.json").write_text(json.dumps({
            "schema": 1, "iteration": 1, "branch": "iter/57-x", "step": 7, "step_name": "rested",
            "phase": "phase-9", "final": False, "sha_at_step": {"7": "abc"}}))
        self._marker(json.dumps({"iteration": 129}))
        r = self.bash('boundary_begin 1; boundary_begin 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual((rec["schema"], rec["pass"], rec["iteration"]), (2, 2, 129))
        prev = rec["previous"]
        self.assertEqual((prev["schema"], prev["iteration"], prev["step"], prev["branch"]), (1, 1, 7, "iter/57-x"))
        self.assertNotIn("pass", prev)

    def test_previous_carries_both_pass_and_iteration(self):
        self._marker(json.dumps({"iteration": 129}) + "\n")
        r = self.bash('boundary_begin 1; boundary_advance 2 abc; boundary_begin 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record()
        self.assertEqual((rec["pass"], rec["iteration"], rec["step"]), (2, 129, 0))
        self.assertEqual((rec["previous"]["pass"], rec["previous"]["iteration"], rec["previous"]["step"]),
                         (1, 129, 2))
        self.assertNotIn("previous", rec["previous"])

    def test_advance_is_monotonic_within_a_boundary(self):
        r = self.bash('boundary_begin 1; boundary_advance 3 abc; boundary_advance 2 def; boundary_step')
        self.assertEqual(r.stdout.strip().splitlines()[-1], "3", r.stderr)
        self.assertEqual(self.record()["sha_at_step"], {"3": "abc"})

    def test_mark_killed_records_the_step_without_advancing(self):
        self.bash('boundary_begin 1; boundary_advance 2; boundary_mark_killed kill')
        rec = self.record()
        self.assertEqual((rec["step"], rec["killed_after"], rec["killed_mode"]), (2, 2, "kill"))

    def test_the_memo_covers_same_tree_and_a_full_tier_covers_fast(self):
        script = '''
boundary_begin 1
verify_memo_record T full scripts/phasekit-verify.sh cmd
verify_memo_hit T fast scripts/phasekit-verify.sh cmd; echo "fast-under-full=$?"
verify_memo_hit T full scripts/phasekit-verify.sh cmd; echo "full-under-full=$?"
verify_memo_hit U full scripts/phasekit-verify.sh cmd; echo "other-tree=$?"
verify_memo_hit T full PHASEKIT_VERIFY_CMD cmd; echo "other-label=$?"
verify_memo_hit T full scripts/phasekit-verify.sh other; echo "other-cmd=$?"
verify_memo_record T fast scripts/phasekit-verify.sh cmd
verify_memo_hit T full scripts/phasekit-verify.sh cmd; echo "full-under-fast=$?"
'''
        r = self.bash(script)
        self.assertEqual(r.stdout.split(), ["fast-under-full=0", "full-under-full=0", "other-tree=1",
                                            "other-label=1", "other-cmd=1", "full-under-fast=1"], r.stderr)

    def test_the_memo_is_only_recorded_for_an_exact_tree(self):
        (self.tmp / "f").write_text("changed\n")   # unstaged tracked change
        r = self.bash('verify_memo_exact_tree; echo "exact=$?"')
        self.assertIn("exact=1", r.stdout)
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        r = self.bash('verify_memo_exact_tree; echo "exact=$?"')
        self.assertIn("exact=0", r.stdout)
        (self.tmp / "new-untracked").write_text("n\n")
        r = self.bash('verify_memo_exact_tree; echo "exact=$?"')
        self.assertIn("exact=1", r.stdout)

    def _deferrals(self, entries, field=True):
        art = self.artifacts / "phase-approval.json"
        payload = {"phase": "p", "suggested_commit_message": "m"}
        if field:
            payload["deferrals"] = entries
        art.write_text(json.dumps(payload))
        r = self.bash(f'normalize_deferral_keys "{art}"; echo "rc=$?"')
        return r, json.loads(art.read_text())

    def test_deferral_keys_explicit_ac_and_slug_never_a_hash(self):
        r, out = self._deferrals([
            {"item": "AC#3 integer math", "reason": "r", "suggested_task": "t"},
            {"item": "  Polish the lobby   animation timing curve, then more", "reason": "r", "suggested_task": "t"},
            {"item": "AC#9 explicit wins", "key": " my-key ", "reason": "r", "suggested_task": "t"},
        ])
        self.assertIn("rc=0", r.stdout, r.stderr)
        keys = [d["key"] for d in out["deferrals"]]
        self.assertEqual(keys, ["AC#3", "polish-the-lobby-animation-timing-curve", "my-key"])
        self.assertEqual([d.get("key_derived") for d in out["deferrals"]], ["ac", "slug", None])
        self.assertNotIn("item-", " ".join(keys))
        self.assertIn("derived key AC#3 (ac)", r.stdout)

    def test_deferral_without_key_or_item_is_left_keyless_with_a_warn(self):
        """Review finding 5 (v0.14.5): a refusal here was BLIND — nothing on
        disk told the model why its commit kept failing. The gate now warns
        and leaves the entry; the supervisor's reader drops what it cannot
        name. Never a refusal, never a stuck state."""
        r, out = self._deferrals([{"reason": "no item at all"}])
        self.assertIn("rc=0", r.stdout)
        self.assertIn("WARN", r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        self.assertNotIn("key", out["deferrals"][0])

    def test_deferrals_that_are_not_an_array_of_objects_warn_and_pass(self):
        r, out = self._deferrals(["AC#3 as a bare string"])
        self.assertIn("rc=0", r.stdout)
        self.assertIn("WARN", r.stderr)
        self.assertEqual(out["deferrals"], ["AC#3 as a bare string"])
        r, _ = self._deferrals({"item": "an object, not an array"})
        self.assertIn("rc=0", r.stdout)

    def test_a_null_deferrals_field_counts_as_absent(self):
        r, out = self._deferrals(None)
        self.assertIn("rc=0", r.stdout)
        self.assertNotIn("WARN", r.stderr)
        self.assertIsNone(out["deferrals"])

    def test_the_red_memo_covers_the_same_tree_and_the_green_one_clears_it(self):
        log = self.tmp / "log.txt"; log.write_text("boom\n")
        script = f'''
boundary_begin 1
verify_memo_record_red T scripts/phasekit-verify.sh cmd 1 "{log}"
verify_memo_hit_red T scripts/phasekit-verify.sh cmd; echo "red-same=$?"
verify_memo_hit_red U scripts/phasekit-verify.sh cmd; echo "red-other=$?"
verify_memo_record T fast scripts/phasekit-verify.sh cmd
verify_memo_hit_red T scripts/phasekit-verify.sh cmd; echo "red-after-green=$?"
'''
        r = self.bash(script)
        self.assertEqual(r.stdout.split(), ["red-same=0", "red-other=1", "red-after-green=1"], r.stderr)
        self.assertIsNone(self.record()["verify_red"])

    def test_the_memo_expires(self):
        r = self.bash('boundary_begin 1; verify_memo_record T fast l c; verify_memo_hit T fast l c; echo "fresh=$?"',
                      env={"PHASEKIT_VERIFY_MEMO_TTL_SECONDS": "1"})
        self.assertIn("fresh=0", r.stdout)
        r = self.bash('''boundary_begin 1
jq '.verify_memo = {tree_sha:"T",tier:"fast",label:"l",command:"c",passed_at:"2020-01-01T00:00:00Z"}' "$ARTIFACTS_DIR/boundary-state.json" > "$ARTIFACTS_DIR/x" && mv "$ARTIFACTS_DIR/x" "$ARTIFACTS_DIR/boundary-state.json"
verify_memo_hit T fast l c; echo "old=$?"
PHASEKIT_VERIFY_MEMO_TTL_SECONDS=0 verify_memo_hit T fast l c; echo "never-expires=$?"''')
        self.assertEqual(r.stdout.split(), ["old=1", "never-expires=0"], r.stderr)

    def test_a_phase_name_with_spaces_survives_the_derivation(self):
        (self.artifacts / "phase-approval.json").write_text(json.dumps({"phase": "Phase 3: build it", "final_phase": True}))
        r = self.bash('read -r final phase <<<"$(_boundary_derive)"; echo "final=$final phase=[$phase]"')
        self.assertIn("final=true phase=[Phase 3: build it]", r.stdout)

    def test_no_deferrals_field_is_left_untouched(self):
        art = self.artifacts / "phase-approval.json"
        art.write_text('{"phase": "p",\n  "summary": "keep my formatting"}\n')
        before = art.read_text()
        r = self.bash(f'normalize_deferral_keys "{art}"; echo "rc=$?"')
        self.assertIn("rc=0", r.stdout)
        self.assertEqual(art.read_text(), before)

    def test_the_kill_probe_is_inert_unless_set(self):
        r = self.bash('_boundary_kill_probe 3 pre; echo alive')
        self.assertEqual(r.stdout.strip(), "alive")
        r = self.bash('_boundary_kill_probe 3 pre; echo alive', env={"PHASEKIT_BOUNDARY_KILL_PROBE": "3:post"})
        self.assertEqual(r.stdout.strip(), "alive")

    def test_synthesized_completion_carries_summary_and_deferrals(self):
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-9", "summary": "last one", "final_phase": True,
            "deferrals": [{"item": "AC#2 x", "key": "AC#2", "reason": "r", "suggested_task": "t"}]}))
        r = self.bash('_boundary_synthesize_completion; echo "rc=$?"')
        self.assertIn("rc=0", r.stdout, r.stderr)
        rec = json.loads((self.artifacts / "project-complete.json").read_text())
        self.assertTrue(rec["done"])
        self.assertIn("phase-9", rec["summary"])
        self.assertIn("last one", rec["summary"])
        self.assertEqual(rec["deferrals"][0]["key"], "AC#2")
        self.assertIn("boundary-state step 3", rec["recorded_by"])
        self.assertIn("phase-9", rec["suggested_commit_message"])
        self.assertNotIn("ts", rec)

    # v0.14.7 (orchestrator #655, iteration 122): the record step 3 leaves must
    # claim the iteration the approval names, else a supervisor reads it as
    # claiming nothing (completed_at null; the consumer's own commit gate red
    # on the commit this step was landing). Red on v0.14.6: KeyError 'iteration'.
    def test_synthesized_completion_carries_the_approvals_iteration_verbatim(self):
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-275", "summary": "shipped", "final_phase": True,
            "iteration": "iteration-122"}))
        r = self.bash('_boundary_synthesize_completion; echo "rc=$?"')
        self.assertIn("rc=0", r.stdout, r.stderr)
        rec = json.loads((self.artifacts / "project-complete.json").read_text())
        self.assertEqual(rec["iteration"], "iteration-122")
        self.assertIn("boundary-state step 3", rec["recorded_by"])

    def test_synthesized_completion_without_an_iteration_carries_null_never_a_missing_key(self):
        (self.artifacts / "phase-approval.json").write_text(json.dumps({
            "phase": "phase-9", "summary": "last one", "final_phase": True}))
        r = self.bash('_boundary_synthesize_completion; echo "rc=$?"')
        self.assertIn("rc=0", r.stdout, r.stderr)
        rec = json.loads((self.artifacts / "project-complete.json").read_text())
        self.assertIn("iteration", rec)
        self.assertIsNone(rec["iteration"])


VERIFY_BAD = """#!/usr/bin/env bash
PHASEKIT_VERIFY_CONFIGURED=1
echo run >> "${STUB_DIR:?}/verify-calls"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/BAD" ]; then exit 1; fi
exit 0
"""


def _approval(phase, msg, final=False):
    return (f"jq -n '{{phase: \"{phase}\", approved: true, summary: \"s\", "
            f"suggested_commit_message: \"{msg}\", final_phase: {'true' if final else 'false'}}}' "
            "> artifacts/phase-approval.json\n")


class Regressions(unittest.TestCase):
    """One functional pin per confirmed review finding (v0.14.5 Stage 5/6),
    each run against the shipped loop end to end."""

    def _repo(self, squash):
        repo = Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        return repo

    def test_a_resumed_project_is_never_re_recorded_complete(self):
        """BLOCKER 1 (+ round-2 F1): after a completion, a new task's session
        deletes the completion record ("deleted until real") and commits that
        deletion; the approval on disk still says final_phase: true. The next
        session must NOT synthesize a completion over the half-done work —
        with the transient record present, and with it ABSENT (a fresh clone,
        a wiped artifacts/): the record is never a gate, so its absence must
        never flip the outcome either."""
        for squash, record_present, retouch in (
                (True, True, None), (False, True, None), (True, False, None), (False, False, None),
                (True, True, "delete-restore"), (False, True, "edit-revert"), (True, False, "mode")):
            with self.subTest(squash=squash, record_present=record_present, retouch=retouch):
                repo = self._repo(squash)
                repo.scenario("echo task1 >> src.txt\n" + _approval("phase-1", "Phase 1 (APPROVED)", final=True))
                r1 = repo.run(env={"MAX_ITERATIONS": "1"})
                self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
                self.assertIn("Run finished successfully.", r1.stdout)
                # task 2: the next intake deletes the completion record "until
                # real" and commits that deletion (v0.14.9: a session that
                # begins on an iteration whose completion still stands is
                # complete and exits before any turn — the intake's deletion
                # is what makes the project resumable); the session then
                # works and checkpoints (phase-update)
                repo.git("rm", "-q", "artifacts/project-complete.json")
                repo.git("commit", "-qm", "intake: completion record deleted until real")
                repo.reset_stub()
                repo.scenario("rm -f artifacts/project-complete.json\necho task2-half >> src.txt\n"
                              "jq -n '{suggested_commit_message: \"wip: task 2 half done\"}' > artifacts/phase-update.json\n")
                r2 = repo.run(env={"MAX_ITERATIONS": "1"})
                self.assertIn("wip: task 2 half done", repo.git("log", "--format=%s"))
                # round-3 finding 1: an operator re-touch of the approval AFTER the
                # resumption with identical content must not hide the deletion.
                ap = "artifacts/phase-approval.json"
                if retouch == "delete-restore":
                    content = (repo.repo / ap).read_text()
                    (repo.repo / ap).unlink(); repo.git("add", "-A"); repo.git("commit", "-qm", "hand: drop approval")
                    (repo.repo / ap).write_text(content); repo.git("add", "-A"); repo.git("commit", "-qm", "hand: restore approval")
                elif retouch == "edit-revert":
                    content = (repo.repo / ap).read_text()
                    (repo.repo / ap).write_text(content + "\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "hand: edit approval")
                    (repo.repo / ap).write_text(content); repo.git("add", "-A"); repo.git("commit", "-qm", "hand: revert approval")
                elif retouch == "mode":
                    os.chmod(repo.repo / ap, 0o755); repo.git("add", "-A"); repo.git("commit", "-qm", "hand: chmod approval")
                if not record_present:
                    repo.artifact("boundary-state.json").unlink()
                # task 2, next session: a stub that writes nothing
                repo.reset_stub(); repo.scenario(ORIENT_SCENARIO)
                r3 = repo.run(env={"MAX_ITERATIONS": "1"})
                out = r3.stdout + r3.stderr
                self.assertNotIn("Run finished successfully.", out)
                self.assertNotIn("recorded artifacts/project-complete.json", out)
                self.assertGreaterEqual(repo.calls(), 1, "the model must get its turn")
                self.assertFalse(repo.tracked("artifacts/project-complete.json", "HEAD"))
                self.assertFalse(repo.artifact("project-complete.json").exists())

    def test_a_fresh_approval_after_a_stale_one_rode_the_completion_still_commits_first(self):
        """MAJOR 3: the rides-completion flag is walk-local. Iteration 1's
        completion is red twice (the second time with a STALE approval that
        rides the sweep); iteration 3 writes a NEW approval + completion,
        green — phase 2 must land under its own message first."""
        repo = self._repo(True)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BAD, executable=True)
        repo.scenario(
            'case "$CALL_N" in\n'
            "  1) touch BAD; echo one >> src.txt; " + _approval("phase-1", "Phase 1 (APPROVED)").rstrip("\n") +
            "; jq -n '{suggested_commit_message: \"Project complete: one\"}' > artifacts/project-complete.json ;;\n"
            "  2) jq -n '{suggested_commit_message: \"Project complete: two\"}' > artifacts/project-complete.json ;;\n"
            "  3) rm -f BAD; echo three >> src.txt; " + _approval("phase-2", "Phase 2 (APPROVED)").rstrip("\n") +
            "; jq -n '{suggested_commit_message: \"Project complete: three\"}' > artifacts/project-complete.json ;;\n"
            "esac\n")
        r = repo.run(env={"MAX_ITERATIONS": "3", "VERIFY_MAX_ATTEMPTS": "5"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        msgs = repo.git("log", "--first-parent", "--format=%s", "iter/1-test")
        self.assertIn("Phase 2 (APPROVED)", msgs, r.stdout + r.stderr)
        # the phase commit sweeps the completion record (v0.12.2: the boundary is NAMED)
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"))
        self.assertEqual((r.stdout + r.stderr).count("Unlanded phase approval detected"), 2,
                         "phase-1 (iteration 1) and phase-2 (iteration 3) each commit under their own message first")

    def test_a_byte_identical_fresh_approval_with_new_work_commits_the_work(self):
        """MAJOR 4: at the in-loop site a fresh artifact is a verdict even
        when byte-identical to HEAD (the v0.6.0 mtime rule) — the work it
        claims must land under its message, not strand to MAX_ITERATIONS."""
        for squash in (True, False):
            with self.subTest(squash=squash):
                repo = self._repo(squash)
                repo.scenario('echo "work $CALL_N" >> src.txt\n' + _approval("phase-1", "Phase 1 (APPROVED)"))
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out = r.stdout + r.stderr
                msgs = repo.git("log", "--first-parent", "--format=%s", repo.branch)
                self.assertEqual(msgs.count("Phase 1 (APPROVED)"), 2, out)
                self.assertEqual([ln for ln in repo.porcelain() if "session-" not in ln], [], out)
                self.assertNotIn("could not be proven", out)
                self.assertEqual(repo.calls(), 2)

    def test_a_known_red_tree_is_not_re_verified_at_loop_start_and_spends_no_attempt(self):
        """MAJOR 6: a red wrap-up strands the approval; the next session's
        recovery must answer the same tree from the red memo — no suite run,
        attempts 0 — and the model's fix (a new tree) runs the gate once."""
        repo = self._repo(True)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BAD, executable=True)
        repo.scenario("touch BAD\necho one >> src.txt\n" + _approval("phase-1", "Phase 1 (APPROVED)") + "touch artifacts/wrapup-requested\n")
        r1 = repo.run(env={"MAX_ITERATIONS": "3"})
        self.assertIn("UNVERIFIED work committed", r1.stdout + r1.stderr)
        # The boundary and the wrap-up each ran the gate: the red memo is
        # honoured only by the loop-start recovery (round-2 F2/F3), never
        # after a model turn.
        self.assertEqual(repo.verify_calls(), 2, r1.stdout + r1.stderr)
        repo.reset_stub()
        repo.scenario("rm -f BAD\n" + ORIENT_SCENARIO)
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r2.stdout + r2.stderr
        self.assertIn("is recorded RED", out)
        self.assertIn("Phase 1 (APPROVED)", repo.git("log", "--format=%s", "main"))
        self.assertEqual(repo.verify_calls(), 1, "only the fixed tree ran the gate")
        self.assertNotIn("Verify FAILED (attempt", out, "no attempt is burned before the model's turn")
        self.assertIn("Verify passed.", out)


    def test_the_breaker_still_trips_when_the_model_never_changes_a_red_tree(self):
        """Round-2 F2/F3: the red memo answers only the loop-start recovery.
        In-loop, a model that re-touches the approval without changing the
        tree runs the gate every time and VERIFY_MAX_ATTEMPTS still bounds
        the session (and a fix made outside the tree is seen)."""
        repo = self._repo(True)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BAD, executable=True)
        repo.scenario("touch BAD\n[ -f src.txt.once ] || { echo one >> src.txt; touch src.txt.once; }\n"
                      + _approval("phase-1", "Phase 1 (APPROVED)"))
        r = repo.run(env={"MAX_ITERATIONS": "8", "VERIFY_MAX_ATTEMPTS": "3"})
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertTrue(repo.artifact("phase-blocked.json").exists())
        self.assertEqual(repo.verify_calls(), 3, out)
        self.assertEqual(repo.calls(), 3, out)
        self.assertNotIn("is recorded RED", out)

    def test_a_final_boundary_stranded_red_is_not_re_verified_at_loop_start(self):
        """Round-2 F4: the synthesized completion is deterministic, so a
        final boundary a red wrap-up stranded matches its red memo at the
        next loop start exactly like a phase boundary does."""
        repo = self._repo(True)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BAD, executable=True)
        repo.scenario("touch BAD\necho one >> src.txt\n" + _approval("phase-1", "Phase 1 (APPROVED)", final=True)
                      + "touch artifacts/wrapup-requested\n")
        r1 = repo.run(env={"MAX_ITERATIONS": "3"})
        self.assertIn("UNVERIFIED work committed", r1.stdout + r1.stderr)
        repo.reset_stub()
        repo.scenario("rm -f BAD\n" + ORIENT_SCENARIO)
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r2.stdout + r2.stderr
        self.assertIn("is recorded RED", out)
        self.assertNotIn("Verify FAILED (attempt", out)
        self.assertEqual(repo.verify_calls(), 1, out)
        self.assertEqual(r2.returncode, 0, out)
        self.assertIn("Run finished successfully.", out)
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"))


class SupervisingIterationLabel(unittest.TestCase):
    """v0.14.8 end to end: the shipped loop, a committed supervisor marker,
    two passes in one session — the record and its `previous` both name the
    supervising iteration, and the pass counter is `pass`. Red on v0.14.7."""

    def _repo(self, marker):
        repo = Repo(squash=True)
        self.addCleanup(repo.cleanup)
        if marker is not None:
            repo.write("artifacts/iteration-mode.json", json.dumps(marker, indent=2) + "\n")
            repo.git("add", "-A"); repo.git("commit", "-qm", "iteration-mode marker (supervisor)")
        return repo

    def test_two_passes_under_one_supervising_iteration(self):
        repo = self._repo({"mode": "standard", "iteration": 129, "grade": "standard"})
        repo.scenario('if [ "$CALL_N" = 1 ]; then echo w >> src.txt\n'
                      + _approval("phase-1", "Phase 1 (APPROVED): first") + 'fi\n')
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        rec = repo.record()
        self.assertIsNotNone(rec, r.stdout + r.stderr)
        self.assertEqual((rec["schema"], rec["pass"], rec["iteration"]), (2, 2, 129), r.stdout + r.stderr)
        self.assertEqual((rec["previous"]["pass"], rec["previous"]["iteration"],
                          rec["previous"]["step"], rec["previous"]["phase"]), (1, 129, RESTED, "phase-1"))
        # the work branch is the supervisor's naming; the label did not come from it
        self.assertEqual(rec["branch"], "iter/1-test")

    def test_a_standalone_run_records_iteration_null(self):
        repo = self._repo(None)
        repo.scenario("echo w >> src.txt\n" + _approval("phase-1", "Phase 1 (APPROVED): only"))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("boundary-state: rested (step 7)", r.stdout, r.stdout + r.stderr)
        rec = repo.record()
        landed = rec["previous"] if rec["step"] == 0 and rec.get("previous") else rec
        self.assertEqual((rec["schema"], landed["pass"], landed["iteration"]), (2, 1, None))


# ---------------------------------------------------------------------------
# v0.14.9 — completion is a terminal state: the no-kill case class
# ---------------------------------------------------------------------------
# The 216-case matrix enumerates kills. The row it cannot contain is the one
# with NO kill: a session lands a final boundary and then keeps going. Two
# live occurrences (xmeo iteration 50 run 714, 2026-09-12; iteration 56 run
# 756, 2026-09-13) shared one shape — the completion commit, the squash and
# the rest all happened, then the verify gate's own re-measurement had left
# tracked files dirty, step 7 could not be proven, and the loop read that as
# "failed verify" and re-entered: a next pass that found no next phase and
# wrote phase-blocked.json; a pacing wrap-up that committed the noise
# straight onto the target. Generated like the 216: every entry point that
# can land a final boundary x both modes x a clean and a noisy gate x every
# path that could run afterwards. Red on v0.14.8 for the noisy gate.

# The live gate (v0.14.9's incident): green, but every run rewrites a tracked
# measurement file. Under v0.14.10 that rewrite is the gate's FOOTPRINT: the
# noisy half of the class below now proves the rule at the final boundary —
# the completion never lands over it, the path is restored and named, and
# the fix rides the existing red-gate path (GateFootprint has the cells).
VERIFY_NOISY = VERIFY_LOGGING.replace(
    "exit 0\n", 'echo "measured $(date +%s%N)" >> "$ROOT/measure.txt"\nexit 0\n')
assert VERIFY_NOISY != VERIFY_LOGGING

# v0.14.10 fixture gates (GateFootprint). The gate script lives IN the tree,
# so a fix is a tree change the memo cannot replay a stale verdict over —
# the production shape (scripts/phasekit-verify.sh is project-owned and
# committed).
VERIFY_WRITES_UNTRACKED = VERIFY_LOGGING.replace(
    "exit 0\n", 'echo "{\\"measured\\": true}" > "$ROOT/evidence.json"\nexit 0\n')
VERIFY_WRITES_IGNORED = VERIFY_LOGGING.replace(
    "exit 0\n", 'mkdir -p "$ROOT/artifacts/logs" && echo "measured $(date +%s%N)" >> "$ROOT/artifacts/logs/measure.txt"\nexit 0\n')
VERIFY_RED_READONLY = VERIFY_LOGGING.replace("exit 0\n", "exit 1\n")
assert VERIFY_WRITES_UNTRACKED != VERIFY_LOGGING and VERIFY_WRITES_IGNORED != VERIFY_LOGGING
assert VERIFY_RED_READONLY != VERIFY_LOGGING
# The convention marker (contracts/interface.json verify-gate-read-only): the
# rule's first clause, verbatim in both human-facing homes and the loop.
GATE_RULE_MARKER = "a verify gate is read-only over tracked files and writes nothing untracked"
# The model's fix, made in its turn: the gate's output moves to the ignored path.
FIX_GATE_SCENARIO = "cat > scripts/phasekit-verify.sh <<'GATE'\n" + VERIFY_WRITES_IGNORED + "GATE\n"

# The run-756 shape of the pass that must never happen: the model finds no
# next phase and declares itself blocked.
BLOCKED_NEXT_PASS = ("jq -n '{blocked: true, reason: \"genuine external input is required: "
                     "a new change request must be filed\"}' > artifacts/phase-blocked.json\n")


def terminal_cases():
    # entry, mode, gate, after, final_kind. "iteration+flag" is the
    # approval-only final (final_phase: true, completion synthesized at
    # step 2/3 — the `land_boundary 1 iteration 1` site); "both" is the
    # project-complete.json site. The recovery entries land at loop start.
    # `wrapup`/`pacing` name the path that would have run afterwards: under
    # the rule the in-pass and recovery exits happen before the loop top,
    # so those cases prove the exit precedes the wrap-up rather than
    # exercising wrapup_commit's own guard (pinned structurally, and by the
    # watchdog's fork-shape test for the last-resort commit).
    cases = []
    for mode in ("squash", "plain"):
        for gate in ("clean", "noisy"):
            for after, fk in (("next-pass", "both"), ("next-pass", "flag"), ("wrapup", "both"), ("pacing", "both")):
                cases.append(("iteration", mode, gate, after, fk))
            for after in ("next-pass", "pacing"):
                cases.append(("stranded-fresh", mode, gate, after, "both"))
                if mode == "squash":
                    # a completion committed by hand in plain mode IS a
                    # resting complete project (same reason the 216 skip it)
                    cases.append(("catchup", mode, gate, after, "both"))
    return cases


def terminal_case_name(case):
    return "/".join(case)


def build_terminal_repo(mode, gate):
    """A Repo whose base commit tracks measure.txt and carries the gate:
    "clean" (the logging gate), "noisy" (VERIFY_NOISY), or a gate script's
    own text."""
    repo = Repo(squash=(mode == "squash"))
    if repo.squash:
        repo.git("checkout", "-q", "main")
    repo.write("measure.txt", "baseline\n")
    if gate == "noisy":
        repo.write("scripts/phasekit-verify.sh", VERIFY_NOISY, executable=True)
    elif gate != "clean":
        repo.write("scripts/phasekit-verify.sh", gate, executable=True)
    repo.git("add", "-A")
    repo.git("commit", "-qm", "base: measurement file + gate")
    repo.base = repo.git("rev-parse", "HEAD")
    if repo.squash:
        repo.git("branch", "-f", "iter/1-test", "main")
        repo.git("checkout", "-q", "iter/1-test")
    return repo


def run_terminal_case(case):
    entry, mode, gate, after, fk = case
    repo = build_terminal_repo(mode, gate)
    try:
        env = {"FINAL_KIND": fk, "MAX_ITERATIONS": "3"}
        if entry == "iteration":
            first = ("touch artifacts/wrapup-requested\n" if after == "wrapup" else "")
            first += APPROVE_SCENARIO
            if after == "pacing":
                # the turn spends 8s of a 12s deadline: a loop top reached
                # afterwards sees ~3s against a 1.2x-average threshold of
                # ~10s and wraps up (review m3: margin for a loaded host)
                first += "sleep 8\n"
            repo.scenario('if [ "$CALL_N" = 1 ]; then\n' + first + "else\n" + BLOCKED_NEXT_PASS + "fi\n")
            if after == "pacing":
                env.update({"PHASEKIT_SESSION_DEADLINE": str(int(time.time()) + 12),
                            "PHASEKIT_PACING_FLOOR_SECONDS": "1",
                            "PHASEKIT_WRAPUP_LEAD_SECONDS": "0",
                            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0"})
        else:
            subprocess.run(["bash", "-c", APPROVE_SCENARIO], cwd=repo.repo, check=True,
                           env={**os.environ, "CALL_N": "1", "FINAL_KIND": "both"})
            if entry == "catchup":
                repo.git("add", "-A")
                repo.git("commit", "-qm", "wip: landed by hand, never squashed")
            repo.scenario(BLOCKED_NEXT_PASS)
            if after == "pacing":
                # remaining (~50s) is under the floor at the very first loop
                # top: a loop that is entered wraps up before any turn
                env.update({"PHASEKIT_SESSION_DEADLINE": str(int(time.time()) + 50),
                            "PHASEKIT_PACING_FLOOR_SECONDS": "100",
                            "PHASEKIT_WRAPUP_LEAD_SECONDS": "0",
                            "PHASEKIT_LASTRESORT_LEAD_SECONDS": "0"})
        r = repo.run(env=env)
        subjects = repo.git("log", "--all", "--format=%s")
        vf = repo.artifact("phase-verify-failed.json")
        return dict(rc=r.returncode, out=r.stdout + r.stderr, calls=repo.calls(),
                    rec=repo.record(), state=snapshot(repo), subjects=subjects,
                    completion_on_main=repo.commits_touching("artifacts/project-complete.json", "main"),
                    # v0.14.10: the measurement file on disk, every commit on ANY
                    # ref that touched it past the base, and the red capture
                    measure_worktree=(repo.repo / "measure.txt").read_text(),
                    noise_commits=[ln for ln in repo.git("log", "--all", "--not", repo.base, "--format=%H",
                                                         "--", "measure.txt").splitlines() if ln],
                    verify_failed_json=json.loads(vf.read_text()) if vf.exists() else None)
    finally:
        repo.cleanup()


class CompletionIsTerminal(unittest.TestCase):
    results = None

    @classmethod
    def setUpClass(cls):
        cases = terminal_cases()
        workers = max(2, min(8, (os.cpu_count() or 4)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            cls.results = dict(zip(cases, pool.map(run_terminal_case, cases)))

    def _assert_footprint_case(self, case, res):
        # v0.14.10: the noisy gate's rewrite is its footprint, and the
        # completion NEVER lands over it — the path is restored byte-for-byte,
        # the gate is red with the path named (its command exited 0), and the
        # existing red-gate paths take it from there: a next pass is the
        # model's turn (the blocked stub → exit 2); a wrap-up or pacing exit
        # preserves the standing work as an UNVERIFIED wip that carries no
        # noise; the catch-up squash is deferred. Never a rest over dirt,
        # never "Run finished", never a commit on any ref with the gate's
        # write in it.
        entry, mode, gate, after, fk = case
        out, st = res["out"], res["state"]
        self.assertIn("the gate WROTE TO THE TREE", out)
        self.assertNotIn("Run finished successfully.", out)
        self.assertNotIn("did not rest", out)
        self.assertEqual(res["measure_worktree"], "baseline\n", out)
        self.assertEqual(res["noise_commits"], [], f"a commit carried the gate's write\n{out}")
        self.assertEqual(res["completion_on_main"], [], out)
        if mode == "squash":
            self.assertEqual(st["trailers"], [], out)
        self.assertFalse(any(ln.endswith("measure.txt") for ln in st["porcelain"]), st["porcelain"])
        vf = res["verify_failed_json"]
        self.assertIsNotNone(vf, out)
        self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
        self.assertEqual(vf["exit_code"], 0, vf)
        self.assertIn(GATE_RULE_MARKER, vf["gate_footprint_rule"])
        self.assertIn("artifacts/logs/", vf["gate_footprint_recipe"])
        rec = res["rec"]
        self.assertFalse(rec.get("final") and rec.get("step", 0) >= 6, f"recorded complete over a red gate: {rec}")
        if entry == "catchup":
            self.assertIn("squash deferred", out)
        if after == "next-pass":
            self.assertEqual(res["rc"], 2, out)
            self.assertTrue(st["blocked"], "the blocked stub's pass ran after the red gate")
        elif entry != "catchup":
            self.assertIn("UNVERIFIED work committed", out)

    def _assert_case(self, case, res):
        entry, mode, gate, after, fk = case
        out = res["out"]
        if gate == "noisy":
            self._assert_footprint_case(case, res)
            return
        # (1) the session exits 0 and says so
        self.assertEqual(res["rc"], 0, out)
        self.assertIn("Run finished successfully.", out)
        # (2) no next pass: the model turned once (the approving turn) or never
        self.assertEqual(res["calls"], 1 if entry == "iteration" else 0, out)
        self.assertNotIn("=== Iteration 2 ===", out)
        self.assertFalse(res["state"]["blocked"], f"phase-blocked.json after a landed completion\n{out}")
        # (3) no commit after the landing: no wrap-up, no last-resort, one completion on the target
        self.assertNotIn("session wrap-up", res["subjects"], out)
        self.assertNotIn("last-resort", res["subjects"], out)
        self.assertEqual(len(res["completion_on_main"]), 1, f"completion commits on main: {res['completion_on_main']}\n{out}")
        if mode == "squash":
            self.assertEqual(len(res["state"]["trailers"]), 1, f"squashes: {res['state']['trailers']}\n{out}")
        # (4) at rest: HEAD on the target, the record complete, no batons
        self.assertEqual(res["state"]["head_branch"], "main")
        rec = res["rec"]
        self.assertTrue(rec["final"], rec)
        self.assertFalse(res["state"]["handoff_on_disk"])
        self.assertFalse(res["state"]["interrupted_on_disk"])
        self.assertTrue(res["state"]["completion_on_disk"])
        self.assertEqual(rec["step"], RESTED, rec)
        self.assertEqual(res["state"]["porcelain"], [], out)
        self.assertNotIn("re-entering loop", out)

    def test_every_landing_entry_and_afterwards_path(self):
        self.assertTrue(self.results, "no cases ran")
        for case in terminal_cases():
            with self.subTest(case=terminal_case_name(case)):
                self._assert_case(case, self.results[case])

    def test_the_class_is_the_full_product(self):
        cases = terminal_cases()
        self.assertEqual(len(cases), 28)
        self.assertEqual({c[0] for c in cases}, {"iteration", "stranded-fresh", "catchup"})
        self.assertEqual({c[1] for c in cases}, {"squash", "plain"})
        self.assertEqual({c[2] for c in cases}, {"clean", "noisy"})
        self.assertEqual({c[3] for c in cases}, {"next-pass", "wrapup", "pacing"})
        self.assertEqual({c[4] for c in cases}, {"both", "flag"})
        self.assertEqual(len([c for c in cases if c[4] == "flag"]), 4)


class TerminalRuleBounds(unittest.TestCase):
    """The rule cannot over-fire, and it reads the record's own identity."""

    def _repo(self, mode, gate):
        repo = build_terminal_repo(mode, gate)
        self.addCleanup(repo.cleanup)
        return repo

    def test_a_phase_boundary_with_a_noisy_gate_is_red_at_the_gate_and_the_loop_continues(self):
        # negative pin for the terminal rule, and v0.14.10's disposition on a
        # NON-final boundary: the footprint makes the phase commit red, the
        # path is restored, and the loop continues to the next turn exactly
        # as any red gate does — never "Run finished", never a rest over dirt
        for mode in ("squash", "plain"):
            with self.subTest(mode=mode):
                repo = self._repo(mode, "noisy")
                repo.scenario(APPROVE_SCENARIO)
                r = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "2"})
                out = r.stdout + r.stderr
                self.assertEqual(repo.calls(), 2, out)
                self.assertIn("=== Iteration 2 ===", out)
                self.assertNotIn("Run finished successfully.", out)
                self.assertFalse(repo.record()["final"])
                self.assertEqual((repo.repo / "measure.txt").read_text(), "baseline\n")
                vf = json.loads(repo.artifact("phase-verify-failed.json").read_text())
                self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
                self.assertEqual(vf["attempts"], 2, "each pass's red spends one attempt")
                self.assertTrue(any(ln.endswith("artifacts/phase-approval.json") for ln in repo.porcelain()),
                                "the approval stays on disk, uncommitted")

    def _seed_complete(self, repo, record, marker=None):
        # a completion-only boundary (approval final_phase: false + a
        # completion record), landed on the target and the branch alike, so
        # no loop-start recovery fires: the loop top's own read decides
        repo.git("checkout", "-q", "main")
        subprocess.run(["bash", "-c", APPROVE_SCENARIO], cwd=repo.repo, check=True,
                       env={**os.environ, "CALL_N": "1", "FINAL_KIND": "no"})
        repo.write("artifacts/project-complete.json",
                   json.dumps({"done": True, "suggested_commit_message": "Iteration complete"}) + "\n")
        if marker is not None:
            repo.write("artifacts/iteration-mode.json", json.dumps(marker) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "Phase 1 + completion, landed by hand")
        if repo.squash:
            repo.git("branch", "-f", "iter/1-test", "main")
            repo.git("checkout", "-q", "iter/1-test")
        repo.write("artifacts/boundary-state.json", json.dumps(record) + "\n")
        repo.scenario(BLOCKED_NEXT_PASS)

    def test_a_recorded_complete_iteration_exits_before_any_turn_on_both_schemas(self):
        base = {"step": 7, "step_name": "rested", "phase": "phase-1", "final": True,
                "sha_at_step": {}, "mode": "squash", "target": "main"}
        for name, record, marker, mode in (
                ("schema-1 by branch", {**base, "schema": 1, "iteration": 1, "branch": "iter/1-test"}, None, "squash"),
                ("schema-1 plain", {**base, "schema": 1, "iteration": 1, "branch": "main", "mode": "plain"}, None, "plain"),
                ("schema-2 by label", {**base, "schema": 2, "pass": 1, "iteration": 129, "branch": "iter/1-test"},
                 {"mode": "standard", "iteration": 129}, "squash"),
                ("schema-2 label as string", {**base, "schema": 2, "pass": 1, "iteration": "129", "branch": "iter/1-test"},
                 {"mode": "standard", "iteration": "129"}, "squash"),
                ("schema-2 unlabelled, by branch", {**base, "schema": 2, "pass": 1, "iteration": None, "branch": "iter/1-test"},
                 None, "squash")):
            with self.subTest(name=name):
                repo = self._repo(mode, "clean")
                self._seed_complete(repo, record, marker)
                r = repo.run(env={"MAX_ITERATIONS": "3"})
                out = r.stdout + r.stderr
                self.assertEqual(r.returncode, 0, out)
                self.assertEqual(repo.calls(), 0, out)
                self.assertIn("already complete", out)
                self.assertIn("Run finished successfully.", out)
                self.assertFalse(repo.artifact("phase-blocked.json").exists())

    def test_another_iterations_record_does_not_terminate_this_one(self):
        # the same completion on disk, but the supervisor's marker names the
        # NEXT iteration: the record is not this session's — the pass runs
        base = {"step": 7, "step_name": "rested", "phase": "phase-1", "final": True,
                "sha_at_step": {}, "mode": "squash", "target": "main", "schema": 2, "pass": 1}
        repo = self._repo("squash", "clean")
        self._seed_complete(repo, {**base, "iteration": 129, "branch": "iter/1-test"},
                            {"mode": "standard", "iteration": 130})
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual(repo.calls(), 1, out)
        self.assertNotIn("already complete", out)

    def test_a_deleted_completion_record_is_a_resumed_project_not_a_complete_one(self):
        # the orchestrator's next-iteration intake: the record deleted "until
        # real" and the deletion committed — the stale complete record on
        # disk must not stop the new iteration
        base = {"step": 7, "step_name": "rested", "phase": "phase-1", "final": True,
                "sha_at_step": {}, "mode": "squash", "target": "main", "schema": 2, "pass": 1}
        repo = self._repo("squash", "clean")
        self._seed_complete(repo, {**base, "iteration": 129, "branch": "iter/1-test"},
                            {"mode": "standard", "iteration": 129})
        repo.git("rm", "-q", "artifacts/project-complete.json")
        repo.git("commit", "-qm", "intake: completion record deleted until real")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual(repo.calls(), 1, out)
        self.assertNotIn("already complete", out)


# ---------------------------------------------------------------------------
# v0.14.10: the verify gate is read-only over the tree. The seven cells of
# the kickoff (§3), in-container = the acceptance: real sessions of the
# shipped loop against fixture gates that write where a gate must not.
# ---------------------------------------------------------------------------

class GateFootprint(unittest.TestCase):
    def _repo(self, mode, gate):
        repo = build_terminal_repo(mode, gate)
        self.addCleanup(repo.cleanup)
        return repo

    @staticmethod
    def _vf(repo):
        p = repo.artifact("phase-verify-failed.json")
        return json.loads(p.read_text()) if p.exists() else None

    @staticmethod
    def _fix_gate(repo):
        # The session's fix, IN the tree: gate output under an ignored path.
        repo.write("scripts/phasekit-verify.sh", VERIFY_WRITES_IGNORED, executable=True)

    @staticmethod
    def _dirt(repo):
        # the tree's dirt minus the phase boundary's own provisional baton
        return [ln for ln in repo.porcelain() if "session-handoff.json" not in ln]

    def test_cell1_a_tracked_rewrite_is_restored_red_and_named_then_the_fixed_gate_lands(self):
        for mode in ("squash", "plain"):
            with self.subTest(mode=mode):
                repo = self._repo(mode, "noisy")
                repo.scenario(APPROVE_SCENARIO)
                r1 = repo.run(env={"FINAL_KIND": "both", "MAX_ITERATIONS": "1"})
                out1 = r1.stdout + r1.stderr
                # restored byte-for-byte and out of git status; the gate red
                # although its command exited 0; the paths, rule and recipe
                # in the artifact; one attempt spent
                self.assertEqual((repo.repo / "measure.txt").read_text(), "baseline\n", out1)
                self.assertFalse(any(ln.endswith("measure.txt") for ln in repo.porcelain()), repo.porcelain())
                vf = self._vf(repo)
                self.assertIsNotNone(vf, out1)
                self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
                self.assertEqual(vf["exit_code"], 0, vf)
                self.assertEqual(vf["attempts"], 1, vf)
                self.assertTrue(vf["verify_failed"])
                self.assertIn(GATE_RULE_MARKER, vf["gate_footprint_rule"])
                self.assertIn("artifacts/logs/", vf["gate_footprint_recipe"])
                self.assertIn("the gate WROTE TO THE TREE", out1)
                self.assertIn("measure.txt", out1)
                self.assertTrue(any(ln.endswith("artifacts/phase-approval.json") for ln in repo.porcelain()),
                                "the approval stays on disk, uncommitted")
                self.assertEqual(repo.commits_touching("artifacts/project-complete.json", "main"), [])
                self.assertEqual(repo.verify_calls(), 1, out1)
                # the red memo carries the footprint: a replay is the same red
                self.assertEqual(((repo.record() or {}).get("verify_red") or {}).get("gate_footprint"),
                                 ["measure.txt"], repo.record())
                # the next session: the gate fixed in the tree (its output
                # under artifacts/logs/, ignored) → green, the completion
                # lands with zero turns, the tree rests clean
                self._fix_gate(repo)
                repo.reset_stub()
                repo.scenario(ORIENT_SCENARIO)
                r2 = repo.run(env={"MAX_ITERATIONS": "1"})
                out2 = r2.stdout + r2.stderr
                self.assertEqual(r2.returncode, 0, out2)
                self.assertIn("Run finished successfully.", out2)
                self.assertEqual(repo.calls(), 0, "the recovery lands the completion before any turn")
                self.assertEqual(repo.porcelain(), [], out2)
                self.assertEqual(repo.head_branch(), "main")
                self.assertIsNone(self._vf(repo), out2)
                self.assertEqual(len(repo.commits_touching("artifacts/project-complete.json", "main")), 1, out2)
                self.assertEqual(repo.git("show", "main:measure.txt"), "baseline")
                self.assertIn("artifacts/logs", repo.git("show", "main:scripts/phasekit-verify.sh"))
                self.assertTrue(repo.artifact("logs/measure.txt").exists(), "the gate's output went to the ignored path")
                self.assertNotIn("WROTE TO THE TREE", out2)

    def test_cell2_an_untracked_file_is_deleted_red_and_named(self):
        repo = self._repo("squash", VERIFY_WRITES_UNTRACKED)
        repo.scenario(APPROVE_SCENARIO)
        r = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertFalse((repo.repo / "evidence.json").exists(), out)
        vf = self._vf(repo)
        self.assertIsNotNone(vf, out)
        self.assertEqual(vf["gate_footprint"], ["evidence.json"], vf)
        self.assertEqual(vf["exit_code"], 0, vf)
        self.assertIn("evidence.json", out)
        self.assertTrue(any(ln.endswith("artifacts/phase-approval.json") for ln in repo.porcelain()))
        self.assertEqual(repo.trailer_commits("main"), [], "nothing reached the target")

    def test_cell3_a_read_only_gate_has_no_footprint_key_green_or_red(self):
        # green — the matrix's own gate (VERIFY_LOGGING), which the 216 cases
        # above run unchanged; here the direct pin
        repo = self._repo("plain", "clean")
        repo.scenario(APPROVE_SCENARIO)
        r = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("Verify passed.", out)
        self.assertNotIn("WROTE TO THE TREE", out)
        self.assertIsNone(self._vf(repo), out)
        self.assertEqual(len(repo.commits_touching("artifacts/phase-approval.json")), 1, out)
        # red by the command, read-only: an ordinary red, no footprint keys
        repo2 = self._repo("plain", VERIFY_RED_READONLY)
        repo2.scenario(APPROVE_SCENARIO)
        r2 = repo2.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        out2 = r2.stdout + r2.stderr
        vf = self._vf(repo2)
        self.assertIsNotNone(vf, out2)
        self.assertEqual(vf["exit_code"], 1, vf)
        for key in ("gate_footprint", "gate_footprint_rule", "gate_footprint_recipe"):
            self.assertNotIn(key, vf)
        self.assertNotIn("WROTE TO THE TREE", out2)
        # and the matrix gate never writes under the tree (structural)
        self.assertNotIn('> "$ROOT', VERIFY_LOGGING)
        self.assertNotIn('>> "$ROOT', VERIFY_LOGGING)

    def test_cell4_session_dirt_is_never_in_the_footprint(self):
        # the session edits src.txt (APPROVE_SCENARIO) and creates notes.txt;
        # the gate rewrites measure.txt: a footprint of exactly one path, the
        # session's work untouched, staged, and committed once the gate is fixed
        repo = self._repo("squash", "noisy")
        repo.scenario("echo notes > notes.txt\n" + APPROVE_SCENARIO)
        r1 = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        out1 = r1.stdout + r1.stderr
        vf = self._vf(repo)
        self.assertIsNotNone(vf, out1)
        self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
        self.assertEqual((repo.repo / "src.txt").read_text(), "base\nwork by call 1\n")
        self.assertEqual((repo.repo / "notes.txt").read_text(), "notes\n")
        staged = repo.git("diff", "--cached", "--name-only").splitlines()
        self.assertIn("src.txt", staged)
        self.assertIn("notes.txt", staged)
        self.assertNotIn("measure.txt", staged)
        self._fix_gate(repo)
        repo.reset_stub()
        repo.scenario(ORIENT_SCENARIO)
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})
        out2 = r2.stdout + r2.stderr
        self.assertEqual(repo.git("show", "main:src.txt"), "base\nwork by call 1", out2)
        self.assertEqual(repo.git("show", "main:notes.txt"), "notes", out2)
        self.assertEqual(repo.git("show", "main:measure.txt"), "baseline", out2)
        self.assertIsNone(self._vf(repo), out2)
        self.assertEqual(len(repo.trailer_commits("main")), 1, out2)

    def test_cell5_a_path_the_session_dirtied_keeps_its_staged_bytes_and_the_gates_rewrite_is_red(self):
        # Review MAJOR-2 (supersedes the kickoff's "formatter caveat"): the
        # xmeo shape whenever the model ran the gate in its own turn — the
        # re-measured evidence file is the session's (in S0), the gate
        # re-measures it after staging, and v0.14.9 rested dirty. The commit
        # sites stage everything before the gate, so the unstaged change that
        # appears on a session path across the gate is the gate's: restored
        # to the staged bytes (never HEAD — the session's edit stays), red,
        # named; once the gate is fixed the staged bytes are what lands.
        repo = self._repo("plain", "noisy")
        repo.scenario("echo session >> measure.txt\n" + APPROVE_SCENARIO)
        r = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("the gate WROTE TO THE TREE", out)
        vf = self._vf(repo)
        self.assertIsNotNone(vf, out)
        self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
        self.assertEqual((repo.repo / "measure.txt").read_text(), "baseline\nsession\n", "the session's staged bytes, not HEAD's")
        self.assertEqual(repo.git("diff", "--cached", "--name-only").splitlines().count("measure.txt"), 1)
        self.assertEqual([ln.strip() for ln in self._dirt(repo) if ln.endswith("measure.txt")], ["M  measure.txt"], repo.porcelain())
        self.assertEqual(repo.commits_touching("artifacts/phase-approval.json"), [], out)
        # the fix lands the session's line and nothing of the gate's
        self._fix_gate(repo)
        repo.reset_stub()
        repo.scenario(ORIENT_SCENARIO)
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})
        out2 = r2.stdout + r2.stderr
        self.assertEqual(repo.git("show", "HEAD:measure.txt"), "baseline\nsession", out2)
        self.assertEqual(len(repo.commits_touching("artifacts/phase-approval.json")), 1, out2)
        self.assertEqual(self._dirt(repo), [], out2)

    def test_cell6_a_kill_between_the_commands_return_and_the_restore(self):
        # The gate has written, the loop has not yet restored, SIGKILL. The
        # before-snapshot outlives the process in the transient record
        # (gate_pending — review MAJOR-1: without it the next loop start
        # staged the gate's dirt as the iteration's work and, with the gate
        # still noisy, the project rested dirty — the very shape this
        # release removes). ONE outcome, whatever the gate's state at the
        # next start: the footprint is restored and recorded red before any
        # turn; a fixed gate then lands clean, an unfixed one is red again
        # at the live gate. No stale measurement rides any commit.
        for mode in ("squash", "plain"):
            for fixed in (True, False):
                with self.subTest(mode=mode, fixed=fixed):
                    repo = self._repo(mode, "noisy")
                    repo.scenario(APPROVE_SCENARIO)
                    r1 = repo.run(env={"FINAL_KIND": "both", "MAX_ITERATIONS": "1",
                                       "PHASEKIT_BOUNDARY_KILL_PROBE": "gate:footprint"})
                    out1 = r1.stdout + r1.stderr
                    self.assertEqual(r1.returncode, -9, out1)
                    self.assertIn("KILL PROBE gate:footprint", out1)
                    self.assertIn("measured", (repo.repo / "measure.txt").read_text(), "the gate's write is on disk")
                    self.assertIsNone(self._vf(repo))
                    self.assertTrue(any(ln.endswith("artifacts/project-complete.json") for ln in repo.porcelain()))
                    pending = (repo.record() or {}).get("gate_pending") or {}
                    self.assertEqual(pending.get("label"), "scripts/phasekit-verify.sh", repo.record())
                    before = [base64.b64decode(p).decode().split("\t", 1) for p in pending.get("before", [])]
                    self.assertNotIn("measure.txt", [p for _, p in before], "clean before the gate")
                    self.assertIn(["A ", "artifacts/project-complete.json"], before, "the records carry their letters")
                    repo.reset_stub()
                    # the fix arrives the only way it can in production: through
                    # the model's turn (CONTINUE_PROMPT step 2 — fix the gate,
                    # re-write the signal artifacts); the stub that writes
                    # nothing is the unfixed case
                    repo.scenario(FIX_GATE_SCENARIO + APPROVE_SCENARIO if fixed else ORIENT_SCENARIO)
                    r2 = repo.run(env={"FINAL_KIND": "both", "MAX_ITERATIONS": "1"})
                    out2 = r2.stdout + r2.stderr
                    # settled first — before the recovery, before any turn — and
                    # without spending an attempt; the recovery's live re-run of
                    # the still-noisy gate is red again (attempt 1)
                    # (the order — settle before the recovery — is pinned in the
                    # source by StructuralPins; stdout and stderr interleave here)
                    self.assertIn("settling that gate's footprint before anything else runs", r2.stderr)
                    self.assertIn("no attempt spent (attempts 0/", out2)
                    self.assertIn("Verify FAILED (attempt 1/", out2)
                    # unfixed: the landing retried after the empty turn is red a third time (attempt 2)
                    self.assertEqual(out2.count("the gate WROTE TO THE TREE"), 2 if fixed else 3, out2)
                    self.assertNotIn("gate_pending", json.dumps(repo.record() or {}), "cleared once settled")
                    self.assertEqual(repo.git("log", "--all", "--not", repo.base, "--format=%H", "--", "measure.txt"),
                                     "", "no commit on any ref carries the gate's write")
                    self.assertNotIn("did not rest", out2)
                    self.assertNotIn("re-entering loop", out2)
                    self.assertEqual(repo.calls(), 1, out2)
                    if fixed:
                        # the turn fixed the gate: the landing after it is green and clean
                        self.assertEqual(r2.returncode, 0, out2)
                        self.assertIn("Run finished successfully.", out2)
                        self.assertEqual(repo.porcelain(), [], out2)
                        self.assertEqual(repo.head_branch(), "main")
                        self.assertIsNone(self._vf(repo), out2)
                        self.assertEqual(len(repo.commits_touching("artifacts/project-complete.json", "main")), 1, out2)
                        self.assertEqual(repo.git("show", "main:measure.txt"), "baseline")
                        self.assertIn("artifacts/logs", repo.git("show", "main:scripts/phasekit-verify.sh"))
                    else:
                        # restored, red, named; the completion never landed — never a rest over dirt
                        self.assertEqual((repo.repo / "measure.txt").read_text(), "baseline\n")
                        self.assertFalse(any(ln.endswith("measure.txt") for ln in repo.porcelain()), repo.porcelain())
                        vf = self._vf(repo)
                        self.assertIsNotNone(vf, out2)
                        self.assertEqual(vf["gate_footprint"], ["measure.txt"], vf)
                        self.assertEqual(vf["attempts"], 2, vf)
                        self.assertNotIn("Run finished successfully.", out2)
                        self.assertEqual(repo.commits_touching("artifacts/project-complete.json", "main"), [])

    def test_the_pending_snapshot_is_recorded_before_the_command_and_cleared_after_the_restore(self):
        # a clean run leaves no gate_pending behind, and a kill DURING the
        # command (not only after its return) is settled the same way
        repo = self._repo("plain", "clean")
        repo.scenario(APPROVE_SCENARIO)
        r = repo.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        self.assertNotIn("gate_pending", json.dumps(repo.record() or {}), r.stdout + r.stderr)
        # a gate that kills the loop from inside the command, once (the next
        # session's re-run of the same gate is noisy but survives)
        killer = VERIFY_NOISY.replace(
            "exit 0\n", 'if [ -f "$STUB_DIR/kill-once" ]; then rm -f "$STUB_DIR/kill-once"; kill -KILL "$PPID"; sleep 5; fi\nexit 0\n')
        repo2 = self._repo("plain", killer)
        (repo2.stub / "kill-once").write_text("")
        repo2.scenario(APPROVE_SCENARIO)
        r1 = repo2.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        self.assertEqual(r1.returncode, -9, r1.stdout + r1.stderr)
        self.assertIn("measured", (repo2.repo / "measure.txt").read_text())
        self.assertIn("gate_pending", json.dumps(repo2.record() or {}))
        repo2.reset_stub()
        repo2.scenario(ORIENT_SCENARIO)
        r2 = repo2.run(env={"MAX_ITERATIONS": "1"})
        out2 = r2.stdout + r2.stderr
        self.assertIn("settling that gate's footprint", out2)
        self.assertEqual((repo2.repo / "measure.txt").read_text(), "baseline\n", out2)
        self.assertEqual(self._vf(repo2)["gate_footprint"], ["measure.txt"], out2)
        # and a session path the killed gate rewrote is restored to the
        # session's STAGED bytes at the settle (the index survived the kill;
        # the record kept the letters) — the incident via the kill path
        repo3 = self._repo("plain", killer)
        (repo3.stub / "kill-once").write_text("")
        repo3.scenario("echo session >> measure.txt\n" + APPROVE_SCENARIO)
        r1 = repo3.run(env={"FINAL_KIND": "no", "MAX_ITERATIONS": "1"})
        self.assertEqual(r1.returncode, -9, r1.stdout + r1.stderr)
        self.assertEqual((repo3.repo / "measure.txt").read_text().count("measured"), 1)
        repo3.reset_stub()
        repo3.scenario(ORIENT_SCENARIO)
        r2 = repo3.run(env={"MAX_ITERATIONS": "1"})
        out2 = r2.stdout + r2.stderr
        self.assertIn("settling that gate's footprint", out2)
        self.assertEqual((repo3.repo / "measure.txt").read_text(), "baseline\nsession\n", out2)
        self.assertEqual(repo3.git("log", "--all", "--not", repo3.base, "--format=%H", "--", "measure.txt"), "",
                         "no commit on any ref carries the gate's write")
        self.assertEqual(self._vf(repo3)["gate_footprint"], ["measure.txt"], out2)


FOOTPRINT_BLOCK = _extract_block(r"^# --- verify-gate footprint \(v0\.14\.10\)", r"^run_contracts_gate\(\) \{")


class FootprintPrimitives(unittest.TestCase):
    """The helpers, extracted from the shipped script, on the shapes the
    review is pointed at: NUL-safe paths (a space, a newline), both ends of
    a rename the session staged, a deletion and a staged add by the gate, an
    untracked directory — and the session's dirt untouched throughout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-footprint-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "artifacts").mkdir()
        (self.tmp / "sub").mkdir()
        for rel, content in (("sp ace.txt", "a\n"), ("old.txt", "b\n"), ("sub/keep", "c\n"),
                             ("measure.txt", "m\n"), ("new\nline.txt", "n\n")):
            (self.tmp / rel).write_text(content)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)

    def bash(self, body):
        prelude = ["set -uo pipefail", f'cd "{self.tmp}"', f'ROOT_DIR="{self.tmp}"',
                   f'ARTIFACTS_DIR="{self.tmp}/artifacts"', _transient_array(), FOOTPRINT_BLOCK]
        return subprocess.run(["bash", "-c", "\n".join(prelude) + "\n" + body], capture_output=True,
                              text=True, timeout=60, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})

    def porcelain(self):
        r = subprocess.run(["git", "-C", str(self.tmp), "status", "--porcelain", "-z", "-uall"],
                           capture_output=True, text=True, check=True)
        return [e for e in r.stdout.split("\0") if e]

    def test_the_footprint_is_after_minus_before_by_path_and_the_restore_is_exact(self):
        r = self.bash(r'''
# the session's dirt (S0): an edit, a staged rename
printf 'x\n' >> 'sp ace.txt'; git mv old.txt new.txt; git add -A
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b" || { echo BEFORE-FAILED; exit 9; }
# the gate: rewrites two tracked files, deletes one, stages a new one, creates untracked files
echo t >> measure.txt; echo t >> $'new\nline.txt'; rm sub/keep; echo n > gateadded; git add gateadded
echo e > evidence.json; mkdir udir; echo u > udir/x
gate_status_snapshot "$a" || { echo AFTER-FAILED; exit 9; }
gate_footprint_diff "$b" "$a" "$f"
echo "FOOTPRINT:$(tr '\0\n' '|~' < "$f")"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
echo "EMPTY:$(: > "$f"; gate_footprint_json "$f")"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "", r.stderr)
        fp = [m for m in r.stdout.splitlines() if m.startswith("FOOTPRINT:")][0]
        self.assertEqual(sorted(fp[len("FOOTPRINT:"):].strip("|").split("|")),
                         sorted(["A \tgateadded", " M\tmeasure.txt", " M\tnew~line.txt", " D\tsub/keep",
                                 "??\tevidence.json", "??\tudir/x"]))
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(sorted(json.loads(js)), sorted(["gateadded", "measure.txt", "new\nline.txt",
                                                           "sub/keep", "evidence.json", "udir/x"]))
        self.assertIn("EMPTY:[]", r.stdout)
        # after the restore only the session's dirt remains — rename (both ends) and the edit
        self.assertEqual(sorted(self.porcelain()),
                         sorted(["R  new.txt", "old.txt", "M  sp ace.txt"]))
        self.assertEqual((self.tmp / "measure.txt").read_text(), "m\n")
        self.assertEqual((self.tmp / "new\nline.txt").read_text(), "n\n")
        self.assertEqual((self.tmp / "sub" / "keep").read_text(), "c\n")
        self.assertEqual((self.tmp / "sp ace.txt").read_text(), "a\nx\n")
        for gone in ("gateadded", "evidence.json", "udir/x"):
            self.assertFalse((self.tmp / gone).exists(), gone)

    def test_a_rename_or_deletion_the_gate_staged_comes_back_whole(self):
        r = self.bash(r'''
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"
git mv old.txt moved.txt; git rm -q sub/keep
gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(sorted(json.loads(js)), ["moved.txt", "old.txt", "sub/keep"])
        self.assertEqual(self.porcelain(), [])
        self.assertEqual((self.tmp / "old.txt").read_text(), "b\n")
        self.assertEqual((self.tmp / "sub" / "keep").read_text(), "c\n")
        self.assertFalse((self.tmp / "moved.txt").exists())

    def test_glob_and_magic_names_restore_only_themselves(self):
        # review MAJOR-3: pathspecs are literal — `a*b.txt` must not revert
        # the session's `axb.txt`, `:colon.txt` must not be read as magic
        for rel in ("a*b.txt", "axb.txt", ":colon.txt", "colon.txt", "[id].txt", "i.txt"):
            (self.tmp / rel).write_text("v1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-qm", "names"], cwd=self.tmp, check=True)
        r = self.bash(r'''
printf 'SESSION\n' >> axb.txt; printf 'SESSION\n' >> colon.txt; printf 'SESSION\n' >> i.txt; git add -A
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"
printf 'gate\n' >> 'a*b.txt'; printf 'gate\n' >> ':colon.txt'; printf 'gate\n' >> '[id].txt'
gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(sorted(json.loads(js)), sorted(["a*b.txt", ":colon.txt", "[id].txt"]))
        for rel in ("a*b.txt", ":colon.txt", "[id].txt"):
            self.assertEqual((self.tmp / rel).read_text(), "v1\n", rel)
        for rel in ("axb.txt", "colon.txt", "i.txt"):
            self.assertEqual((self.tmp / rel).read_text(), "v1\nSESSION\n", rel)
        self.assertEqual(sorted(self.porcelain()), sorted(["M  axb.txt", "M  colon.txt", "M  i.txt"]))

    def test_a_session_path_the_gate_rewrites_is_restored_to_the_index_not_head(self):
        # review MAJOR-2: staged (session) bytes kept, the gate's unstaged
        # rewrite gone; a leading-dash name rides jq --args (MINOR-1)
        r = self.bash(r'''
printf 'SESSION\n' >> measure.txt; printf 'd\n' > -dash.txt; git add -A
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"
printf 'gate\n' >> measure.txt; printf 'gate\n' >> -dash.txt; rm 'sp ace.txt'
gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "FOOTPRINT:$(tr '\0' '|' < "$f")"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        fp = [m for m in r.stdout.splitlines() if m.startswith("FOOTPRINT:")][0]
        self.assertEqual(sorted(fp[len("FOOTPRINT:"):].strip("|").split("|")),
                         sorted(["SM\tmeasure.txt", "SM\t-dash.txt", " D\tsp ace.txt"]))
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(sorted(json.loads(js)), sorted(["measure.txt", "-dash.txt", "sp ace.txt"]))
        self.assertEqual((self.tmp / "measure.txt").read_text(), "m\nSESSION\n")
        self.assertEqual((self.tmp / "-dash.txt").read_text(), "d\n")
        self.assertEqual((self.tmp / "sp ace.txt").read_text(), "a\n")
        self.assertEqual(sorted(self.porcelain()), sorted(["A  -dash.txt", "M  measure.txt"]))

    def test_a_file_the_session_deleted_and_the_gate_recreated_is_deleted_again(self):
        # review NEW-1: before `D `, after `D ` + `??` — the `??` is the gate's
        r = self.bash(r'''
git rm -q old.txt; git add -A
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"
printf 'gate\n' > old.txt
gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(json.loads(js), ["old.txt"])
        self.assertFalse((self.tmp / "old.txt").exists())
        self.assertEqual(self.porcelain(), ["D  old.txt"], "the session's deletion stands")
        # ...but a healed tracked transient has the same `D ` + `??` shape and
        # is the loop's, never the gate's (heal_tracked_transients; the v0.6.6
        # deferred-heal tests)
        (self.tmp / "artifacts" / "phase-verify-failed.json").write_text("{}\n")
        subprocess.run(["git", "add", "-f", "artifacts/phase-verify-failed.json"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-qm", "legacy tracked transient"], cwd=self.tmp, check=True)
        r = self.bash(r'''
git rm -q --cached artifacts/phase-verify-failed.json
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"; gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "SIZE:$(stat -c %s "$f")"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SIZE:0", r.stdout)
        self.assertTrue((self.tmp / "artifacts" / "phase-verify-failed.json").exists())

    def test_a_gate_that_unignores_files_by_rewriting_gitignore_does_not_get_them_deleted(self):
        # review MINOR-5 + MINOR-3: .gitignore restored first, then the
        # exposed files are ignored again and kept; `git rm --cached` yields
        # two records for one path and the file survives
        (self.tmp / ".gitignore").write_text("logs/\n")
        (self.tmp / "logs").mkdir()
        (self.tmp / "logs" / "run.log").write_text("kept\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore"], cwd=self.tmp, check=True)
        r = self.bash(r'''
b=$(mktemp); a=$(mktemp); f=$(mktemp)
gate_status_snapshot "$b"
: > .gitignore; git rm -q --cached measure.txt
gate_status_snapshot "$a"; gate_footprint_diff "$b" "$a" "$f"
echo "JSON:$(gate_footprint_json "$f")"
gate_footprint_restore "$f"
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        js = [m for m in r.stdout.splitlines() if m.startswith("JSON:")][0][len("JSON:"):]
        self.assertEqual(sorted(json.loads(js)), sorted([".gitignore", "measure.txt", "logs/run.log"]))
        self.assertIn("ignored again", r.stderr)
        self.assertEqual((self.tmp / "logs" / "run.log").read_text(), "kept\n")
        self.assertEqual((self.tmp / "measure.txt").read_text(), "m\n")
        self.assertEqual(self.porcelain(), [])

    def test_a_snapshot_that_git_cannot_take_returns_nonzero_and_writes_nothing(self):
        r = self.bash('ROOT_DIR="$ROOT_DIR/does-not-exist"; b=$(mktemp); gate_status_snapshot "$b"; echo "rc=$? size=$(stat -c %s "$b")"')
        self.assertIn("rc=128 size=0", r.stdout, r.stdout + r.stderr)


class StructuralPins(unittest.TestCase):
    def test_the_gate_measures_its_footprint_around_the_command_and_restores_before_the_verdict(self):
        fn = _extract_block(r"^run_verify_gate\(\) \{", r"^\}")
        first = fn.index('gate_status_snapshot "$fp_before"')
        cmd = fn.index('bash "$cmd" >"$log"')
        second = fn.index('gate_status_snapshot "$fp_after"')
        probe = fn.index("_boundary_kill_probe gate footprint")
        restore = fn.index('gate_footprint_restore "$fp_paths"')
        green = fn.index('echo "  Verify passed."')
        self.assertLess(fn.index("verify_memo_hit"), first, "a memo hit runs no command and measures nothing")
        self.assertLess(first, cmd)
        self.assertLess(cmd, second)
        self.assertLess(second, probe)
        self.assertLess(probe, restore)
        self.assertLess(restore, green)
        # the before-snapshot outlives the process from before the command
        # until after the restore (review MAJOR-1), and is settled at loop
        # start before the recovery stages anything
        pend = fn.index('gate_pending_record "$fp_before" "$cmd" "$label"')
        clear = fn.index("gate_pending_clear")
        self.assertLess(first, pend)
        self.assertLess(pend, cmd)
        self.assertLess(restore, clear)
        self.assertLess(clear, green)
        main_flow = SOURCE[SOURCE.index("heal_tracked_transients || true"):]
        self.assertLess(main_flow.index("gate_settle_pending || true"),
                        main_flow.index("# --- Boundary recovery at loop start"))
        settle = _extract_block(r"^gate_settle_pending\(\) \{", r"^\}")
        self.assertIn('record_verify_failure "$cmd" "$label" null "$log" settle "$json"', settle)
        writer = _extract_block(r"^record_verify_failure\(\) \{", r"^\}")
        self.assertIn('[[ "$mode" == "memo" || "$mode" == "settle" ]] && attempts="$prior_attempts"', writer)
        diff = _extract_block(r"^gate_footprint_diff\(\) \{", r"^\}")
        self.assertIn('skip["artifacts/session-handoff.json"]=1', diff)
        self.assertIn('skip["artifacts/$sig"]=1', diff)
        # the loop's own paths are excluded BEFORE the deleted-then-recreated
        # rule (review NEW-4 / the v0.6.6 deferred-heal tests)
        self.assertLess(diff.index('if [[ -n "${skip[$key]:-}" ]]; then continue; fi'),
                        diff.index('== *D* ]]'))
        # review MAJOR-1/-3, MINOR-1/-2: literal pathspecs on every restore;
        # jq --args always behind `--`; the pending record is base64; the
        # watchdog restores a pending footprint before its last-resort commit
        gitfn = _extract_block(r"^_gate_git\(\) \{", r"^\}")
        self.assertIn("git --literal-pathspecs -C", gitfn)
        restore = _extract_block(r"^gate_footprint_restore\(\) \{", r"^\}")
        bare = re.findall(r"(?m)^\s*(?:if )?git [^\n]*", restore)
        self.assertEqual(len(bare), 1, f"every restoring git call goes through _gate_git; only check-ignore is bare: {bare}")
        self.assertIn("check-ignore", bare[0])
        self.assertLess(restore.index("check-ignore"), restore.index('rm -rf --'))
        for helper in ("gate_footprint_json", "gate_pending_record"):
            self.assertIn("--args -- ", _extract_block(rf"^{helper}\(\) \{{", r"^\}"))
        self.assertIn("base64 -w0", _extract_block(r"^gate_pending_record\(\) \{", r"^\}"))
        lastresort = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        # a pending gate ⇒ the last-resort commit takes the index as it stands
        self.assertLess(lastresort.index("_gp_pending=1"), lastresort.index("      git add -A 2>/dev/null"))
        self.assertIn('if [[ "$_gp_pending" -eq 0 ]]; then\n      git add -A', lastresort)
        self.assertNotIn("gate_pending_restore_now", lastresort, "no restore races the running gate")
        pend_rec = _extract_block(r"^gate_pending_record\(\) \{", r"^\}")
        self.assertIn('printf \'%s\' "$rec" | base64 -w0', pend_rec, "the whole record, letters included")
        self.assertIn('record_verify_failure "$cmd" "$label" null "$log" settle "$json"',
                      _extract_block(r"^gate_settle_pending\(\) \{", r"^\}"))
        self.assertLess(settle.index("gate_pending_restore_now"), settle.index("record_verify_failure"))
        self.assertIn("gate_pending_clear", settle)
        self.assertIn('[[ "$verify_status" -eq 0 && -z "$footprint_json" ]]', fn,
                      "green needs BOTH a passing command and an empty footprint")
        self.assertIn('record_verify_failure "$cmd" "$label" "$verify_status" "$log" "" "$footprint_json"', fn)
        self.assertIn('verify_memo_record_red "$memo_tree" "$label" "$cmd" "$verify_status" "$log" "$footprint_json"', fn)
        # the memo replay carries the paths: a red by footprint is never
        # replayed as a plain red (nor, with its command rc 0, as a green)
        self.assertIn("boundary_get '.verify_red.gate_footprint // null'", fn)
        self.assertIn('record_verify_failure "$cmd" "$label" "$memo_code" "$memo_log" memo "$memo_fp"', fn)
        red = _extract_block(r"^verify_memo_record_red\(\) \{", r"^\}")
        self.assertIn("gate_footprint: $footprint", red)
        writer = _extract_block(r"^record_verify_failure\(\) \{", r"^\}")
        for key in ("gate_footprint:", "gate_footprint_rule:", "gate_footprint_recipe:"):
            self.assertIn(key, writer)
        # one snapshot shape for both sides, NUL-safe, every untracked file its own record
        snap = _extract_block(r"^gate_status_snapshot\(\) \{", r"^\}")
        self.assertIn("status --porcelain -z --untracked-files=all", snap)
        self.assertIn("read -r -d ''", snap)

    def test_the_contract_declares_the_footprint_keys_and_the_convention(self):
        m = json.loads(MANIFEST.read_text())
        entry = [a for a in m["artifacts"] if a["name"] == "phase-verify-failed.json"][0]
        for key in ("gate_footprint", "gate_footprint_rule", "gate_footprint_recipe"):
            self.assertIn(key, entry["keys"])
        self.assertIn("gate_footprint", entry["when"])
        conv = [c for c in m["conventions"] if c["name"] == "verify-gate-read-only"]
        self.assertEqual(len(conv), 1, "exactly one verify-gate-read-only convention")
        conv = conv[0]
        self.assertEqual(conv["marker"], GATE_RULE_MARKER)
        self.assertEqual(set(conv["declared_in"]), {"docs/QUALITY_GATES.md", "CONTINUE_PROMPT.txt"})
        self.assertIn("session", conv["consumers"])
        # the marker literal in every home: both docs and the loop's own rule
        for path in ("docs/QUALITY_GATES.md", "CONTINUE_PROMPT.txt"):
            self.assertIn(GATE_RULE_MARKER, (REPO_ROOT / path).read_text(), path)
        self.assertIn(f'GATE_FOOTPRINT_RULE="{GATE_RULE_MARKER}', SOURCE)
        prompt = (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text()
        self.assertIn("`gate_footprint`", prompt)
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        self.assertIn("`gate_footprint`", gates)
        self.assertIn("artifacts/logs/", gates)
        probe = [e for e in m["env"] if e["name"] == "PHASEKIT_BOUNDARY_KILL_PROBE"][0]
        self.assertIn("gate:footprint", probe["semantics"])

    def test_the_wrapup_commit_keys_the_deferrals_it_sweeps(self):
        fn = _extract_block(r"^wrapup_commit\(\) \{", r"^\}")
        self.assertLess(fn.index("normalize_deferral_keys"), fn.index("git add -A"))

    def test_the_red_memo_is_honoured_only_at_loop_start(self):
        fn = _extract_block(r"^run_verify_gate\(\) \{", r"^\}")
        self.assertIn('"${BOUNDARY_WALK_CONTEXT:-}" == "stranded"', fn)
        land = _extract_block(r"^land_boundary\(\) \{", r"^\}")
        self.assertEqual(land.count('BOUNDARY_WALK_CONTEXT=""'), 3, "every return resets the walk context")
        self.assertNotIn("trap ", land)

    def test_the_synthesized_completion_has_no_wall_clock_field(self):
        fn = _extract_block(r"^_boundary_synthesize_completion\(\) \{", r"^\}")
        self.assertNotIn("ts:", fn)
        self.assertNotIn("date ", fn)

    def test_only_land_boundary_advances_the_record(self):
        calls = [m.start() for m in re.finditer(r"^\s+boundary_advance ", SOURCE, re.M)]
        fn_start = SOURCE.index("land_boundary() {")
        fn_end = SOURCE.index("\n}\n", fn_start)
        self.assertTrue(calls, "boundary_advance is never called")
        for pos in calls:
            self.assertTrue(fn_start < pos < fn_end, "boundary_advance called outside land_boundary")

    def test_every_entry_point_calls_land_boundary(self):
        sites = re.findall(r"^\s+land_boundary (\S+) (\w+)", SOURCE, re.M)
        contexts = {ctx for _, ctx in sites}
        self.assertEqual(contexts, {"stranded", "iteration", "completion"})
        self.assertIn('land_boundary "$recover_from" stranded 1', SOURCE)
        self.assertIn("land_boundary 1 iteration 1", SOURCE)
        self.assertIn('land_boundary 1 completion "$approval_fresh"', SOURCE)
        # the loop never calls the old deciders directly at a site any more
        main_flow = SOURCE[SOURCE.index("iteration=1\n"):]
        for old in ("ensure_squashed_or_block", "commit_pending_approval_first", "rest_on_target",
                    "clear_consumed_batons_at_completion", "squash_to_target"):
            self.assertNotRegex(main_flow, rf"^\s+{old}\b", f"{old} is called at a site instead of inside the sequence")

    def test_the_record_is_transient_and_hidden(self):
        for arr in ("TRANSIENT_SIGNALS", "HIDDEN_TRANSIENTS"):
            m = re.search(rf"^{arr}=\((.*?)^\)", SOURCE, re.S | re.M)
            self.assertIn('"boundary-state.json"', m.group(1))
        m = json.loads(MANIFEST.read_text())
        entry = [a for a in m["artifacts"] if a["name"] == "boundary-state.json"][0]
        self.assertTrue(entry["transient_signal"] and entry["hidden"])
        self.assertIn("supervisor", entry["consumers"])
        for key in ("step", "final", "sha_at_step", "verify_memo", "killed_after", "pass", "iteration"):
            self.assertIn(key, entry["keys"])
        # v0.14.8: the marker is declared as the source of `iteration`, read by the loop
        marker = [a for a in m["artifacts"] if a["name"] == "iteration-mode.json"][0]
        self.assertIn("scripts/run-until-done.sh", marker["consumers"])
        self.assertEqual(marker["writers"], ["supervisor"])
        self.assertEqual(marker["keys"], ["iteration"])
        self.assertIn("schema 2", entry["when"])
        probe = [e for e in m["env"] if e["name"] == "PHASEKIT_BOUNDARY_KILL_PROBE"][0]
        self.assertFalse(probe["container_forwarded"])
        approval = [a for a in m["artifacts"] if a["name"] == "phase-approval.json"][0]
        self.assertIn("final_phase", approval["keys"])

    def test_the_watchdog_names_the_step_it_interrupted(self):
        fn = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        self.assertIn('boundary_mark_killed "$mode"', fn)
        self.assertLess(fn.index("boundary_mark_killed"), fn.index("for attempt in 1 2 3 4 5"))

    def test_the_verify_gate_consults_the_memo_after_the_contracts_gate(self):
        fn = _extract_block(r"^run_verify_gate\(\) \{", r"^\}")
        self.assertLess(fn.index("run_contracts_gate"), fn.index("verify_memo_hit"))
        self.assertLess(fn.index("VERIFY_SKIP"), fn.index("verify_memo_hit"))
        self.assertIn("verify_memo_exact_tree", fn)
        self.assertLess(fn.index('echo "  Verify passed."'), fn.index("verify_memo_record"))

    def test_commit_from_artifact_normalizes_deferral_keys_before_staging(self):
        fn = _extract_block(r"^commit_from_artifact\(\) \{", r"^\}")
        self.assertLess(fn.index("normalize_deferral_keys"), fn.index('git add -f "$file"'))
        self.assertLess(fn.index("normalize_deferral_keys"), fn.index("run_verify_gate"))

    def test_the_final_unrecorded_discriminator_requires_no_completion_touch_since_the_approval(self):
        fn = _extract_block(r"^approval_final_unrecorded\(\) \{", r"^\}")
        self.assertIn('git rev-list "$ap_commit..HEAD" -- artifacts/project-complete.json', fn)
        self.assertIn('== "$blob" ]] || continue', fn, "the anchor is the OLDEST commit carrying the current blob")
        self.assertNotIn("|| break", fn)
        self.assertIn("approval_final_unrecorded", SOURCE.split("# --- Boundary recovery at loop start")[1])

    def test_record_writes_are_locked_and_tmp_files_live_under_logs(self):
        fn = _extract_block(r"^_boundary_write\(\) \{", r"^\}")
        self.assertIn('flock -w 5 9', fn)
        self.assertIn('"$ARTIFACTS_DIR/logs/.boundary-state.$BASHPID.tmp"', fn)
        norm = _extract_block(r"^normalize_deferral_keys\(\) \{", r"^\}")
        self.assertIn('"$ARTIFACTS_DIR/logs/.deferrals.$BASHPID.tmp"', norm)
        self.assertNotIn('"$file.tmp"', norm)
        self.assertNotIn("REFUSED", norm)

    def test_commit_from_artifact_normalizes_both_artifacts_but_only_unlanded_ones(self):
        fn = _extract_block(r"^commit_from_artifact\(\) \{", r"^\}")
        self.assertIn("for _art in phase-approval.json project-complete.json; do", fn)
        self.assertIn('if artifact_never_landed "$ARTIFACTS_DIR/$_art"; then normalize_deferral_keys', fn)
        wd = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        self.assertIn("normalize_deferral_keys", wd)

    def test_iteration_start_begins_a_new_boundary_after_cleanup(self):
        i = SOURCE.index("  cleanup_artifacts\n  boundary_begin \"$iteration\"\n  touch \"$ITER_START_MARKER\"")
        self.assertGreater(i, 0)

    def test_the_label_is_read_from_the_marker_only_inside_boundary_begin(self):
        """v0.14.8: one reader, one key, no branch-name parsing anywhere."""
        fn = _extract_block(r"^boundary_begin\(\) \{", r"^\}")
        self.assertIn("schema: 2,", fn)
        self.assertIn("pass: ($pass | tonumber),", fn)
        self.assertIn("iteration: $iteration,", fn)
        self.assertIn('--argjson iteration "$(supervising_iteration_json)"', fn)
        reader = _extract_block(r"^supervising_iteration_json\(\) \{", r"^\}")
        self.assertIn('"$ARTIFACTS_DIR/iteration-mode.json"', reader)
        self.assertEqual(SOURCE.count("iteration-mode.json\""), 1, "exactly one code read of the marker")
        # (no branch-name regex here — review MINOR 4: the load-bearing guard
        # against deriving the label from the branch is the end-to-end pin
        # asserting iteration == 129 while branch == "iter/1-test")
        self.assertIn("supervising_iteration_json", BOUNDARY_BLOCK)

    def test_the_sequence_keeps_every_gate(self):
        """Nothing loosens: the gates the kickoff names are still called from
        the commit path the sequence uses."""
        fn = _extract_block(r"^commit_from_artifact\(\) \{", r"^\}")
        for gate in ("run_verify_gate", "staged_touches_security_pair", "post_verify_commit_gates",
                     "unstage_transient_adds"):
            self.assertIn(gate, fn)
        wd = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        self.assertIn("_disarm_deploy_artifact ready-to-deploy.json", wd)

    def test_completion_is_terminal_at_every_landing_site_and_the_loop_top(self):
        """v0.14.9: every site that lands a final boundary tests
        boundary_complete right after land_boundary and ends in
        finish_complete; the loop top tests boundary_complete_here before
        the wrap-up sentinel and the pacing check; the wrap-up and the
        watchdog's last-resort commit stand down on a complete record."""
        sites = [m.start() for m in re.finditer(r"^\s+land_boundary ", SOURCE, re.M)]
        self.assertEqual(len(sites), 4)
        for pos in sites:
            after = SOURCE[pos:pos + 700]
            self.assertIn("if boundary_complete; then", after, SOURCE[pos:pos + 120])
            self.assertIn("finish_complete", after)
        loop = SOURCE[SOURCE.index('while [[ "$iteration" -le "$MAX_ITERATIONS" ]]; do'):]
        top = loop[:loop.index('echo "=== Iteration $iteration ==="')]
        self.assertLess(top.index("boundary_complete_here"), top.index('-f "$WRAPUP_SENTINEL"'))
        self.assertLess(top.index("boundary_complete_here"), top.index("deadline pacing"))
        wrap = _extract_block(r"^wrapup_commit\(\) \{", r"^\}")
        self.assertLess(wrap.index("if boundary_complete; then"), wrap.index(".wrapup-in-progress"))
        wd = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        self.assertLess(wd.index("if boundary_complete; then"), wd.index("_disarm_deploy_artifact"))
        # the predicate is defined in the boundary block, before the watchdog fork
        self.assertIn("boundary_complete()", BOUNDARY_BLOCK)
        fin = _extract_block(r"^finish_complete\(\) \{", r"^\}")
        self.assertIn('echo "Run finished successfully."', fin)
        self.assertIn("exit 0", fin)
        self.assertNotIn("git add", fin); self.assertNotIn("git commit", fin); self.assertNotIn("git checkout", fin)
        # the rule never reads the orchestrator's landing-only flag
        self.assertNotIn("LANDING_ONLY", SOURCE)
        self.assertNotIn("LANDING_ONLY", (REPO_ROOT / "scripts" / "container-setup.sh").read_text())

    def test_docs_name_the_record_and_the_field(self):
        exec_modes = (REPO_ROOT / "docs" / "EXECUTION_MODES.md").read_text()
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        self.assertIn("boundary-state.json", exec_modes)
        self.assertIn("land_boundary", exec_modes)
        self.assertIn("`pass`", exec_modes)
        self.assertIn("`artifacts/iteration-mode.json`'s `iteration`", exec_modes)
        self.assertIn("terminal", exec_modes)
        m = json.loads(MANIFEST.read_text())
        entry = [a for a in m["artifacts"] if a["name"] == "boundary-state.json"][0]
        self.assertIn("TERMINAL", entry["lifecycle"])
        self.assertIn("final_phase", gates)
        self.assertIn("boundary-state.json", gates)


if __name__ == "__main__":
    unittest.main()
