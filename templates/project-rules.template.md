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
reused or renumbered. A level-2 or level-3 heading of the form
`<ID> — <rule>` is an entry, in this format:

```markdown
## RULE-1 — Prices shown and prices charged come from one table
- Incident: 2026-11-07, the cart showed the old price after a sale ended
- Implemented in: `src/pricing/table.ts` (`priceFor`)
- Test: `tests/pricing.test.ts` `cart and checkout read one table`
```

An id is capital letters, digits and hyphens, starting with a letter
(`RULE-1`, `PLACEMENT-2`). `Test:` is one or more test file paths from the
repository root in backticks, each optionally followed by a test name in
backticks (text that appears in that file) and separated by `;`, or exactly
`no test yet`. Field names may be bold (`- **Test:** …`) but never change
(`Tests:` or `Function:` is just another bullet). Any other heading is a
section: group entries under `## Some area` and write them as `### RULE-3 — …`.
All entries use the level of the first one. A field bullet under a heading
that is not an entry, or a heading that reads like a mistyped entry
(`RULE 2 — …`), is a finding.

No rules yet.
