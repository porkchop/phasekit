"""v0.29.0 (audit D2): the acceptance ledger — criteria as data, ids
append-only, status mutable — and the SPEC → ledger migration.

Pinned here:
  * the checker: no ledger passes (exit 0); a clean ledger passes; a duplicate
    id, a bad status, a superseded entry without a ledger target, a withdrawn
    entry without a reason, not-JSON are findings (exit 3); an id present at
    --base and gone now is a finding whatever else changed; a text or status
    change is not;
  * the migration: numbered items, explicit AC ids and SUPERSEDED lines become
    entries with the right status; id-less lines are counted, not dropped; the
    SPEC is archived and a living skeleton written; a ledger already present,
    no SPEC or no acceptance heading refuse (exit 4); --dry-run writes nothing;
  * the loop: a landing in ledger mode that lost an id is REFUSED before the
    gate (commit_refusal, the ledger label) and lands once the entry is back
    as superseded; `phasekit verify` reports the same tree RED without running
    the gate; a project without a ledger is untouched;
  * `phasekit spec migrate` (CLI, pinned project): one gated commit, the
    files; a red gate restores byte for byte (exit 4); a loop session is
    refused; `phasekit check` is 8 on ledger findings and 0 when clean;
  * the registry and the prompt carry the convention.

Run from the repo root: python3 -m unittest tests.test_spec_ledger
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-spec.py"
LEDGER = "docs/acceptance.json"

_if = importlib.util.spec_from_file_location(
    "pk_iteration_facts_for_ledger", Path(__file__).resolve().parent / "test_iteration_facts.py")
IF = importlib.util.module_from_spec(_if)
_if.loader.exec_module(IF)
_eo = importlib.util.spec_from_file_location(
    "pk_engine_outside_for_ledger", Path(__file__).resolve().parent / "test_engine_outside.py")
EO = importlib.util.module_from_spec(_eo)
_eo.loader.exec_module(EO)


def tool(*args, cwd=None):
    return subprocess.run([sys_python(), str(TOOL), *args], capture_output=True, text=True, cwd=cwd)


def sys_python():
    import sys
    return sys.executable


def ledger(*entries):
    return json.dumps({"schema": 1, "entries": list(entries)}, indent=1) + "\n"


def entry(i, status="met", **kw):
    e = {"id": f"AC#{i}", "text": f"criterion {i}", "status": status}
    e.update(kw)
    return e


class Checker(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-ledger-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "docs").mkdir()
        for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"],
                     ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True)

    def commit(self, text):
        (self.root / LEDGER).write_text(text)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ledger"], check=True)

    def check(self, text=None, base=None, as_json=False):
        if text is not None:
            (self.root / LEDGER).write_text(text)
        extra = (["--base", base] if base else []) + (["--json"] if as_json else [])
        return tool("check", "--repo", str(self.root), *extra)

    def test_no_ledger_passes(self):
        r = self.check()
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_a_clean_ledger_passes(self):
        self.commit(ledger(entry(1), entry(2, "open"), entry(3, "superseded", superseded_by="AC#2"),
                           entry(4, "withdrawn", reason="not a thing")))
        r = self.check()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_shape_findings(self):
        self.commit(ledger(entry(1)))
        cases = {
            "duplicate": (ledger(entry(1), entry(1)), "duplicate id AC#1"),
            "status": (ledger(entry(1, "done")), "`status` must be one of"),
            "superseded target": (ledger(entry(1, "superseded", superseded_by="AC#9")), "must name an id"),
            "withdrawn reason": (ledger(entry(1, "withdrawn")), "non-empty `reason`"),
            "bad id": (ledger({"id": "12", "text": "t", "status": "met"}), "`id` must be `AC#`"),
            "empty text": (ledger({"id": "AC#1", "text": " ", "status": "met"}), "`text` must be"),
            "not json": ("{nope", "not JSON"),
        }
        for name, (text, phrase) in cases.items():
            r = self.check(text)
            self.assertEqual(r.returncode, 3, (name, r.stdout, r.stderr))
            self.assertIn(phrase, r.stdout, name)

    def test_a_lost_id_is_a_finding_and_moved_text_or_status_is_not(self):
        self.commit(ledger(entry(1), entry(2, "open")))
        r = self.check(ledger(entry(1)))
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("AC#2 was in the ledger at HEAD and is gone", r.stdout)
        r = self.check(ledger({"id": "AC#1", "text": "reworded", "status": "superseded", "superseded_by": "AC#2"},
                              entry(2, "met")))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        # the base can be named
        self.commit(ledger(entry(1), entry(2), entry(3)))
        r = self.check(ledger(entry(1), entry(2)), base="HEAD~1")
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.check(ledger(entry(1), entry(2)))
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_json_output(self):
        self.commit(ledger(entry(1)))
        r = self.check(ledger(entry(1), entry(1)), as_json=True)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["findings"][0][:20], "docs/acceptance.json")


SPEC_FIXTURE = """# demo spec

## Product goal
Make things tick.

## Acceptance criteria (testable)
1. The clock ticks every second.
2. The clock stops on STOP. It also beeps.
SUPERSEDED by AC#4: 3. The clock pauses for setup.
4. The clock pauses for setup with a message.
- **AC-E14.7 (liveness v0)** — a window whose seal does not arrive is void.
Some prose with no id at all.
SUPERSEDED by AC#99: 5. A criterion superseded by one the archive lacks.

### Nested acceptance for the lobby
- AC#L1: the lobby admits three matches.

## Edge cases
- Nothing here.
"""


SPEC_CLEAN = SPEC_FIXTURE.replace(
    "SUPERSEDED by AC#99: 5. A criterion superseded by one the archive lacks.\n", "")

# criteria appended OUTSIDE the acceptance heading (review B1: the orchestrator
# appended 324–395 under "## Operational notes"), a wrapped criterion with
# sub-items (M2), an "AC…" word that is not an id (M1), a numbered list that
# is prose (not cited) and a heading that only mentions acceptance in passing
SPEC_SPRAWL = """# sprawl

## Acceptance criteria
1. ACTIVE users see the list.
2. The scheduler dispatches one session per project
   and never two for one project at a time:
   (a) the lock is per project;
   (b) a stale lock is reclaimed after 10 minutes.

## Non-acceptance notes
1. this is prose, a numbered aside nobody cites

## Operational notes
Rolling criteria continue here, cited by the tests as AC#324 and AC#325.
324. The queue drains oldest first.
325. A paused project is skipped.
- AC#326: a bare id-carrying item outside the section.
342. **AC#2(f)'s uncontended half is recorded**, cited by AC#342's own tests.

## Prose that only looks like citations
The verifiable form AC#76-100 established, the appended AC#129-132, a placeholder
`AC#N` or `AC#nn`, and `AC#108-supersedes-AC#2(a)` are not criteria this plan must find
(AC#108 and AC#129 are: they are cited on their own below).
108. A real criterion cited by the prose above.
129. Another one.
"""


class Migration(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-mig-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "SPEC.md").write_text(SPEC_FIXTURE)

    def test_a_cited_id_the_plan_misses_refuses_unless_allowed(self):
        r = tool("migrate", "--repo", str(self.root), "--dry-run")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("1 id(s) the SPEC cites were NOT captured as criteria: AC#99", r.stdout)
        self.assertIn("refused", r.stderr)
        self.assertFalse((self.root / LEDGER).exists())
        r = tool("migrate", "--repo", str(self.root), "--dry-run", "--json", "--allow-missing")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["cited_not_captured"], ["AC#99"])

    def test_the_plan_and_the_files(self):
        r = tool("migrate", "--repo", str(self.root), "--dry-run", "--json", "--allow-missing")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual(plan["ids"], ["AC#1", "AC#2", "AC#3", "AC#4", "AC#E14.7", "AC#5", "AC#L1"])
        self.assertEqual(plan["counts"], {"met": 5, "superseded": 1, "withdrawn": 1, "skipped_lines": 0})
        self.assertTrue(plan["dry_run"])
        self.assertFalse((self.root / LEDGER).exists(), "a dry run writes nothing")
        self.assertEqual((self.root / "docs" / "SPEC.md").read_text(), SPEC_FIXTURE)
        r = tool("migrate", "--repo", str(self.root), "--allow-missing")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads((self.root / LEDGER).read_text())
        by = {e["id"]: e for e in data["entries"]}
        self.assertEqual(by["AC#3"]["status"], "superseded")
        self.assertEqual(by["AC#3"]["superseded_by"], "AC#4")
        self.assertEqual(by["AC#5"]["status"], "withdrawn", "superseded by an id the archive lacks")
        self.assertEqual(by["AC#2"]["text"], "The clock stops on STOP. It also beeps.")
        self.assertEqual(by["AC#E14.7"]["text"].split(" — ", 1)[1],
                         "a window whose seal does not arrive is void. Some prose with no id at all.",
                         "the id token is stripped; the prose line below joins the entry")
        self.assertEqual(by["AC#1"]["since"], "migrated")
        archive = list((self.root / "docs" / "archive").glob("SPEC-*.md"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0].read_text(), SPEC_FIXTURE)
        skeleton = (self.root / "docs" / "SPEC.md").read_text()
        self.assertIn("## Product goal\nMake things tick.", skeleton)
        self.assertIn("## Edge cases", skeleton)
        self.assertIn("## Acceptance (ledger mode)", skeleton)
        self.assertIn("- `AC#2` The clock stops on STOP.", skeleton, "first sentence only")
        self.assertNotIn("AC#3", skeleton, "superseded entries are not behaviour")
        self.assertIn("### Nested acceptance for the lobby", skeleton)
        self.assertLess(len(skeleton), len(SPEC_FIXTURE) + 900)
        # the migrated tree is clean under the checker
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        r = tool("check", "--repo", str(self.root))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_criteria_outside_the_section_wrapped_lines_and_non_ids(self):
        (self.root / "docs" / "SPEC.md").write_text(SPEC_SPRAWL)
        r = tool("migrate", "--repo", str(self.root), "--dry-run", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual(plan["ids"], ["AC#1", "AC#2", "AC#324", "AC#325", "AC#326", "AC#342",
                                       "AC#108", "AC#129"])
        self.assertEqual(plan["cited_not_captured"], [], "ranges and placeholders are not citations")
        r = tool("migrate", "--repo", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        by = {e["id"]: e for e in json.loads((self.root / LEDGER).read_text())["entries"]}
        self.assertEqual(by["AC#1"]["text"], "ACTIVE users see the list.", "ACTIVE is not an id (M1)")
        self.assertEqual(by["AC#2"]["text"],
                         "The scheduler dispatches one session per project and never two for one project "
                         "at a time: (a) the lock is per project; (b) a stale lock is reclaimed after "
                         "10 minutes.", "continuation lines and sub-items join the entry (M2)")
        self.assertEqual(by["AC#324"]["text"], "The queue drains oldest first.")
        self.assertEqual(by["AC#326"]["text"], "a bare id-carrying item outside the section.")
        self.assertEqual(by["AC#342"]["text"],
                         "**AC#2(f)'s uncontended half is recorded**, cited by AC#342's own tests.",
                         "a numbered item owns its number; the AC#2 in its text is a citation")
        skeleton = (self.root / "docs" / "SPEC.md").read_text()
        self.assertIn("## Non-acceptance notes\n1. this is prose", skeleton, "uncited prose stays prose")
        self.assertIn("## Operational notes\nRolling criteria continue here", skeleton)
        self.assertNotIn("324. The queue", skeleton, "a captured criterion leaves the prose")

    def test_refusals(self):
        (self.root / "docs" / "SPEC.md").write_text(SPEC_CLEAN)
        r = tool("migrate", "--repo", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = tool("migrate", "--repo", str(self.root))
        self.assertEqual(r.returncode, 4, "already in ledger mode")
        self.assertIn("already exists", r.stderr)
        other = Path(tempfile.mkdtemp(prefix="pk-mig2-"))
        self.addCleanup(shutil.rmtree, other, True)
        (other / "docs").mkdir()
        self.assertEqual(tool("migrate", "--repo", str(other)).returncode, 4, "no SPEC")
        (other / "docs" / "SPEC.md").write_text("# spec\n\n## Goal\nx\n")
        r = tool("migrate", "--repo", str(other))
        self.assertEqual(r.returncode, 4, "no acceptance heading")
        self.assertIn("no heading", r.stderr)

    def test_init_writes_an_empty_ledger_once(self):
        r = tool("init", "--repo", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((self.root / LEDGER).read_text()), {"schema": 1, "entries": []})
        self.assertEqual(tool("init", "--repo", str(self.root)).returncode, 4)


APPROVE = r"""
echo "work by call $CALL_N" >> src.txt
jq -n '{phase: "phase-188", approved: true, iteration: "88",
        summary: "ledger work",
        suggested_commit_message: "iteration 88 phase 188: ledger work"}' > artifacts/phase-approval.json
"""


def _ship_tool(repo):
    """The harness repo carries the shipped loop; the ledger tool rides beside
    it as the engine's file (vendored: in the tree; pinned: in the engine)."""
    repo.put_engine("scripts/phasekit-spec.py", TOOL.read_text(), executable=True, commit="ledger tool")


class LoopLanding(unittest.TestCase):
    def _repo(self):
        repo = IF.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        _ship_tool(repo)
        repo.write(LEDGER, ledger(entry(1), entry(2, "open")))
        repo.git("add", "-A")
        repo.git("commit", "-qm", "ledger mode")
        return repo

    def test_a_lost_id_refuses_the_landing_and_the_fix_lands(self):
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.scenario(APPROVE + f"\ncat > {LEDGER} <<'EOF'\n{ledger(entry(1))}EOF\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — the acceptance ledger", out)
        self.assertIn("AC#2 was in the ledger at", out)
        failed = json.loads((repo.repo / "artifacts" / "phase-verify-failed.json").read_text())
        self.assertTrue(failed.get("commit_refusal"), failed)
        self.assertEqual(failed.get("label"), "acceptance ledger (docs/acceptance.json)")
        self.assertNotIn("Verify passed", out, "the gate never ran")
        self.assertEqual(repo.git("log", "--format=%s", "-1"), "unrelated intake words", "nothing landed")
        # the repair turn: the entry back as superseded, the record re-written
        repo.scenario(f"cat > {LEDGER} <<'EOF'\n"
                      + ledger(entry(1), {"id": "AC#2", "text": "criterion 2", "status": "superseded",
                                         "superseded_by": "AC#1"})
                      + "EOF\n" + APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("boundary-state: rested (step 7)", out)
        self.assertEqual(repo.git("log", "--format=%s", "-1"), "iteration 88 phase 188: ledger work")

    def test_an_update_checkpoint_keeps_a_bad_ledger_out_and_the_baseline_holds(self):
        # review B2: a phase-update commit used to carry the session's edited
        # ledger unjudged, and the next verdict then judged against it.
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.scenario(f"echo w >> src.txt\ncat > {LEDGER} <<'EOF'\n{ledger(entry(1))}EOF\n"
                      "jq -n '{summary: \"half done\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("stays OUT of this checkpoint commit", out)
        committed = json.loads(repo.git("show", f"HEAD:{LEDGER}"))
        self.assertEqual([e["id"] for e in committed["entries"]], ["AC#1", "AC#2"], "HEAD keeps both ids")
        self.assertIn("src.txt", repo.git("show", "--stat", "--format=", "HEAD"), "the work landed")
        # the worktree still holds the session's edit; the next verdict judges it
        self.assertEqual([e["id"] for e in json.loads((repo.repo / LEDGER).read_text())["entries"]], ["AC#1"])
        repo.scenario(APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — the acceptance ledger", out)
        self.assertIn("AC#2 was in the ledger at", out)

    def test_the_baseline_is_the_last_landing_not_a_wip(self):
        # a commit of kind wip that carried a bad ledger (a kill path) must not
        # move the baseline: the next verdict is judged against the landing
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.write(LEDGER, ledger(entry(1)))
        repo.git("add", "-A")
        repo.git("commit", "-qm", "wip: killed mid-turn", "-m", "Phasekit-Kind: wip")
        repo.scenario(APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — the acceptance ledger", out)
        self.assertIn("AC#2 was in the ledger at", out)

    def test_a_wip_that_deleted_the_ledger_does_not_leave_ledger_mode(self):
        # review B2 second pass: the watchdog's last-resort wip carried a
        # deletion; HEAD lacks the file, the baseline (the landing) has it
        repo = self._repo()
        IF.supervised(repo, 88)
        repo.git("rm", "-q", LEDGER)
        repo.git("commit", "-qm", "wip: killed mid-turn", "-m", "Phasekit-Kind: wip")
        repo.scenario(APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — the acceptance ledger", out)
        self.assertIn("the ledger is gone from the tree", out)

    def test_the_last_resort_commit_keeps_a_bad_ledger_out(self):
        # structural: the watchdog's wip stages with `git add -A` and must run
        # the keep-out right after (a kill scenario is the boundary suite's)
        text = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()
        fn = text[text.index("deadline_lastresort_commit() {"):]
        fn = fn[:fn.index("\n}\n")]
        add = fn.index("git add -A 2>/dev/null || { sleep 2; continue; }")
        self.assertIn("keep_bad_ledger_out", fn[add:add + 400])

    def test_phasekit_verify_is_red_without_running_the_gate(self):
        repo = self._repo()
        IF.with_cli(repo)
        IF.supervised(repo, 88)
        repo.write(LEDGER, ledger(entry(1)))
        repo.write("artifacts/phase-approval.json", json.dumps(
            {"phase": "phase-188", "approved": True, "summary": "s", "suggested_commit_message": "m"}) + "\n")
        r = IF.cli(repo, "verify")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RED — the landing will refuse this verdict: the acceptance ledger", r.stderr)
        self.assertIn("The gate was not run", r.stderr)
        self.assertFalse((repo.stub / "verify-calls").exists(), "the gate did not run")

    def test_a_project_without_a_ledger_is_untouched(self):
        repo = IF.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        _ship_tool(repo)
        IF.supervised(repo, 88)
        repo.scenario(APPROVE)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("boundary-state: rested (step 7)", out)
        self.assertNotIn("acceptance ledger", out)


class MigrateCommand(EO.EngineFixture):
    """`phasekit spec migrate` on a pinned project."""

    def _project(self, spec=SPEC_CLEAN):
        proj = self.new_project()
        (proj / "docs" / "SPEC.md").write_text(spec)
        EO._git(proj, "add", "-A"); EO._git(proj, "commit", "-qm", "a spec with criteria")
        return proj

    def test_a_cited_id_the_plan_misses_refuses_the_command(self):
        proj = self._project(SPEC_FIXTURE)
        before = EO._git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "spec", "migrate")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("AC#99", r.stdout)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD"), before)
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        r = self.cli(proj, "spec", "migrate", "--allow-missing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_one_gated_commit_into_ledger_mode(self):
        proj = self._project()
        before = EO._git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "spec", "migrate", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 criteria", r.stdout)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD"), before)
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        r = self.cli(proj, "spec", "migrate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gate:", r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD^"), before, "exactly one commit")
        self.assertEqual(EO._git(proj, "log", "-1", "--format=%s"),
                         "chore(phasekit): enter acceptance-ledger mode (docs/acceptance.json)")
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        tracked = set(EO._git(proj, "ls-files").splitlines())
        self.assertIn(LEDGER, tracked)
        self.assertTrue(any(t.startswith("docs/archive/SPEC-") for t in tracked), tracked)
        self.assertIn("## Acceptance (ledger mode)", (proj / "docs" / "SPEC.md").read_text())
        r = self.cli(proj, "spec", "check")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # a lost id: `phasekit check` says 8, `phasekit spec check` 3
        data = json.loads((proj / LEDGER).read_text())
        data["entries"] = data["entries"][1:]
        (proj / LEDGER).write_text(json.dumps(data) + "\n")
        r = self.cli(proj, "spec", "check")
        self.assertEqual(r.returncode, 3, r.stdout)
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertIn("LEDGER:", r.stderr)

    def test_a_red_gate_restores_byte_for_byte(self):
        proj = self._project()
        (proj / "scripts" / "phasekit-verify.sh").write_text("#!/usr/bin/env bash\nPHASEKIT_VERIFY_CONFIGURED=1\nexit 1\n")
        EO._git(proj, "add", "-A"); EO._git(proj, "commit", "-qm", "red gate")
        before = EO._git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "spec", "migrate")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(EO._git(proj, "rev-parse", "HEAD"), before)
        self.assertEqual(EO._git(proj, "status", "--porcelain"), "")
        self.assertEqual((proj / "docs" / "SPEC.md").read_text(), SPEC_CLEAN)
        self.assertFalse((proj / LEDGER).exists())
        self.assertFalse((proj / "docs" / "archive").exists())

    def test_a_loop_session_is_refused(self):
        proj = self._project()
        r = self.cli(proj, "spec", "migrate", PHASEKIT_ITER_MARKER="1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("operator's command", r.stderr)


class Declared(unittest.TestCase):
    def test_the_registry_the_installer_and_the_prompt_carry_it(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location("pk_enrich_ledger", REPO_ROOT / "scripts" / "enrich-project.py")
        m = iu.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertIn("scripts/phasekit-spec.py", m.ALWAYS_INSTALLED_FILE_PATHS)
        self.assertIn("phasekit-spec:", (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text())
        prompt = (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text()
        self.assertIn("docs/acceptance.json", prompt)
        self.assertIn("LEDGER mode", prompt)
        contract = json.loads((REPO_ROOT / "contracts" / "interface.json").read_text())
        names = {c["name"] for c in contract["conventions"]}
        self.assertIn("acceptance-ledger", names)
        self.assertIn("scripts/phasekit-spec.py", contract["exit_codes"])
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        self.assertIn("### Ledger mode (v0.29.0)", gates)


if __name__ == "__main__":
    unittest.main()
