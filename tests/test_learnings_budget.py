#!/usr/bin/env python3
"""The learnings budget (v0.19.5, Aaron 2026-10-14) — the loop caps docs/LEARNINGS.md.

Every session reads docs/LEARNINGS.md; the prompt's "curate past ~150 lines"
rule did not hold it (xmeo-v3's reached ~60 KB). At every approval-class
landing, and in `phasekit verify`, the loop moves WHOLE entries, oldest
first, to the bottom of docs/LEARNINGS-ARCHIVE.md until the file is within
LEARNINGS_BUDGET_BYTES. These tests pin:

  - the rotation itself (the shipped `rotate_learnings`, extracted from the
    loop and run in a scratch tree): a bullet file, a sectioned file, an
    under-budget no-op, idempotence, byte conservation, the preamble kept,
    the newest entry never moved, a code fence never split, an existing
    case-variant archive reused, and a kill between the two writes;
  - the close-out ordering, by running the whole loop with a stub model: the
    session's learnings write, then the rotation, then the verify gate (which
    judges the rotated file), then the phase commit carrying both files; a
    checkpoint never rotates; `phasekit verify` rotates and the commit gate
    still reuses its verdict; the credential scan refuses an archive.

Run from the repo root: python3 -m unittest tests.test_learnings_budget
"""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_lb", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

_spec2 = importlib.util.spec_from_file_location(
    "pk_iteration_facts_lb", Path(__file__).resolve().parent / "test_iteration_facts.py")
IF = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(IF)

BUDGET = int(re.search(r"^LEARNINGS_BUDGET_BYTES=(\d+)$", SRC, re.M).group(1))
BATCH = "## Moved from docs/LEARNINGS.md by the phasekit loop (oldest first)\n\n"


def fn(name):
    start = SRC.index("\n" + name + "() {\n") + 1
    return SRC[start:SRC.index("\n}\n", start) + 3]


ROTATE = "set -euo pipefail\nLEARNINGS_BUDGET_BYTES=%d\n" % BUDGET + fn("rotate_learnings") + "rotate_learnings\n"

PREAMBLE = ("# Learnings — demo\n\nRules:\n\n- **One dated bullet per entry.** a rule bullet, not an entry\n"
            "\n---\n\n<!-- entries below, newest last -->\n\n")


def bullet(i, size=1000):
    body = ("lesson %d " % i) * (size // 8 + 1)
    return "- 2026-08-%02d: **entry %d** %s\n  continuation of entry %d\n\n" % (i % 28 + 1, i, body[:size], i)


def section(i, size=1000):
    return ("## 2026-09-%02d entry %d\n\n- a bullet inside section %d\n- another\n\n%s\n\n"
            % (i % 28 + 1, i, i, ("words %d " % i) * (size // 8)))


class Rotation(unittest.TestCase):
    """The shipped function, run in a scratch tree."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pk-learn-"))
        (self.root / "docs").mkdir()
        (self.root / "artifacts").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def rotate(self):
        env = {**os.environ, "ROOT_DIR": str(self.root), "ARTIFACTS_DIR": str(self.root / "artifacts")}
        r = subprocess.run(["bash", "-c", ROTATE], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stderr

    def active(self):
        return (self.root / "docs" / "LEARNINGS.md").read_bytes()

    def archive(self, name="LEARNINGS-ARCHIVE.md"):
        p = self.root / "docs" / name
        return p.read_bytes() if p.exists() else None

    def put(self, text, name="LEARNINGS.md"):
        (self.root / "docs" / name).write_bytes(text.encode())

    def assert_conserved(self, original, entries, archive_before=None):
        """active + the archive's appended block == the original, entry for entry."""
        active, archive = self.active().decode(), self.archive().decode()
        prefix = archive_before if archive_before is not None else archive[:archive.index("\n" + BATCH)]
        self.assertTrue(archive.startswith(prefix))
        appended = archive[len(prefix):]
        self.assertTrue(appended.startswith("\n" + BATCH), appended[:200])
        moved = appended[len("\n" + BATCH):]
        # every original entry is whole in exactly one of the two files, in order
        k = 0
        while "".join(entries[:k]) != moved:
            k += 1
            self.assertLessEqual(k, len(entries), "the moved block is not a run of whole leading entries")
        self.assertEqual(original.replace(moved, "", 1), active)
        self.assertTrue(active.endswith("".join(entries[k:])))
        return k

    def test_a_bullet_file_moves_whole_oldest_entries_until_within_budget(self):
        entries = [bullet(i) for i in range(30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        out = self.rotate()
        self.assertIn("learnings budget", out)
        self.assertLessEqual(len(self.active()), BUDGET)
        k = self.assert_conserved(original, entries)
        self.assertGreater(k, 0)
        # it moved no more than needed: putting the last moved entry back would exceed the budget
        self.assertGreater(len(self.active()) + len(entries[k - 1].encode()), BUDGET)
        self.assertTrue(self.active().decode().startswith(PREAMBLE), "the title/preamble never moves")
        self.assertIn("entry 29", self.active().decode(), "the newest entry stays")
        self.assertNotIn("**entry 0**", self.active().decode())

    def test_a_sectioned_file_moves_whole_sections(self):
        entries = [section(i) for i in range(30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertLessEqual(len(self.active()), BUDGET)
        k = self.assert_conserved(original, entries)
        self.assertGreater(k, 0)
        archive = self.archive().decode()
        self.assertIn("- a bullet inside section 0\n- another\n", archive, "a section's bullets ride with it")

    def test_a_file_within_budget_is_untouched_and_no_archive_appears(self):
        original = PREAMBLE + "".join(bullet(i) for i in range(5))
        self.put(original)
        st = os.stat(self.root / "docs" / "LEARNINGS.md")
        self.assertEqual(self.rotate(), "")
        self.assertEqual(self.active().decode(), original)
        self.assertEqual(os.stat(self.root / "docs" / "LEARNINGS.md").st_mtime_ns, st.st_mtime_ns)
        self.assertIsNone(self.archive())

    def test_exactly_at_budget_is_within_it(self):
        head = PREAMBLE + bullet(1)
        pad = BUDGET - len(head.encode()) - len("- last\n")
        original = head + "- last " + "x" * (pad - 1) + "\n"
        self.assertEqual(len(original.encode()), BUDGET)
        self.put(original)
        self.rotate()
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_idempotent_a_second_run_changes_nothing(self):
        self.put(PREAMBLE + "".join(bullet(i) for i in range(30)))
        self.rotate()
        a1, r1 = self.active(), self.archive()
        self.assertEqual(self.rotate(), "")
        self.assertEqual((self.active(), self.archive()), (a1, r1))

    def test_deterministic_the_same_input_gives_the_same_bytes(self):
        text = PREAMBLE + "".join(section(i) for i in range(25))
        results = []
        for _ in range(2):
            self.put(text)
            p = self.root / "docs" / "LEARNINGS-ARCHIVE.md"
            if p.exists():
                p.unlink()
            self.rotate()
            results.append((self.active(), self.archive()))
        self.assertEqual(results[0], results[1])

    def test_an_existing_archive_is_appended_at_its_bottom_byte_for_byte(self):
        before = "# Learnings archive\n\nold history\n- 2026-07-01: an old retired entry"   # no trailing newline
        self.put(before, "LEARNINGS-ARCHIVE.md")
        entries = [bullet(i) for i in range(30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assert_conserved(original, entries, archive_before=before + "\n")

    def test_a_case_variant_archive_is_the_archive(self):
        # xmeo-v3's archive is docs/LEARNINGS-archive.md: no second archive appears
        self.put("# Learnings archive — xmeo\n\n## Iteration 1\n\n- old\n", "LEARNINGS-archive.md")
        self.put(PREAMBLE + "".join(bullet(i) for i in range(30)))
        self.rotate()
        names = sorted(os.listdir(self.root / "docs"))
        self.assertIn(names, (["LEARNINGS-archive.md", "LEARNINGS.md"],
                              ["LEARNINGS-ARCHIVE.md", "LEARNINGS.md"]))   # a case-insensitive fs: one file
        self.assertIn("**entry 0**", self.archive(names[0]).decode())

    def test_a_kill_between_the_two_writes_never_duplicates_on_the_rerun(self):
        entries = [bullet(i) for i in range(30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive()
        # the kill: the archive was written, the active file was not
        self.put(original)
        self.rotate()
        self.assertEqual(self.archive(), archive_once, "the rerun recognised its own block")
        self.assert_conserved(original, entries)

    def test_the_seeded_archive_and_its_preamble(self):
        self.put(PREAMBLE + "".join(bullet(i) for i in range(30)))
        self.rotate()
        archive = self.archive().decode()
        self.assertTrue(archive.startswith("# Learnings archive\n"))
        self.assertIn("Cold history", archive)

    def test_no_marker_falls_back_to_the_first_rule_then_to_the_title(self):
        # foundry-dashboard's shape: a `---` but no marker; its rule bullets stay
        pre = "# Learnings — dash\n\n- a rule bullet above the rule\n\n---\n\n"
        entries = [bullet(i) for i in range(30)]
        self.put(pre + "".join(entries))
        self.rotate()
        self.assertTrue(self.active().decode().startswith(pre))
        self.assert_conserved(pre + "".join(entries), entries)
        # neither marker nor rule: only the title is kept
        (self.root / "docs" / "LEARNINGS-ARCHIVE.md").unlink()
        pre2 = "# Learnings\n"
        self.put(pre2 + "".join(entries))
        self.rotate()
        self.assertTrue(self.active().decode().startswith(pre2))
        self.assert_conserved(pre2 + "".join(entries), entries)

    def test_a_code_fence_never_starts_an_entry(self):
        fenced = ("- 2026-08-01: **fenced entry** with a fence\n\n```\n- not an entry\n## not a section\n```\n"
                  + "x" * 3000 + "\n\n")
        entries = [fenced] + [bullet(i) for i in range(1, 30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assert_conserved(original, entries)
        self.assertIn("```\n- not an entry\n## not a section\n```", self.archive().decode())

    def test_one_huge_entry_is_kept_and_the_file_is_left_as_it_is(self):
        original = PREAMBLE + bullet(1, size=BUDGET + 500)
        self.put(original)
        out = self.rotate()
        self.assertIn("nothing moved", out)
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_the_newest_entry_never_moves_even_when_it_alone_is_over_budget(self):
        entries = [bullet(i) for i in range(5)] + [bullet(99, size=BUDGET + 10)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertEqual(self.active().decode(), PREAMBLE + entries[-1])
        self.assert_conserved(original, entries)

    def test_a_file_without_a_trailing_newline_and_non_utf8_bytes_are_conserved(self):
        entries = [bullet(i) for i in range(29)] + ["- 2026-09-30: last entry, no newline"]
        raw = (PREAMBLE + "".join(entries)).encode() + b" \xff\xfe"
        (self.root / "docs" / "LEARNINGS.md").write_bytes(raw)
        self.rotate()
        active = self.active()
        self.assertTrue(active.endswith(b"last entry, no newline \xff\xfe"))
        moved = self.archive()[self.archive().index(BATCH.encode()) + len(BATCH):]
        self.assertEqual(raw.replace(moved, b"", 1), active)

    def test_a_kill_then_a_session_append_appends_only_the_rest(self):
        # review #4: the archive got the batch, the kill came before the active
        # write, then a session appended — the rerun moves one entry more
        entries = [bullet(i) for i in range(30)]
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive().decode()
        newest = bullet(30, size=3000)
        self.put(original + newest)
        self.rotate()
        archive = self.archive().decode()
        self.assertEqual(archive.count("**entry 0**"), 1, "no entry archived twice")
        self.assertEqual(archive.count(BATCH), 1, "one batch, extended")
        self.assertTrue(archive.startswith(archive_once))
        self.assert_conserved(original + newest, entries + [newest])

    def test_a_heading_below_a_bullet_file_does_not_freeze_its_bullets(self):
        # review #5: bullets first, then a `## ` heading — every bullet and the
        # heading start entries; `* ` bullets count too
        entries = [bullet(i).replace("- ", "* ", 1) if i % 2 else bullet(i) for i in range(30)]
        entries.append("## 2026-10-01 a late section\n\n- its own bullet\n")
        original = PREAMBLE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertLessEqual(len(self.active()), BUDGET)
        self.assertGreater(self.assert_conserved(original, entries), 10)

    def test_yaml_front_matter_is_preamble(self):
        # review #6
        pre = "---\ntitle: learnings\ntags:\n- a\n- b\n---\n# Learnings\n\n"
        entries = [bullet(i) for i in range(30)]
        self.put(pre + "".join(entries))
        self.rotate()
        self.assertTrue(self.active().decode().startswith(pre))
        self.assert_conserved(pre + "".join(entries), entries)

    def test_a_symlinked_archive_is_left_alone(self):
        # review #8: os.replace would turn the link into a plain file
        target = self.root / "elsewhere.md"
        target.write_text("# elsewhere\n")
        os.symlink(target, self.root / "docs" / "LEARNINGS-ARCHIVE.md")
        original = PREAMBLE + "".join(bullet(i) for i in range(30))
        self.put(original)
        self.assertIn("symlink; nothing moved", self.rotate())
        self.assertEqual(self.active().decode(), original)
        self.assertEqual(target.read_text(), "# elsewhere\n")

    def test_an_ignored_archive_path_moves_nothing(self):
        # review #9: git add -A would skip it, and the moved entries would never be committed
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("docs/LEARNINGS-ARCHIVE.md\n")
        original = PREAMBLE + "".join(bullet(i) for i in range(30))
        self.put(original)
        self.assertIn("ignored by git", self.rotate())
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_a_python_failure_is_fail_open(self):
        original = PREAMBLE + "".join(bullet(i) for i in range(30))
        self.put(original)
        (self.root / "docs" / "LEARNINGS-ARCHIVE.md").mkdir()   # the archive cannot be written
        out = self.rotate()
        self.assertIn("not rotated (nothing moved)", out)
        self.assertEqual(self.active().decode(), original)
        scratch = self.root / "artifacts" / "scratch"
        self.assertEqual(os.listdir(scratch) if scratch.exists() else [], [], "no temp file left behind")


# --- the close-out ordering: the whole loop, a stub model ------------------

# Logs every run; RED when the active file is over budget — so a green commit
# proves the gate judged the ROTATED file.
VERIFY_BUDGET = """#!/usr/bin/env bash
PHASEKIT_VERIFY_CONFIGURED=1
echo run >> "${STUB_DIR:?}/verify-calls"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/docs/LEARNINGS.md" ] && [ "$(wc -c < "$ROOT/docs/LEARNINGS.md")" -gt %d ]; then
  echo "LEARNINGS over budget at gate time"; exit 1
fi
exit 0
""" % BUDGET

APPEND_LESSON = "printf -- '- 2026-10-14: **the newest lesson** the session learned\\n' >> docs/LEARNINGS.md\n"


class CloseOutOrdering(unittest.TestCase):

    def _repo(self, squash=False, pre_entries=None):
        repo = H.Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BUDGET, executable=True)
        # just under budget at the start: the session's own append tips it over
        entries = pre_entries or [bullet(i) for i in range(40)]
        text = PREAMBLE
        for e in entries:
            if len((text + e).encode()) > BUDGET - 200:
                break
            text += e
        # pad with a filler entry to exactly BUDGET - 20: the session's append tips it over
        filler = "- 2026-10-01: filler "
        text += filler + "f" * (BUDGET - 20 - len((text + filler).encode()) - 1) + "\n"
        self.assertEqual(len(text.encode()), BUDGET - 20)
        repo.write("docs/LEARNINGS.md", text)
        if squash:
            repo.git("checkout", "-q", "main")
        repo.git("add", "-A")
        repo.git("commit", "-qm", "learnings")
        if squash:
            repo.git("checkout", "-q", "-B", "iter/1-test", "main")
        return repo, text

    def test_learnings_write_then_rotation_then_gate_then_the_phase_commit(self):
        for squash in (False, True):
            with self.subTest(squash=squash):
                repo, before = self._repo(squash=squash)
                repo.scenario(APPEND_LESSON + H.APPROVE_SCENARIO)
                r = repo.run(env={"MAX_ITERATIONS": "1"})
                out = r.stdout + r.stderr
                self.assertIn("learnings budget — docs/LEARNINGS.md was", out)
                self.assertIn("boundary-state: rested (step 7)", out, out[-2000:])
                ref = "main"
                active = repo.git("show", f"{ref}:docs/LEARNINGS.md")
                archive = repo.git("show", f"{ref}:docs/LEARNINGS-ARCHIVE.md")
                self.assertIn("the newest lesson", active, "the session's write is kept, newest")
                self.assertIn("**entry 0**", archive, "the oldest entry was archived")
                self.assertNotIn("**entry 0**", active)
                self.assertLessEqual(len(active.encode()) + 1, BUDGET)
                # one commit carries both: the phase's own landing
                touching = repo.git("log", "--format=%s", f"{repo.base}..{ref}", "--",
                                    "docs/LEARNINGS-ARCHIVE.md").splitlines()
                self.assertEqual(len(touching), 1, touching)
                self.assertIn("built it", touching[0])
                self.assertEqual([p for p in repo.porcelain() if "LEARNINGS" in p], [])

    def test_a_checkpoint_never_rotates(self):
        repo, _ = self._repo()
        repo.scenario(APPEND_LESSON + "echo more >> src.txt\n"
                      "jq -n '{summary: \"not done yet\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotIn("learnings budget — docs/LEARNINGS.md was", out)
        self.assertFalse((repo.repo / "docs" / "LEARNINGS-ARCHIVE.md").exists())

    def test_phasekit_verify_rotates_and_the_commit_gate_reuses_its_verdict(self):
        repo, _ = self._repo()
        repo.write("scripts/phasekit-verify.sh", VERIFY_BUDGET, executable=True)
        IF.with_cli(repo)
        repo.scenario(APPEND_LESSON + H.APPROVE_SCENARIO
                      + IF.PV + ' > "$STUB_DIR/pv.out" 2>&1; echo $? > "$STUB_DIR/pv.rc"\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        pv = (repo.stub / "pv.out").read_text()
        self.assertEqual((repo.stub / "pv.rc").read_text().strip(), "0", pv)
        self.assertIn("learnings budget — docs/LEARNINGS.md was", pv)
        self.assertEqual(repo.verify_calls(), 1, "the rotation is idempotent, so the memo covers the commit\n" + out)
        self.assertIn("reusing the green verdict recorded for this exact tree", out)
        self.assertIn("**entry 0**", repo.git("show", "HEAD:docs/LEARNINGS-ARCHIVE.md"))

    def test_the_credential_scan_refuses_an_archive(self):
        key = "AKIA" + "Q" * 16
        entries = [bullet(0).replace("continuation of entry 0", "pasted " + key)] + [bullet(i) for i in range(1, 40)]
        repo, _ = self._repo(pre_entries=entries)   # committed by hand, as a legacy file can be
        repo.scenario(APPEND_LESSON + H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — docs/LEARNINGS-ARCHIVE.md matches a credential pattern", out)
        self.assertEqual(repo.git("log", "-1", "--format=%s"), "learnings", "nothing landed")
        self.assertFalse(repo.tracked("docs/LEARNINGS-ARCHIVE.md"))


class Wording(unittest.TestCase):
    """The prompt no longer asks for judgment curation, and never sends a session to the archive."""

    def test_the_prompt_and_template_describe_the_rotation_honestly(self):
        prompt = (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text()
        template = (REPO_ROOT / "templates" / "learnings.template.md").read_text()
        self.assertIn("Read it EARLY", prompt)
        self.assertNotIn("~150 lines", prompt)
        self.assertNotIn("~150 lines", template)
        for text in (prompt, template):
            self.assertIn("docs/LEARNINGS-ARCHIVE.md", text)
            self.assertNotRegex(text, r"(?i)read (the|docs/LEARNINGS-ARCHIVE)[^.\n]*archive")
        self.assertIn("archived, not deleted", prompt)
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        for text in (prompt, template, gates):
            self.assertIn("%d KiB" % (BUDGET // 1024), text)
        self.assertIn("(%d bytes)" % BUDGET, gates)


class WrapUpLanding(unittest.TestCase):
    """review #3: a wrap-up that lands an approval-class record rotates like a landing."""

    def test_the_wrap_up_rotates_before_it_stages_an_unlanded_record(self):
        body = fn("wrapup_commit")
        self.assertIn("rotate_learnings", body)
        self.assertLess(body.index("rotate_learnings"), body.index("\n  stage_all\n"))
        guard = body[:body.index("rotate_learnings")]
        self.assertIn('artifact_never_landed "$ARTIFACTS_DIR/phase-approval.json"', guard)
        self.assertIn('artifact_never_landed "$ARTIFACTS_DIR/project-complete.json"', guard)


if __name__ == "__main__":
    unittest.main()
