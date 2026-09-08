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
artifact_never_landed() { [[ -f "$1" ]] || return 1; [[ -n "$(git status --porcelain --ignored=matching -- "$1" 2>/dev/null)" ]]; }
squash_pending() { return 1; }
artifact_written_this_iteration() { return 1; }
'''


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
        self.assertEqual((rec["step"], rec["step_name"], rec["iteration"]), (0, "idle", 4))
        self.assertEqual(rec["verify_memo"]["tree_sha"], "t1")
        self.assertEqual(rec["sha_at_step"], {})

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
                # task 2: a session deletes the record, works, checkpoints (phase-update), ends
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


class StructuralPins(unittest.TestCase):
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
        for key in ("step", "final", "sha_at_step", "verify_memo", "killed_after"):
            self.assertIn(key, entry["keys"])
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

    def test_the_sequence_keeps_every_gate(self):
        """Nothing loosens: the gates the kickoff names are still called from
        the commit path the sequence uses."""
        fn = _extract_block(r"^commit_from_artifact\(\) \{", r"^\}")
        for gate in ("run_verify_gate", "staged_touches_security_pair", "post_verify_commit_gates",
                     "unstage_transient_adds"):
            self.assertIn(gate, fn)
        wd = _extract_block(r"^deadline_lastresort_commit\(\) \{", r"^\}")
        self.assertIn("_disarm_deploy_artifact ready-to-deploy.json", wd)

    def test_docs_name_the_record_and_the_field(self):
        exec_modes = (REPO_ROOT / "docs" / "EXECUTION_MODES.md").read_text()
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        self.assertIn("boundary-state.json", exec_modes)
        self.assertIn("land_boundary", exec_modes)
        self.assertIn("final_phase", gates)
        self.assertIn("boundary-state.json", gates)


if __name__ == "__main__":
    unittest.main()
