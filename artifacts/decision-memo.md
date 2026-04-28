# Decision Memo — M9 Install Lifecycle and Provenance (v2.1)

Status: revised after `architecture-red-team` review of v1, then closed-out edits after the v2 review (`artifacts/red-team-review-v2.md`). Plan only; no implementation.
Scope: `docs/META_PHASES.md` §M9.

## 0. Changelog vs v1

- **F1 (B):** Adopted. `ignore:` constrained; positive/negative-control tests (§8).
- **F2 (B):** Adopted. 5-class taxonomy; `scaffold-generated-once` splits into `bootstrap-frozen` and `bootstrap-with-template-tracking` (§2).
- **F3 (B):** Adopted. Collision-novel state with default exit 3, `--adopt`, `--rename-local` (§6).
- **F4 (M):** Adopted. Linear-chain pure-function migrations, fixture per migration, `--migrate-only` (§3).
- **F5 (M):** Adopted. `fcntl.flock` per target dir, `--no-lock`, degradation documented (§9).
- **F6 (M):** Adopted. Normalized hash default; per-file `text:`; `--strict` (§3).
- **F7 (B):** Adopted. `overlays: []` reserved; `*.project.md` skipped in M9 (§3, §7).
- **F8 (M):** Adopted. Per-file `.scaffold-tmp` + `os.replace`; orphan sweep (§5).
- **F9 (M):** Adopted. Class-assignment appendix §A; `.scaffold/manifest.json` MUST be committed; downstream `enrich-project.py` is `scaffold` with sha-disagreement warning.
- **F10 (M):** Adopted verbatim, replaces v1 §13.
- **F11 (M):** Adopted: symlink refusal, pre-install secret scan, removal policy, manifest commit policy (§9).
- **F12 (Mi):** Adopted. `--interactive` flag, mutex with `--yes`.

No pushbacks. Pure adoption.

### v2.1 closeout (post-v2 review)

- **NI-1:** Adopted. Acceptance criterion #11 added (§13) covering F2's advisory-drift behavior on `template_sha` change.
- **NI-2:** Adopted. Slice B (§10) now ships `templates/settings.template.json` alongside `templates/AGENTS.template.md` so `.claude/settings.json`'s `bootstrap-with-template-tracking` class is honest from day one.
- **Q2:** Adopted. One-line clarification in §A intro: classes describe downstream behavior; in the scaffold repo, `bootstrap-*` and `scaffold-template` files are scaffold-internal in the sense they are never re-installed into the scaffold itself.
- **NI-3:** Adopted. §3 hashing now states explicitly that `--check --strict` does not re-check `bootstrap-frozen` files.
- **Q1, Q3:** No memo change required (low migration cost; non-blocking).

## 1. Decision summary

- 5-class taxonomy: `scaffold`, `bootstrap-frozen`, `bootstrap-with-template-tracking`, `scaffold-template`, `scaffold-internal`; plus a constrained `ignore:` glob list (§8).
- Ownership in `capabilities/project-capabilities.yaml` (extended); single source of truth.
- `.scaffold/manifest.json` carries integer `schema_version`, normalized hashing recipe, `overlays: []` per file; MUST be committed.
- `enrich-project.py` adds `--upgrade`, `--uninstall`, `--check`, `--reconcile`, `--self-check`, `--migrate-only`, `--interactive`. Default plan-then-confirm.
- Per-file atomic copy (`.scaffold-tmp` + `os.replace`); manifest write last; `fcntl.flock` per target.
- Overlay mechanism deferred to M9.4; schema and path conventions reserved now.

## 2. Taxonomy (5 classes)

| Class | Where | Upgrade behavior |
|---|---|---|
| `scaffold` | scaffold + downstream | re-enrichment overwrites after drift ack |
| `bootstrap-frozen` | downstream, write-once | never re-rendered, never re-checked |
| `bootstrap-with-template-tracking` | downstream, write-once | never auto-overwrite; manifest stores `template_sha`; advisory drift on template change |
| `scaffold-template` | scaffold only (`templates/`) | rendered, never copied verbatim |
| `scaffold-internal` | scaffold only | never installable; `--self-check` enforces |

The split (F2) ensures that a v2 scaffold updating `CLAUDE.template.md` surfaces an advisory rather than going silent.

Allowlist semantics retained: downstream files not enumerated in `.scaffold/manifest.json` are project-owned. Collision-novel handled per §6.

## 3. Manifest schema (downstream `.scaffold/manifest.json`)

```json
{
  "schema_version": 1,
  "scaffold_version": "0.0.0+git.d33a601",
  "scaffold_commit": "d33a601",
  "profile": "default",
  "enriched_at": "2026-04-25T18:00:00Z",
  "normalization": {
    "recipe": "lf-trim-trailing-ws-single-final-newline",
    "version": 1
  },
  "files": [
    {
      "path": ".claude/agents/project-lead.md",
      "ownership": "scaffold",
      "text": true,
      "sha256": "ab12...",
      "sha256_strict": "cd34...",
      "overlays": [],
      "installed_at": "2026-04-25T18:00:00Z"
    },
    {
      "path": ".claude/CLAUDE.md",
      "ownership": "bootstrap-with-template-tracking",
      "text": true,
      "sha256": "ef56...",
      "sha256_strict": "78ab...",
      "rendered_from": "templates/CLAUDE.template.md",
      "template_sha": "9c01...",
      "overlays": [],
      "installed_at": "2026-04-25T18:00:00Z"
    }
  ]
}
```

### Hashing (F6)

- Default `sha256` is normalized: UTF-8, LF endings, strip trailing whitespace per line, single trailing newline.
- `sha256_strict` is byte-exact, used by `--check --strict`.
- Binary files: `text: false`, `sha256 == sha256_strict`.
- Recipe is named and versioned; if M9.x changes it, recipe version increments and a migration rewrites stored hashes.
- `--check --strict` does not re-check `bootstrap-frozen` files (consistent with their never-re-checked semantics in §2 — the strict-mode hash is still stored for forensic purposes but the upgrade path bypasses them).
- Falsifiable: identical content under LF/CRLF/CR-only must hash equal under default, distinct under `--strict`.

### Migrations (F4)

- Each release ships exactly one `scripts/migrations/vN_to_vN+1.py` with `def migrate(d: dict) -> dict`. Pure, no I/O.
- Engine composes linearly from on-disk version to current. No 2D matrix.
- Each migration ships fixtures (`vN_old.json`, `vN_expected.json`) with round-trip and idempotency assertions.
- `--migrate-only` rewrites manifest schema without other actions.
- Falsifiable: synthetic v0 against v3 engine equals `compose(v0_to_v1, v1_to_v2, v2_to_v3)(v0)`.

### Reserved (F7)

- `overlays: []` per file entry from `schema_version: 1`. Empty in M9; M9.4 populates without version bump.
- `*.project.md` skipped by `--check`/`--upgrade` in M9.
- `--keep-local` documented as M9.0–M9.3 stop-gap.

## 4. Scaffold-side manifest location

Extend `capabilities/project-capabilities.yaml` (unchanged from v1):

- `ownership:` per existing typed asset.
- Top-level `files:` map for root files, container files, templates, `CONTINUE_PROMPT.txt`. Retires the hardcoded copy lists in `enrich-project.py` (workflow scripts ~L240, container files ~L277, root files ~L264).
- Top-level `ignore:` glob list, constrained by §8.
- Top-level `text_default: true` with per-entry `text:` overrides.

## 5. CLI surface

Shared flags: `--dry-run`, `--profile`, `--target DIR`, `--yes`, `--interactive` (mutex with `--yes`), `--no-lock`, `--strict`. Exit codes: `0` clean, `1` user-visible failure, `2` usage error, `3` drift / collision-novel.

| Flag | Behavior | Notes |
|---|---|---|
| `--check` | per-file table: clean / drifted / missing / collision-novel | read-only; `--strict` uses `sha256_strict` |
| `--upgrade` | plan-then-confirm; mandatory `y/N` unless `--yes`; per-file `[k/t/d/s]` under `--interactive` | exit 3 if any collision-novel unresolved (F3) |
| `--uninstall` | `--include-once` to remove `bootstrap-*` classes; uninstall log written before deletion | |
| `--reconcile` | retroactive manifest for projects enriched pre-M9; `--drop-orphans` to remove `scaffold-orphan` files (F11) | |
| `--self-check` | classify every `git ls-files` path in scaffold repo against the manifest | runs `ignore:` constraint (§8) |
| `--migrate-only` | upgrade manifest schema without other action | F4 |
| `--adopt PATH` | resolve collision-novel: start tracking on-disk content under scaffold-new sha | F3 |
| `--rename-local PATH=NEWPATH` | resolve collision-novel: move on-disk file aside | F3 |
| `--accept-removal PATH` | actually delete a removed scaffold file (default: leave + downgrade to `scaffold-orphan`) | F11 |
| `--keep-local PATH` / `--take-new PATH` | per-file plan override | stop-gap for M9.4 |

### Atomicity (resolves F8)

- Every per-file write goes to `<dest>.scaffold-tmp` then `os.replace`. Manifest write goes to `.scaffold/manifest.json.scaffold-tmp` then `os.replace`, last.
- Engine startup sweeps `**/*.scaffold-tmp` under the target, logs and removes orphans.
- Falsifiable: monkeypatched `os.replace` raising mid-run leaves no partial dest; second clean run completes; manifest reflects only what landed.

## 6. Drift / upgrade states

Per scaffold-owned file at `--upgrade`:

- **(a) clean** — local == manifest; engine overwrites silently if scaffold-new differs.
- **(b) drifted, take-new** — local != manifest; user opts in.
- **(c) drifted, keep-local** — local != manifest; user opts to preserve.
- **(d) collision-novel** (F3) — manifest absent, scaffold-new declares path, on-disk file exists. Default: refuse, exit 3, name path. Resolve with `--adopt PATH` or `--rename-local PATH=NEWPATH`.

`bootstrap-with-template-tracking` files appear in `--check` as advisory when `template_sha` changed; never auto-overwritten. `--upgrade --rerender PATH` (Slice C) re-runs the template.

## 7. Overlays — hooks now, mechanism in M9.4

M9 reserves `overlays: []` and `*.project.md` (F7). M9.4 introduces concat semantics, conflict resolution, and overlay storage without bumping `schema_version` or path conventions.

## 8. `--self-check` (F1)

Pass: every `git ls-files` path classifies into exactly one of the five classes, or matches an `ignore:` glob.

`ignore:` is constrained: at runtime, assert no glob matches any path in `git ls-files docs/ .claude/ scripts/ templates/ .devcontainer/ capabilities/`. Violation → exit 1, name the offending glob and path. Allowed targets: `artifacts/*.json`, `artifacts/*.md`, `dist/skills/**`, `generated/**`, `examples/**`, `docs/adr/.gitkeep`.

Tests:
- Positive: `docs/QUALITY_GATES.md → scaffold`, `templates/CLAUDE.template.md → scaffold-template`, `LICENSE → scaffold-internal`.
- Negative: CI injects `docs/UNCLASSIFIED.md`; expect exit 1 with path named.

Falsifiable: must fail on `master` today, pass after Slice A, and fail again if `ignore: ["docs/**"]` is added.

## 9. Failure modes

| # | Mode | Resolution |
|---|---|---|
| 1 | Corrupt manifest | refuse all ops except `--reconcile --force` |
| 2 | Partial copy | `.scaffold-tmp` + `os.replace`; orphan sweep (F8) |
| 3 | Line-ending churn | normalized hash (F6) |
| 4 | Schema migration | linear chain + fixtures (F4) |
| 5 | Concurrent enrichment | `fcntl.flock` per target; `--no-lock` for CI; warn-and-proceed on filesystems lacking flock; two-subprocess test, exactly one wins (F5) |
| 6 | Hand-edits during plan→apply | re-hash at apply time, bail on mismatch |
| 7 | Symlinks (F11a) | refuse when target subdir realpath escapes target; scaffold-side symlinks are `--self-check` errors |
| 8 | Secrets in scaffold (F11b) | pre-install regex: `AKIA[0-9A-Z]{16}`, `BEGIN PRIVATE KEY`, `xox[baprs]`, `sk-ant-[a-zA-Z0-9_-]{20,}`; documented placeholder excludes; exit 1 on match |
| 9 | Scaffold removed a file (F11c) | plan `removed` section; default keep + downgrade to `scaffold-orphan`; `--accept-removal PATH` deletes; `--reconcile --drop-orphans` cleans up |
| 10 | Manifest commit policy (F11d) | `.scaffold/manifest.json` MUST be committed; engine warns when gitignored |
| 11 | Cross-platform paths | forward slashes in manifest; case-sensitive matching documented |
| 12 | Stale downstream `enrich-project.py` (F9) | engine compares own sha to manifest's; warns on disagreement |

## 10. Implementation slicing

### Slice A — read-only foundation
- Extend `capabilities/project-capabilities.yaml`: `ownership:` per asset, top-level `files:`, `ignore:` (constrained), `text_default`.
- Implement `--self-check` (positive + negative controls), `--check` (normalized + `--strict`).
- Class-assignment appendix (§A) is the seed for the YAML extension.
- Update `docs/CAPABILITY_MANIFEST.md`.

Acceptance: F10 criteria 1, 2 pass.

### Slice B — manifest writes + reconcile
- Atomic manifest writer; `schema_version: 1`; `migrations/` skeleton + first fixture.
- `fcntl.flock` per-target lock (F5).
- `--reconcile` retrofit with normalized + strict shas.
- `templates/AGENTS.template.md` and `templates/settings.template.json` (both `bootstrap-with-template-tracking`); the latter materializes the template that `.claude/settings.json`'s class implies.
- `docs/INSTALL_LIFECYCLE.md` worked example (commit policy, secret-scan placeholders, `*.project.md` reservation).

Acceptance: F10 criteria 3, 8, 9, 10 pass.

### Slice C — mutating lifecycle
- `--upgrade` (plan-then-confirm, `--interactive`, collision-novel resolution, `--accept-removal`).
- `--uninstall --include-once` with uninstall log.
- Per-file `.scaffold-tmp` + `os.replace`; orphan sweep.
- Pre-install secret regex scan.
- Symlink refusal.
- ADR `docs/adr/ADR-NNNN-install-lifecycle.md`.
- Retire hardcoded copy lists in `enrich-project.py`.

Acceptance: F10 criteria 4, 5, 6, 7 pass.

## 11. Out of scope

M9.1–M9.3 (already declared). New: M9.4 overlays, M9.5 manifest signing, M9.6 multi-profile.

## 12. Risks (delta)

| Risk | Mitigation |
|---|---|
| Constrained `ignore:` still too permissive | whitelist of allowed prefixes, audited in `--self-check` |
| Template-tracking advisory becomes noise | `--check` excludes template-source diff unless `--include-templates` |
| Linear migrations churn on branchy releases | hotfixes skip; only minor+ ships migrations |
| Secret-scan false-positives | documented placeholder forms; CI fixture asserts non-match |

## 13. Acceptance criteria for this planning step (replaces v1 §13)

Verbatim from F10:

1. `--self-check` on `master` exits non-zero before Slice A; exits zero after Slice A; the diff between the two states is exclusively a manifest extension and an implementation in `enrich-project.py`. No `ignore:` glob added that matches a path in `git ls-files docs/ .claude/ scripts/ templates/ .devcontainer/ capabilities/`.
2. Slice A includes a positive-control test asserting three named files (`docs/QUALITY_GATES.md`, `templates/CLAUDE.template.md`, `LICENSE`) land in their expected classes, and a negative-control test injecting an unclassified file in CI and asserting exit 1 with the path named.
3. Slice B `--reconcile` on a synthetic v0 project (no `.scaffold/`) produces a manifest where every file's `sha256` is reproducible by an independent script computing sha256 over the same byte normalization recipe.
4. Slice C `--upgrade` on a synthetic project with one drifted scaffold-owned file plus one collision-novel file (`docs/RUNBOOK.md` already present, scaffold v2 declares it) produces exit 3 and names both paths in the plan.
5. Slice C `--upgrade` interrupted by SIGKILL between two file copies leaves the target with no `*.scaffold-tmp` files after a clean re-run, and the manifest reflects the post-rerun state.
6. Slice C round-trip: enrich → modify scaffold-owned file → `--upgrade --keep-local PATH` preserves the edit, updates only the manifest sha, and `--check` exits 0 thereafter.
7. `--uninstall --include-once` on a synthetic project removes all `scaffold` and `bootstrap-*` files; non-manifest files are byte-identical before and after.
8. Manifest schema includes `overlays: []` per file entry as a reserved field, even when unused.
9. Migration test fixture: synthetic v0 manifest → run engine that ships at v1 → manifest is now v1, content equivalent; round-trip and idempotency assertions pass.
10. Concurrent enrichment of the same target by two processes: exactly one succeeds, the other exits with a non-zero usage error code; no corrupted manifest.
11. After Slice B, modify `templates/CLAUDE.template.md` in the scaffold; on a downstream project enriched before the change, `--check --include-templates` exits with an advisory code, names the affected path, and never auto-overwrites the rendered file. Verifies F2's `template_sha` advisory-drift behavior end-to-end.

`artifacts/phase-approval.json` is **not** written by this step. Phase approval comes after Slice C lands and the 11 criteria above are demonstrably green.

---

## A. Class-assignment appendix (resolves F9)

Every file from `git ls-files` today, with proposed class. Seeds the Slice A YAML extension.

Note: classes describe downstream behavior. In the scaffold repo itself, `bootstrap-*` and `scaffold-template` files are scaffold-internal in the sense that they are never re-installed *into* the scaffold; the appendix uses the downstream-facing class so the same YAML can seed both `--self-check` (scaffold-side) and the per-file ownership annotations consumed when generating downstream manifests.

```yaml
# scaffold (copied verbatim downstream)
.claude/agents/architecture-red-team.md: scaffold
.claude/agents/backend-builder.md: scaffold
.claude/agents/code-reviewer.md: scaffold
.claude/agents/engine-builder.md: scaffold
.claude/agents/frontend-builder.md: scaffold
.claude/agents/project-lead.md: scaffold
.claude/agents/qa-playwright.md: scaffold
.claude/agents/release-hardening.md: scaffold
.claude/agents/strategy-planner.md: scaffold
.claude/hooks/deny-dangerous-commands.sh: scaffold
.claude/settings.container.example.json: scaffold
.claude/skills/autonomous-product-builder/SKILL.md: scaffold
.claude/skills/autonomous-product-builder/agents/openai.yaml: scaffold
.claude/skills/autonomous-product-builder/references/adversarial-checklist.md: scaffold
.claude/skills/autonomous-product-builder/references/decision-memo-template.md: scaffold
.claude/skills/autonomous-product-builder/references/workflows.md: scaffold
.claude/skills/autonomous-product-builder/scripts/check_scaffold.py: scaffold
.devcontainer/Dockerfile: scaffold
.devcontainer/devcontainer.json: scaffold
.devcontainer/entrypoint.sh: scaffold
.devcontainer/init-firewall.sh: scaffold
CONTINUE_PROMPT.txt: scaffold
docs/ADR_TEMPLATE.md: scaffold
docs/CAPABILITY_MANIFEST.md: scaffold
docs/CONTAINERIZATION.md: scaffold
docs/EXECUTION_MODES.md: scaffold
docs/QUALITY_GATES.md: scaffold
docs/REASONING_PROFILES.md: scaffold
docs/USAGE_PATTERNS.md: scaffold
scripts/container-setup.sh: scaffold
scripts/enrich-project.py: scaffold              # engine warns on downstream sha mismatch
scripts/generate-skill.py: scaffold
scripts/package-skill.py: scaffold
scripts/run-phase.sh: scaffold
scripts/run-until-done.sh: scaffold
scripts/validate-skill.py: scaffold
scripts/verify-container.sh: scaffold

# bootstrap-frozen (write-once downstream, scaffold-internal in repo)
docs/ARCHITECTURE.md: bootstrap-frozen
docs/PHASES.md: bootstrap-frozen
docs/PROD_REQUIREMENTS.md: bootstrap-frozen
docs/SPEC.md: bootstrap-frozen

# bootstrap-with-template-tracking (advisory drift on template change)
.claude/settings.json: bootstrap-with-template-tracking

# scaffold-template (rendered downstream)
templates/CLAUDE.template.md: scaffold-template
templates/adr.template.md: scaffold-template
templates/architecture.template.md: scaffold-template
templates/prod-requirements.template.md: scaffold-template
templates/skill/SKILL.template.md: scaffold-template
templates/skill/agents/openai.yaml: scaffold-template
templates/spec.template.md: scaffold-template

# scaffold-internal (never installable)
.claude/CLAUDE.md: scaffold-internal             # downstream gets a rendered copy
.gitignore: scaffold-internal
AGENTS.md: scaffold-internal                     # downstream gets templates/AGENTS.template.md
CONTRIBUTING.md: scaffold-internal
KICKOFF.md: scaffold-internal
LICENSE: scaffold-internal
README.md: scaffold-internal
capabilities/project-capabilities.yaml: scaffold-internal  # M9.6 will extend via overlay
docs/COMPATIBILITY.md: scaffold-internal
docs/EXTENSION_PATTERNS.md: scaffold-internal
docs/META_PHASES.md: scaffold-internal
docs/META_SPEC.md: scaffold-internal
docs/SELF_APPLICATION_EXAMPLE.md: scaffold-internal
scripts/adopt-existing-repo.sh: scaffold-internal
scripts/bootstrap-new-project.sh: scaffold-internal
scripts/verify-phase.sh: scaffold-internal

# ignore (constrained closed list, audited per §8)
artifacts/decision-memo.md: ignore
artifacts/phase-approval.json: ignore
docs/adr/.gitkeep: ignore
examples/game-project/README.md: ignore
examples/saas-project/README.md: ignore
generated/skills/autonomous-product-builder/SKILL.md: ignore
generated/skills/autonomous-product-builder/agents/openai.yaml: ignore

# downstream-only path (not in scaffold git ls-files)
.scaffold/manifest.json: project-committed       # MUST be committed; engine warns if gitignored
```

Allowed `ignore:` globs: `artifacts/*.json`, `artifacts/*.md`, `docs/adr/.gitkeep`, `examples/**`, `generated/**`, `dist/skills/**`.
