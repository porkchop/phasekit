# Incident record (moved out of the process docs, v0.29.0)

The process documents state rules. The incidents that taught them are kept
here, dated, so a rule's "why" is one click away without every session paying
to read it (audit 2026-11-08, item A3: the engine's QUALITY_GATES.md is
reference, not required reading). Nothing here is a rule; the rule each entry
taught is named with the section that carries it.

## Evidence integrity (QUALITY_GATES.md "Evidence integrity gate")

Upstreamed 2026-08-11 from foundry-dashboard, which adopted the rules at its
iteration-10 close after stale evidence decided three consecutive signoffs.
The phase numbers are that project's.

- At Phase 29 a memo cited a browser round that predated its own final source
  edits, *in the same paragraph* that explained why superseded evidence must
  not be cited. At Phase 30 the identical defect recurred one phase later: a
  session updated the memo to say "all 23 md5s matched", then changed three of
  the 23 files before it was interrupted. Both were caught by comparison, and
  neither would have been caught by reading. → *Every gate records the digests
  of what it judged; before citing, compare.*
- Every phase that edits a source file shifts the lines below its edit,
  silently invalidating citations elsewhere in the repo; this blocked Phases
  27, 28, 29 and 30, each time via the diff's *own* edits. → *A `path:line`
  citation is a claim about a tree; sweep the citations an edit moved.*

## Verify budget (QUALITY_GATES.md "Verify budget")

- 2026-08 (v0.6.4): three consecutive sessions became no-commit sessions on a
  suite that had grown to ~83 s. → *Target ~30 s, 60 s ceiling; the escape is
  a measured fast/slow split, never a skipped gate.*
- 2026-10-15 (v0.20.0): foundry-orchestrator's own copy of the gate harness
  drifted from the real gate three times in one week; it ran 37 tests red for
  want of the engine mount and `PHASEKIT_CONTRACT`. → *`phasekit verify
  --in-runner` is the one harness; nothing hand-builds a `docker run`.*
- 2026-10-28 (v0.21.2): a test run inside an unbounded session container took
  about 25 GB plus all host swap, and the host OOM killer reached the operator's
  own processes first. → *Every container is bounded; a bad value is the
  default, never unbounded.*

## Compaction re-anchor (retired v0.29.0)

The `compact-reanchor` SessionStart hook (v0.4.7) was to re-anchor a session
after context compaction from disk. Measured 2026-11-08: 0 fires in 574
session logs; the loop's turns are short, bounded and resumed by id, so
compaction does not occur. Retired outright (Aaron, 2026-11-09): no hook and no
compaction clause in the prompt — accommodating a state that does not occur
costs tokens on every turn for nothing. If compaction ever appears in the
session logs, that is the moment to add something.
