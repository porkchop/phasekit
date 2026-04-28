# Install Lifecycle

This document describes how scaffold capabilities are installed into a downstream project, how to detect drift, how to upgrade, and how to uninstall cleanly. The contract is implemented in `scripts/enrich-project.py` and is governed by Phase M9 of the scaffold (`docs/META_PHASES.md`).

## TL;DR

```bash
# First-time install (greenfield)
bash /path/to/scaffold/scripts/bootstrap-new-project.sh

# Adopt an existing project (no overwrites)
bash /path/to/scaffold/scripts/adopt-existing-repo.sh

# Audit current state against the recorded manifest
python3 /path/to/scaffold/scripts/enrich-project.py --check .

# Rebuild the manifest from disk (for projects enriched before M9)
python3 /path/to/scaffold/scripts/enrich-project.py --reconcile .

# Migrate the manifest forward to the current schema (no other side effects)
python3 /path/to/scaffold/scripts/enrich-project.py --migrate-only .
```

## Provenance: `.scaffold/manifest.json`

After every successful enrichment, the engine writes `.scaffold/manifest.json` in the downstream project. **This file MUST be committed to the project's git history** (not gitignored). The engine warns if it is gitignored.

The manifest records:

- `schema_version` — integer; the engine migrates older manifests in-memory before any operation
- `scaffold_version` and `scaffold_commit` — what version of the scaffold installed this state
- `profile` — which profile was active (`default`, `game-project`, etc.)
- `enriched_at` — UTC ISO-8601 timestamp
- `normalization` — the recipe used for content hashing (`lf-trim-trailing-ws-single-final-newline` v1)
- `files` — one entry per scaffold-installed path:
  - `path`, `ownership`, `text` (binary or text)
  - `sha256` (normalized) and `sha256_strict` (byte-exact)
  - `overlays: []` (reserved for M9.4)
  - `installed_at` (UTC)
  - For `bootstrap-with-template-tracking`: `rendered_from` and `template_sha`

## Ownership classes (M9 §2)

| Class | Behavior on upgrade |
|---|---|
| `scaffold` | Re-enrichment overwrites after acknowledged drift; default upgrade target |
| `bootstrap-frozen` | Never re-rendered, never re-checked under `--check --strict` |
| `bootstrap-with-template-tracking` | Never auto-overwritten; manifest stores `template_sha`; advisory drift when source template changes |
| `scaffold-template` | Lives only in the scaffold (`templates/`); rendered into downstream files |
| `scaffold-internal` | Lives only in the scaffold; never installable |
| `scaffold-orphan` | Downstream-only; assigned by `--upgrade` when the new scaffold no longer declares a previously-tracked path. Left on disk; `--uninstall` removes. |

See `docs/CAPABILITY_MANIFEST.md` for the full schema and per-class semantics.

## Worked example: detecting and resolving drift

A team enriched a project with the `default` profile. Months later, they want to verify nothing has drifted from the canonical scaffold version.

```bash
$ python3 enrich-project.py --check ~/projects/myapp
--check: scaffold 0.0.0+git.fbfe29d
  clean: 26
  drifted: 1
  missing: 0
  DRIFT: docs/QUALITY_GATES.md  (scaffold)
```

Exit code is `3` (drift detected). The team has three options:

1. **Take the scaffold's version** (e.g. their hand-edits were a mistake): re-enrich with `--force`, or wait for `--upgrade --take-new` (Slice C).
2. **Keep their local edits**: do nothing; the drift will continue surfacing on every `--check` until they either revert or run `--reconcile` to snapshot the current state as the new manifest baseline.
3. **Reconcile**: run `--reconcile --force` to record the current on-disk state as authoritative. Use this when the drift represents intentional project-specific work that should be tracked locally rather than reverted.

## Worked example: retrofitting an existing project

A project was enriched before M9 and has no `.scaffold/manifest.json`. To bring it under the lifecycle contract:

```bash
$ python3 enrich-project.py --reconcile ~/projects/myapp
--reconcile: 27 files found on disk, 0 missing
Manifest written: ~/projects/myapp/.scaffold/manifest.json
```

After this, `--check` works normally, and future scaffold upgrades can be planned against the recorded baseline.

## Worked example: upgrading to a new schema version

When the scaffold ships a new manifest schema (e.g. v1 → v2 in the future):

```bash
$ python3 enrich-project.py --check ~/projects/myapp
--check: ... runs cleanly even on a v1 manifest; the engine migrates in memory.

$ python3 enrich-project.py --migrate-only ~/projects/myapp
Migrated manifest from schema v1 to v2.
```

`--migrate-only` rewrites the on-disk manifest without other side effects. Migrations are linear-chain pure functions in `scripts/migrations/`; the engine composes them in order.

## Reserved conventions

These are reserved by M9 for future sub-phases. Do not use them yet:

- **`overlays: []`** per file entry — M9.4 will populate with overlay metadata enabling append-only project-specific extensions to subagent files.
- **`*.project.md`** files alongside `.claude/agents/<name>.md` — M9.4 will introduce concat semantics so customizations survive scaffold upgrades.

In M9, agent customizations show up as drift. Use `--check` to inventory them; manual merge for now.

## Concurrency and locking

The engine takes a per-target advisory lock (`fcntl.flock` on `.scaffold/manifest.json.lock`) before any mutating operation. Concurrent runs against the same target are rejected with exit code `2`.

For CI environments that have their own mutual exclusion, pass `--no-lock` to bypass the engine's lock.

The lock is per-target-directory, never per-scaffold-repo. Concurrent enrichment of *different* targets is fully supported.

## Things M9 does NOT yet do

These are deferred to later sub-phases:

- `--upgrade` with plan-then-confirm and `--keep-local`/`--take-new` per-file overrides — **Slice C**
- `--uninstall` with `--include-once` and an uninstall log — **Slice C**
- Per-file atomic copy via `<dest>.scaffold-tmp` + `os.replace` for downstream files — **Slice C** (manifest writes are already atomic)
- Pre-install secrets regex scan (`AKIA*`, `BEGIN PRIVATE KEY`, `xox*`, real `sk-ant-*`) — **Slice C**
- Symlink refusal (refuse when target subdir realpath escapes the target) — **Slice C**
- Subagent overlay mechanism (`*.project.md` concat with conflict resolution) — **M9.4**
- Manifest signing / tamper detection — **M9.5**
- Multi-profile additive installs — **M9.6**

## Troubleshooting

**`No .scaffold/manifest.json in <target>`** — This project was enriched before M9. Run `--reconcile` to build a retroactive manifest.

**Exit code 2 with "another enrich-project.py process is operating on..."** — The lock is held by a concurrent run. Wait for it, or use `--no-lock` if you have your own mutex.

**`--check` exits 3 on a file you intentionally edited** — That's the drift detection working. Either revert your edits, or run `--reconcile --force` to record the current state as the new baseline. A future `--upgrade --keep-local <path>` (Slice C) will let you preserve edits while still pulling in scaffold updates elsewhere.

**Manifest is committed but `git status` shows it changed after every enrich** — Expected. The `enriched_at` and per-file `installed_at` timestamps update on every run. If this churn is undesirable in your workflow, `--check` is the read-only alternative.

**`--upgrade` interrupted partway (disk full, SIGTERM, etc.)** — Files that were successfully copied are scaffold-canonical on disk; the manifest, written last, may still record pre-upgrade shas for them. A subsequent `--check` will report drift on those files even though they match the scaffold. Recovery: re-run `--upgrade --yes` (or with the same per-file flags). The second pass sees clean files and is a no-op for them; any tmp files from the interrupt are swept by the orphan sweep at engine startup.

## See also

- `docs/CAPABILITY_MANIFEST.md` — manifest schema and ownership taxonomy
- `docs/META_PHASES.md` §M9 — phase definition and acceptance criteria
- `artifacts/decision-memo.md` — full design rationale (planning gate output)
- `artifacts/red-team-review.md`, `artifacts/red-team-review-v2.md` — adversarial reviews of the design
