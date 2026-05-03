# ADR-0001: Install lifecycle and provenance

## Status

Accepted (2026-04-28). Implemented across Phase M9 Slices A, B, C.
Decision memo: `artifacts/decision-memo.md` (v2.1, post-red-team-review-v2).
Red-team reviews: `artifacts/red-team-review.md`, `artifacts/red-team-review-v2.md`.

## Context

The `phasekit` scaffold installs subagents, hooks, settings, scripts, and documentation into downstream projects. Before M9, none of this was tracked: there was no record of which files came from the scaffold, which version installed them, or whether they had been hand-edited locally. This made upgrade silent (or destructive), uninstall guesswork, and drift undetectable. A real downstream project (`meewar2`) had already accumulated 19 lines of un-recorded hand-edits to `docs/QUALITY_GATES.md` and 5 agent files with project-specific extensions, exactly the failure mode the absence of provenance produces.

Constraints:

- The scaffold must remain reusable across many project types and updatable by additive changes.
- The phase-gated workflow demands changes that can be reviewed, gated, and rolled back. M9 itself went through the planning gate (`artifacts/decision-memo.md`), the control-loop change gate, and two adversarial reviews before any code landed.
- Downstream consumers cannot adopt a complex distribution mechanism. The lifecycle had to ride on top of the existing `enrich-project.py` engine.
- The taxonomy must be expressive enough to describe both the scaffold's own files (so `--self-check` can audit) and the downstream files (so `--check` can audit), even when those classes differ for the same path.

## Options

**A. Status quo: copy + skip-if-exists.** Cheap, simple. Cannot detect drift, cannot upgrade selectively, cannot uninstall cleanly, no version traceability. Rejected as the failure mode driving M9.

**B. Provenance via filenames (e.g., suffix every scaffold-installed file).** Visually obvious, no JSON to parse. Breaks Claude Code conventions (`.claude/agents/<name>.md` is not negotiable), creates two source-of-truth paths per asset, and doesn't actually solve upgrade because there's nowhere to store the recorded hash. Rejected.

**C. Provenance manifest in the downstream project (`.scaffold/manifest.json`), with an explicit ownership taxonomy in the scaffold's capability manifest.** Adopted. The taxonomy lives in `capabilities/project-capabilities.yaml` (ownership per typed asset, plus a top-level `files:` map for unenumerated paths and an `ignore:` glob list for generated/example/test paths). The downstream record carries a versioned schema, scaffold version/commit, profile, normalization recipe, and per-file shas (normalized + strict).

**D. Plugin-based distribution (`.claude-plugin/marketplace.json`).** Listed as out-of-scope (M9.3). Would require redesigning the install model and trades scaffold ergonomics for marketplace ergonomics. Possibly revisited later; not in M9.

## Decision

Adopt option C with the following concrete decisions, all defensible against red-team review:

1. **Five ownership classes** (after splitting `scaffold-generated-once` per F2):
   - `scaffold` — copied verbatim; default upgrade target.
   - `bootstrap-frozen` — downstream-only, write-once, never re-rendered, never re-checked under `--check --strict`.
   - `bootstrap-with-template-tracking` — downstream-only, write-once, manifest stores `template_sha`; advisory drift on template change (`--check --include-templates`).
   - `scaffold-template` — scaffold-only (`templates/`); rendered into downstream files.
   - `scaffold-internal` — scaffold-only; never installable; runtime guard refuses any code path that tries.

2. **Allowlist semantics.** Anything not enumerated in `.scaffold/manifest.json` is implicitly project-owned. New scaffold paths colliding with existing project paths produce a fourth upgrade state ("collision-novel") that requires explicit `--adopt PATH` or `--rename-local PATH=NEWPATH` resolution.

3. **`.scaffold/manifest.json`** must be committed by the downstream project (the engine warns when it is gitignored). Schema is integer-versioned (`schema_version: 1`) with a linear-chain migration system (`MIGRATIONS` registry of pure functions composed in order).

4. **Plan-then-confirm upgrade.** `--upgrade` produces a per-file plan with default actions; ambiguous cases (drift on `scaffold` class, collision-novel) require explicit per-file flags before applying. No silent overwrite.

5. **Atomic writes throughout.** Every file copy uses `<dest>.scaffold-tmp` + `os.replace`. Engine startup sweeps `*.scaffold-tmp` orphans (recovery from SIGKILL). Manifest writes are atomic. Per-target advisory lock via `fcntl.flock` (with `--no-lock` escape hatch for CI).

6. **Pre-install safety.** Each file is regex-scanned for credential-shaped strings (AWS, Anthropic, Slack, GitHub, PEM); install refuses on match. Symlink escapes from the target dir are refused.

7. **Self-application.** `--self-check` walks the scaffold's own `git ls-files` and verifies every tracked file classifies into exactly one ownership class or matches an `ignore:` glob; the `ignore:` policy is constrained at runtime (no glob may match a path under `docs/`, `.claude/`, `scripts/`, `templates/`, `.devcontainer/`, or `capabilities/`).

8. **Subagent overlays deferred to M9.4.** `overlays: []` is reserved per-entry in v1; `*.project.md` is reserved as a path convention skipped by `--check`/`--upgrade` in M9. M9.4 will introduce concat semantics without a schema bump.

## Consequences

Positive:

- Downstream projects gain a real upgrade story: `--check` surfaces drift, `--upgrade` resolves it deterministically, `--uninstall` removes scaffold-owned files cleanly.
- The scaffold can evolve its templates over time without silently freezing downstream copies (advisory `template_sha` drift via `--include-templates`).
- The taxonomy is small (5 classes) but expressive enough to cover every tracked file in the scaffold itself, validated by `--self-check`.
- Hardcoded copy lists in `enrich-project.py` (workflow scripts filter, container files, root files, scaffold-internal deny-list) are migrating into the manifest, restoring single-source-of-truth.
- Each piece is independently testable; M9 ships with 24 tests covering all 11 acceptance criteria.

Negative / accepted tradeoffs:

- The on-disk manifest carries timestamps that change on every enrichment. Routine re-enrichments produce git churn; teams who dislike this can use `--check` (read-only) instead.
- Drifted agent files (5 of 9 in meewar2) require `--keep-local PATH` per file at every upgrade until M9.4 ships an overlay mechanism. The escape hatch is documented.
- The scaffold's own `.claude/settings.json` and `templates/settings.template.json` must be kept in sync manually until a future M9.x adds a lint enforcing this (currently a documented invariant).
- Concurrent enrichment is rejected via `fcntl.flock`. CI environments that legitimately run parallel enriches against shared homedirs must use `--no-lock` and provide their own mutex.

Migration:

- Existing downstream projects (e.g. meewar2) enrich-without-manifest are retrofitted via `--reconcile`, which snapshots the current on-disk state as the manifest baseline. Subsequent `--check` works normally from there.
- The `SCAFFOLD_INTERNAL_FILES` constant introduced in commit `73b1059` as a pre-M9 hotfix is retired; its function moves to `get_scaffold_internal_paths()`, which derives the deny-list from the capability manifest at runtime.
