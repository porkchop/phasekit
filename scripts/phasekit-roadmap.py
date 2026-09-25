#!/usr/bin/env python3
"""An OPTIONAL project roadmap, and the one reader/writer of its format (v0.15.0).

WHY THIS EXISTS
---------------
A supervisor that picks a project's next unit of work needs to ask one question
— "what is next here?" — without a model reading prose, a network call, or a
guess. This module answers it from a single markdown file, `docs/ROADMAP.md`,
and owns that file's format end to end: reading it (`next`), recording that an
entry shipped (`done`), and starting one (`init`). A caller that wants to move
an entry never edits the markdown itself; it asks this module, so the format has
exactly one home.

OPTIONAL IS A HARD CONSTRAINT
-----------------------------
phasekit is a public tool that runs with no supervisor. A project that never
wants a roadmap must notice nothing:

  * no file                -> `next` answers `absent: true, exhausted: true`, exit 0
  * nothing in the loop, the gates, CONTINUE_PROMPT or QUALITY_GATES reads,
    requires or mentions the file (tests/test_roadmap.py pins that)
  * `phasekit upgrade` never writes one; only an explicit `init` does

THE READER IS TOLERANT, AND LOUD ABOUT WHAT IT SKIPS
----------------------------------------------------
Only three things in the file mean anything: the first `Direction:` line, and the
checkbox entries under the level-2 headings `## Next`, `## Later` and `## Done`.
Every other line — prose, any other heading and what sits under it, fenced code
blocks (CommonMark rules: length, info string), HTML comments — is ignored, so a
project can keep a long narrative in the same file. What the reader skips is never
SILENT when it could hide intent: a line that looks like an entry but does not
parse, an `R<n>` checkbox outside the three sections, a fence or comment that
never closes, and cycles are all reported in `warnings` and the entry excluded.

THE FAIL DIRECTION
------------------
A caller must be able to tell "nothing is next" from "the reader broke". Exit 0
with `exhausted: true` means the reader LOOKED: the repo is a directory and the
file is either absent (ENOENT) or was read. Anything else — a repo path that is
not a directory, a permission error, bytes that are not UTF-8, an unexpected
internal error — is exit 5, never a quiet "nothing".

THE FORMAT (pinned in contracts/interface.json, convention `roadmap-entries`)
----------------------------------------------------------------------------
    Direction: <one paragraph — the current theme; a planner ranks by this>

    ## Next
    - [ ] R1 <title> — <one-line scope>            (needs: —)
    - [ ] R2 <title> — <one-line scope>            (needs: R1)
    - [ ] R2.1 <bridge title> — <scope>            (needs: R1)
    ## Later
    - [ ] R3 …
    ## Done
    - [x] R0 <title> — iteration 7 (2026-09-25)

  * Ids are `R<n>`, bridges `R<n>.<k>`, no leading zeros. APPEND-ONLY: never
    renumbered, reused or deleted. To replace an entry keep its line and write
    `SUPERSEDED by R<m>: ` right after the id; a `needs:` naming it is then
    satisfied by the end of the supersession chain.
  * `(needs: …)` must be the last thing on an unchecked entry's line.
  * ELIGIBLE = under `## Next`, unchecked, not superseded, id unique, every
    `needs:` done. `## Later` is never eligible.
  * An entry is done when it is checked, wherever it sits.

Usage:
  python3 scripts/phasekit-roadmap.py next [--repo DIR] [--file PATH]
  python3 scripts/phasekit-roadmap.py done R<n> --iteration LABEL [--date YYYY-MM-DD]
                                                [--repo DIR] [--file PATH]
  python3 scripts/phasekit-roadmap.py init [--repo DIR] [--file PATH]

Exit codes (every subcommand):
  0  success — `next` read the file or found none; `done` moved the entry or it
     was already done; `init` wrote the file
  2  usage error (argparse)
  3  `done`: the id is not in the roadmap, is superseded, appears more than once,
     or its line does not parse
  4  `init`: a roadmap already exists — refused, never overwritten
  5  could not complete: the repo path is not a directory, the file could not be
     read or written (permission, not UTF-8, read-only), `done` found no file at
     all, or an unexpected internal error. Never means "nothing is next".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import errno
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

try:  # POSIX advisory lock; absent on Windows, where concurrent `done` is unguarded
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNKNOWN_ID = 3
EXIT_EXISTS = 4
EXIT_FILE = 5

DEFAULT_REL = "docs/ROADMAP.md"
SCHEMA = 1
SECTIONS = ("next", "later", "done")
TMP_PREFIX = ".phasekit-roadmap-write-"
TMP_SUFFIX = ".tmp"
# mkstemp adds exactly 8 chars from [a-z0-9_]; only that shape is ever swept.
TMP_RE = re.compile(r"^\.phasekit-roadmap-write-[a-z0-9_]{8}\.tmp$")

_NUM = r"(?:0|[1-9]\d*)"
_ID = rf"R{_NUM}(?:\.{_NUM})?"
HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<title>.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
DIRECTION_RE = re.compile(r"^ {0,3}Direction:(?P<text>.*)$")
ENTRY_RE = re.compile(r"^ {0,3}[-*+][ \t]+\[(?P<mark>[ xX])\][ \t]+(?P<rest>.*?)[ \t]*$")
ID_RE = re.compile(rf"^(?P<id>{_ID})(?:[ \t]+(?P<text>.*))?$")
NEEDS_ANY_RE = re.compile(r"\(\s*needs\s*:", re.IGNORECASE)
NEEDS_END_RE = re.compile(r"\(\s*needs\s*:\s*(?P<list>[^)]*)\)\s*$", re.IGNORECASE)
SUPERSEDED_RE = re.compile(rf"^SUPERSEDED by (?P<by>{_ID}):\s*(?P<text>.*)$")
SUPERSEDED_LOOSE_RE = re.compile(r"^superseded\b", re.IGNORECASE)
ID_ONLY_RE = re.compile(rf"^{_ID}$")
NO_NEEDS = {"", "—", "–", "-", "none"}
SCOPE_SPLIT_RE = re.compile(r"\s+(?:—|--)\s+")

TEMPLATE = """\
# Roadmap — {project}

Optional. Delete this file if you do not want a roadmap: nothing in phasekit
requires it. The format is pinned in `contracts/interface.json` (convention
`roadmap-entries`) and read by `phasekit roadmap next`. Prose anywhere in this
file is ignored; only the `Direction:` line and the checkbox entries under the
three headings below are read.

Direction:

## Next

## Later

## Done

How an entry is written (this block is an example and is never read):

```
Direction: one paragraph — the current theme; a planner ranks by this.

## Next
- [ ] R1 Short title — one-line scope                 (needs: —)
- [ ] R2 Next thing — one-line scope                  (needs: R1)
## Later
- [ ] R3 Someday — one-line scope
## Done
- [x] R0 Shipped thing — iteration 3 (2026-09-25)
```

Ids are `R<n>` (bridges `R<n>.<k>`) and are never renumbered, reused or deleted.
To replace an entry, keep its line and write `SUPERSEDED by R<m>: ` right after
its id. `(needs: …)` goes last on the line. Only unchecked entries under
`## Next` whose needs are all done are "next"; `## Later` is a parking lot you
promote from by moving a line.
"""


def _emit(text: str) -> None:
    """Print a result. A reader that closed its end of the pipe must not turn a
    completed write into an undeclared exit 120 (round-2 review #10)."""
    if sys.stdout is None:  # stdout closed outright (`>&-`): the work is done; nothing to say
        return
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


class RoadmapError(Exception):
    """A condition that must surface as exit 5, never as 'nothing is next'."""


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

def split_lines(text: str) -> list[str]:
    """Split on LF only, keeping each line's own ending ("\\r\\n" or "\\n").

    NOT `str.splitlines`: that also splits on \\v, \\f, \\x1c-\\x1e, \\x85,
    U+2028/9, so a title or an --iteration label carrying one of them would
    become two "lines" to the reader while staying one line to every editor.
    """
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _content(raw: str, index: int) -> str:
    line = raw.rstrip("\r\n")
    if index == 0 and line.startswith("\ufeff"):
        line = line[1:]
    return line


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class Entry:
    __slots__ = ("id", "section", "done", "title", "scope", "needs",
                 "superseded_by", "line", "index", "end", "broken", "duplicated",
                 "mark_col")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Parsed:
    def __init__(self):
        self.direction: str | None = None
        self.entries: dict[str, Entry] = {}
        self.order: list[str] = []
        self.warnings: list[dict] = []
        self.done_heading: int | None = None  # line index of `## Done`
        self.lines: list[str] = []

    def warn(self, line: int, message: str) -> None:
        self.warnings.append({"line": line, "message": message})


def _split_needs(text: str, parsed: Parsed, line_no: int, eid: str, strict: bool):
    """Return (text_without_needs, needs_list, ok). A checked entry (strict=False)
    never has its needs judged — they no longer decide anything."""
    anywhere = list(NEEDS_ANY_RE.finditer(text))
    if not anywhere:
        return text.strip(), [], True
    end = NEEDS_END_RE.search(text)
    if end is None or len(anywhere) > 1 or anywhere[-1].start() != end.start():
        if strict:
            parsed.warn(line_no, f"{eid}: '(needs: …)' must be the last thing on the line "
                                 "(and appear once); the entry is excluded until it is")
            return text.strip(), [], False
        return text.strip(), [], True
    body = text[:end.start()].rstrip()
    if not strict:
        return body, [], True
    needs, ok = [], True
    for token in (t.strip() for t in end.group("list").split(",")):
        if token.lower() in NO_NEEDS:
            continue
        if not ID_ONLY_RE.match(token):
            parsed.warn(line_no, f"{eid}: 'needs:' names {token!r}, which is not an R<n> id")
            ok = False
            continue
        if token == eid:
            parsed.warn(line_no, f"{eid}: needs itself")
            ok = False
            continue
        needs.append(token)
    return body, needs, ok


def _opens_fence(line: str) -> bool:
    """CommonMark: 3+ backticks/tildes at <= 3 spaces; a backtick info string
    holding a backtick is inline code, not a fence. One predicate for the
    reader and the body scanner so they cannot disagree."""
    om = FENCE_OPEN_RE.match(line)
    return bool(om) and not (om.group("fence")[0] == "`" and "`" in om.group("info"))


# Anything in an entry's body that LOOKS like roadmap structure. The body is
# otherwise opaque, so these are the lines that would silently change meaning
# (round-3 review MAJOR 1): a wrapped `(needs: …)`, and a checkbox at ANY indent
# whose text starts with an R<n>-shaped token (a nested entry, a tab-indented
# one, a duplicate, a malformed id).
ANY_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+(?P<rest>.*?)[ \t]*$")
R_TOKEN_RE = re.compile(r"^R\d")


def _continuation_end(lines: list[str], start: int) -> int:
    """Index one past the last continuation line of the entry at `start`: the
    following non-blank lines indented deeper than the entry's own bullet.

    A continuation stops at anything that could change how the REST of the file
    reads if it moved: a heading, a fence opener, an HTML comment opener (its
    closer may sit on a line that does not move — round-2 review MAJOR 1, which
    made the rest of a roadmap vanish), a `Direction:` line, or another R<n>
    entry. A blank line also ends it (loose lists are ambiguous; text left
    behind is ignored prose, never lost). Nested checkboxes WITHOUT an id — an
    entry's own sub-tasks — travel with it.
    """
    head = lines[start].rstrip("\r\n").lstrip("\ufeff")
    base = len(head) - len(head.lstrip(" \t"))
    j = start + 1
    while j < len(lines):
        line = lines[j].rstrip("\r\n")
        indent = len(line) - len(line.lstrip(" \t"))
        if not line.strip() or indent <= base:
            break
        stripped = line.lstrip()
        if (HEADING_RE.match(line) or _opens_fence(line) or stripped.startswith("<!--")
                or DIRECTION_RE.match(line)):
            break
        em = ENTRY_RE.match(line)
        if em and ID_RE.match(em.group("rest")):
            break
        j += 1
    return j


def parse(text: str) -> Parsed:
    parsed = Parsed()
    parsed.lines = split_lines(text)
    section: str | None = None
    fence: tuple[str, int, int] | None = None     # (char, length, opened at line)
    comment_at: int | None = None
    direction_seen = False
    dup_first: dict[str, int] = {}
    cont_until = -1   # lines before this index belong to the previous entry's body
    body_owner: str | None = None

    for index, raw in enumerate(parsed.lines):
        line = _content(raw, index)
        line_no = index + 1

        if fence is not None:
            cm = FENCE_CLOSE_RE.match(line)
            if cm and cm.group("fence")[0] == fence[0] and len(cm.group("fence")) >= fence[1]:
                fence = None
            continue
        if comment_at is not None:
            if "-->" in line:
                comment_at = None
            continue

        if _opens_fence(line):
            om = FENCE_OPEN_RE.match(line)
            fence = (om.group("fence")[0], len(om.group("fence")), line_no)
            continue
        stripped = line.lstrip()
        if stripped.startswith("<!--"):
            if "-->" not in stripped[4:]:
                comment_at = line_no
            continue

        hm = HEADING_RE.match(line)
        if hm:
            level = len(hm.group("hashes"))
            name = (hm.group("title") or "").strip().rstrip(":").strip().lower()
            if level == 2 and name in SECTIONS:
                section = name
                if name == "done" and parsed.done_heading is None:
                    parsed.done_heading = index
            elif level <= 2:
                section = None
            # a level-3+ heading keeps the section it sits in (sub-grouping)
            continue

        dm = DIRECTION_RE.match(line)
        if dm:
            if direction_seen:
                parsed.warn(line_no, "a second 'Direction:' line; the first one wins")
            else:
                parsed.direction = dm.group("text").strip() or None
                direction_seen = True
            continue

        if index < cont_until:
            # An entry's own sub-tasks and notes — the writer moves them with it.
            # But a body line that looks like roadmap structure is never read
            # silently: the owner is excluded (and `done` refuses it) until the
            # user outdents or unwraps it.
            owner = parsed.entries.get(body_owner) if body_owner else None
            cm = ANY_CHECKBOX_RE.match(line)
            problem = None
            if NEEDS_ANY_RE.search(line):
                problem = "a '(needs: …)' wrapped onto a body line is not read"
            elif cm and R_TOKEN_RE.match(cm.group("rest")):
                problem = ("an R<n> checkbox nested in a body is not an entry — outdent it to at "
                           "most 3 spaces with no tab")
            if problem and body_owner:
                parsed.warn(line_no, f"{body_owner}: {problem}; {body_owner} is excluded until "
                                     "it is fixed")
                if owner is not None:
                    owner.broken = True
            continue
        em = ENTRY_RE.match(line)
        if not em:
            continue
        rest = em.group("rest")
        im = ID_RE.match(rest)
        if section is None:
            if im:
                parsed.warn(line_no, f"{im.group('id')}: a checkbox outside ## Next / ## Later / "
                                     "## Done is ignored")
            continue
        if not im:
            parsed.warn(line_no, f"a checkbox under '## {section.title()}' with no valid R<n> id "
                                 f"(no leading zeros): {rest[:60]!r}")
            continue
        eid = im.group("id")
        body = (im.group("text") or "").strip()
        cont_until = _continuation_end(parsed.lines, index)
        body_owner = eid
        if eid in parsed.entries:
            first = dup_first.setdefault(eid, parsed.entries[eid].line)
            parsed.entries[eid].duplicated = True
            parsed.warn(line_no, f"{eid} appears again (first at line {first}); {eid} is excluded "
                                 "until exactly one line carries it")
            continue

        done = em.group("mark") in "xX"
        superseded_by = None
        sm = SUPERSEDED_RE.match(body)
        near_miss = sm is None and bool(SUPERSEDED_LOOSE_RE.match(body))
        if sm:
            superseded_by = sm.group("by")
            body = sm.group("text")
        elif near_miss:
            parsed.warn(line_no, f"{eid}: looks superseded but the marker is not exactly "
                                 "'SUPERSEDED by R<m>: '; the entry is excluded until it is")
        text_no_needs, needs, ok = _split_needs(body, parsed, line_no, eid, strict=not done)
        parts = SCOPE_SPLIT_RE.split(text_no_needs, maxsplit=1)
        parsed.entries[eid] = Entry(
            id=eid, section=section, done=done,
            title=parts[0].strip(), scope=parts[1].strip() if len(parts) > 1 else "",
            needs=needs, superseded_by=superseded_by,
            line=line_no, index=index, end=cont_until,
            broken=(not ok) or near_miss, duplicated=False,
            mark_col=raw.index("[", len(raw) - len(raw.lstrip("\ufeff"))) + 1,
        )
        parsed.order.append(eid)

    if fence is not None:
        parsed.warn(fence[2], "a fenced block opened here never closes; everything after it "
                              "was ignored")
    if comment_at is not None:
        parsed.warn(comment_at, "an HTML comment opened here never closes; everything after it "
                                "was ignored")
    return parsed


def _satisfied(parsed: Parsed, eid: str, warn_at: int) -> bool | None:
    """True if `eid` (following supersession) is done; False if not yet;
    None if it cannot be resolved (unknown or duplicated id, broken chain, cycle)."""
    seen = []
    cur = eid
    while True:
        if cur in seen:
            parsed.warn(warn_at, "supersession cycle: " + " → ".join(seen + [cur]))
            return None
        seen.append(cur)
        entry = parsed.entries.get(cur)
        if entry is None or entry.duplicated:
            return None
        if entry.superseded_by:
            cur = entry.superseded_by
            continue
        return bool(entry.done)


def _chain_end(parsed: Parsed, eid: str) -> str | None:
    """The live entry a `needs:` on `eid` really waits for: the end of its
    supersession chain. None for a missing id or a supersession cycle."""
    seen = set()
    cur = eid
    while cur not in seen:
        seen.add(cur)
        entry = parsed.entries.get(cur)
        if entry is None:
            return None
        if not entry.superseded_by:
            return cur
        cur = entry.superseded_by
    return None


def _warn_needs_cycles(parsed: Parsed) -> None:
    """Name every group of undone entries that wait on each other through
    `needs:` — followed THROUGH supersession — and so can never become eligible.
    Reported once per strongly connected group, every member named (round-3
    review #5: one back-edge per search missed cycles, and a loop through a
    superseded id was invisible). Iterative Kosaraju: a long chain cannot
    exhaust the recursion limit."""
    live = {eid for eid, e in parsed.entries.items() if not e.done and not e.superseded_by}
    graph: dict[str, list[str]] = {}
    for eid in live:
        targets = []
        for need in parsed.entries[eid].needs:
            end = _chain_end(parsed, need)
            if end in live:
                targets.append(end)
        graph[eid] = targets

    order: list[str] = []
    seen: set[str] = set()
    for root in [e for e in parsed.order if e in live]:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(graph[root]))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                order.append(node)
                stack.pop()
            elif nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(graph[nxt])))

    reverse: dict[str, list[str]] = {eid: [] for eid in graph}
    for u, targets in graph.items():
        for v in targets:
            reverse[v].append(u)
    assigned: set[str] = set()
    for root in reversed(order):
        if root in assigned:
            continue
        group, stack = [], [root]
        assigned.add(root)
        while stack:
            node = stack.pop()
            group.append(node)
            for prev in reverse[node]:
                if prev not in assigned:
                    assigned.add(prev)
                    stack.append(prev)
        looped = len(group) > 1 or group[0] in graph[group[0]]
        if looped:
            members = sorted(group, key=lambda e: parsed.entries[e].line)
            parsed.warn(parsed.entries[members[0]].line,
                        "needs cycle among " + ", ".join(members)
                        + " — none of them can ever become eligible")


def next_report(parsed: Parsed, rel: str) -> dict:
    eligible, blocked = [], []
    counts = {s: 0 for s in SECTIONS}
    for eid in parsed.order:
        e = parsed.entries[eid]
        if e.superseded_by and e.superseded_by not in parsed.entries:
            parsed.warn(e.line, f"{eid} is superseded by {e.superseded_by}, which is not in "
                                "the roadmap")
    _warn_needs_cycles(parsed)
    for eid in parsed.order:
        e = parsed.entries[eid]
        counts[e.section] += 1
        if e.section != "next" or e.done or e.superseded_by or e.broken or e.duplicated:
            continue
        waiting, unresolvable = [], False
        for need in e.needs:
            state = _satisfied(parsed, need, e.line)
            if state is None:
                if need not in parsed.entries:
                    parsed.warn(e.line, f"{eid} needs {need}, which is not in the roadmap")
                else:
                    parsed.warn(e.line, f"{eid} needs {need}, which cannot be resolved "
                                        "(duplicated, or its supersession chain is broken)")
                unresolvable = True
            elif not state:
                waiting.append(need)
        if unresolvable:
            continue
        item = {"id": eid, "title": e.title, "scope": e.scope, "needs": e.needs}
        if waiting:
            blocked.append({**item, "waiting_on": waiting})
        else:
            eligible.append(item)
    return {
        "schema": SCHEMA,
        "file": rel,
        "absent": False,
        "direction": parsed.direction,
        "eligible": eligible,
        "blocked": blocked,
        "exhausted": not eligible,
        "counts": counts,
        "warnings": _unique(parsed.warnings),
    }


def _unique(warnings: list[dict]) -> list[dict]:
    seen, out = set(), []
    for w in warnings:
        key = (w["line"], w["message"])
        if key not in seen:
            seen.add(key)
            out.append(w)
    return sorted(out, key=lambda w: w["line"])


# ---------------------------------------------------------------------------
# Paths and files
# ---------------------------------------------------------------------------

def _resolve(args) -> tuple[Path, Path, str]:
    repo = Path(args.repo)
    try:
        st = os.stat(repo)
    except OSError as exc:
        reason = os.strerror(exc.errno or 0) or type(exc).__name__
        raise RoadmapError(f"--repo {args.repo}: {reason}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise RoadmapError(f"--repo {args.repo} is not a directory")
    repo = repo.resolve()
    if args.file:
        path = Path(args.file)
        path = path if path.is_absolute() else (repo / path)
        try:
            rel = str(path.resolve().relative_to(repo))
        except ValueError:
            rel = str(path)
        return repo, path, rel
    return repo, repo / DEFAULT_REL, DEFAULT_REL


def _present(path: Path) -> bool:
    """True if a regular file is there, False ONLY on ENOENT for the path itself.
    Everything else raises (exit 5): a permission error (the Path.exists() this
    replaces answered False on Python >= 3.12 and exited 1 on 3.11), a dangling
    symlink (a link to an unmounted place is not "no roadmap"), a parent that is
    a file, and anything that is not a regular file (a FIFO would hang the read,
    a directory cannot be one)."""
    try:
        os.lstat(path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise RoadmapError(f"cannot inspect {path}: {os.strerror(exc.errno or 0)}") from exc
        # ENOENT for the file is "no roadmap" only if nothing above it is a
        # dangling link: `docs -> /mnt/unmounted` is an unreachable place, not
        # an absent roadmap (round-3 review #3).
        for ancestor in path.parents:
            if os.path.islink(ancestor) and not os.path.exists(ancestor):
                raise RoadmapError(f"{ancestor} is a link whose target cannot be "
                                   "reached") from None
        return False
    try:
        st = os.stat(path)
    except OSError as exc:
        raise RoadmapError(f"{path} is a link whose target cannot be reached: "
                           f"{os.strerror(exc.errno or 0)}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise RoadmapError(f"{path} is not a regular file")
    return True


def _read(path: Path) -> str:
    # newline="" keeps "\r\n" as written: universal-newline mode would hand the
    # writer LF-only text and it would silently rewrite every line of a CRLF file.
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError:
        raise RoadmapError(f"{path} is not UTF-8") from None
    except OSError as exc:
        raise RoadmapError(f"cannot read {path}: {os.strerror(exc.errno or 0)}") from exc


class _DirLock:
    """An advisory lock on the file's DIRECTORY (no lock file to litter the tree),
    held across read-modify-write so two `done` calls cannot lose an update."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.fd = None

    def __enter__(self):
        if fcntl is not None:
            try:
                self.fd = os.open(self.directory, os.O_RDONLY)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            except OSError:
                if self.fd is not None:
                    os.close(self.fd)
                self.fd = None  # an unlockable directory degrades to unlocked
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
        return False


def _sweep_stale_tmp(directory: Path) -> None:
    """Remove temp files a killed writer left behind — only names in the exact
    shape this module creates (TMP_RE), never a user's file. Safe under the
    lock: no other writer can be mid-write in this directory."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if TMP_RE.match(name):
            try:
                os.unlink(directory / name)
            except OSError:
                pass


def _write_atomic(target: Path, text: str) -> None:
    mode = stat.S_IMODE(os.stat(target).st_mode)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=TMP_PREFIX, suffix=TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_next(args) -> int:
    _, path, rel = _resolve(args)
    if not _present(path):
        _emit(json.dumps({
            "schema": SCHEMA, "file": rel, "absent": True, "direction": None,
            "eligible": [], "blocked": [], "exhausted": True,
            "counts": {s: 0 for s in SECTIONS}, "warnings": [],
        }, indent=2))
        return EXIT_OK
    _emit(json.dumps(next_report(parse(_read(path)), rel), indent=2))
    return EXIT_OK


def _refuse(message: str) -> int:
    print(f"phasekit roadmap: {message}", file=sys.stderr)
    return EXIT_UNKNOWN_ID


def cmd_done(args) -> int:
    _, path, rel = _resolve(args)
    if not _present(path):
        raise RoadmapError(f"no roadmap at {rel}")
    # Write through a symlink to its target, so the link survives the replace.
    target = Path(os.path.realpath(path))
    if not os.access(target, os.W_OK):
        raise RoadmapError(f"{rel} is read-only")

    with _DirLock(target.parent):
        _sweep_stale_tmp(target.parent)
        text = _read(target)                     # read UNDER the lock
        parsed = parse(text)
        entry = parsed.entries.get(args.id)
        if entry is None:
            return _refuse(f"{args.id} is not in {rel}")
        if entry.duplicated:
            return _refuse(f"{args.id} appears on more than one line of {rel}; fix that by "
                           "hand first")
        if entry.superseded_by:
            return _refuse(f"{args.id} is superseded by {entry.superseded_by}; mark "
                           f"{entry.superseded_by} instead")
        if entry.broken:
            return _refuse(f"{args.id}'s line does not parse (line {entry.line}); fix it by "
                           "hand first")
        if entry.done:
            _emit(json.dumps({"id": args.id, "changed": False, "reason": "already done"}))
            return EXIT_OK

        lines = list(parsed.lines)
        eol = "\r\n" if "\r\n" in text else "\n"
        raw = lines[entry.index]
        body = raw.rstrip("\r\n").rstrip()
        # The user's line is kept verbatim: only the mark flips and the
        # iteration suffix is appended (trailing whitespace is trimmed).
        first = body[:entry.mark_col] + "x" + body[entry.mark_col + 1:]
        moved = [f"{first} — iteration {args.iteration} ({args.date}){eol}"]
        moved += [ln if ln.endswith("\n") else ln + eol for ln in lines[entry.index + 1:entry.end]]

        others = [e for e in parsed.entries.values()
                  if e.section == "done" and e.id != entry.id]
        if others:
            insert = max(e.end for e in others)
        elif parsed.done_heading is not None:
            insert = parsed.done_heading + 1
        else:
            insert = None

        span = entry.end - entry.index
        del lines[entry.index:entry.end]
        if insert is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += eol
            if lines and lines[-1].strip():
                lines.append(eol)
            lines.append(f"## Done{eol}")
            lines.extend(moved)
        else:
            if insert > entry.index:
                insert -= span
            if insert > 0 and not lines[insert - 1].endswith("\n"):
                lines[insert - 1] += eol
            lines[insert:insert] = moved
        try:
            _write_atomic(target, "".join(lines))
        except OSError as exc:
            raise RoadmapError(f"cannot write {rel}: {os.strerror(exc.errno or 0)}") from exc
    _emit(json.dumps({"id": args.id, "changed": True, "iteration": args.iteration,
                      "date": args.date}))
    return EXIT_OK


def cmd_init(args) -> int:
    repo, path, rel = _resolve(args)
    if _present(path):
        print(f"phasekit roadmap: {rel} already exists; refusing to overwrite it", file=sys.stderr)
        return EXIT_EXISTS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(TEMPLATE.format(project=repo.name))
    except FileExistsError:
        print(f"phasekit roadmap: {rel} already exists; refusing to overwrite it", file=sys.stderr)
        return EXIT_EXISTS
    except OSError as exc:
        raise RoadmapError(f"cannot write {rel}: {os.strerror(exc.errno or 0)}") from exc
    _emit(rel)
    return EXIT_OK


# ---------------------------------------------------------------------------

def _date(value: str) -> str:
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _one_line(value: str, what: str) -> str:
    value = value.strip()
    bad = [c for c in value if unicodedata.category(c)[0] == "C"
           or unicodedata.category(c) in ("Zl", "Zp")]
    if not value or bad:
        raise argparse.ArgumentTypeError(f"the {what} must be one non-empty line with no "
                                         "control or line-separator characters")
    return value


def _iteration(value: str) -> str:
    return _one_line(value, "iteration label")


def _entry_id(value: str) -> str:
    if not ID_ONLY_RE.match(value):
        raise argparse.ArgumentTypeError(f"not an R<n> id (no leading zeros): {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phasekit roadmap",
        description="Read or update an optional docs/ROADMAP.md.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".", help="project root (default: .)")
        p.add_argument("--file", default=None,
                       help=f"roadmap path, relative to --repo (default: {DEFAULT_REL})")

    common(sub.add_parser("next", help="print the eligible entries as JSON"))
    p_done = sub.add_parser("done", help="mark an entry done and move it under ## Done")
    p_done.add_argument("id", type=_entry_id)
    p_done.add_argument("--iteration", required=True, type=_iteration)
    p_done.add_argument("--date", type=_date,
                        default=time.strftime("%Y-%m-%d", time.gmtime()))
    common(p_done)
    common(sub.add_parser("init", help="write a starter roadmap if none exists"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"next": cmd_next, "done": cmd_done, "init": cmd_init}[args.cmd]
    try:
        return handler(args)
    except RoadmapError as exc:
        print(f"phasekit roadmap: {exc}", file=sys.stderr)
        return EXIT_FILE
    except Exception as exc:  # noqa: BLE001 — never let a traceback become exit 1
        print(f"phasekit roadmap: internal error ({type(exc).__name__}). Any write is atomic, "
              "so the roadmap is either unchanged or fully updated, never partial.",
              file=sys.stderr)
        return EXIT_FILE


if __name__ == "__main__":
    sys.exit(main())
