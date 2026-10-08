"""Multi-phase iterations: every phase's landing carries ITS OWN phase (v0.19.1).

round-clock, 2026-10-08 (the first pinned project, iterations 19 and 21): the
last phase of a two-phase iteration was closed by the completion record
alone — `{"phase": "phase-35", …}`, no approval of its own — and v0.18.0's
iteration facts took the phase of the approval on disk, which was phase 34's:
landed at phase 34's boundary and stamped with the same iteration. Phase 35's
commit and its squash landed as "iteration 21 phase 34: <phase 34's title>"
with `Phasekit-Phase: 34`, the record was stamped `final_phase: "phase-34"`,
the evidence file, the plan check and the boundary record all said 34. The
v0.19.0 invariance suite landed single-phase iterations only. Measured: the
defect is layout-independent (v0.18.0 onward, both layouts); it surfaced on
the pinned pilot because round-clock is the fleet's only project that closes
a phase with the completion record alone.

This module runs two- and three-phase iterations in ONE session, in every
final-phase shape the fleet writes, plus kills between phases and inside the
final landing (each resumed), and asserts in the CURRENT layout
(PHASEKIT_TEST_LAYOUT — tests/run-layouts.sh runs it in both) that each
commit carrying phase K's work is labelled K (subject, trailer), and that the
completion record, the evidence file and the boundary record name the last
phase. `LayoutInvariance` then builds BOTH layouts itself and asserts the
landings, the approval and completion artifacts and the boundary records are
identical (shas and times aside) — and correct, since a defect shared by both
layouts passes a pure differential.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import importlib.util as _ilu
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _suite_tmp  # noqa: F401,E402  (every test under its own TMPDIR; tests/_suite_tmp.py)

_hs = _ilu.spec_from_file_location("pk_boundary_harness_mp", Path(__file__).resolve().parent / "test_boundary_state.py")
_H = _ilu.module_from_spec(_hs)
_hs.loader.exec_module(_H)

HAVE_TOOLS = all(shutil.which(t) for t in ("bash", "git", "jq", "python3"))

ITERATION = 21
PHASE_IDS = ("34", "35", "36")
TITLES = {"34": "Session record and the totals", "35": "Summary card", "36": "Share the summary"}

# The model. $STUB_DIR/queue holds one line per turn still to come: the phase
# it builds and how it closes it (`approve` | `record` | `both` | `flag` |
# `record-nophase`); an empty queue is a turn that only orients. KILL_IN_TURN=1
# SIGKILLs the loop once the turn has written its verdict (a kill between the
# work and its landing).
SCENARIO = r"""
rm -f artifacts/session-handoff.json
line="$(head -n1 "$STUB_DIR/queue" 2>/dev/null || true)"
[ -n "$line" ] || exit 0
tail -n +2 "$STUB_DIR/queue" > "$STUB_DIR/queue.next"; mv "$STUB_DIR/queue.next" "$STUB_DIR/queue"
set -- $line
p="$1"; how="$2"
echo "phase $p work" > "src-$p.txt"
case "$how" in
  approve)
    jq -n --arg p "phase-$p" '{phase: $p, approved: true, iteration: "iteration-21", final_phase: false,
      summary: ("built " + $p), suggested_commit_message: ("phase " + $p + " body")}' > artifacts/phase-approval.json ;;
  record)
    jq -n --arg p "phase-$p" '{phase: $p, approved: true, iteration: "iteration-21",
      summary: ("built " + $p + "; iteration complete"), suggested_commit_message: ("phase " + $p + " body; complete")}' \
      > artifacts/project-complete.json ;;
  record-nophase)
    jq -n --arg p "$p" '{done: true, iteration: 21,
      summary: ("built " + $p + "; iteration complete"), suggested_commit_message: ("phase " + $p + " body; complete")}' \
      > artifacts/project-complete.json ;;
  both)
    jq -n --arg p "phase-$p" '{phase: $p, approved: true, final_phase: true,
      summary: ("built " + $p), suggested_commit_message: ("phase " + $p + " body")}' > artifacts/phase-approval.json
    jq -n '{final_phase: true, summary: "complete", suggested_commit_message: "complete"}' > artifacts/project-complete.json ;;
  flag)
    jq -n --arg p "phase-$p" '{phase: $p, approved: true, final_phase: true,
      summary: ("built " + $p), suggested_commit_message: ("phase " + $p + " body")}' > artifacts/phase-approval.json ;;
esac
if [ "${KILL_IN_TURN:-0}" = 1 ] && [ -z "$(cat "$STUB_DIR/queue")" ]; then
  kill -KILL "$PHASEKIT_TEST_LOOP_PID"; sleep 5
fi
"""

SHAPES = ("record", "both", "flag", "record-nophase")


def _plan(n, planned=None):
    out = ["# Phases", "", f"# Iteration {ITERATION} — summary", ""]
    for p in PHASE_IDS[:max(n, planned or 0)]:
        out += [f"## Phase {p} — {TITLES[p]}", "", f"Build {p}.", ""]
        out += [f"### Progress record (iteration {ITERATION}, Phase {p})", "", "notes", ""]
    return "\n".join(out)


def _queue(n, shape):
    return [f"{p} approve" for p in PHASE_IDS[:n - 1]] + [f"{PHASE_IDS[n - 1]} {shape}"]


def _make_repo(n, squash, pinned=None, filler=0, planned=None):
    repo = _H.Repo(squash=squash, pinned=pinned)
    if filler:
        _fill_history(repo, filler)
    repo.write("docs/PHASES.md", _plan(n, planned))
    repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": ITERATION}) + "\n")
    repo.git("add", "-A")
    repo.git("commit", "-qm", f"iteration {ITERATION} intake: summary\n\n"
             f"Phasekit-Iteration: {ITERATION}\nPhasekit-Kind: intake")
    if squash:
        repo.git("branch", "-f", "main", "HEAD")
    repo.base = repo.git("rev-parse", "HEAD")
    repo.scenario(SCENARIO)
    return repo


def _fill_history(repo, count):
    """`count` commits between the base and the intake (fast-import): a
    history long enough that `git log -n 400` is still writing when an
    early-exiting reader has already closed its pipe."""
    head = repo.git("rev-parse", "HEAD")
    branch = repo.git("symbolic-ref", "HEAD")
    lines = []
    for i in range(count):
        msg = f"filler {i}\n\nPhasekit-Iteration: 20\nPhasekit-Kind: phase\n".encode()
        lines.append(f"commit {branch}\nmark :{i + 1}\ncommitter t <t@t> {1700000000 + i} +0000\n"
                     f"data {len(msg)}\n".encode() + msg)
        lines.append((f"from {head}\n" if i == 0 else f"from :{i}\n").encode())
        lines.append(f"M 644 inline filler.txt\ndata {len(str(i))}\n{i}\n".encode())
    subprocess.run(["git", "-C", str(repo.repo), "fast-import", "--quiet", "--force"],
                   input=b"".join(lines), check=True)
    repo.git("reset", "-q", "--hard", branch)


def _set_queue(repo, lines):
    (repo.stub / "queue").write_text("".join(ln + "\n" for ln in lines))


def _commits(repo, ref):
    """(kind, phase trailer, subject, the phases whose work it adds) per
    commit in base..ref, oldest first."""
    out = []
    log = repo.git("log", "--reverse", "--topo-order", "--format=%H%x09%(trailers:key=Phasekit-Kind,valueonly,separator=%x2C)"
                   "%x09%(trailers:key=Phasekit-Phase,valueonly,separator=%x2C)%x09%s", f"{repo.base}..{ref}")
    for ln in log.splitlines():
        sha, kind, phase, subject = ln.split("\t", 3)
        files = repo.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha).split()
        work = sorted(re.match(r"src-([\w.]+)\.txt$", f).group(1) for f in files if re.match(r"src-[\w.]+\.txt$", f))
        out.append((kind, phase, subject, work))
    return out


def _show_json(repo, ref, rel):
    r = subprocess.run(["git", "-C", str(repo.repo), "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def check_landing(tc, repo, n, shape, label=""):
    """Each commit that carries phase K's work is labelled K; the record,
    the evidence and the boundary record name the last phase."""
    last = PHASE_IDS[n - 1]
    refs = ["main"] + ([repo.branch] if repo.squash else [])
    for ref in refs:
        commits = _commits(repo, ref)
        seen = set()
        for kind, phase, subject, work in commits:
            if kind in ("intake", "merge-back", ""):
                continue
            for k in work:
                seen.add(k)
                if shape == "record-nophase" and k == last:
                    tc.assertEqual(phase, "", f"{label} {ref}: {subject!r} claims phase {phase} for underivable work")
                    tc.assertFalse(re.match(rf"iteration {ITERATION} phase \w+:", subject),
                                   f"{label} {ref}: {subject!r}")
                    continue
                tc.assertEqual(phase, k, f"{label} {ref}: the commit carrying phase {k}'s work is labelled "
                                         f"{phase!r}: {subject!r}\n{commits}")
                tc.assertTrue(subject.startswith(f"iteration {ITERATION} phase {k}: {TITLES[k]}"),
                              f"{label} {ref}: {subject!r}")
        tc.assertEqual(seen, set(PHASE_IDS[:n]), f"{label} {ref}: {commits}")
    rec = _show_json(repo, "main", "artifacts/project-complete.json")
    tc.assertIsNotNone(rec, f"{label}: no completion record on main")
    if shape == "record":
        tc.assertEqual(rec.get("final_phase"), f"phase-{last}", f"{label}: {rec}")
    elif shape == "flag":
        tc.assertEqual(rec.get("final_phase"), f"phase-{last}", f"{label}: {rec}")
    elif shape == "both":
        tc.assertIs(rec.get("final_phase"), True, f"{label}: {rec}")
    else:
        tc.assertIsNone(rec.get("final_phase"), f"{label}: {rec}")
    if shape == "record":   # the record drives its own landing (else the approval does)
        tc.assertEqual((rec.get("plan_paths") or {}).get("phase"), last, f"{label}: {rec.get('plan_paths')}")
    ev = _show_json(repo, "main", f"artifacts/iterations/{ITERATION}/complete.json")
    if ev is not None and shape != "record-nophase":
        tc.assertEqual(ev.get("phase"), last, f"{label}: evidence {ev.get('phase')}")
    b = repo.record() or {}
    phase = re.sub(r"^phase-", "", str(b.get("phase")))
    tc.assertEqual(phase, "project" if shape == "record-nophase" else last, f"{label}: boundary record {b}")
    tc.assertTrue(b.get("final"), f"{label}: {b}")
    tc.assertEqual(b.get("step"), 7, f"{label}: {b}")


def _run(repo, max_iterations, extra=None):
    return repo.run(env={"MAX_ITERATIONS": str(max_iterations), **(extra or {})}, timeout=300)


def one_session(n, shape, squash, pinned=None):
    repo = _make_repo(n, squash, pinned)
    _set_queue(repo, _queue(n, shape))
    r = _run(repo, n + 1)
    return repo, r.stdout + r.stderr


# Kills. `turn`: the final turn wrote its verdict and the loop is SIGKILLed
# before landing it. `<step>:<pre|post>`: the final landing's walk is killed
# at that seam. `mid`: phase 1's approval boundary is killed after its commit
# (2:post) and the resumed session builds the rest.
KILLS_SQUASH = ("turn", "1:post", "3:pre", "3:post", "4:post", "5:post", "mid")
KILLS_PLAIN = ("turn", "1:post", "3:pre", "3:post", "mid")


def killed_session(n, shape, squash, kill, planned=None):
    repo = _make_repo(n, squash, planned=planned)
    q = _queue(n, shape)
    if kill == "mid":
        _set_queue(repo, q)
        r1 = _run(repo, 1, {"PHASEKIT_BOUNDARY_KILL_PROBE": "2:post"})
        if r1.returncode != -9:
            raise AssertionError(f"mid: not killed (rc {r1.returncode})\n{r1.stdout}\n{r1.stderr}")
        r2 = _run(repo, n + 1)
        return repo, r1.stdout + r1.stderr + r2.stdout + r2.stderr
    _set_queue(repo, q[:-1])
    r0 = _run(repo, n - 1)
    out = r0.stdout + r0.stderr
    _set_queue(repo, q[-1:])
    env = {"KILL_IN_TURN": "1"} if kill == "turn" else {"PHASEKIT_BOUNDARY_KILL_PROBE": kill}
    r1 = _run(repo, 1, env)
    out += r1.stdout + r1.stderr
    if r1.returncode != -9:
        raise AssertionError(f"{kill}: the final landing was not killed (rc {r1.returncode})\n{out}")
    r2 = _run(repo, 1)
    return repo, out + r2.stdout + r2.stderr


def _parallel(fn, cases):
    workers = min(8, os.cpu_count() or 2)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {c: ex.submit(fn, *c) for c in cases}
        return {c: f for c, f in futs.items()}


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class MultiPhaseLabels(unittest.TestCase):
    """Two- and three-phase iterations in one session, every final shape,
    both landing modes — in the current layout."""

    CASES = [(n, shape, squash) for n in (2, 3) for shape in SHAPES for squash in (True, False)]

    def test_every_phase_lands_under_its_own_label(self):
        futs = _parallel(one_session, self.CASES)
        for case, fut in futs.items():
            n, shape, squash = case
            with self.subTest(phases=n, final=shape, squash=squash):
                repo, out = fut.result()
                self.addCleanup(repo.cleanup)
                self.assertIn("boundary-state: rested (step 7)", out, out[-4000:])
                check_landing(self, repo, n, shape, label=f"{n}/{shape}/{'squash' if squash else 'plain'}")

    def test_the_session_names_the_underivable_phase_instead_of_guessing(self):
        repo, out = one_session(2, "record-nophase", True)
        self.addCleanup(repo.cleanup)
        self.assertIn("its phase cannot be derived (F5)", out, out[-4000:])

    def test_a_completion_after_the_last_planned_phase_still_takes_its_approval(self):
        # The approval and its record landed as two commits (a kill between
        # them): the record names no phase, the plan names nothing after the
        # approval's phase — the completion closes that phase.
        repo = _make_repo(1, True)
        self.addCleanup(repo.cleanup)
        _set_queue(repo, ["34 approve"])
        _run(repo, 1)
        (repo.stub / "queue").write_text("")
        repo.scenario(r"""rm -f artifacts/session-handoff.json
jq -n '{done: true, iteration: 21, summary: "closed", suggested_commit_message: "close out"}' > artifacts/project-complete.json
""")
        _run(repo, 1)
        commits = [c for c in _commits(repo, repo.branch) if c[0] == "completion"]
        self.assertEqual([c[1] for c in commits], ["34"], commits)
        rec = _show_json(repo, "main", "artifacts/project-complete.json")
        self.assertEqual(rec.get("final_phase"), "phase-34", rec)


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class PhaseEvidenceSince(unittest.TestCase):
    """Phase K's evidence is what phase K changed: `since` is the previous
    phase's close. v0.18.0-v0.19.0 found that commit with `git log -n 400 |
    awk '… { print; exit }'` under pipefail: awk's early exit SIGPIPEd git,
    the pipeline failed, `|| since=""` threw the found sha away and the
    evidence fell back to the iteration base — phase 35's evidence listed
    phase 34's files too, nondeterministically (found by LayoutInvariance:
    3 of 16 identical runs differed)."""

    def test_the_second_phases_evidence_starts_at_the_first_phases_close(self):
        def one(_):
            repo = _make_repo(3, False, filler=450)
            _set_queue(repo, _queue(3, "flag"))
            _run(repo, 4)
            return repo
        for repo in _parallel(lambda i: one(i), [(i,) for i in range(4)]).values():
            repo = repo.result()
            self.addCleanup(repo.cleanup)
            closes = repo.git("log", "--reverse", "--format=%H", f"{repo.base}..main").split()
            for k, p in enumerate(PHASE_IDS):
                ev = _show_json(repo, "main", f"artifacts/iterations/{ITERATION}/{p}.json")
                want = repo.base if k == 0 else closes[k - 1]
                self.assertEqual(ev.get("since"), want, f"phase {p}: {ev.get('since')} (closes {closes})")
                self.assertEqual(sorted(c["path"] for c in ev["changed"] if c["path"].startswith("src-")),
                                 [f"src-{p}.txt"], ev["changed"])


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class ReviewShapes(unittest.TestCase):
    """The v0.19.1 review's shapes (fresh-context round 1)."""

    COPIED = r"""rm -f artifacts/session-handoff.json
echo "phase 35 work" > src-35.txt
jq -n --arg fp "$FP" '{phase: "phase-30", iteration: 20, summary: "copied forward", suggested_commit_message: "close it"}
  + (if $fp == "" then {} else {final_phase: $fp} end)' > artifacts/project-complete.json
"""

    def _copied(self, fp):
        repo = _make_repo(2, True)
        self.addCleanup(repo.cleanup)
        _set_queue(repo, ["34 approve"])
        _run(repo, 1)
        repo.scenario(self.COPIED)
        r = _run(repo, 1, {"FP": fp})
        return repo, r.stdout + r.stderr

    def test_a_record_copied_from_another_iteration_never_labels_the_landing(self):
        # F1: the copy's phase (30), with or without a copied final_phase,
        # is that iteration's; phase 34's approval is not this landing's
        # (the plan names 35 after it) — the phase is left out, everywhere.
        for fp in ("", "phase-30"):
            with self.subTest(copied_final_phase=fp or None):
                repo, out = self._copied(fp)
                for ref in ("main", repo.branch):
                    for kind, phase, subject, work in _commits(repo, ref):
                        if "35" in work:
                            self.assertEqual(phase, "", f"{ref}: {subject!r}\n{out[-3000:]}")
                            self.assertNotRegex(subject, r"phase (30|34)\b", subject)
                rec = _show_json(repo, "main", "artifacts/project-complete.json")
                self.assertEqual(rec.get("iteration"), ITERATION, rec)
                self.assertEqual(rec.get("phase_as_written"), "phase-30", rec)
                self.assertNotIn("phase", rec)
                self.assertNotIn("final_phase", rec)
                self.assertEqual((repo.record() or {}).get("phase"), "project", repo.record())

    def test_preparing_a_copied_record_twice_prepares_the_same_bytes(self):
        # F1 idempotence: `phasekit verify` then the commit gate.
        repo = _make_repo(2, True)
        self.addCleanup(repo.cleanup)
        _set_queue(repo, ["34 approve"])
        _run(repo, 1)
        subprocess.run(["bash", "-c", self.COPIED], cwd=repo.repo, check=True,
                       env={**os.environ, "STUB_DIR": str(repo.stub), "FP": "phase-30"})
        seen = []
        for _ in range(2):
            repo.run_verb("verify")
            seen.append(repo.artifact("project-complete.json").read_text())
        self.assertEqual(seen[0], seen[1])
        self.assertNotIn('"final_phase"', seen[0])

    def test_a_final_approvals_boundary_keeps_its_phase_across_a_resume_with_more_planned(self):
        # F2: the "both" shape (record final_phase: true, no phase) with the
        # plan naming a phase after it; killed after the landing, resumed.
        for kill in ("3:post", "4:post"):
            with self.subTest(kill=kill):
                repo, out = killed_session(2, "both", True, kill, planned=3)
                self.addCleanup(repo.cleanup)
                self.assertEqual((repo.record() or {}).get("phase"), "phase-35", out[-3000:])
                check_landing(self, repo, 2, "both", label=f"both+later/{kill}")

    def test_a_bare_phase_id_planned_later_is_seen(self):
        # F4: `## M9.4 — …` then `## M9.5 — …`; M9.4 approved and landed, the
        # completion names no phase — M9.5 is planned after it: left out.
        repo = _make_repo(2, True)
        self.addCleanup(repo.cleanup)
        repo.write("docs/PHASES.md", "# Phases\n\n## M9.4 — Lobby\n\nx\n\n## M9.5 — Share\n\ny\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "plan")
        repo.scenario(r"""rm -f artifacts/session-handoff.json
if [ ! -f src-a.txt ]; then echo a > src-a.txt
  jq -n '{phase: "M9.4", approved: true, iteration: 21, summary: "a", suggested_commit_message: "a"}' > artifacts/phase-approval.json
else echo b > src-b.txt
  jq -n '{done: true, iteration: 21, summary: "b", suggested_commit_message: "b"}' > artifacts/project-complete.json
fi
""")
        _run(repo, 3)
        labels = {tuple(w): ph for kind, ph, subj, w in _commits(repo, repo.branch) if w}
        self.assertEqual(labels.get(("a",)), "M9.4", labels)
        self.assertEqual(labels.get(("b",)), "", labels)


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class ReviewShapesRound2(unittest.TestCase):
    """The v0.19.1 review's round-2 shapes."""

    def test_a_resumed_projects_old_final_approval_never_names_a_new_completion(self):
        # MAJOR 1: the previous iteration's final approval carries no
        # iteration (landed by hand, pre-v0.18); the project completed, the
        # record was retired, and a later turn closes the work with a record
        # that names no phase — supervised and standalone.
        for supervised in (True, False):
            with self.subTest(supervised=supervised):
                repo = _H.Repo(squash=False)
                self.addCleanup(repo.cleanup)
                repo.write("docs/PHASES.md", "# Phases\n\n## Phase 10 — Old thing\n")
                repo.write("artifacts/phase-approval.json", json.dumps(
                    {"phase": "phase-10", "approved": True, "final_phase": True, "summary": "old"}) + "\n")
                repo.write("artifacts/project-complete.json", json.dumps({"done": True, "summary": "old"}) + "\n")
                repo.git("add", "-A"); repo.git("commit", "-qm", "old iteration, landed by hand")
                repo.git("rm", "-q", "artifacts/project-complete.json")
                if supervised:
                    repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "light", "iteration": ITERATION}) + "\n")
                    repo.git("add", "-A")
                    repo.git("commit", "-qm", f"iteration {ITERATION} intake: typo\n\nPhasekit-Iteration: {ITERATION}\nPhasekit-Kind: intake")
                else:
                    repo.git("commit", "-qm", "resume")
                repo.base = repo.git("rev-parse", "HEAD")
                repo.scenario(r"""rm -f artifacts/session-handoff.json
echo fixed > src-new.txt
jq -n '{done: true, summary: "fixed", suggested_commit_message: "fix the typo"}' > artifacts/project-complete.json
""")
                _run(repo, 1)
                log = repo.git("log", "--format=%s|%(trailers:key=Phasekit-Phase,valueonly,separator=)", f"{repo.base}..HEAD")
                self.assertNotIn("10", log, log)
                rec = _show_json(repo, "HEAD", "artifacts/project-complete.json")
                self.assertNotIn("final_phase", rec, rec)

    def test_a_record_with_an_illegible_or_padded_iteration_keeps_its_phase(self):
        # MINOR 4: "iteration-021" is 21; "21 (light)" names nothing legible.
        for label in ("iteration-021", "21 (light)"):
            with self.subTest(label=label):
                repo = _make_repo(2, True)
                self.addCleanup(repo.cleanup)
                _set_queue(repo, ["34 approve"])
                _run(repo, 1)
                repo.scenario(r"""rm -f artifacts/session-handoff.json
echo w > src-35.txt
jq -n --arg it "$LBL" '{phase: "phase-35", iteration: $it, summary: "s", suggested_commit_message: "b"}' > artifacts/project-complete.json
""")
                _run(repo, 1, {"LBL": label})
                labels = {tuple(w): ph for kind, ph, subj, w in _commits(repo, repo.branch) if w}
                self.assertEqual(labels.get(("35",)), "35", labels)

    def test_an_approval_written_after_a_verify_stamp_still_names_the_landing(self):
        # MINOR 5: in one turn the session writes the record (no phase), runs
        # `phasekit verify` (which stamps final_phase from the landed
        # phase-34 approval — the plan names nothing after 34 yet), then
        # plans phase 35 and writes its approval.
        repo = _make_repo(1, True)
        self.addCleanup(repo.cleanup)
        _set_queue(repo, ["34 approve"])
        _run(repo, 1)
        repo.scenario(r"""rm -f artifacts/session-handoff.json
echo w > src-35.txt
jq -n '{done: true, iteration: 21, summary: "s", suggested_commit_message: "b"}' > artifacts/project-complete.json
PHASEKIT_PROJECT_DIR="$PWD" bash "$LOOP" verify >/dev/null 2>&1 || true
jq -r '.final_phase // "none"' artifacts/project-complete.json > "$STUB_DIR/stamped"
printf '\n## Phase 35 — Summary card\n' >> docs/PHASES.md
jq -n '{phase: "phase-35", approved: true, final_phase: true, summary: "35", suggested_commit_message: "35 body"}' > artifacts/phase-approval.json
""")
        _run(repo, 1, {"LOOP": str(repo.layout.loop())})
        self.assertEqual((repo.stub / "stamped").read_text().strip(), "phase-34")
        labels = {tuple(w): ph for kind, ph, subj, w in _commits(repo, repo.branch) if w}
        self.assertEqual(labels.get(("35",)), "35", labels)
        self.assertEqual(_show_json(repo, "main", "artifacts/project-complete.json").get("final_phase"), "phase-35")
        self.assertEqual((repo.record() or {}).get("phase"), "phase-35")

    def test_a_plan_that_does_not_plan_the_approvals_phase_proves_nothing(self):
        # MINOR 2: fail closed — `## Phase 34 (light) — …` is not a heading
        # of phase 34, so nothing shows that no phase follows it.
        repo = _make_repo(2, True)
        self.addCleanup(repo.cleanup)
        repo.write("docs/PHASES.md", "# Phases\n\n## Phase 34 (light) — A\n\n## Phase 35 (light) — B\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "plan")
        _set_queue(repo, ["34 approve", "35 record-nophase"])
        _run(repo, 3)
        labels = {tuple(w): ph for kind, ph, subj, w in _commits(repo, repo.branch) if w}
        self.assertEqual(labels.get(("35",)), "", labels)


class NoEarlyReaderUnderPipefail(unittest.TestCase):
    """The loop runs `set -o pipefail`: a reader that stops early (`awk …
    exit`, `grep -q`, `head`) SIGPIPEs its writer and FAILS the pipeline
    although it found what it wanted — a lost evidence base, a security-pair
    check that reads "untouched" (`git diff --cached --name-only | grep -q`),
    an assignment that ends the loop under `set -e`. A reader on a pipe reads
    to the end (`awk '!f && … { print; f = 1 }'`, `grep … >/dev/null`,
    `sed -n '1,Np'`); the exceptions are a status nobody reads (inside
    `[[ … ]]`, or a message's text)."""

    EARLY = re.compile(r"(?<!\|)\|(?!\|)\s*(awk\b.*\bexit\s*\}|grep\s+-[A-Za-z]*q|head\b)")
    STATUS_UNREAD = ('[[ -n "$(', '[[ -z "$(', '&& -n "$(', "record_commit_refusal", "$(printf '%s' \"$STAGE_ERR\"",
                     "_phase_plan_py title")

    def test_no_reader_stops_early_where_the_status_counts(self):
        src = (_H.LOOP_SCRIPT).read_text().splitlines()
        bad = [f"{i}: {ln.strip()}" for i, ln in enumerate(src, 1)
               if not ln.lstrip().startswith("#") and self.EARLY.search(ln)
               and not any(tok in ln for tok in self.STATUS_UNREAD)]
        self.assertEqual(bad, [], "early readers on a pipefail pipeline:\n" + "\n".join(bad))


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class MultiPhaseKills(unittest.TestCase):
    """A kill between phases and at every seam of the final landing: the
    resume lands every phase under its own label."""

    CASES = [(n, shape, squash, kill)
             for n in (2, 3) for shape in ("record", "both") for squash in (True, False)
             for kill in (KILLS_SQUASH if squash else KILLS_PLAIN)]

    def test_resumed_landings_keep_every_phases_label(self):
        futs = _parallel(killed_session, self.CASES)
        for case, fut in futs.items():
            n, shape, squash, kill = case
            with self.subTest(phases=n, final=shape, squash=squash, kill=kill):
                repo, out = fut.result()
                self.addCleanup(repo.cleanup)
                check_landing(self, repo, n, shape,
                              label=f"{n}/{shape}/{'squash' if squash else 'plain'}/kill@{kill}")


def _normal(repo):
    """What must be identical across layouts: every commit's subject, body,
    trailers and changed paths; the approval and completion artifacts and the
    evidence on main; the boundary record — shas and times aside."""
    log = repo.git("log", "--reverse", "--topo-order", "--format=%x01%s%n%b%x02", "--name-status", "--no-renames",
                  f"{repo.base}..main")
    if repo.squash:
        log += repo.git("log", "--reverse", "--topo-order", "--format=%x01%s%n%b%x02", "--name-status", "--no-renames",
                        f"{repo.base}..{repo.branch}")
    arts = {rel: _show_json(repo, "main", rel) for rel in
            ("artifacts/phase-approval.json", "artifacts/project-complete.json",
             f"artifacts/iterations/{ITERATION}/complete.json")}
    arts.update({f"artifacts/iterations/{ITERATION}/{p}.json":
                 _show_json(repo, "main", f"artifacts/iterations/{ITERATION}/{p}.json") for p in PHASE_IDS})
    b = repo.record() or {}
    rec = {k: b.get(k) for k in ("phase", "final", "step", "step_name", "iteration", "mode")}
    rec["previous"] = {k: (b.get("previous") or {}).get(k) for k in ("phase", "final", "step", "iteration")}
    blob = json.dumps({"log": log, "arts": arts, "boundary": rec}, sort_keys=True, indent=1)
    blob = re.sub(r"\b[0-9a-f]{7,64}\b", "<sha>", blob)
    blob = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(Z|[+-]\d\d:\d\d)", "<time>", blob)
    return blob


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class LayoutInvariance(unittest.TestCase):
    """Design §9.1 gate 13 for multi-phase iterations: the same sessions
    through a vendored and a pinned project land identical commits, approval
    and completion artifacts, evidence and boundary records — and correct
    ones. Builds both layouts itself (the result is the same under either
    PHASEKIT_TEST_LAYOUT)."""

    CASES = [(n, shape, squash) for n in (2, 3) for shape in ("record", "both", "flag") for squash in (True, False)]

    def test_multi_phase_landings_are_identical_and_correct_in_both_layouts(self):
        cases = [(n, shape, squash, pinned) for (n, shape, squash) in self.CASES for pinned in (False, True)]
        futs = _parallel(one_session, cases)
        for (n, shape, squash) in self.CASES:
            with self.subTest(phases=n, final=shape, squash=squash):
                shapes = {}
                for pinned in (False, True):
                    repo, out = futs[(n, shape, squash, pinned)].result()
                    self.addCleanup(repo.cleanup)
                    check_landing(self, repo, n, shape,
                                  label=f"{'pinned' if pinned else 'vendored'} {n}/{shape}")
                    shapes[pinned] = _normal(repo)
                self.assertEqual(shapes[False], shapes[True])


if __name__ == "__main__":
    unittest.main()
