# Learnings — {{PROJECT_NAME}}

Durable, non-obvious discoveries that future sessions need. Each session
reads this file early and appends what it learned the hard way.

Rules (also in CONTINUE_PROMPT.txt):

- **One dated bullet per entry: what + why it matters.** Environment gotchas,
  build flakes and their causes, design context that isn't obvious from the
  code, tool quirks, decisions with non-obvious rationale.
- **NOT for progress notes** — "implemented X", "phase N done" dies with the
  session; the phase artifacts carry that.
- **Soft cap ~150 lines.** Over the cap, CURATE: merge related entries,
  tighten wording, drop what later work made obvious. Never blind
  oldest-first pruning — old environment facts are often the most durable.
- **No secrets, tokens, keys, or credentials — ever.** The final-commit gate
  scans this file and will refuse the commit.

---

<!-- entries below, newest last -->
