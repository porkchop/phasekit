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

v0.19.6 (Aaron 2026-10-14): a file whose preamble carries
`<!-- phasekit: learnings priority-ordered -->` is kept most important first
and loses its LOWEST-RANKED (bottom) entries instead; a file without it is
chronological and rotates oldest-first exactly as v0.19.5. Every rotation
test runs in both orders (Rotation = chronological, PriorityRotation =
marked), and the close-out ordering runs in both too.

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
BATCH_P = "## Moved from docs/LEARNINGS.md by the phasekit loop (lowest-ranked)\n\n"
MARKER = "<!-- phasekit: learnings priority-ordered -->"


def fn(name):
    start = SRC.index("\n" + name + "() {\n") + 1
    return SRC[start:SRC.index("\n}\n", start) + 3]


ROTATE = "set -euo pipefail\nLEARNINGS_BUDGET_BYTES=%d\n" % BUDGET + fn("rotate_learnings") + "rotate_learnings\n"

PREAMBLE = ("# Learnings — demo\n\nRules:\n\n- **One dated bullet per entry.** a rule bullet, not an entry\n"
            "\n---\n\n<!-- entries below, newest last -->\n\n")
PREAMBLE_P = ("# Learnings — demo\n\n" + MARKER + "\n\nRules:\n\n"
              "- **One dated bullet per entry.** a rule bullet, not an entry\n"
              "\n---\n\n<!-- entries below, most important first -->\n\n")


def mark(pre):
    """Put the marker into a preamble, after its title (or at its end)."""
    m = re.search(r"^# [^\n]*\n", pre, re.M)
    at = m.end() if m else len(pre)
    return pre[:at] + MARKER + "\n" + pre[at:]


def bullet(i, size=1000):
    body = ("lesson %d " % i) * (size // 8 + 1)
    return "- 2026-08-%02d: **entry %d** %s\n  continuation of entry %d\n\n" % (i % 28 + 1, i, body[:size], i)


def section(i, size=1000):
    return ("## 2026-09-%02d entry %d\n\n- a bullet inside section %d\n- another\n\n%s\n\n"
            % (i % 28 + 1, i, i, ("words %d " % i) * (size // 8)))


class Rotation(unittest.TestCase):
    """The shipped function, run in a scratch tree — a chronological file (no marker).

    PriorityRotation reruns every test here on a marked file; the helpers
    below carry the one difference (which end of the entry list moves)."""

    PRIORITY = False

    @property
    def PRE(self):
        return PREAMBLE_P if self.PRIORITY else PREAMBLE

    @property
    def BATCH(self):
        return BATCH_P if self.PRIORITY else BATCH

    def pre(self, pre):
        return mark(pre) if self.PRIORITY else pre

    def first_to_move(self, entries):
        """The entry the rotation archives first: the oldest, or the lowest-ranked."""
        return entries[-1] if self.PRIORITY else entries[0]

    def never_moves(self, entries):
        """The newest entry, or the top-ranked one."""
        return entries[0] if self.PRIORITY else entries[-1]

    def arrange(self, special, rest):
        """Entries with `special` where the rotation reaches it first."""
        return rest + [special] if self.PRIORITY else [special] + rest

    def add_new(self, entries, new):
        """Where a session writes a new, important lesson."""
        return [new] + entries if self.PRIORITY else entries + [new]

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
        batch = self.BATCH
        prefix = archive_before if archive_before is not None else archive[:archive.index("\n" + batch)]
        self.assertTrue(archive.startswith(prefix))
        appended = archive[len(prefix):]
        self.assertTrue(appended.startswith("\n" + batch), appended[:200])
        moved = appended[len("\n" + batch):]
        # every original entry is whole in exactly one of the two files, in order
        n, k = len(entries), 0
        if self.PRIORITY:
            while "".join(entries[n - k:]) != moved:
                k += 1
                self.assertLessEqual(k, n, "the moved block is not a run of whole trailing entries")
            self.assertTrue(original.endswith(moved))
            self.assertEqual(original[:len(original) - len(moved)], active)
            self.assertTrue(active.endswith("".join(entries[n - k - 1:n - k])))
        else:
            while "".join(entries[:k]) != moved:
                k += 1
                self.assertLessEqual(k, n, "the moved block is not a run of whole leading entries")
            self.assertEqual(original.replace(moved, "", 1), active)
            self.assertTrue(active.endswith("".join(entries[k:])))
        return k

    def moved_last(self, entries, k):
        """The last entry the rotation moved (putting it back would exceed the budget)."""
        return entries[len(entries) - k] if self.PRIORITY else entries[k - 1]

    def test_a_bullet_file_moves_whole_entries_until_within_budget(self):
        entries = [bullet(i) for i in range(30)]
        original = self.PRE + "".join(entries)
        self.put(original)
        out = self.rotate()
        self.assertIn("learnings budget", out)
        self.assertIn("lowest-ranked" if self.PRIORITY else "oldest", out)
        self.assertLessEqual(len(self.active()), BUDGET)
        k = self.assert_conserved(original, entries)
        self.assertGreater(k, 0)
        # it moved no more than needed: putting the last moved entry back would exceed the budget
        self.assertGreater(len(self.active()) + len(self.moved_last(entries, k).encode()), BUDGET)
        self.assertTrue(self.active().decode().startswith(self.PRE), "the title/preamble never moves")
        self.assertIn(self.never_moves(entries), self.active().decode(), "the newest / top entry stays")
        self.assertNotIn(self.first_to_move(entries), self.active().decode())

    def test_a_sectioned_file_moves_whole_sections(self):
        entries = [section(i) for i in range(30)]
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertLessEqual(len(self.active()), BUDGET)
        k = self.assert_conserved(original, entries)
        self.assertGreater(k, 0)
        archive = self.archive().decode()
        i = 29 if self.PRIORITY else 0
        self.assertIn("- a bullet inside section %d\n- another\n" % i, archive, "a section's bullets ride with it")

    def test_a_file_within_budget_is_untouched_and_no_archive_appears(self):
        original = self.PRE + "".join(bullet(i) for i in range(5))
        self.put(original)
        st = os.stat(self.root / "docs" / "LEARNINGS.md")
        self.assertEqual(self.rotate(), "")
        self.assertEqual(self.active().decode(), original)
        self.assertEqual(os.stat(self.root / "docs" / "LEARNINGS.md").st_mtime_ns, st.st_mtime_ns)
        self.assertIsNone(self.archive())

    def test_exactly_at_budget_is_within_it(self):
        head = self.PRE + bullet(1)
        pad = BUDGET - len(head.encode()) - len("- last\n")
        original = head + "- last " + "x" * (pad - 1) + "\n"
        self.assertEqual(len(original.encode()), BUDGET)
        self.put(original)
        self.rotate()
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_idempotent_a_second_run_changes_nothing(self):
        self.put(self.PRE + "".join(bullet(i) for i in range(30)))
        self.rotate()
        a1, r1 = self.active(), self.archive()
        self.assertEqual(self.rotate(), "")
        self.assertEqual((self.active(), self.archive()), (a1, r1))

    def test_deterministic_the_same_input_gives_the_same_bytes(self):
        text = self.PRE + "".join(section(i) for i in range(25))
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
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assert_conserved(original, entries, archive_before=before + "\n")

    def test_a_case_variant_archive_is_the_archive(self):
        # xmeo-v3's archive is docs/LEARNINGS-archive.md: no second archive appears
        self.put("# Learnings archive — xmeo\n\n## Iteration 1\n\n- old\n", "LEARNINGS-archive.md")
        entries = [bullet(i) for i in range(30)]
        self.put(self.PRE + "".join(entries))
        self.rotate()
        names = sorted(os.listdir(self.root / "docs"))
        self.assertIn(names, (["LEARNINGS-archive.md", "LEARNINGS.md"],
                              ["LEARNINGS-ARCHIVE.md", "LEARNINGS.md"]))   # a case-insensitive fs: one file
        self.assertIn(self.first_to_move(entries), self.archive(names[0]).decode())

    def test_a_kill_between_the_two_writes_never_duplicates_on_the_rerun(self):
        entries = [bullet(i) for i in range(30)]
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive()
        # the kill: the archive was written, the active file was not
        self.put(original)
        self.rotate()
        self.assertEqual(self.archive(), archive_once, "the rerun recognised its own block")
        self.assert_conserved(original, entries)

    def test_the_seeded_archive_and_its_preamble(self):
        self.put(self.PRE + "".join(bullet(i) for i in range(30)))
        self.rotate()
        archive = self.archive().decode()
        self.assertTrue(archive.startswith("# Learnings archive\n"))
        self.assertIn("Cold history", archive)
        self.assertNotIn("read this file.\n", archive.replace("asked to read this file.\n", ""))

    def test_no_marker_falls_back_to_the_first_rule_then_to_the_title(self):
        # foundry-dashboard's shape: a `---` but no entries-below line; its rule bullets stay
        pre = self.pre("# Learnings — dash\n\n- a rule bullet above the rule\n\n---\n\n")
        entries = [bullet(i) for i in range(30)]
        self.put(pre + "".join(entries))
        self.rotate()
        self.assertTrue(self.active().decode().startswith(pre))
        self.assert_conserved(pre + "".join(entries), entries)
        # neither entries-below line nor rule: only the title (and the order line) are kept
        (self.root / "docs" / "LEARNINGS-ARCHIVE.md").unlink()
        pre2 = self.pre("# Learnings\n")
        self.put(pre2 + "".join(entries))
        self.rotate()
        self.assertTrue(self.active().decode().startswith(pre2))
        self.assert_conserved(pre2 + "".join(entries), entries)

    def test_a_code_fence_never_starts_an_entry(self):
        fenced = ("- 2026-08-01: **fenced entry** with a fence\n\n```\n- not an entry\n## not a section\n```\n"
                  + "x" * 3000 + "\n\n")
        entries = self.arrange(fenced, [bullet(i) for i in range(1, 30)])
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assert_conserved(original, entries)
        self.assertIn("```\n- not an entry\n## not a section\n```", self.archive().decode())

    def test_one_huge_entry_is_kept_and_the_file_is_left_as_it_is(self):
        original = self.PRE + bullet(1, size=BUDGET + 500)
        self.put(original)
        out = self.rotate()
        self.assertIn("nothing moved", out)
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_the_protected_entry_never_moves_even_when_it_alone_is_over_budget(self):
        # chronological: the newest (last); priority: the top-ranked (first)
        entries = self.arrange(bullet(99, size=BUDGET + 10), [bullet(i) for i in range(5)])
        entries.reverse()
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertEqual(self.active().decode(), self.PRE + self.never_moves(entries))
        self.assertIn("**entry 99**", self.never_moves(entries))
        self.assert_conserved(original, entries)

    def test_a_file_without_a_trailing_newline_and_non_utf8_bytes_are_conserved(self):
        entries = [bullet(i) for i in range(29)] + ["- 2026-09-30: last entry, no newline"]
        raw = (self.PRE + "".join(entries)).encode() + b" \xff\xfe"
        (self.root / "docs" / "LEARNINGS.md").write_bytes(raw)
        self.rotate()
        active, archive = self.active(), self.archive()
        moved = archive[archive.index(self.BATCH.encode()) + len(self.BATCH):]
        self.assertEqual(raw.replace(moved, b"", 1), active)
        # the unterminated, non-UTF-8 last entry stays (chronological: newest)
        # or is archived byte for byte (priority: lowest-ranked)
        holder = archive if self.PRIORITY else active
        self.assertTrue(holder.endswith(b"last entry, no newline \xff\xfe"))

    def test_a_kill_then_a_session_write_moves_only_the_rest(self):
        # review #4: the archive got the batch, the kill came before the active
        # write, then a session wrote a new lesson (at the end, or at the top of
        # a priority file) — the rerun moves one entry more and archives nothing twice
        entries = [bullet(i) for i in range(30)]
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive().decode()
        newest = bullet(30, size=3000)
        entries2 = self.add_new(entries, newest)
        original2 = self.PRE + "".join(entries2)
        self.put(original2)
        self.rotate()
        archive = self.archive().decode()
        active = self.active().decode()
        for e in entries2:   # every entry whole in exactly one file
            self.assertEqual(archive.count(e) + active.count(e), 1, e[:40])
        self.assertEqual(archive.count(self.BATCH), 1, "one batch, extended")
        self.assertTrue(archive.startswith(archive_once))
        self.assertIn(newest, active)
        moved = archive[archive.index("\n" + self.BATCH) + len("\n" + self.BATCH):]
        self.assertEqual(len(active.encode()) + len(moved.encode()), len(original2.encode()),
                         "byte conservation")
        self.assertLessEqual(len(active.encode()), BUDGET)

    def _kill_then(self, edit):
        """Rotate, simulate the kill (active restored), let `edit` change the
        entries, rerun; every entry must be whole in exactly one place."""
        entries = [bullet(i) for i in range(30)]
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive().decode()
        entries2 = edit(list(entries))
        self.put(self.PRE + "".join(entries2))
        self.rotate()
        archive, active = self.archive().decode(), self.active().decode()
        self.assertTrue(archive.startswith(archive_once))
        for e in entries2:   # nothing lost, nothing archived twice
            self.assertGreaterEqual(archive.count(e) + active.count(e), 1, e[:40])
            self.assertLessEqual(archive.count(e), 1, "archived twice: " + e[:40])
            self.assertLessEqual(active.count(e), 1, e[:40])
        self.assertLessEqual(len(active.encode()), BUDGET)
        return archive, active

    def test_review_kill_then_a_new_entry_at_the_bottom_never_duplicates(self):
        # v0.19.6 review #1: a lowest-ranked (priority) / newest (chronological) entry
        self._kill_then(lambda es: es + [bullet(40, size=500)])

    def test_review_kill_then_an_insert_in_the_middle_never_duplicates(self):
        self._kill_then(lambda es: es[:15] + [bullet(41, size=500)] + es[15:])

    def test_review_kill_then_a_tightened_entry_never_duplicates_the_rest(self):
        def tighten(es):
            i = 29 if self.PRIORITY else 0     # an entry the first run archived
            es[i] = es[i].replace("continuation", "tightened", 1)
            return es
        archive, _ = self._kill_then(tighten)
        self.assertEqual(archive.count(self.BATCH), 1, "the block extended, not repeated")

    def test_review_kill_with_an_unterminated_last_entry_never_duplicates(self):
        # v0.19.6 review #2: in priority order the unterminated last entry moves
        entries = [bullet(i) for i in range(29)] + ["- 2026-09-30: last entry, no newline"]
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        archive_once = self.archive()
        self.put(original)       # the kill
        self.rotate()
        self.assertEqual(self.archive(), archive_once)
        self.assertEqual(self.archive().count(b"last entry, no newline"),
                         1 if self.PRIORITY else 0)

    def test_review_a_mode_switch_between_kill_and_rerun_never_duplicates(self):
        # v0.19.6 review #4: the batch heading changed under the rerun
        entries = [bullet(i) for i in range(30)]
        self.put(self.PRE + "".join(entries))
        self.rotate()
        other = PREAMBLE if self.PRIORITY else PREAMBLE_P
        self.put(other + "".join(entries))     # the kill, then the opt-in (or out)
        self.rotate()
        archive = self.archive().decode()
        for e in entries:
            self.assertLessEqual(archive.count(e), 1, e[:40])

    def test_a_heading_below_a_bullet_file_does_not_freeze_its_bullets(self):
        # review #5: bullets first, then a `## ` heading — every bullet and the
        # heading start entries; `* ` bullets count too
        entries = [bullet(i).replace("- ", "* ", 1) if i % 2 else bullet(i) for i in range(30)]
        entries.append("## 2026-10-01 a late section\n\n- its own bullet\n")
        original = self.PRE + "".join(entries)
        self.put(original)
        self.rotate()
        self.assertLessEqual(len(self.active()), BUDGET)
        self.assertGreater(self.assert_conserved(original, entries), 10)

    def test_yaml_front_matter_is_preamble(self):
        # review #6
        pre = "---\ntitle: learnings\ntags:\n- a\n- b\n---\n" + self.pre("# Learnings\n\n")
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
        original = self.PRE + "".join(bullet(i) for i in range(30))
        self.put(original)
        self.assertIn("symlink; nothing moved", self.rotate())
        self.assertEqual(self.active().decode(), original)
        self.assertEqual(target.read_text(), "# elsewhere\n")

    def test_an_ignored_archive_path_moves_nothing(self):
        # review #9: git add -A would skip it, and the moved entries would never be committed
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("docs/LEARNINGS-ARCHIVE.md\n")
        original = self.PRE + "".join(bullet(i) for i in range(30))
        self.put(original)
        self.assertIn("ignored by git", self.rotate())
        self.assertEqual(self.active().decode(), original)
        self.assertIsNone(self.archive())

    def test_a_python_failure_is_fail_open(self):
        original = self.PRE + "".join(bullet(i) for i in range(30))
        self.put(original)
        (self.root / "docs" / "LEARNINGS-ARCHIVE.md").mkdir()   # the archive cannot be written
        out = self.rotate()
        self.assertIn("not rotated (nothing moved)", out)
        self.assertEqual(self.active().decode(), original)
        scratch = self.root / "artifacts" / "scratch"
        self.assertEqual(os.listdir(scratch) if scratch.exists() else [], [], "no temp file left behind")


class PriorityRotation(Rotation):
    """Every Rotation test on a priority-ordered file (the marker in its preamble)."""

    PRIORITY = True


class Marker(unittest.TestCase):
    """Which order a file is in: the marker counts only in the preamble, outside a fence."""

    def setUp(self):
        self.r = Rotation("test_a_python_failure_is_fail_open")   # its scratch tree and helpers
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.entries = [bullet(i) for i in range(30)]

    def order_of(self, text):
        self.r.put(text)
        out = self.r.rotate()
        if "lowest-ranked" in out:
            self.assertIn(BATCH_P, self.r.archive().decode())
            return "priority"
        self.assertIn("oldest", out)
        self.assertIn(BATCH, self.r.archive().decode())
        return "chronological"

    def test_an_unmarked_file_rotates_oldest_first_exactly_as_v0195(self):
        # the regression pin: v0.19.5's batch, seed and choice, byte for byte
        original = PREAMBLE + "".join(self.entries)
        self.assertEqual(self.order_of(original), "chronological")
        active, archive = self.r.active().decode(), self.r.archive().decode()
        k = 0
        while len((PREAMBLE + "".join(self.entries[k:])).encode()) > BUDGET:
            k += 1
        self.assertEqual(active, PREAMBLE + "".join(self.entries[k:]))
        self.assertEqual(archive, "# Learnings archive\n\nEntries the phasekit loop moved out of "
                         "`docs/LEARNINGS.md`, whole and\noldest first, to keep that file within its "
                         "budget. Cold history: no session\nis asked to read this file.\n"
                         "\n" + BATCH + "".join(self.entries[:k]))

    def test_a_marked_file_rotates_from_the_bottom(self):
        original = PREAMBLE_P + "".join(self.entries)
        self.assertEqual(self.order_of(original), "priority")
        active, archive = self.r.active().decode(), self.r.archive().decode()
        m = len(self.entries)
        while len((PREAMBLE_P + "".join(self.entries[:m])).encode()) > BUDGET:
            m -= 1
        self.assertEqual(active, PREAMBLE_P + "".join(self.entries[:m]))
        self.assertTrue(archive.endswith("\n" + BATCH_P + "".join(self.entries[m:])))
        self.assertIn("lowest-ranked entries of a file kept\nin priority order", archive)

    def test_the_marker_inside_a_code_fence_does_not_count(self):
        pre = "# Learnings\n\n```\n" + MARKER + "\n```\n\n---\n\n"
        self.assertEqual(self.order_of(pre + "".join(self.entries)), "chronological")

    def test_the_marker_inside_an_entry_does_not_count(self):
        entries = list(self.entries)
        entries[3] = entries[3].replace("  continuation", "  " + MARKER + "\n  continuation", 1)
        entries[5] = entries[5] + MARKER + "\n\n"     # a bare line riding with entry 5
        self.assertEqual(self.order_of(PREAMBLE + "".join(entries)), "chronological")

    def test_the_marker_quoted_inline_does_not_count(self):
        pre = "# Learnings\n\nAdd `" + MARKER + "` to opt in.\n\n---\n\n"
        self.assertEqual(self.order_of(pre + "".join(self.entries)), "chronological")

    def test_the_marker_tolerates_inner_whitespace_and_indentation(self):
        pre = "# Learnings\n\n  <!--  phasekit:   learnings   priority-ordered -->\n\n---\n\n"
        self.assertEqual(self.order_of(pre + "".join(self.entries)), "priority")

    def test_the_marker_below_the_entries_below_line_still_counts(self):
        pre = "# Learnings\n\n---\n\n<!-- entries below -->\n" + MARKER + "\n\n"
        self.assertEqual(self.order_of(pre + "".join(self.entries)), "priority")


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
# A priority-ordered file: the session places its (important) lesson at the top.
RANK_LESSON = ("awk '{print} /<!-- entries below/{print \"- 2026-10-14: **the newest lesson** the session "
               "learned\"}' docs/LEARNINGS.md > docs/.l && mv docs/.l docs/LEARNINGS.md\n")


class CloseOutOrdering(unittest.TestCase):
    """The whole loop, chronological; PriorityCloseOutOrdering reruns it on a marked file."""

    PRIORITY = False

    @property
    def LESSON(self):
        return RANK_LESSON if self.PRIORITY else APPEND_LESSON

    def _repo(self, squash=False, pre_entries=None, filler_extra=""):
        repo = H.Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        repo.write("scripts/phasekit-verify.sh", VERIFY_BUDGET, executable=True)
        # just under budget at the start: the session's own write tips it over
        entries = pre_entries or [bullet(i) for i in range(40)]
        text = PREAMBLE_P if self.PRIORITY else PREAMBLE
        for e in entries:
            if len((text + e).encode()) > BUDGET - 200:
                break
            text += e
        # pad with a filler entry (the last: oldest-ranked last, lowest-ranked) to exactly BUDGET - 20
        filler = "- 2026-10-01: filler " + filler_extra
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

    def assert_rotated(self, active, archive):
        self.assertIn("the newest lesson", active, "the session's write is kept")
        if self.PRIORITY:
            self.assertIn("2026-10-01: filler", archive, "the lowest-ranked entry was archived")
            self.assertNotIn("2026-10-01: filler", active)
            self.assertIn("**entry 0**", active, "the top-ranked entries stay")
            self.assertLess(active.index("the newest lesson"), active.index("**entry 0**"))
        else:
            self.assertIn("**entry 0**", archive, "the oldest entry was archived")
            self.assertNotIn("**entry 0**", active)

    def test_learnings_write_then_rotation_then_gate_then_the_phase_commit(self):
        for squash in (False, True):
            with self.subTest(squash=squash):
                repo, before = self._repo(squash=squash)
                repo.scenario(self.LESSON + H.APPROVE_SCENARIO)
                r = repo.run(env={"MAX_ITERATIONS": "1"})
                out = r.stdout + r.stderr
                self.assertIn("learnings budget — docs/LEARNINGS.md was", out)
                self.assertIn("boundary-state: rested (step 7)", out, out[-2000:])
                ref = "main"
                active = repo.git("show", f"{ref}:docs/LEARNINGS.md")
                archive = repo.git("show", f"{ref}:docs/LEARNINGS-ARCHIVE.md")
                self.assert_rotated(active, archive)
                self.assertLessEqual(len(active.encode()) + 1, BUDGET)
                # one commit carries both: the phase's own landing
                touching = repo.git("log", "--format=%s", f"{repo.base}..{ref}", "--",
                                    "docs/LEARNINGS-ARCHIVE.md").splitlines()
                self.assertEqual(len(touching), 1, touching)
                self.assertIn("built it", touching[0])
                self.assertEqual([p for p in repo.porcelain() if "LEARNINGS" in p], [])

    def test_a_checkpoint_never_rotates(self):
        repo, _ = self._repo()
        repo.scenario(self.LESSON + "echo more >> src.txt\n"
                      "jq -n '{summary: \"not done yet\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertNotIn("learnings budget — docs/LEARNINGS.md was", out)
        self.assertFalse((repo.repo / "docs" / "LEARNINGS-ARCHIVE.md").exists())

    def test_phasekit_verify_rotates_and_the_commit_gate_reuses_its_verdict(self):
        repo, _ = self._repo()
        repo.write("scripts/phasekit-verify.sh", VERIFY_BUDGET, executable=True)
        IF.with_cli(repo)
        repo.scenario(self.LESSON + H.APPROVE_SCENARIO
                      + IF.PV + ' > "$STUB_DIR/pv.out" 2>&1; echo $? > "$STUB_DIR/pv.rc"\n')
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        pv = (repo.stub / "pv.out").read_text()
        self.assertEqual((repo.stub / "pv.rc").read_text().strip(), "0", pv)
        self.assertIn("learnings budget — docs/LEARNINGS.md was", pv)
        self.assertEqual(repo.verify_calls(), 1, "the rotation is idempotent, so the memo covers the commit\n" + out)
        self.assertIn("reusing the green verdict recorded for this exact tree", out)
        self.assert_rotated(repo.git("show", "HEAD:docs/LEARNINGS.md"),
                            repo.git("show", "HEAD:docs/LEARNINGS-ARCHIVE.md"))

    def test_the_credential_scan_refuses_an_archive(self):
        key = "AKIA" + "Q" * 16
        if self.PRIORITY:   # the lowest-ranked entry carries it
            repo, _ = self._repo(filler_extra="pasted " + key + " ")
        else:               # the oldest entry carries it
            entries = ([bullet(0).replace("continuation of entry 0", "pasted " + key)]
                       + [bullet(i) for i in range(1, 40)])
            repo, _ = self._repo(pre_entries=entries)   # committed by hand, as a legacy file can be
        repo.scenario(self.LESSON + H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertIn("REFUSED — docs/LEARNINGS-ARCHIVE.md matches a credential pattern", out)
        self.assertEqual(repo.git("log", "-1", "--format=%s"), "learnings", "nothing landed")
        self.assertFalse(repo.tracked("docs/LEARNINGS-ARCHIVE.md"))


class PriorityCloseOutOrdering(CloseOutOrdering):
    """The close-out ordering on a priority-ordered file: the lowest-ranked entries move."""

    PRIORITY = True


class Wording(unittest.TestCase):
    """The prompt describes both orders honestly, and never sends a session to the archive."""

    def test_the_prompt_and_template_describe_the_rotation_honestly(self):
        prompt = (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text()
        template = (REPO_ROOT / "templates" / "learnings.template.md").read_text()
        gates = (REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text()
        self.assertIn("Read it EARLY", prompt)
        for text in (prompt, template):
            self.assertNotIn("~150 lines", text)
            self.assertNotIn("pruning", text, "the stale 'never oldest-first pruning' wording is gone")
            self.assertIn("docs/LEARNINGS-ARCHIVE.md", text)
            self.assertNotRegex(text, r"(?i)read (the|docs/LEARNINGS-ARCHIVE)[^.\n]*archive")
        self.assertIn("archived, not deleted", prompt)
        for text in (prompt, template, gates):
            self.assertIn("%d KiB" % (BUDGET // 1024), text)
        self.assertIn("(%d bytes)" % BUDGET, gates)
        # v0.19.6: both orders, the marker named, the conversion as the way to opt in
        for text in (prompt, gates):
            self.assertIn(MARKER, text)
        self.assertIn("place each new entry where it ranks (most important first); the lowest-ranked "
                      "entries are archived first when the file passes 12 KiB; merging or tightening "
                      "is welcome", prompt)
        self.assertIn("CHRONOLOGICAL", prompt)
        self.assertIn("is how a project opts in", prompt)

    def test_the_template_seeds_the_marker_in_its_preamble(self):
        template = (REPO_ROOT / "templates" / "learnings.template.md").read_text()
        self.assertIn("\n" + MARKER + "\n", template)
        self.assertLess(template.index(MARKER), template.index("<!-- entries below"))
        self.assertIn("<!-- entries below, most important first -->", template)
        self.assertNotIn("newest last", template)


class NewProject(unittest.TestCase):
    """A project initialised with no LEARNINGS.md starts priority-ordered."""

    def test_enrich_seeds_a_priority_ordered_learnings_file(self):
        root = Path(tempfile.mkdtemp(prefix="pk-learn-init-")) / "proj"
        self.addCleanup(shutil.rmtree, root.parent, True)
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        r = subprocess.run(["python3", str(REPO_ROOT / "scripts" / "enrich-project.py"), str(root)],
                           capture_output=True, text=True,
                           env={**os.environ, "PHASEKIT_UPGRADE_VERIFY": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = (root / "docs" / "LEARNINGS.md").read_text()
        self.assertIn("\n" + MARKER + "\n", text)
        self.assertNotIn("{{", text)
        # and the rotation reads it as priority-ordered: pad it over budget
        (root / "artifacts").mkdir(exist_ok=True)
        (root / "docs" / "LEARNINGS.md").write_text(text + "\n" + "".join(bullet(i) for i in range(30)))
        env = {**os.environ, "ROOT_DIR": str(root), "ARTIFACTS_DIR": str(root / "artifacts")}
        out = subprocess.run(["bash", "-c", ROTATE], capture_output=True, text=True, env=env).stderr
        self.assertIn("lowest-ranked", out)
        self.assertIn("**entry 0**", (root / "docs" / "LEARNINGS.md").read_text())


class StartNote(unittest.TestCase):
    """The loop-start size note names the order the rotation will use."""

    def run_note(self, text):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.write("docs/LEARNINGS.md", text + "x" * (48 * 1024))
        repo.git("add", "-A"); repo.git("commit", "-qm", "l")
        r = repo.run(env={"MAX_ITERATIONS": "0"})
        self.assertIn("advisory threshold", r.stderr)
        return r.stderr

    def test_a_chronological_file_is_told_how_to_opt_in(self):
        err = self.run_note("# Learnings\n\n---\n\n")
        self.assertIn("It is chronological: the loop moves its oldest entries", err)
        self.assertIn("add the line " + MARKER, err)

    def test_a_marked_file_is_told_its_bottom_moves(self):
        err = self.run_note("# Learnings\n\n" + MARKER + "\n\n---\n\n")
        self.assertIn("It is priority-ordered: the loop moves its lowest-ranked (bottom) entries", err)


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
