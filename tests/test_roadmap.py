"""The optional docs/ROADMAP.md and its one reader/writer (v0.15.0).

Four families:

* READER — `next` against fixtures, including one built to be harder than any
  real file, plus the CommonMark edge cases a fresh-context review broke the
  first draft with (fences by length and info string, indented headings, BOM,
  HTML comments, needs placement, duplicates, cycles).
* WRITER — `done` flips one mark, keeps the user's line verbatim, moves the
  entry's continuation lines with it, never loses a concurrent update, writes
  through a symlink, refuses a read-only file; `init` never overwrites.
* FAIL DIRECTION — a caller can always tell "nothing is next" (exit 0) from
  "the reader could not look" (exit 5, never an undeclared exit 1).
* NO UNDUE BURDEN — Aaron's hard constraint (2026-08-28): a project that never
  wants a roadmap must notice nothing. Pinned structurally (every profile),
  behaviourally (a real enrich + upgrade, including upgrading a project enriched
  by a pre-roadmap release), and against the loop, hooks and prompt docs.

Tests drive the shipped script through subprocess, so they break iff it does.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "scripts" / "phasekit-roadmap.py"
CLI = REPO_ROOT / "scripts" / "phasekit.sh"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
PRE_ROADMAP_TAG = "v0.14.12"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def run(*args, repo):
    return subprocess.run([sys.executable, str(TOOL), *args, "--repo", str(repo)],
                          capture_output=True, text=True)


def next_of(repo, file=None):
    extra = ["--file", file] if file else []
    r = run("next", *extra, repo=repo)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def ids(items):
    return [i["id"] for i in items]


def messages(d):
    return [w["message"] for w in d["warnings"]]


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "proj"
        (self.repo / "docs").mkdir(parents=True)
        self.path = self.repo / "docs" / "ROADMAP.md"

    def tearDown(self):
        for p in (self.repo / "docs", self.path):
            try:
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        self._tmp.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def done(self, eid, it="7", date="2026-09-25"):
        return run("done", eid, "--iteration", it, "--date", date, repo=self.repo)


# ---------------------------------------------------------------------------
# READER
# ---------------------------------------------------------------------------

class Reader(_Repo):
    def test_absent_file_is_exhausted_not_an_error(self):
        d = next_of(self.repo)
        self.assertTrue(d["absent"])
        self.assertTrue(d["exhausted"])
        self.assertEqual(d["eligible"], [])
        self.assertEqual(d["warnings"], [])

    def test_needs_chain_yields_only_the_unblocked_entry(self):
        self.write(
            "Direction: multiplayer\n\n## Next\n"
            "- [ ] R1 Lobby — create and join    (needs: —)\n"
            "- [ ] R2 Spectate — watch    (needs: R1)\n"
            "- [ ] R3 Chat — lobby chat\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1", "R3"])
        self.assertEqual([(b["id"], b["waiting_on"]) for b in d["blocked"]], [("R2", ["R1"])])
        self.assertEqual(d["direction"], "multiplayer")
        self.assertEqual((d["eligible"][0]["title"], d["eligible"][0]["scope"]),
                         ("Lobby", "create and join"))

    def test_a_checked_entry_anywhere_satisfies_needs(self):
        self.write("## Next\n- [x] R1 Done by hand\n- [ ] R2 Then this (needs: R1)\n")
        self.assertEqual(ids(next_of(self.repo)["eligible"]), ["R2"])

    def test_later_is_never_eligible_and_all_checked_is_exhausted(self):
        self.write("## Next\n- [x] R1 A\n## Later\n- [ ] R2 Someday\n")
        d = next_of(self.repo)
        self.assertTrue(d["exhausted"])
        self.assertEqual(d["counts"], {"next": 1, "later": 1, "done": 0})

    def test_superseded_entry_is_never_eligible_and_needs_follow_the_chain(self):
        base = ("## Next\n- [ ] R2 SUPERSEDED by R5: old shape\n"
                "- [ ] R3 Depends on the old one (needs: R2)\n")
        self.write(base + "- [ ] R5 New shape\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R5"])
        self.assertEqual([(b["id"], b["waiting_on"]) for b in d["blocked"]], [("R3", ["R2"])])
        self.write(base + "- [x] R5 New shape\n")
        self.assertEqual(ids(next_of(self.repo)["eligible"]), ["R3"])

    def test_superseded_by_a_missing_id_is_reported_not_silent(self):
        self.write("## Next\n- [ ] R2 SUPERSEDED by R9: old\n- [ ] R3 C (needs: R2)\n")
        d = next_of(self.repo)
        self.assertEqual((d["eligible"], d["blocked"]), ([], []))
        joined = " | ".join(messages(d))
        self.assertIn("R2 is superseded by R9, which is not in the roadmap", joined)
        self.assertIn("R3 needs R2, which cannot be resolved", joined)

    def test_supersession_and_needs_cycles_warn(self):
        self.write("## Next\n- [ ] R1 SUPERSEDED by R2: a\n- [ ] R2 SUPERSEDED by R1: b\n"
                   "- [ ] R3 c (needs: R1)\n- [ ] R4 d (needs: R5)\n- [ ] R5 e (needs: R4)\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        joined = " | ".join(messages(d))
        self.assertIn("supersession cycle", joined)
        self.assertIn("needs cycle", joined)

    def test_a_near_miss_supersession_marker_excludes_and_warns(self):
        """Round-2 review MAJOR 2: 'Superseded by R5:' dispatched the retired entry."""
        self.write("## Next\n- [ ] R2 Superseded by R5: old\n"
                   "- [ ] R3 SUPERSEDED by R5 old no colon\n- [ ] R5 new\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R5"])
        self.assertEqual(sum("looks superseded" in m for m in messages(d)), 2)
        self.assertEqual(self.done("R2").returncode, 3)

    def test_inline_code_is_not_a_fence(self):
        """Round-2 review #13: the rule was masked in the combined fence test."""
        self.write("## Next\n```a`b is inline code\n- [ ] R4 seen\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R4"])
        self.assertEqual(d["warnings"], [])

    def test_a_long_needs_chain_does_not_exhaust_recursion(self):
        """Round-2 review #11."""
        n = 3000
        lines = ["## Next", "- [ ] R1 first"]
        lines += [f"- [ ] R{i} step (needs: R{i - 1})" for i in range(2, n + 1)]
        self.write("\n".join(lines) + "\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(len(d["blocked"]), n - 1)

    def test_a_body_line_that_carries_meaning_is_never_silent(self):
        """Round-3 review MAJOR 1: a wrapped `(needs:` was swallowed, so R2 was
        dispatched before R1; a nested (4-space or tab) R<n> checkbox vanished,
        and `done` on the owner carried the unchecked nested entry into Done."""
        self.write("## Next\n- [ ] R2 B\n  (needs: R1)\n## Later\n- [ ] R1 A\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        self.assertTrue(any("wrapped onto a body line" in m for m in messages(d)))
        for nested in ("    - [ ] R2 nested", "\t- [ ] R2 tabbed", "    - [ ] R1 dup",
                       "  - [ ] R01 meant"):
            self.write(f"## Next\n- [ ] R1 A\n{nested}\n## Done\n")
            snapshot = self.path.read_bytes()
            d = next_of(self.repo)
            self.assertEqual(d["eligible"], [], nested)
            self.assertTrue(any("nested in a body" in m for m in messages(d)), nested)
            self.assertEqual(self.done("R1").returncode, 3, nested)
            self.assertEqual(self.path.read_bytes(), snapshot, nested)

    def test_id_less_sub_tasks_and_inline_code_in_a_body_stay_quiet(self):
        """Round-3 review #6: an inline-code line used to end the body early."""
        self.write("## Next\n- [ ] R1 A\n  ```x`y``` is inline code\n  - [ ] sub\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(d["warnings"], [])

    def test_every_needs_cycle_is_named_including_through_supersession(self):
        """Round-3 review #5: one back-edge per search missed R4's cycle, and a
        loop through a superseded id was a silent permanent deadlock."""
        self.write("## Next\n- [ ] R1 a (needs: R2, R4)\n- [ ] R2 b (needs: R3)\n"
                   "- [ ] R3 c (needs: R1)\n- [ ] R4 d (needs: R3)\n")
        cyc = [m for m in messages(next_of(self.repo)) if "needs cycle" in m]
        self.assertEqual(len(cyc), 1)
        for eid in ("R1", "R2", "R3", "R4"):
            self.assertIn(eid, cyc[0])
        self.write("## Next\n- [ ] R1 a (needs: R2)\n- [ ] R2 SUPERSEDED by R3: b\n"
                   "- [ ] R3 c (needs: R1)\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        self.assertTrue(any("needs cycle among R1, R3" in m for m in messages(d)), messages(d))

    def test_unknown_need_warns_and_excludes(self):
        self.write("## Next\n- [ ] R1 Chat (needs: R9)\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        self.assertEqual(messages(d), ["R1 needs R9, which is not in the roadmap"])

    def test_needs_must_be_last_or_the_entry_is_excluded(self):
        """Review M3: a trailing period or scope after the needs used to drop
        the need silently and dispatch blocked work."""
        self.write("## Next\n- [ ] R1 A\n- [ ] R2 B (needs: R1).\n"
                   "- [ ] R3 C (needs: R1) — scope\n- [ ] R4 D (needs: R1) (needs: R1)\n"
                   "- [x] R5 Done with a mid-line need (needs: R1) — fine\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(sum("must be the last thing" in m for m in messages(d)), 3,
                         "checked entries are exempt — their needs no longer decide anything")

    def test_duplicate_ids_are_excluded_entirely(self):
        """Review M2: keeping the first copy let a finished entry resurface."""
        self.write("## Next\n- [ ] R1 A\n- [ ] R1 A again\n- [ ] R2 B (needs: R1)\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        self.assertTrue(any("appears again" in m for m in messages(d)))

    def test_malformed_lines_warn_and_never_become_eligible(self):
        self.write("## Next\n- [ ] no id here\n- [ ] R1 First\n- [ ] R2 Bad need (needs: soon)\n"
                   "- [ ] R3 Self (needs: R3)\n- [ ] R01 Leading zero\n- [ ] R1.2.3 Too deep\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1"])
        joined = " | ".join(messages(d))
        for needle in ("no valid R<n> id", "'soon'", "needs itself", "'R01 Leading zero'",
                       "'R1.2.3 Too deep'"):
            self.assertIn(needle, joined)

    def test_scope_split_and_bridge_ids(self):
        self.write("## Next\n- [ ] R1 Alpha — one\n- [ ] R2 Beta -- two\n- [ ] R3 Gamma\n"
                   "- [x] R4 Big\n- [ ] R4.1 Bridge (needs: R4)\n")
        e = {i["id"]: (i["title"], i["scope"]) for i in next_of(self.repo)["eligible"]}
        self.assertEqual(e, {"R1": ("Alpha", "one"), "R2": ("Beta", "two"), "R3": ("Gamma", ""),
                             "R4.1": ("Bridge", "")})

    def test_the_tolerance_fixture(self):
        """Harder than any real file: xmeo's 2,283-line narrative has no
        checkboxes and no fences at all (measured at release time)."""
        self.write("""\
# A project

Some prose. The word Direction: appears mid-line and means nothing.

## Status
- [ ] R40 a checkbox under a foreign heading — not an entry
- [x] R41 another one

## The long history
### Iteration 12
- [ ] R42 still foreign, even under a sub-heading

Direction: build the thing people asked for

## Next
Free prose inside the section is fine.
- [ ] R1 Real — first
### Grouped
- [ ] R2 Still under Next — second (needs: R1)

```
## Next
- [ ] R90 inside a fence — never read
Direction: inside a fence — never read
```

~~~md
- [ ] R91 inside a tilde fence
~~~

## Appendix
- [ ] R43 after Next ended

Direction: a second one
""")
        d = next_of(self.repo)
        self.assertEqual(d["direction"], "build the thing people asked for")
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(ids(d["blocked"]), ["R2"])
        self.assertEqual(d["counts"], {"next": 2, "later": 0, "done": 0})
        outside = [m for m in messages(d) if "outside ## Next" in m]
        self.assertEqual([m.split(":")[0] for m in outside], ["R40", "R41", "R42", "R43"])
        self.assertIn("a second 'Direction:' line; the first one wins", messages(d))
        self.assertFalse(any("R90" in m or "R91" in m for m in messages(d)))

    def test_fences_follow_commonmark(self):
        """Review MINOR 2: a longer fence is not closed by a shorter inner one;
        inline code is not a fence; an info string never closes one."""
        self.write("## Next\n"
                   "````md\n```\n- [ ] R8 inside the four-tick example\n```\n````\n"
                   "```x``` is inline code, not a fence\n"
                   "```py\n- [ ] R9 in a fence\n```py\n- [ ] R10 still inside\n```\n"
                   "~~~\n```\n- [ ] R11 a backtick line does not close a tilde fence\n~~~\n"
                   "- [ ] R1 Real\n")
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(d["warnings"], [])

    def test_an_unterminated_fence_or_comment_is_loud(self):
        """Review MINOR 2: a stray fence used to swallow the file silently —
        'the reader broke' disguised as 'nothing is next'."""
        self.write("## Next\n```\n- [ ] R1 swallowed\n")
        d = next_of(self.repo)
        self.assertEqual(d["eligible"], [])
        self.assertTrue(any("never closes" in m for m in messages(d)))
        self.write("## Next\n<!-- a note\n- [ ] R1 swallowed\n")
        self.assertTrue(any("HTML comment" in m for m in messages(next_of(self.repo))))

    def test_headings_comments_bom_and_indented_code(self):
        """Review MINOR 3."""
        self.write("﻿Direction: through a BOM\n## Next:\n- [ ] R1 A\n"
                   "   ## Later\n- [ ] R2 B\n## Next\n"
                   "<!-- - [ ] R3 commented out -->\n<!--\n- [ ] R4 in a block comment\n-->\n"
                   "    - [ ] R5 indented code, not an entry\n")
        d = next_of(self.repo)
        self.assertEqual(d["direction"], "through a BOM")
        self.assertEqual(ids(d["eligible"]), ["R1"])
        self.assertEqual(d["counts"], {"next": 1, "later": 1, "done": 0})

    def test_a_vertical_tab_does_not_split_a_line(self):
        """Review MINOR 6: str.splitlines treated \\v as a line break."""
        self.write("## Next\n- [ ] R1 A title\x0b## Later\n")
        self.assertEqual(ids(next_of(self.repo)["eligible"]), ["R1"])

    def test_a_long_narrative_with_no_roadmap_reads_as_exhausted_and_silent(self):
        body = "".join(f"## Iteration {n}\n\nWhat happened, at length.\n\n- a plain bullet\n\n"
                       for n in range(300))
        self.write("# history\n\n" + body)
        d = next_of(self.repo)
        self.assertTrue(d["exhausted"])
        self.assertEqual(d["warnings"], [])
        self.assertFalse(d["absent"])

    def test_file_option_reads_another_path(self):
        (self.repo / "PLAN.md").write_text("## Next\n- [ ] R1 Elsewhere\n", encoding="utf-8")
        d = next_of(self.repo, file="PLAN.md")
        self.assertEqual((ids(d["eligible"]), d["file"]), (["R1"], "PLAN.md"))


# ---------------------------------------------------------------------------
# FAIL DIRECTION
# ---------------------------------------------------------------------------

class FailDirection(_Repo):
    def test_a_repo_that_is_not_a_directory_is_exit_5_not_absent(self):
        """Review M1: a typo'd --repo used to read as 'this project has no roadmap'."""
        for cmd in (["next"], ["done", "R1", "--iteration", "7"], ["init"]):
            r = subprocess.run([sys.executable, str(TOOL), *cmd, "--repo", "/nonexistent/typo"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 5, (cmd, r.stdout, r.stderr))
            self.assertNotIn("Traceback", r.stderr)

    @unittest.skipIf(IS_ROOT, "root bypasses directory permissions")
    def test_an_unsearchable_docs_directory_is_exit_5_on_every_python(self):
        """Review M1: Path.exists() said False on 3.12+ and raised exit 1 on 3.11."""
        self.write("## Next\n- [ ] R1 A\n")
        os.chmod(self.repo / "docs", 0)
        for cmd in (["next"], ["done", "R1", "--iteration", "7"]):
            r = run(*cmd, repo=self.repo)
            self.assertEqual(r.returncode, 5, (cmd, r.stdout, r.stderr))
            self.assertNotIn("Traceback", r.stderr)

    def test_not_a_regular_file_is_exit_5_never_absent_and_never_a_hang(self):
        """Round-2 review #6, #7, #8: a dangling link read as 'absent', a parent
        that is a file read as 'absent', and a FIFO hung the reader forever."""
        cases = []
        dangling = Path(self._tmp.name) / "dangling"
        (dangling / "docs").mkdir(parents=True)
        (dangling / "docs" / "ROADMAP.md").symlink_to("/nonexistent/x.md")
        cases.append(dangling)
        filedocs = Path(self._tmp.name) / "filedocs"
        filedocs.mkdir()
        (filedocs / "docs").write_text("not a directory", encoding="utf-8")
        cases.append(filedocs)
        if hasattr(os, "mkfifo"):
            fifo = Path(self._tmp.name) / "fifo"
            (fifo / "docs").mkdir(parents=True)
            os.mkfifo(fifo / "docs" / "ROADMAP.md")
            cases.append(fifo)
        for repo in cases:
            for cmd in (["next"], ["done", "R1", "--iteration", "7"], ["init"]):
                r = subprocess.run([sys.executable, str(TOOL), *cmd, "--repo", str(repo)],
                                   capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 5, (repo.name, cmd, r.stdout, r.stderr))

    def test_a_dangling_parent_link_is_exit_5_not_absent(self):
        """Round-3 review #3: `docs -> /mnt/unmounted` read as 'no roadmap', and
        `init` claimed the file already existed."""
        repo = Path(self._tmp.name) / "unmounted"
        repo.mkdir()
        (repo / "docs").symlink_to("/nonexistent/docs")
        for cmd in (["next"], ["done", "R1", "--iteration", "7"], ["init"]):
            r = subprocess.run([sys.executable, str(TOOL), *cmd, "--repo", str(repo)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 5, (cmd, r.stdout, r.stderr))

    def test_unreadable_bytes_are_exit_5(self):
        self.path.write_bytes(b"## Next\n- [ ] R1 \xff\xfe bad bytes\n")
        self.assertEqual(run("next", repo=self.repo).returncode, 5)

    def test_done_with_no_file_is_exit_5(self):
        self.assertEqual(self.done("R1").returncode, 5)


# ---------------------------------------------------------------------------
# WRITER
# ---------------------------------------------------------------------------

BASE = """\
Direction: x

## Next
- [ ] R1 Lobby — create and join    (needs: —)
- [ ] R2 Spectate — watch    (needs: R1)

## Later
- [ ] R3 Ranked

## Done
- [x] R0 Engine — iteration 1 (2026-09-01)

Trailing prose after Done.
"""
R1_DONE = "- [x] R1 Lobby — create and join    (needs: —) — iteration 7 (2026-09-25)\n"


class Writer(_Repo):
    def test_done_moves_exactly_one_line_verbatim_and_touches_nothing_else(self):
        self.write(BASE)
        before = BASE.splitlines(keepends=True)
        r = self.done("R1")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        expected = list(before)
        expected.remove("- [ ] R1 Lobby — create and join    (needs: —)\n")
        at = expected.index("- [x] R0 Engine — iteration 1 (2026-09-01)\n") + 1
        expected.insert(at, R1_DONE)
        self.assertEqual(after, expected)
        self.assertEqual(ids(next_of(self.repo)["eligible"]), ["R2"])

    def test_the_users_bullet_indent_and_needs_survive(self):
        """Review MINOR 4: the first draft rebuilt the line and dropped text."""
        self.write("## Next\n  * [ ] R1 A — s   (needs: R0)\t\n## Done\n- [x] R0 Z\n")
        self.done("R1")
        self.assertIn("  * [x] R1 A — s   (needs: R0) — iteration 7 (2026-09-25)\n",
                      self.path.read_text(encoding="utf-8"))

    def test_continuation_lines_travel_with_their_entry(self):
        """Review MINOR 5: they used to be left behind, and the Done row was
        wedged between R0 and R0's own notes."""
        self.write("## Next\n- [ ] R1 A\n  detail of R1\n  more of R1\n- [ ] R2 B\n"
                   "## Done\n- [x] R0 Z\n  notes of R0\n\nprose\n")
        self.done("R1")
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "## Next\n- [ ] R2 B\n## Done\n- [x] R0 Z\n  notes of R0\n"
                         "- [x] R1 A — iteration 7 (2026-09-25)\n  detail of R1\n  more of R1\n"
                         "\nprose\n")

    def test_a_comment_opened_in_a_continuation_does_not_swallow_the_roadmap(self):
        """Round-2 review MAJOR 1: the indented `<!--` moved with R1 and left
        its `-->` behind, so after one `done` the rest of the roadmap read as
        Done and `next` said nothing was next, with no warning."""
        self.write("## Done\n- [x] R0 Z\n\n## Next\n- [ ] R1 A\n  <!-- private note\n"
                   "that ends here -->\n- [ ] R2 B\n- [ ] R3 C\n")
        self.assertEqual(self.done("R1").returncode, 0)
        d = next_of(self.repo)
        self.assertEqual(ids(d["eligible"]), ["R2", "R3"])
        self.assertEqual(d["warnings"], [])

    def test_sub_tasks_travel_and_direction_stays_put(self):
        """Round-2 review #3 and #4."""
        self.write("Intro\n## Next\n- [ ] R1 A\n  - [ ] design\n  - [ ] build\n"
                   "  Direction: real theme\n- [ ] R2 B\n## Done\n- [x] R0 Z\n  - [x] polished\n")
        self.done("R1")
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "Intro\n## Next\n  Direction: real theme\n- [ ] R2 B\n## Done\n"
                         "- [x] R0 Z\n  - [x] polished\n"
                         "- [x] R1 A — iteration 7 (2026-09-25)\n  - [ ] design\n  - [ ] build\n")
        d = next_of(self.repo)
        self.assertEqual(d["direction"], "real theme")
        self.assertEqual(d["warnings"], [])

    @unittest.skipUnless(sys.platform != "win32", "POSIX pipes")
    def test_a_closed_stdout_does_not_turn_success_into_an_undeclared_exit(self):
        """Round-2 review #10: exit 120 after the write had already landed."""
        self.write(BASE)
        r = subprocess.run(["bash", "-c", f'set -o pipefail; "{sys.executable}" "{TOOL}" done R1 '
                            f'--iteration 7 --date 2026-09-25 --repo "{self.repo}" | true'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(R1_DONE, self.path.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform != "win32", "POSIX shells")
    def test_a_closed_stdout_fd_does_not_turn_success_into_exit_5(self):
        """Round-3 review #4: `>&-` left sys.stdout None and the tool exited 5
        after the write had landed."""
        self.write(BASE)
        r = subprocess.run(["bash", "-c", f'"{sys.executable}" "{TOOL}" done R1 --iteration 7 '
                            f'--date 2026-09-25 --repo "{self.repo}" >&-'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(R1_DONE, self.path.read_text(encoding="utf-8"))

    def test_done_twice_is_a_no_op(self):
        self.write(BASE)
        self.done("R1")
        snapshot = self.path.read_bytes()
        r = self.done("R1", it="8")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["changed"])
        self.assertEqual(self.path.read_bytes(), snapshot)

    def test_refusals_leave_the_file_untouched(self):
        self.write("## Next\n- [ ] R2 SUPERSEDED by R5: old\n- [ ] R4 bad (needs: soon)\n"
                   "- [ ] R5 new\n- [ ] R6 once\n- [ ] R6 twice\n")
        snapshot = self.path.read_bytes()
        for eid in ("R9", "R2", "R4", "R6"):
            r = self.done(eid)
            self.assertEqual(r.returncode, 3, (eid, r.stderr))
            self.assertEqual(self.path.read_bytes(), snapshot)
        self.assertIn("R5", self.done("R2").stderr)

    def test_missing_done_heading_is_created_at_the_end(self):
        self.write("## Next\n- [ ] R1 A\n- [ ] R2 B")  # no trailing newline, no Done
        self.assertEqual(self.done("R1").returncode, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "## Next\n- [ ] R2 B\n\n## Done\n- [x] R1 A — iteration 7 (2026-09-25)\n")

    def test_done_lands_under_the_heading_not_after_trailing_prose(self):
        self.write("## Done\n\nProse.\n\n```\n- [x] R99 example\n```\n\n## Next\n- [ ] R1 A\n")
        self.done("R1")
        self.assertEqual(self.path.read_text(encoding="utf-8").splitlines()[:2],
                         ["## Done", "- [x] R1 A — iteration 7 (2026-09-25)"])

    def test_crlf_bom_and_mode_are_preserved(self):
        self.path.write_bytes(("﻿" + BASE).replace("\n", "\r\n").encode("utf-8"))
        os.chmod(self.path, 0o640)
        self.assertEqual(self.done("R1").returncode, 0)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith("﻿".encode("utf-8")))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(R1_DONE.replace("\n", "\r\n").encode("utf-8"), raw)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_a_symlinked_roadmap_is_written_through_and_stays_a_link(self):
        """Review MINOR 8: the replace used to turn the link into a file."""
        real = self.repo / "real-roadmap.md"
        real.write_text(BASE, encoding="utf-8")
        self.path.symlink_to(real)
        self.assertEqual(self.done("R1").returncode, 0)
        self.assertTrue(self.path.is_symlink())
        self.assertIn(R1_DONE, real.read_text(encoding="utf-8"))

    @unittest.skipIf(IS_ROOT, "root may write a read-only file")
    def test_a_read_only_roadmap_is_refused(self):
        self.write(BASE)
        os.chmod(self.path, 0o444)
        self.assertEqual(self.done("R1").returncode, 5)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASE)

    def test_a_killed_writers_temp_file_is_swept(self):
        self.write(BASE)
        stale = self.repo / "docs" / ".phasekit-roadmap-write-abc12345.tmp"
        stale.write_text("half a write", encoding="utf-8")
        mine = [self.repo / "docs" / n for n in (".roadmap-notes.tmp",
                                                   ".phasekit-roadmap-write-notes.tmp")]
        for m in mine:
            m.write_text("precious", encoding="utf-8")
        self.assertEqual(self.done("R1").returncode, 0)
        self.assertFalse(stale.exists())
        for m in mine:  # round-2 review #9: the sweep deleted a user's file
            self.assertEqual(m.read_text(encoding="utf-8"), "precious", m.name)

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX only")
    def test_concurrent_done_calls_lose_nothing(self):
        """Review MINOR 7: 6 of 30 unlocked races lost one move."""
        for _ in range(12):
            self.write("## Next\n- [ ] R1 A\n- [ ] R2 B\n## Done\n")
            procs = [subprocess.Popen([sys.executable, str(TOOL), "done", eid, "--iteration", "7",
                                       "--date", "2026-09-25", "--repo", str(self.repo)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for eid in ("R1", "R2")]
            for p in procs:
                p.communicate()
                self.assertEqual(p.returncode, 0)
            text = self.path.read_text(encoding="utf-8")
            self.assertIn("- [x] R1 A", text)
            self.assertIn("- [x] R2 B", text)

    def test_usage_errors_are_exit_2(self):
        self.write(BASE)
        for argv in (["done", "X1", "--iteration", "7"],
                     ["done", "R01", "--iteration", "7"],
                     ["done", "R1", "--iteration", "   "],
                     ["done", "R1", "--iteration", "7\f## Next\f- [ ] R66 injected"],
                     ["done", "R1", "--iteration", "7 more"],
                     ["done", "R1", "--iteration", "7", "--date", "yesterday"],
                     ["done", "R1"],
                     []):
            r = subprocess.run([sys.executable, str(TOOL), *argv] +
                               (["--repo", str(self.repo)] if argv else []),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, argv)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASE)


class Init(_Repo):
    def test_init_writes_a_template_that_reads_clean(self):
        self.path.parent.rmdir()
        r = run("init", repo=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Roadmap — proj", text)
        d = next_of(self.repo)
        self.assertTrue(d["exhausted"])
        self.assertEqual(d["warnings"], [], "the starter file must parse clean — its example "
                         "lives in a fence precisely so it is never read")
        self.assertIsNone(d["direction"])
        self.assertIn("SUPERSEDED by R", text)

    def test_init_never_overwrites(self):
        self.write("mine\n")
        self.assertEqual(run("init", repo=self.repo).returncode, 4)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "mine\n")


class Cli(_Repo):
    def test_phasekit_roadmap_verb_forwards_to_the_tool(self):
        self.write("## Next\n- [ ] R1 Via the CLI\n")
        r = subprocess.run(["bash", str(CLI), "roadmap", "next"], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ids(json.loads(r.stdout)["eligible"]), ["R1"])


# ---------------------------------------------------------------------------
# NO UNDUE BURDEN
# ---------------------------------------------------------------------------

def _enrich_module():
    spec = importlib.util.spec_from_file_location("enrich_project_roadmap_test", ENRICH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Everything the loop, the container or a session is handed. A roadmap reference
# in any of them would put the file on a solo user's path.
LOOP_SURFACES = (
    "scripts/run-until-done.sh", "scripts/run-phase.sh", "scripts/verify-phase.sh",
    "scripts/container-setup.sh", "scripts/verify-container.sh",
    ".devcontainer/entrypoint.sh", ".devcontainer/Dockerfile",
)
ROADMAP_CALLS = re.compile(r"phasekit-roadmap|phasekit roadmap|ROADMAP\.md|roadmap (next|done|init)")


class NoUndueBurden(unittest.TestCase):
    def test_prompt_and_gate_docs_never_mention_a_roadmap(self):
        for rel in ("CONTINUE_PROMPT.txt", "docs/QUALITY_GATES.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotRegex(text, re.compile("roadmap", re.I),
                                f"{rel} must not ask a session anything about a roadmap")

    def test_the_loop_container_hooks_and_templates_never_call_the_tool(self):
        paths = [REPO_ROOT / rel for rel in LOOP_SURFACES]
        paths += sorted((REPO_ROOT / ".claude" / "hooks").glob("*"))
        paths += sorted((REPO_ROOT / "templates").glob("CLAUDE*"))
        checked = 0
        for p in paths:
            if p.is_file():
                checked += 1
                self.assertNotRegex(p.read_text(encoding="utf-8"), ROADMAP_CALLS, str(p))
        self.assertGreaterEqual(checked, len(LOOP_SURFACES), "a pinned surface went missing")

    def test_no_profile_installs_a_roadmap(self):
        m = _enrich_module()
        manifest = m.load_manifest()
        for name in manifest["profiles"]:
            targets = m.enumerate_install_targets(manifest,
                                                  m.resolve_profile(manifest["profiles"], name))
            self.assertNotIn("docs/ROADMAP.md", {t["path"] for t in targets}, name)

    def _assert_ships_tool_and_no_roadmap(self, target):
        up = subprocess.run([sys.executable, str(ENRICH), "--upgrade", str(target), "--yes"],
                            capture_output=True, text=True)
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
        self.assertTrue((target / "scripts" / "phasekit-roadmap.py").is_file())
        self.assertFalse((target / "docs" / "ROADMAP.md").exists())
        check = subprocess.run([sys.executable, str(ENRICH), "--check", str(target)],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_enrich_then_upgrade_ships_the_tool_and_no_roadmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "solo"
            target.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run([sys.executable, str(ENRICH), str(target)], check=True,
                           capture_output=True, text=True)
            self._assert_ships_tool_and_no_roadmap(target)

    def test_upgrading_a_project_from_a_pre_roadmap_release(self):
        """Review MINOR 11: the path the fleet actually takes — a project
        enriched by an older phasekit, upgraded by this one."""
        has_tag = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "-q", "--verify",
                                  f"refs/tags/{PRE_ROADMAP_TAG}"], capture_output=True)
        if has_tag.returncode != 0:
            self.skipTest(f"{PRE_ROADMAP_TAG} is not fetched in this clone")
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-phasekit"
            old.mkdir()
            archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", PRE_ROADMAP_TAG],
                                     capture_output=True, check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
                tf.extractall(old)
            target = Path(tmp) / "fleet-project"
            target.mkdir()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run([sys.executable, str(old / "scripts" / "enrich-project.py"),
                            str(target)], check=True, capture_output=True, text=True)
            self.assertFalse((target / "scripts" / "phasekit-roadmap.py").exists())
            self._assert_ships_tool_and_no_roadmap(target)


class Registration(unittest.TestCase):
    def test_ships_downstream(self):
        m = _enrich_module()
        self.assertIn("scripts/phasekit-roadmap.py", m.ALWAYS_INSTALLED_FILE_PATHS)
        caps = (REPO_ROOT / "capabilities" / "project-capabilities.yaml").read_text(encoding="utf-8")
        self.assertIn("path: scripts/phasekit-roadmap.py", caps)

    def test_contract_pins_the_convention_and_the_exit_codes(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        conv = [c for c in data["conventions"] if c["name"] == "roadmap-entries"]
        self.assertEqual(len(conv), 1)
        for rel in conv[0]["declared_in"]:
            self.assertIn(conv[0]["marker"], (REPO_ROOT / rel).read_text(encoding="utf-8"), rel)
        constants = {int(c) for c in re.findall(r"(?m)^EXIT_[A-Z_]+ = (\d+)",
                                               TOOL.read_text(encoding="utf-8"))}
        declared = {int(c) for c in data["exit_codes"]["scripts/phasekit-roadmap.py"]["codes"]}
        self.assertEqual(constants, declared)


if __name__ == "__main__":
    unittest.main()
