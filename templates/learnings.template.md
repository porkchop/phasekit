# Learnings — {{PROJECT_NAME}}

Durable, non-obvious discoveries that future sessions need. Each session
reads this file early and appends what it learned the hard way.

Rules (also in CONTINUE_PROMPT.txt):

- **One dated bullet per entry: what + why it matters.** Environment gotchas,
  build flakes and their causes, design context that isn't obvious from the
  code, tool quirks, decisions with non-obvious rationale.
- **NOT for progress notes** — "implemented X", "phase N done" dies with the
  session; the phase artifacts carry that.
- **Newest last.** Append at the end of the file. The phasekit loop keeps
  this file within its budget (12 KiB) by moving the OLDEST entries, whole,
  to `docs/LEARNINGS-ARCHIVE.md` at each phase landing — archived, never
  deleted. Merging or tightening related entries is welcome, not required.
- **No secrets, tokens, keys, or credentials — ever.** The final-commit gate
  scans this file and will refuse the commit.

---

<!-- entries below, newest last -->
