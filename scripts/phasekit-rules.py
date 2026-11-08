#!/usr/bin/env python3
"""A project's rules register, and the one checker of its format (v0.26.0).

WHY THIS EXISTS
---------------
On 2026-11-07 three halt-mode bugs in one fleet project had one shape: the
building blocks were shared, but the sequence joining them was written inline
per page, and a rule an earlier incident had taught ("the spawn ring and the
painted pieces come from one board") was held as code in two places, one of
which the new mode updated and the other not. Nothing made the project write
down the rules it had learned, so nothing could ask whether a change kept them.

`docs/project/RULES.md` is that list: PROJECT-OWNED (seeded once, never
overwritten), one entry per rule, each naming the incident that taught it, the
one place it is implemented and the test that pins it. This module owns the
format end to end: it checks the register (`check`), checks the register ids a
phase record names in `rules_touched` (`check --record FILE`), and seeds an
empty register (`init`). phasekit's loop runs `check` at every landing; a
session or an operator runs it through `phasekit rules check` or `phasekit
check`. It is phasekit's check, not a project test ("A project's tests test the
project").

OPTIONAL STAYS OPTIONAL
-----------------------
No register, or one with no entries, passes: a project that never writes a rule
notices nothing. Only what is written is judged.

THE FORMAT (pinned in contracts/interface.json, convention `project-rules`)
--------------------------------------------------------------------------
Outside fenced code and HTML comments, a level-2 or level-3 heading of the
form `<ID> — <rule>` is an entry (v0.27.0: level 3 too, so entries can be
grouped under section headings):

    ## Pricing
    ### RULE-1 — Prices shown and prices charged come from one table
    - Incident: 2026-11-07, the cart showed the old price (iteration 12)
    - **Implemented in:** `src/pricing/table.ts` (`priceFor`)
    - Test: `tests/pricing.test.ts` `cart and checkout read one table`; `tests/cart.test.ts`

  * The id is capital letters, digits and hyphens, starting with a letter
    (`RULE-1`, `PLACEMENT-2`); ids are unique and never reused. The separator
    is an em dash, an en dash or a hyphen with a space each side; the rule is
    one sentence, non-empty.
  * Any other heading is a SECTION heading, never a finding — except that it
    ends the entry above it when it is not deeper than that entry's heading
    (a deeper non-entry heading sits inside the entry). A level-1 heading
    always ends it.
  * `Incident:`, `Implemented in:` and `Test:` are top-level bullets (`-`, `*`
    or `+`, up to 3 spaces of indent; the names are case-insensitive and may
    be bold: `- **Test:** …` or `- **Test**: …`), each exactly once per entry
    with a non-empty value. The names are fixed (no aliases: `Tests:` or
    `Function:` is just another bullet). Other bullets and lines in an entry
    are free prose. A field bullet OUTSIDE an entry is a finding (a malformed
    entry heading would otherwise hide its rule silently).
  * `Test:` is either exactly `no test yet` (the whole value), or one or more
    items separated by `;`, each a test file path from the repository root in
    backticks, optionally followed by a test name in backticks: each file must
    exist (a regular file inside the repository), and each name, when given,
    must appear verbatim in its file.
  * A fence or HTML comment that never closes is a finding (it would hide
    every entry after it).

A record's `rules_touched` (artifacts/phase-approval.json, or the completion
record) is OPTIONAL: absent or null is accepted. When present it is an array of
register ids; each must be an entry heading of the register.

Usage:
  python3 scripts/phasekit-rules.py check [--repo DIR] [--record FILE]... [--json]
  python3 scripts/phasekit-rules.py init [--repo DIR]

Exit codes:
  0  `check`: the register is absent, empty, or every entry is well-formed and
     names tests that exist, and every --record's `rules_touched` names only
     register ids; `init`: wrote the register
  2  usage error (argparse)
  3  `check`: findings — one line each on stdout (`--json`: in `findings`)
  4  `init`: a register already exists — refused, never overwritten
  5  could not complete: the repo path is not a directory, the register is not
     a regular file or could not be read (permission, not UTF-8), a --record
     could not be read or is not JSON, `init` found no template beside this
     script, or an unexpected internal error. Never means "the register is
     fine" and never means "findings".
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FINDINGS = 3
EXIT_EXISTS = 4
EXIT_FILE = 5

DEFAULT_REL = "docs/project/RULES.md"
TEMPLATE_REL = "templates/project-rules.template.md"
SCHEMA = 1
NO_TEST = "no test yet"
FIELDS = ("incident", "implemented in", "test")

ID_RE = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*"
HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<title>.*?))?(?:[ \t]+#+)?[ \t]*$")
ENTRY_RE = re.compile(rf"^(?P<id>{ID_RE})[ \t]+(?:—|–|-)[ \t]+(?P<rule>\S.*)$")
# A heading that is not an entry but reads like a mistyped one (`RULE 2 — x`,
# `rule-1 — x`): a short leading token with a digit, then a dash separator.
LOOKS_ENTRY_RE = re.compile(r"^(?P<tok>[A-Za-z][A-Za-z0-9]*(?:[ _-]+[A-Za-z0-9]+){0,2})"
                            r"[ \t]+(?:—|–|-)[ \t]+\S")
_FIELD_NAMES = r"incident|implemented[ \t]+in|test"
# `- Name: v`, `- **Name:** v`, `- **Name**: v` (or __ for **); never `Tests:`.
FIELD_RE = re.compile(r"^ {0,3}[-*+][ \t]+(?:"
                      rf"(?P<b>\*\*|__)(?P<bname>{_FIELD_NAMES})(?:[ \t]*:(?P=b)|(?P=b)[ \t]*:)"
                      rf"|(?P<name>{_FIELD_NAMES})[ \t]*:)"
                      r"[ \t]*(?P<value>.*?)[ \t]*$", re.IGNORECASE)
FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
# A code span is a backtick run closed by a run of the same length (CommonMark), so
# ``a`b`` can carry a backtick and ``x`` is one span, not two.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def _code_spans(value: str) -> list[str]:
    spans = []
    for m in CODE_SPAN_RE.finditer(value):
        body = m.group(2)
        if len(body) > 2 and body[0] == " " and body[-1] == " " and body.strip():
            body = body[1:-1]
        spans.append(body)
    return spans


class RulesError(Exception):
    """Could not complete (exit 5)."""


class Entry:
    def __init__(self, rid: str, rule: str, line: int, level: int = 2):
        self.id = rid
        self.rule = rule
        self.line = line
        self.level = level
        self.fields: dict[str, tuple[str, int]] = {}

    def as_json(self) -> dict:
        test = self.fields.get("test", ("", 0))[0]
        return {"id": self.id, "rule": self.rule, "line": self.line, "test": test}


def _opens_fence(line: str) -> re.Match | None:
    m = FENCE_OPEN_RE.match(line)
    # a backtick fence's info string cannot hold a backtick (CommonMark)
    if m and m.group("fence")[0] == "`" and "`" in m.group("info"):
        return None
    return m


def parse(text: str) -> tuple[list[Entry], list[tuple[int, str]]]:
    """Entries and structural findings [(line, message)] of a register's text."""
    if text.startswith("﻿"):
        text = text[1:]
    entries: list[Entry] = []
    findings: list[tuple[int, str]] = []
    current: Entry | None = None
    fence: tuple[str, int, int] | None = None   # (char, length, opened at line)
    comment_at = 0
    entry_level = 0          # fixed by the first entry heading: 2 or 3 for the whole file
    section_level = 0        # level of the newest non-entry heading (0: the preamble)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            cm = FENCE_CLOSE_RE.match(line)
            if cm and cm.group("fence")[0] == fence[0] and len(cm.group("fence")) >= fence[1]:
                fence = None
            continue
        if comment_at:
            if "-->" in line:
                comment_at = 0
            continue
        om = _opens_fence(line)
        if om:
            fence = (om.group("fence")[0], len(om.group("fence")), line_no)
            continue
        stripped = line.lstrip(" ")
        if stripped.startswith("<!--") and len(line) - len(stripped) <= 3:
            if "-->" not in stripped[4:]:
                comment_at = line_no
            continue
        hm = HEADING_RE.match(line)
        if hm:
            level = len(hm.group("hashes"))
            title = (hm.group("title") or "").strip()
            em = ENTRY_RE.match(title) if level in (2, 3) else None
            if em and entry_level and level != entry_level:
                if level < entry_level:
                    findings.append((line_no, f"{em.group('id')}: entries in this register are "
                                              f"level-{entry_level} headings (the first is on "
                                              f"line {entries[0].line}); this one is level "
                                              f"{level}"))
                em = None   # a deeper one is content of the entry above (as in v0.26.0)
            if em:
                entry_level = entry_level or level
                current = Entry(em.group("id"), em.group("rule").strip(), line_no, level)
                entries.append(current)
                continue
            if current is not None and level > current.level:
                continue     # a deeper heading stays inside the entry
            current = None   # a section heading not deeper than the entry ends it
            section_level = level
            lm = LOOKS_ENTRY_RE.match(title) if level in (2, 3) else None
            if lm and any(c.isdigit() for c in lm.group("tok")):
                findings.append((line_no, "a heading that reads like an entry but is not one: "
                                          "write `## <ID> — <the rule in one sentence>` (ID: "
                                          "capital letters, digits and hyphens, starting with "
                                          f"a letter); got {title[:80]!r}"))
            continue
        fm = FIELD_RE.match(line)
        if fm and current is None and section_level < 2:
            continue     # the preamble or a level-1 section: prose about the format
        if fm and current is None:
            shown = re.sub(r"[ \t]+", " ", (fm.group("bname") or fm.group("name")))
            findings.append((line_no, f"a `{shown}:` field outside an entry; is the heading above "
                                      "it meant to be `## <ID> — <the rule in one sentence>` (ID: "
                                      "capital letters, digits and hyphens, starting with a "
                                      "letter)?"))
            continue
        if fm:
            written = fm.group("bname") or fm.group("name")
            name = re.sub(r"[ \t]+", " ", written.lower())
            if name in current.fields:
                findings.append((line_no, f"{current.id}: `{written}:` appears twice "
                                          f"(first on line {current.fields[name][1]})"))
            else:
                current.fields[name] = (fm.group("value"), line_no)
    if fence is not None:
        findings.append((fence[2], "a fenced block opened here never closes; every entry after "
                                   "it is hidden"))
    if comment_at:
        findings.append((comment_at, "an HTML comment opened here never closes; every entry "
                                     "after it is hidden"))
    return entries, findings


def _is_no_test(value: str) -> bool:
    return value.strip().rstrip(".").strip().strip("`").strip().lower() == NO_TEST


def _split_items(value: str) -> list[str]:
    """`Test:` items: the value split at `;` outside code spans."""
    items, start = [], 0
    spans = [(m.start(), m.end()) for m in CODE_SPAN_RE.finditer(value)]
    for i, ch in enumerate(value):
        if ch != ";" or any(a <= i < b for a, b in spans):
            continue
        items.append(value[start:i])
        start = i + 1
    items.append(value[start:])
    return [it.strip() for it in items]


def _test_findings(repo: Path, entry: Entry) -> list[tuple[int, str]]:
    value, line_no = entry.fields["test"]
    if _is_no_test(value):
        return []
    out: list[tuple[int, str]] = []
    items = _split_items(value)
    # An item with no code span is prose (`… (covers the cart; checkout too)`),
    # not a test, unless no item names one: then the value says nothing checkable.
    with_spans = [it for it in items if CODE_SPAN_RE.search(it)]
    for item in with_spans or [value.strip()]:
        out.extend(_item_findings(repo, entry, item, line_no))
    return out


def _item_findings(repo: Path, entry: Entry, item: str, line_no: int) -> list[tuple[int, str]]:
    spans = _code_spans(item)
    if not spans:
        return [(line_no, f"{entry.id}: each `Test:` item (items separated by `;`) must be a test "
                          "file in backticks, optionally followed by a test name in backticks; "
                          f"or the whole value is exactly `{NO_TEST}`; got {item[:80]!r}")]
    rel = spans[0].strip()
    name = spans[1] if len(spans) > 1 else None
    if name is not None and not name.strip():
        return [(line_no, f"{entry.id}: the test name after `{rel}` is blank")]
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        return [(line_no, f"{entry.id}: test file `{rel}` must be a path from the repository "
                          "root (not absolute, no `..`)")]
    target = repo / p
    try:
        real = target.resolve()
        inside = real == repo or repo in real.parents
        st = os.stat(target)
    except OSError:
        return [(line_no, f"{entry.id}: test file `{rel}` does not exist")]
    except (ValueError, RuntimeError):  # NUL byte in the path; symlink loop (Python < 3.13)
        return [(line_no, f"{entry.id}: test file `{rel}` cannot be resolved")]
    if not inside:
        return [(line_no, f"{entry.id}: test file `{rel}` is outside the repository")]
    if not stat.S_ISREG(st.st_mode):
        return [(line_no, f"{entry.id}: test file `{rel}` is not a regular file")]
    if name is None:
        return []
    try:
        body = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [(line_no, f"{entry.id}: test file `{rel}` cannot be read "
                          f"({os.strerror(exc.errno or 0)})")]
    if name not in body:
        return [(line_no, f"{entry.id}: test `{name}` is not in `{rel}`")]
    return []


def check_register(repo: Path, text: str) -> tuple[list[Entry], list[tuple[int, str]]]:
    entries, findings = parse(text)
    seen: dict[str, int] = {}
    for e in entries:
        if e.id in seen:
            findings.append((e.line, f"{e.id}: duplicate id (first on line {seen[e.id]}); ids "
                                     "are unique and never reused — give this rule a new id"))
        else:
            seen[e.id] = e.line
        missing = [f for f in FIELDS if f not in e.fields]
        if missing:
            findings.append((e.line, f"{e.id}: missing "
                                     + ", ".join(f"`- {f[0].upper() + f[1:]}:`" for f in missing)))
        empty = [f for f in FIELDS if f in e.fields and not e.fields[f][0]]
        for f in empty:
            findings.append((e.fields[f][1], f"{e.id}: `{f[0].upper() + f[1:]}:` is empty"))
        if "test" in e.fields and e.fields["test"][0]:
            findings.extend(_test_findings(repo, e))
    findings.sort(key=lambda f: f[0])
    return entries, findings


def check_record(path: Path, shown: str, ids: set[str], register_absent: bool) -> list[str]:
    """Findings for one record's `rules_touched` (absent or null: none)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RulesError(f"cannot read {shown} as JSON ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or data.get("rules_touched") is None:
        return []
    touched = data["rules_touched"]
    if not isinstance(touched, list):
        return [f"{shown}: `rules_touched` must be an array of register ids (or absent); got "
                f"{type(touched).__name__}"]
    out = []
    for i, item in enumerate(touched):
        if not isinstance(item, str) or not item.strip():
            out.append(f"{shown}: rules_touched[{i}] is not a register id (a non-empty string)")
            continue
        rid = item.strip()
        if rid in ids:
            continue
        where = (f"there is no {DEFAULT_REL}" if register_absent
                 else f"{DEFAULT_REL} has no entry `## {rid} — …`")
        out.append(f"{shown}: rules_touched names {rid}, but {where}")
    return out


def _repo(arg: str) -> Path:
    try:
        st = os.stat(arg)
    except OSError as exc:
        raise RulesError(f"--repo {arg}: {os.strerror(exc.errno or 0)}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise RulesError(f"--repo {arg} is not a directory")
    return Path(arg).resolve()


def _read_register(path: Path) -> str | None:
    """The register's text, or None when it is absent (ENOENT, nothing else)."""
    try:
        st = os.stat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT and not os.path.islink(path):
            return None
        raise RulesError(f"cannot inspect {DEFAULT_REL}: {os.strerror(exc.errno or 0)}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise RulesError(f"{DEFAULT_REL} is not a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RulesError(f"{DEFAULT_REL} is not UTF-8") from None
    except OSError as exc:
        raise RulesError(f"cannot read {DEFAULT_REL}: {os.strerror(exc.errno or 0)}") from exc


def _shown(repo: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo))
    except ValueError:
        return str(path)


def cmd_check(args) -> int:
    repo = _repo(args.repo)
    text = _read_register(repo / DEFAULT_REL)
    entries, structural = check_register(repo, text) if text is not None else ([], [])
    lines = [f"{DEFAULT_REL} line {n}: {msg}" for n, msg in structural]
    ids = {e.id for e in entries}
    for rec in args.record or []:
        p = Path(rec)
        p = p if p.is_absolute() else Path.cwd() / p
        lines.extend(check_record(p, _shown(repo, p), ids, text is None))
    if args.json:
        print(json.dumps({"schema": SCHEMA, "file": DEFAULT_REL, "absent": text is None,
                          "entries": [e.as_json() for e in entries], "findings": lines}, indent=2))
    elif lines:
        print("\n".join(lines))
    elif text is None:
        print(f"phasekit rules: no {DEFAULT_REL} — nothing to check")
    else:
        print(f"phasekit rules: {DEFAULT_REL}: {len(entries)} rule(s), every one well-formed and "
              "every named test found")
    return EXIT_FINDINGS if lines else EXIT_OK


def cmd_init(args) -> int:
    repo = _repo(args.repo)
    dest = repo / DEFAULT_REL
    if dest.exists() or dest.is_symlink():
        print(f"phasekit rules: {DEFAULT_REL} already exists; refusing to overwrite it",
              file=sys.stderr)
        return EXIT_EXISTS
    template = Path(__file__).resolve().parent.parent / TEMPLATE_REL
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"no template at {template} (run `phasekit rules init`, which uses the "
                         "installed phasekit)") from exc
    text = text.replace("{{PROJECT_NAME}}", repo.name)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError:
        print(f"phasekit rules: {DEFAULT_REL} already exists; refusing to overwrite it",
              file=sys.stderr)
        return EXIT_EXISTS
    except OSError as exc:
        raise RulesError(f"cannot write {DEFAULT_REL}: {os.strerror(exc.errno or 0)}") from exc
    print(f"phasekit rules: wrote {DEFAULT_REL}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phasekit rules",
        description=f"Check or seed the project rules register ({DEFAULT_REL}).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="check the register (and records' rules_touched)")
    p_check.add_argument("--repo", default=".", help="project root (default: .)")
    p_check.add_argument("--record", action="append", metavar="FILE",
                         help="a phase record whose `rules_touched` to check (repeatable)")
    p_check.add_argument("--json", action="store_true", help="print the result as JSON")
    p_init = sub.add_parser("init", help="write an empty register if none exists")
    p_init.add_argument("--repo", default=".", help="project root (default: .)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"check": cmd_check, "init": cmd_init}[args.cmd]
    try:
        return handler(args)
    except RulesError as exc:
        print(f"phasekit rules: {exc}", file=sys.stderr)
        return EXIT_FILE
    except Exception as exc:  # noqa: BLE001 — never let a traceback become a verdict
        print(f"phasekit rules: internal error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FILE


if __name__ == "__main__":
    sys.exit(main())
