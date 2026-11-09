#!/usr/bin/env python3
"""The acceptance ledger — a project's criteria as data (v0.29.0, audit D2).

WHY THIS EXISTS
---------------
The SPEC's acceptance-criteria section was append-only since v0.11.0: a
session may add a criterion and supersede one, never delete or renumber. The
invariant is right (an agent must not quietly move its own goalposts); the
place was wrong. Measured on 2026-11-08: SPECs of 1.2–3.8 MB across the fleet,
137 of 180 sessions in a week committing a SPEC change on the largest project,
and a median SPEC read of 0 lines per session. The document everyone pays to
carry is read by nobody.

LEDGER MODE moves the invariant out of the prose. `docs/acceptance.json` holds
one entry per criterion — append-only IDS, mutable STATUS — and `docs/SPEC.md`
becomes a living document that states current behaviour, edited in place, its
history in git. A project is in ledger mode exactly when the ledger file
exists; everything else keeps the append-only SPEC convention unchanged.

THE LEDGER (pinned in contracts/interface.json, convention `acceptance-ledger`)
-----------------------------------------------------------------------------
    {
      "schema": 1,
      "entries": [
        {"id": "AC#12", "text": "…", "status": "met", "since": "iteration-88"},
        {"id": "AC#7",  "text": "…", "status": "superseded", "superseded_by": "AC#12"},
        {"id": "AC#3",  "text": "…", "status": "withdrawn", "reason": "…"}
      ]
    }

  * `id`: `AC#` followed by letters, digits, dots and hyphens; unique; NEVER
    deleted, renumbered or reused. Cited as `AC#<id>` wherever criteria are
    cited today (deferral keys, records, commit bodies).
  * `text`: the criterion, non-empty. Editing it is allowed while the meaning
    holds; a change of meaning is a NEW id with the old one `superseded`.
  * `status`: one of exactly `open | met | superseded | withdrawn`. A
    `superseded` entry names its replacement in `superseded_by` (an id of the
    ledger); a `withdrawn` entry carries a non-empty `reason`.
  * `since`: optional, free text (an iteration label); other keys are kept but
    not judged.

THE CHECK (`check`), run by the loop at every landing that rides a verdict and
by `phasekit verify`, `phasekit check` and `phasekit spec check`:
  * the file parses and has the shape above; every entry well-formed;
  * no duplicate id;
  * every id present in the ledger at `--base REV` (default HEAD — the
    previous landing) is present now: a lost id is a finding, whatever else
    changed. The text and status may move.
No ledger: nothing is judged (append-only mode; the SPEC convention stands).

THE MIGRATION (`migrate`), run ONCE per project by an operator through
`phasekit spec migrate` (at rest, gated like `phasekit migrate`):
  * parses today's SPEC: under every heading whose text contains "cceptance"
    (and until a heading of the same or higher level that does not), each
    numbered item `N.` → AC#N, each item carrying an explicit `AC#x` / `AC-x`
    id → AC#x; a line prefixed `SUPERSEDED by AC#y:` → status superseded,
    superseded_by AC#y; everything else → status `met`, `since: "migrated"`
    (the session that follows corrects what is open);
  * writes the ledger, moves the SPEC to `docs/archive/SPEC-<date>.md` and
    writes a living-SPEC skeleton: the non-acceptance sections of the old
    SPEC verbatim (goal, users, scope), then one short line per met criterion
    under its heading, for the session to rewrite as behaviour.
  Continuation lines join the entry above them; a criterion appended OUTSIDE
  the acceptance sections is captured when the document cites its `AC#N`
  (review B1); every cited id the plan did not capture is listed and the
  migration refuses (exit 4) unless `--allow-missing`. Lines under an
  acceptance heading that join no entry are counted, never silently dropped:
  they stay in the archive.

Usage:
  python3 scripts/phasekit-spec.py check   [--repo DIR] [--base REV] [--json]
  python3 scripts/phasekit-spec.py migrate [--repo DIR] [--dry-run] [--json]
  python3 scripts/phasekit-spec.py init    [--repo DIR]

Exit codes:
  0  `check`: no ledger, or clean; `migrate`: done (or dry run); `init`: wrote
  2  usage error (argparse)
  3  `check`: findings — one line each on stdout (`--json`: in `findings`)
  4  `init`/`migrate`: a ledger already exists (never overwritten); `migrate`:
     no docs/SPEC.md, no acceptance heading in it, or ids the SPEC cites that
     the plan did not capture (listed; `--allow-missing` overrides)
  5  could not complete: the repo path is not a directory, a file could not be
     read or written, the base revision has no readable ledger shape
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FINDINGS = 3
EXIT_EXISTS = 4
EXIT_FILE = 5

LEDGER = "docs/acceptance.json"
SPEC = "docs/SPEC.md"
ARCHIVE_DIR = "docs/archive"
STATUSES = ("open", "met", "superseded", "withdrawn")
ID_RE = re.compile(r"^AC#[A-Za-z0-9][A-Za-z0-9.\-]*$")
SUPERSEDED_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?SUPERSEDED by (AC#[A-Za-z0-9][A-Za-z0-9.\-]*):\s*(.*)$")
# an item that carries an explicit id: `AC#12`, `AC-E14.7`, `**AC#3**`, `AC12`
EXPLICIT_ID_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*|`)?\s*AC(?:#|-|(?=\d))([A-Za-z0-9][A-Za-z0-9.\-]*?)"
    r"(?:\*\*|`|[\s:(—–-])")
# a NUMERIC id cited anywhere in the document (`AC#12`, `AC#12(f)`); the plan
# must capture every one of them or say which it did not (review B1). Not a
# range (`AC#76-100`), not a placeholder (`AC#N`, `AC#nn`), not an alphanumeric
# id (`AC#E14.7`: those are captured only from the lines that define them).
CITED_RE = re.compile(r"AC#(\d+)(?![\d]|-\d)")
LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
NUMBERED_RE = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


# --- ledger shape --------------------------------------------------------------

def _load_ledger_text(text: str, where: str):
    """(entries, findings) from ledger JSON text."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, [f"{where}: not JSON ({exc})"]
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None, [f"{where}: the ledger is an object with an `entries` array"]
    if data.get("schema") != 1:
        return None, [f"{where}: `schema` must be 1"]
    return data["entries"], []


def _of(eid):
    return f" ({eid})" if eid else ""


def check_entries(entries, where=LEDGER):
    findings = []
    ids = {}
    for i, e in enumerate(entries):
        tag = f"{where} entry {i + 1}"
        if not isinstance(e, dict):
            findings.append(f"{tag}: not an object")
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or not ID_RE.match(eid):
            findings.append(f"{tag}: `id` must be `AC#` + letters, digits, dots, hyphens "
                            f"(got {eid!r})")
            eid = None
        elif eid in ids:
            findings.append(f"{tag}: duplicate id {eid} (ids are never reused)")
        else:
            ids[eid] = e
        text = e.get("text")
        if not isinstance(text, str) or not text.strip():
            findings.append(f"{tag}{_of(eid)}: `text` must be a non-empty string")
        status = e.get("status")
        if status not in STATUSES:
            findings.append(f"{tag}{_of(eid)}: `status` must be one of "
                            f"{' | '.join(STATUSES)} (got {status!r})")
        if status == "withdrawn" and not (isinstance(e.get("reason"), str) and e["reason"].strip()):
            findings.append(f"{tag}{_of(eid)}: a withdrawn entry carries a non-empty `reason`")
    for eid, e in ids.items():
        if e.get("status") == "superseded":
            by = e.get("superseded_by")
            if not isinstance(by, str) or by not in ids:
                findings.append(f"{where} {eid}: superseded — `superseded_by` must name an id "
                                f"of the ledger (got {by!r})")
            elif by == eid:
                findings.append(f"{where} {eid}: superseded by itself")
    return findings, set(ids)


def _git(root: Path, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def base_ids(root: Path, base: str):
    """(ids at BASE, finding) — ids of the ledger committed at BASE; an absent
    ledger there means no ids to keep (a project entering ledger mode)."""
    r = _git(root, "show", f"{base}:{LEDGER}")
    if r.returncode != 0:
        return set(), None
    entries, findings = _load_ledger_text(r.stdout, f"{LEDGER} at {base}")
    if findings:
        return None, findings[0]
    ids = set()
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            ids.add(e["id"])
    return ids, None


def cmd_check(args) -> int:
    root = Path(args.repo).resolve()
    if not root.is_dir():
        _say_err(f"phasekit spec check: {root} is not a directory")
        return EXIT_FILE
    path = root / LEDGER
    if not path.exists():
        if args.json:
            print(json.dumps({"schema": 1, "ledger": False, "findings": []}))
        return EXIT_OK
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        _say_err(f"phasekit spec check: cannot read {LEDGER}: {exc}")
        return EXIT_FILE
    entries, findings = _load_ledger_text(text, LEDGER)
    ids = set()
    if entries is not None:
        more, ids = check_entries(entries)
        findings += more
        kept, err = base_ids(root, args.base)
        if err:
            _say_err(f"phasekit spec check: {err}")
            return EXIT_FILE
        lost = sorted(kept - ids)
        for eid in lost:
            findings.append(f"{LEDGER}: {eid} was in the ledger at {args.base} and is gone — ids "
                            "are never deleted or renumbered; mark it superseded (with "
                            "superseded_by) or withdrawn (with a reason)")
    if args.json:
        print(json.dumps({"schema": 1, "ledger": True, "entries": len(entries or []),
                          "findings": findings}))
    else:
        for f in findings:
            print(f)
    return EXIT_FINDINGS if findings else EXIT_OK


# --- migration ----------------------------------------------------------------

def _is_acceptance_heading(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"\bacceptance\b", t)) and not re.search(r"\bnon[- ]?acceptance\b", t)


def parse_spec(spec_text: str):
    """(entries, kept_sections, skipped, acc_headings, cited) — the criteria
    found, the non-acceptance body (list of lines), the count of lines under
    acceptance headings that joined no entry, the number of acceptance
    headings, and every id the document cites.

    A criterion is: under an acceptance heading, a numbered item (`N.` →
    AC#N) or an item with an explicit id (`AC#x`, `AC-x`, `ACx` → AC#x);
    ANYWHERE else in the document (review B1: the orchestrator appended
    criteria 324–395 under `## Operational notes`), a numbered item whose
    `AC#N` is cited somewhere in the document, or a list item carrying an
    explicit id. Continuation lines — indented, or not themselves an item,
    heading or blank — join the entry above them (review M2), so a wrapped
    criterion and its `(a) … (e)` sub-items keep their text."""
    lines = spec_text.splitlines()
    cited = set(CITED_RE.findall(spec_text))
    entries = []
    seen = set()
    kept = []
    skipped = 0
    in_acc = False
    acc_level = 0
    heading_path = ""
    in_fence = False
    acc_headings = 0
    current = None   # the entry continuation lines join

    def close():
        nonlocal current
        if current is not None:
            current["text"] = re.sub(r"\s+", " ", current["text"]).strip()
            current = None

    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            close()
            if not in_acc:
                kept.append(ln)
            else:
                skipped += 1
            continue
        if in_fence:
            if not in_acc:
                kept.append(ln)
            else:
                skipped += 1
            continue
        hm = HEADING_RE.match(ln)
        if hm:
            close()
            level, text = len(hm.group(1)), hm.group(2)
            if in_acc and level <= acc_level and not _is_acceptance_heading(text):
                in_acc = False
            if _is_acceptance_heading(text) and not in_acc:
                in_acc = True
                acc_level = level
                acc_headings += 1
            if in_acc:
                heading_path = text
                continue
            heading_path = text
            kept.append(ln)
            continue
        if not stripped:
            close()
            if not in_acc:
                kept.append(ln)
            continue
        sm = SUPERSEDED_RE.match(ln)
        body = sm.group(2) if sm else ln
        eid = None
        nm = NUMBERED_RE.match(body)
        # a numbered item's number IS its id — an `AC#x` in its text is a
        # citation (`342. **AC#251(f)'s uncontended half …**`; review B1 second
        # pass); an explicit id names an item only on a `-`/`*` list item or an
        # unnumbered line
        em = None if nm else EXPLICIT_ID_RE.match(body + " ")
        if nm:
            eid = "AC#" + nm.group(1)
        elif em:
            eid = "AC#" + em.group(1).rstrip(".")
        is_item = bool(sm or em or nm)
        if not in_acc:
            # outside the acceptance sections only a criterion the document
            # itself cites (or an explicitly id-carrying list item) counts
            take = bool(eid and ID_RE.match(eid)
                        and ((em and LIST_ITEM_RE.match(body)) or sm or (nm and eid[3:] in cited)))
            if not take:
                joins = ln[:1].isspace() or not LIST_ITEM_RE.match(ln)
                if current is not None and not is_item and joins:
                    current["text"] += " " + stripped
                else:
                    close()
                    kept.append(ln)
                continue
        if not is_item:
            # a continuation of the entry above, or an orphan line
            if current is not None:
                current["text"] += " " + stripped
            else:
                skipped += 1
            continue
        if not eid or not ID_RE.match(eid):
            close()
            skipped += 1
            continue
        if eid in seen:
            # the same id on a later line (a restatement): kept with the first
            close()
            skipped += 1
            continue
        close()
        seen.add(eid)
        text = body.strip()
        text = re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", text)
        if em:
            text = re.sub(r"^(?:\*\*|`)?\s*AC(?:#|-|(?=\d))[A-Za-z0-9.\-]+(?:\*\*|`)?\s*[:—–-]?\s*",
                          "", text, count=1)
        current = {"id": eid, "text": text, "status": "met", "since": "migrated",
                   "heading": heading_path or "Acceptance criteria"}
        if sm:
            current["status"] = "superseded"
            current["superseded_by"] = sm.group(1)
        entries.append(current)
    close()
    # a supersession whose target is not in the ledger would be a finding:
    # keep the entry but as withdrawn, with the target noted
    ids = {e["id"] for e in entries}
    for e in entries:
        if e["status"] == "superseded" and e["superseded_by"] not in ids:
            by = e["superseded_by"]
            e["text"] = f"[superseded by {by}, which is not in this ledger] " + e["text"]
            e["status"] = "withdrawn"
            e["reason"] = (f"superseded in the archived SPEC by {by}, an id the archive does "
                           "not carry")
            e.pop("superseded_by", None)
    for e in entries:
        if not e["text"]:
            e["text"] = f"(criterion {e['id']}: no text on its line in the archived SPEC)"
    return entries, kept, skipped, acc_headings, cited


def _skeleton(project: str, kept_lines, entries, archive_rel: str) -> str:
    out = []
    body = "\n".join(kept_lines).strip("\n")
    if body:
        out.append(body)
        out.append("")
    out.append("## Acceptance (ledger mode)")
    out.append("")
    out.append(f"This project's acceptance criteria are entries of `{LEDGER}` (ids `AC#…`, "
               "append-only; status open | met | superseded | withdrawn). This document states "
               "the behaviour as it now is and is edited in place; the pre-migration SPEC is "
               f"archived at `{archive_rel}` and is not read by the loop.")
    out.append("")
    by_heading: dict[str, list] = {}
    for e in entries:
        if e["status"] != "met":
            continue
        by_heading.setdefault(e.get("heading") or "Acceptance criteria", []).append(e)
    for heading, items in by_heading.items():
        out.append(f"### {heading}")
        out.append("")
        for e in items:
            first = e["text"]
            cut = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
            if len(cut) > 200:
                cut = cut[:197].rstrip() + "…"
            out.append(f"- `{e['id']}` {cut}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def migrate_plan(root: Path):
    spec = root / SPEC
    if (root / LEDGER).exists():
        raise MigrateError(f"{LEDGER} already exists — this project is already in ledger mode",
                           EXIT_EXISTS)
    if not spec.is_file():
        raise MigrateError(f"no {SPEC} to migrate", EXIT_EXISTS)
    try:
        text = spec.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrateError(f"cannot read {SPEC}: {exc}", EXIT_FILE) from exc
    entries, kept, skipped, acc_headings, cited = parse_spec(text)
    if acc_headings == 0:
        raise MigrateError(f"{SPEC} has no heading containing \"cceptance\" — nothing to migrate",
                           EXIT_EXISTS)
    captured = {e["id"] for e in entries}
    missing = sorted(("AC#" + c for c in cited if "AC#" + c not in captured), key=_id_key)
    date = _dt.date.today().isoformat()
    archive_rel = f"{ARCHIVE_DIR}/SPEC-{date}.md"
    n = 2
    while (root / archive_rel).exists():
        archive_rel = f"{ARCHIVE_DIR}/SPEC-{date}-{n}.md"
        n += 1
    ledger_entries = []
    for e in entries:
        le = {k: v for k, v in e.items() if k != "heading"}
        ledger_entries.append(le)
    skeleton = _skeleton(root.name, kept, entries, archive_rel)
    return {
        "entries": ledger_entries,
        "counts": {"met": sum(e["status"] == "met" for e in entries),
                   "superseded": sum(e["status"] == "superseded" for e in entries),
                   "withdrawn": sum(e["status"] == "withdrawn" for e in entries),
                   "skipped_lines": skipped},
        "cited_not_captured": missing,
        "archive": archive_rel,
        "skeleton": skeleton,
        "spec_bytes": len(text.encode("utf-8")),
        "skeleton_bytes": len(skeleton.encode("utf-8")),
    }


def apply_migration(root: Path, plan) -> list[str]:
    """Write the ledger, the archive and the skeleton. Returns the paths
    written or moved (relative), for the caller to stage."""
    archive = root / plan["archive"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    spec = root / SPEC
    spec.replace(archive)
    (root / LEDGER).write_text(json.dumps({"schema": 1, "entries": plan["entries"]}, indent=1,
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    spec.write_text(plan["skeleton"], encoding="utf-8")
    return [LEDGER, SPEC, plan["archive"]]


def _id_key(eid):
    tail = eid[3:]
    return (0, int(tail)) if tail.isdigit() else (1, tail)


class MigrateError(Exception):
    def __init__(self, msg, code=EXIT_FILE):
        super().__init__(msg)
        self.code = code


def cmd_migrate(args) -> int:
    root = Path(args.repo).resolve()
    if not root.is_dir():
        _say_err(f"phasekit spec migrate: {root} is not a directory")
        return EXIT_FILE
    try:
        plan = migrate_plan(root)
    except MigrateError as exc:
        _say_err(f"phasekit spec migrate: {exc}")
        return exc.code
    c = plan["counts"]
    missing = plan["cited_not_captured"]
    if args.json:
        report = {k: v for k, v in plan.items() if k not in ("skeleton", "entries")}
        report["ids"] = [e["id"] for e in plan["entries"]]
        report["dry_run"] = bool(args.dry_run)
        print(json.dumps(report))
    else:
        print(f"phasekit spec migrate: {len(plan['entries'])} criteria → {LEDGER} "
              f"({c['met']} met, {c['superseded']} superseded, {c['withdrawn']} withdrawn; "
              f"{c['skipped_lines']} line(s) under acceptance headings joined no entry and stay "
              "in the archive)")
        print(f"  {SPEC} ({plan['spec_bytes']} bytes) → {plan['archive']}; living skeleton "
              f"{plan['skeleton_bytes']} bytes")
        if missing:
            print(f"  {len(missing)} id(s) the SPEC cites were NOT captured as criteria: "
                  + ", ".join(missing[:30]) + (" …" if len(missing) > 30 else ""))
    if missing and not args.allow_missing:
        _say_err(f"phasekit spec migrate: refused — {len(missing)} cited id(s) would be missing "
                 "from the ledger (listed above). Give each a numbered or `AC#<id>` line the SPEC "
                 "cites, or pass --allow-missing to migrate without them (archive only).")
        return EXIT_EXISTS
    if args.dry_run:
        return EXIT_OK
    try:
        apply_migration(root, plan)
    except OSError as exc:
        _say_err(f"phasekit spec migrate: could not write: {exc}")
        return EXIT_FILE
    return EXIT_OK


def cmd_init(args) -> int:
    root = Path(args.repo).resolve()
    path = root / LEDGER
    if path.exists():
        _say_err(f"phasekit spec init: {LEDGER} already exists")
        return EXIT_EXISTS
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "entries": []}, indent=1) + "\n", encoding="utf-8")
    except OSError as exc:
        _say_err(f"phasekit spec init: could not write {LEDGER}: {exc}")
        return EXIT_FILE
    print(f"phasekit spec init: wrote an empty {LEDGER} — this project is now in ledger mode")
    return EXIT_OK


def _say_err(msg):
    print(msg, file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(prog="phasekit-spec", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="verb", required=True)
    pc = sub.add_parser("check",
                        help="check the acceptance ledger (and that no id was lost since --base)")
    pc.add_argument("--repo", default=".")
    pc.add_argument("--base", default="HEAD",
                    help="the revision whose ledger ids must all survive (default HEAD)")
    pc.add_argument("--json", action="store_true")
    pm = sub.add_parser("migrate", help="SPEC criteria → the ledger; the SPEC archived; "
                                        "a living skeleton written")
    pm.add_argument("--repo", default=".")
    pm.add_argument("--dry-run", action="store_true")
    pm.add_argument("--json", action="store_true")
    pm.add_argument("--allow-missing", action="store_true",
                    help="migrate even when ids the SPEC cites were not captured as criteria")
    pi = sub.add_parser("init", help="write an empty ledger (enter ledger mode on a new project)")
    pi.add_argument("--repo", default=".")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"check": cmd_check, "migrate": cmd_migrate, "init": cmd_init}[args.verb](args)


if __name__ == "__main__":
    sys.exit(main())
