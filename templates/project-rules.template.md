# {{PROJECT_NAME}} — project rules

> **Project-owned rules register.** phasekit seeded this file once and never
> writes it again. Each entry is a rule this project learned, usually from an
> incident, that its code must keep. phasekit checks this file at every landing
> and in `phasekit check` (`docs/QUALITY_GATES.md` "Project rules register"): a
> malformed entry, a duplicate id, or a `Test:` naming a file or test that does
> not exist refuses the landing. A phase whose change touches a rule keeps it
> and names its id in the record's `rules_touched`.

Add an entry when a phase fixes a defect whose cause was a rule held in two
places, or a rule nobody had written down. One entry per rule; an id is never
reused or renumbered. Every level-2 heading in this file is an entry, in this
format:

```markdown
## RULE-1 — Prices shown and prices charged come from one table
- Incident: 2026-11-07, the cart showed the old price after a sale ended
- Implemented in: `src/pricing/table.ts` (`priceFor`)
- Test: `tests/pricing.test.ts` `cart and checkout read one table`
```

An id is capital letters, digits and hyphens, starting with a letter
(`RULE-1`, `PLACEMENT-2`). `Test:` is a test file path from the repository root
in backticks, optionally followed by a test name in backticks (text that
appears in that file), or exactly `no test yet`.

No rules yet.
