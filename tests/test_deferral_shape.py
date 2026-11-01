#!/usr/bin/env python3
"""A landing never carries a deferral entry without an `item` (v0.24.0, queue row 2418).

The contract (contracts/interface.json `approval-deferrals`) says a deferral
entry is {item, reason, suggested_task} plus a key. Until v0.23.0 the loop
upserted entries into artifacts/deferrals.json by key without checking that
`item` was there: xmeo-v3's committed ledger (2026-10-31) held 7 of 35 open
entries with only `summary` or `reason`, which a supervisor's mirror (it
requires `item`) silently never imported — three of them MAJOR.

Pinned here, against the shipped loop run whole in a scratch repo with a stub
model (the boundary-state harness):
  * an approval whose entry has no `item` is REFUSED at landing, through the
    repair turn's channel (phase-verify-failed.json, commit_refusal) and
    before the gate runs; nothing lands; the refusal names the key and the
    fields the entry does carry;
  * the same for a keyless entry and an entry that is not an object;
  * an entry IDENTICAL to an open entry HEAD's ledger already carries is
    carried, not refused (a completion record's composed set is the whole
    ledger) — re-listing that key WITH an item replaces it;
  * `phasekit verify` says RED for the same tree and does not run the gate;
  * `phasekit check`'s advisory names ledger entries committed without one.

Run from the repo root: python3 -m unittest tests.test_deferral_shape
"""

import importlib.util
import json
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
Repo = H.Repo


def approval(deferrals):
    return ("echo \"work by call $CALL_N\" >> src.txt\n"
            "cat > artifacts/phase-approval.json <<'JSON'\n"
            + json.dumps({"phase": "phase-1", "approved": True, "summary": "built it",
                          "suggested_commit_message": "Phase 1 (APPROVED): built it",
                          "deferrals": deferrals})
            + "\nJSON\n")


GOOD = {"item": "AC#3 integer math", "reason": "later", "suggested_task": "fixed point"}
SUMMARY_ONLY = {"summary": "slim older peaks unheld", "reason": "measured later",
                "suggested_task": "hold them", "key": "slim-older-peaks-unheld", "severity": "MAJOR"}
LEGACY = {"summary": "preroll overhead hosted unmeasured", "reason": "r", "key": "preroll-overhead"}


class DeferralShape(unittest.TestCase):
    def _repo(self, ledger=None):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        if ledger is not None:
            repo.write("artifacts/deferrals.json",
                       json.dumps({"schema": 1, "note": "n", "open": ledger, "closed": []}) + "\n")
            repo.git("add", "-A")
            repo.git("commit", "-qm", "a ledger an older loop wrote")
        return repo

    def _refused(self, repo, r):
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — deferral entries without an `item`", out)
        pvf = repo.repo / "artifacts" / "phase-verify-failed.json"
        self.assertTrue(pvf.exists(), out)
        rec = json.loads(pvf.read_text())
        self.assertTrue(rec.get("commit_refusal"), rec)
        self.assertIn("deferral entries without item", rec.get("label", ""))
        self.assertNotIn("phase-approval.json", repo.git("ls-files", "artifacts"),
                         "a refused landing commits nothing of the approval")
        self.assertEqual(repo.verify_calls(), 0, "refused before the gate runs")
        return rec, out

    def test_an_entry_with_summary_but_no_item_is_refused_and_named(self):
        repo = self._repo()
        head = repo.git("rev-parse", "HEAD")
        repo.scenario(approval([GOOD, SUMMARY_ONLY]))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        rec, out = self._refused(repo, r)
        self.assertEqual(repo.git("rev-parse", "HEAD"), head)
        tail = rec.get("log_tail", "")
        self.assertIn("key slim-older-peaks-unheld has no `item`", tail)
        self.assertIn("summary", tail)
        self.assertNotIn("AC#3", tail, "an entry with an item is not named")
        # never invented from `summary`
        on_disk = json.loads((repo.repo / "artifacts" / "phase-approval.json").read_text())
        self.assertNotIn("item", on_disk["deferrals"][1])

    def test_the_repair_turn_that_adds_the_item_lands(self):
        repo = self._repo()
        repo.scenario(approval([SUMMARY_ONLY]))
        self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        fixed = dict(SUMMARY_ONLY, item="Hold the older peaks in the slim board")
        repo.reset_stub()
        repo.scenario(approval([fixed]))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        ledger = json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))
        self.assertEqual([e.get("item") for e in ledger["open"]], ["Hold the older peaks in the slim board"])
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(), out)

    def test_a_keyless_entry_and_a_non_object_are_refused(self):
        repo = self._repo()
        repo.scenario(approval([{"reason": "only a reason"}, "a bare string"]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        tail = rec.get("log_tail", "")
        self.assertIn("deferrals[0] (no key) has no `item`", tail)
        self.assertIn("deferrals[1] is not an object", tail)

    def test_a_blank_or_non_string_item_is_refused(self):
        repo = self._repo()
        repo.scenario(approval([{"item": "   ", "reason": "r", "key": "blank"},
                                {"item": {"text": "x"}, "reason": "r", "key": "obj"}]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("key blank has no `item`", rec["log_tail"])
        self.assertIn("key obj has no `item`", rec["log_tail"])

    def test_an_entry_the_ledger_already_carries_is_carried_not_refused(self):
        # A completion record's `deferrals` is composed as the whole open set,
        # legacy entries included; a copy-forward of one is not this record's
        # writing. It lands; the advisory (below) names it.
        repo = self._repo(ledger=[LEGACY])
        repo.scenario("echo w >> src.txt\n"
                      "cat > artifacts/project-complete.json <<'JSON'\n"
                      + json.dumps({"done": True, "summary": "s", "deferrals": [LEGACY, GOOD]})
                      + "\nJSON\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotIn("REFUSED", out)
        record = json.loads(repo.git("show", "HEAD:artifacts/project-complete.json"))
        self.assertEqual([d["key"] for d in record["deferrals"]], ["preroll-overhead", "AC#3"], out)

    def test_a_carried_key_changed_without_an_item_is_refused(self):
        repo = self._repo(ledger=[LEGACY])
        repo.scenario(approval([dict(LEGACY, reason="re-measured, still unheld")]))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("key preroll-overhead has no `item`", rec["log_tail"])

    def test_relisting_a_legacy_key_with_an_item_replaces_it(self):
        repo = self._repo(ledger=[LEGACY])
        repo.scenario(approval([dict(LEGACY, item="Measure the hosted preroll overhead")]))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotIn("REFUSED", out)
        ledger = json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))
        self.assertEqual([(e["key"], e.get("item")) for e in ledger["open"]],
                         [("preroll-overhead", "Measure the hosted preroll overhead")])

    def test_phasekit_verify_is_red_and_runs_no_gate(self):
        repo = self._repo()
        repo.write("src.txt", "changed\n")
        repo.write("artifacts/phase-approval.json", json.dumps(
            {"phase": "phase-1", "approved": True, "summary": "s", "deferrals": [SUMMARY_ONLY]}) + "\n")
        r = repo.run_verb("verify")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("phasekit verify: RED — the landing will refuse deferral entries", r.stderr)
        self.assertIn("key slim-older-peaks-unheld", r.stderr)
        self.assertEqual(repo.verify_calls(), 0)
        self.assertFalse((repo.repo / "artifacts" / "phase-verify-failed.json").exists(),
                         "the model's own check spends no breaker attempt")

    def test_a_wrap_up_never_lands_what_the_boundary_refused(self):
        # review round 1, BLOCKER: with the supervisor's sentinel present the
        # loop went from the refused landing straight to the wrap-up, which
        # committed the approval and the item-less ledger entry and cleared
        # the refusal. Now: the rest is a checkpoint, the verdict stays out,
        # the refusal stands.
        repo = self._repo()
        repo.scenario(approval([SUMMARY_ONLY]) + "touch artifacts/wrapup-requested\n")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = r.stdout + r.stderr
        self.assertIn("committing the rest as a checkpoint", out)
        tracked = repo.git("ls-files", "artifacts")
        self.assertNotIn("phase-approval.json", tracked, out)
        if "artifacts/deferrals.json" in tracked:
            self.assertEqual(json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))["open"], [])
        self.assertIn("work by call 1", repo.git("show", "HEAD:src.txt"), "the work itself is kept")
        pvf = repo.repo / "artifacts" / "phase-verify-failed.json"
        self.assertTrue(pvf.exists(), out)
        self.assertIn("slim-older-peaks-unheld", json.loads(pvf.read_text())["log_tail"])
        self.assertTrue((repo.repo / "artifacts" / "phase-approval.json").exists(), "kept on disk for the repair")

    def test_the_wrap_up_spends_no_second_breaker_attempt_on_the_same_record(self):
        # review round 2, MAJOR 1: light mode (2 attempts) blocked with zero
        # repair turns — the boundary refused, the wrap-up refused the same
        # bytes again
        repo = self._repo()
        repo.scenario(approval([SUMMARY_ONLY]) + "touch artifacts/wrapup-requested\n")
        r = repo.run(env={"MAX_ITERATIONS": "3", "VERIFY_MAX_ATTEMPTS": "2"})
        out = r.stdout + r.stderr
        self.assertIn("no breaker attempt spent", out)
        self.assertFalse((repo.repo / "artifacts" / "phase-blocked.json").exists(), out)
        pvf = json.loads((repo.repo / "artifacts" / "phase-verify-failed.json").read_text())
        self.assertEqual(pvf["attempts"], 1)

    def test_a_deferrals_field_that_is_not_an_array_is_refused(self):
        repo = self._repo()
        repo.scenario(approval({"summary": "AC#4 later", "reason": "r"}))
        rec, _ = self._refused(repo, repo.run(env={"MAX_ITERATIONS": "1"}))
        self.assertIn("`deferrals` is not an array", rec["log_tail"])

    def test_a_closed_key_relisted_without_an_item_is_not_refused(self):
        # it can never enter the ledger (closed keys do not reopen): refusing
        # it would spend a repair turn on an entry with no effect
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("artifacts/deferrals.json", json.dumps(
            {"schema": 1, "note": "n", "open": [], "closed": ["gone-key"]}) + "\n")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "ledger with a closed key")
        repo.scenario(approval([{"summary": "old", "reason": "r", "key": "gone-key"}]))
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotIn("REFUSED", out)
        self.assertIn("phase-approval.json", repo.git("ls-files", "artifacts"), out)
        self.assertEqual(json.loads(repo.git("show", "HEAD:artifacts/deferrals.json"))["open"], [])

    def test_the_refusal_precedes_the_gate_and_uses_the_repair_channel(self):
        # structural pin: inside _commit_from_artifact, after the
        # substantive-change gate and before run_verify_gate
        src = H.LOOP_SCRIPT.read_text()
        body = src[src.index("\n_commit_from_artifact() {"):]
        body = body[:body.index("\n}\n")]
        i_shape = body.index("deferral_shape_refusals")
        self.assertLess(body.index("No substantive change staged"), i_shape)
        self.assertLess(i_shape, body.index("if ! run_verify_gate; then"))
        seg = body[i_shape:body.index("if ! run_verify_gate; then")]
        self.assertIn("record_deferral_shape_refusal", seg)
        self.assertIn("unstage_verdicts_after_refusal", seg)
        helper = src[src.index("\nrecord_deferral_shape_refusal() {"):]
        self.assertIn("record_commit_refusal", helper[:helper.index("\n}\n")])


class LedgerAdvisory(unittest.TestCase):
    def _surface(self):
        spec = importlib.util.spec_from_file_location("pk_surface", REPO_ROOT / "scripts" / "phasekit-surface.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_check_names_open_ledger_entries_without_an_item(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("artifacts/deferrals.json", json.dumps({"schema": 1, "open": [
            dict(GOOD, key="AC#3"), LEGACY, {"reason": "only", "key": "reason-only"}], "closed": []}) + "\n")
        mod = self._surface()
        self.assertEqual(mod.deferral_item_lines(repo.repo), [], "a draft on disk is not the committed ledger")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "ledger")
        lines = mod.deferral_item_lines(repo.repo)
        self.assertTrue(lines and lines[0].startswith("ADVISORY deferral-items: 2 open"), lines)
        self.assertIn("preroll-overhead", "\n".join(lines))
        self.assertIn("reason-only", "\n".join(lines))
        self.assertNotIn("AC#3", "\n".join(lines))
        self.assertTrue(any(ln.startswith("ADVISORY deferral-items") for ln in mod.check_lines(repo.repo)))

    def test_check_is_silent_on_a_clean_or_missing_or_broken_ledger(self):
        repo = Repo(squash=False)
        self.addCleanup(repo.cleanup)
        mod = self._surface()
        self.assertEqual(mod.deferral_item_lines(repo.repo), [])
        for text in (json.dumps({"schema": 1, "open": [GOOD], "closed": []}) + "\n", "{not json\n"):
            repo.write("artifacts/deferrals.json", text)
            repo.git("add", "-A")
            repo.git("commit", "-qm", "ledger")
            self.assertEqual(mod.deferral_item_lines(repo.repo), [])


if __name__ == "__main__":
    unittest.main()
