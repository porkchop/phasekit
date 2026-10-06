# Phasekit

This repository is a reusable development operating system.

## Core operating rules
- Work in audit-first mode
- phasekit is developed by hand-built releases (`docs/RELEASING.md`, plus Foundry's `phasekit-release` runbook); no phase loop runs on this repository
- Prefer minimal, backward-compatible changes
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally
- The loop owns every commit: never run git commands that write history, refs or the index (commit, add, rm, mv, reset, restore, checkout, switch, stash, merge, rebase, cherry-pick, revert, tag, branch -f/-D, update-ref, worktree, push) — the command guard refuses them. Write your verdict artifact; the loop commits it, verify-gated. To undo an edit of your own, edit the file back (`git show HEAD:<path> > <path>` restores the committed bytes).
- Tests read the declared surface: a test reads this project's own tree and phasekit's DECLARED surface (`contracts/interface.json` `facts`, or `bash scripts/phasekit.sh facts --json`), never scaffold-owned files (the vendored loop and scripts, the hooks, the scaffold docs); a fact a test needs that the surface lacks is a request to phasekit, not a parse (docs/QUALITY_GATES.md "Tests read the declared surface").
- Scratch files go in artifacts/scratch/ (ignored, never committed, cleared when an iteration starts) or /tmp — never elsewhere in the tree: the loop commits everything else it finds.
- Any new tracked file (script, devcontainer asset, doc, template, …) must be registered in `capabilities/project-capabilities.yaml` and, if it should ship to downstream projects, in `ALWAYS_INSTALLED_FILE_PATHS` in `scripts/enrich-project.py`. Run `python3 scripts/enrich-project.py --self-check` before committing. See `CONTRIBUTING.md` "Adding a new tracked file" for the full procedure. Unregistered files pass local tests but silently fail to provision downstream on `phasekit --upgrade`.

## Execution mode rules
- Prefer normal interactive collaboration mode by default
- Treat containerized unattended mode as opt-in
- Keep permissive execution confined to local/container-specific configuration or explicit command-line overrides
- Do not make ordinary direct work on this repository cumbersome

## Required references
Read on demand. These are named, not `@`-imported: an import loads the whole file into every session's context (these five are about 119 KB), and an `@path` in this file would resolve relative to `.claude/`, not the repository root.
- `docs/RELEASING.md`
- `docs/QUALITY_GATES.md`
- `docs/CAPABILITY_MANIFEST.md`
- `capabilities/project-capabilities.yaml`
- `docs/USAGE_PATTERNS.md`