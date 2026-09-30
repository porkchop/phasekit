# {{PROJECT_NAME}}

This repository uses the phasekit workflow.

## Core operating rules
- Work in audit-first mode
- Start from the earliest unapproved phase
- Prefer minimal, backward-compatible changes unless a rewrite is explicitly justified
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally
- The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
- Scratch files go in artifacts/scratch/ (ignored, never committed, cleared when an iteration starts) or /tmp — never elsewhere in the tree: the loop commits everything else it finds.
- Do not edit scaffold-owned files (`"ownership": "scaffold"` in `.scaffold/manifest.json`, e.g. `docs/QUALITY_GATES.md`): upgrades replace them. Project additions go in the companion `docs/project/<NAME>.md`; changes to the scaffold's text go upstream to phasekit. `docs/CONVENTIONS.md` and this file are the project's own.

## Required references
- @docs/SPEC.md
- @docs/ARCHITECTURE.md
- @docs/PHASES.md
- @docs/QUALITY_GATES.md
- @docs/project/QUALITY_GATES.md
- @docs/PROD_REQUIREMENTS.md

## Optional references
- @docs/DESIGN.md — steady-state system design (subsystems, data flows, hot spots, boundaries). Read first if present; not every project has one.

{{OPTIONAL_REFERENCES}}