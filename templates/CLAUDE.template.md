# {{PROJECT_NAME}}

This repository uses the phasekit workflow.

## Core operating rules
- Work in audit-first mode
- Start from the earliest unapproved phase
- Prefer minimal, backward-compatible changes unless a rewrite is explicitly justified
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally
- The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree, push) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
- Scratch files go in artifacts/scratch/ (ignored, never committed, cleared when an iteration starts) or /tmp — never elsewhere in the tree: the loop commits everything else it finds.
{{#vendored}}- Do not edit scaffold-owned files (`"ownership": "scaffold"` in `.scaffold/manifest.json`, e.g. `docs/QUALITY_GATES.md`): upgrades replace them.{{/vendored}}{{#pinned}}- phasekit's engine is not in this repository: it runs read-only from outside it, pinned by `.phasekit-version` (`phasekit docs` prints where its docs are — `QUALITY_GATES.md` and the other process docs). Never copy engine files in; `phasekit upgrade` moves the pin.{{/pinned}} Project additions go in the companion `docs/project/<NAME>.md`; changes to the scaffold's text go upstream to phasekit. `docs/CONVENTIONS.md` and this file are the project's own.
- Tests read the declared surface: a test reads this project's own tree and phasekit's DECLARED surface (`contracts/interface.json` `facts`, {{#vendored}}or `bash scripts/phasekit.sh facts --json`), never scaffold-owned files (the vendored loop and scripts, the hooks, the scaffold docs); a fact a test needs that the surface lacks is a request to phasekit, not a parse (docs/QUALITY_GATES.md "Tests read the declared surface").{{/vendored}}{{#pinned}}or `phasekit facts --json`), never the engine's files (the loop and scripts, the hooks, the engine docs); a fact a test needs that the surface lacks is a request to phasekit, not a parse (the engine's QUALITY_GATES.md "Tests read the declared surface").{{/pinned}}

## Required references
Read on demand. These are named, not `@`-imported: an import loads the whole file into every session's context, and a SPEC grows without bound (an `@path` in this file would also resolve relative to `.claude/`, not the repository root).
- `docs/SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/PHASES.md`
{{#vendored}}- `docs/QUALITY_GATES.md`
{{/vendored}}{{#pinned}}- `QUALITY_GATES.md` in phasekit's engine docs (`phasekit docs` prints the directory)
{{/pinned}}- `docs/project/QUALITY_GATES.md`
- `docs/PROD_REQUIREMENTS.md`

## Optional references
- `docs/DESIGN.md` — steady-state system design (subsystems, data flows, hot spots, boundaries). Read first if present; not every project has one.

{{OPTIONAL_REFERENCES}}