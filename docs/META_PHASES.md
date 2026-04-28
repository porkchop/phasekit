# Meta-Phases for Scaffold Self-Improvement

## Execution rule

Start from the beginning in audit mode.

If a capability already exists:
- approve it if it satisfies the phase
- patch it minimally if it is incomplete
- rebuild only if the design is fundamentally flawed

Do not skip review, validation, or commit gates because code already exists.

Note: Existing approved phase numbers are stable and must not be renumbered retroactively.
New work discovered later should be appended as new phases rather than inserted before already-approved phases.

---

## Meta Phase M0 - Self-hosting foundation

### Goal
Define the scaffold as its own product and establish the self-improvement workflow.

### Deliverables
- `docs/META_SPEC.md`
- `docs/META_PHASES.md`
- updated `docs/QUALITY_GATES.md` with meta-project rules
- updated lead instructions for scaffold self-improvement mode

### Acceptance criteria
- scaffold self-improvement scope is documented
- audit-first behavior is documented
- meta-phase execution rules are documented
- control-loop changes require rationale and rollback notes

---

## Meta Phase M1 - Capability manifest

### Goal
Create a single source of truth for which capabilities a downstream project should receive.

### Deliverables
- `capabilities/project-capabilities.yaml`
- schema/rules documentation for the manifest
- docs explaining how manifest entries map to generated outputs

### Acceptance criteria
- manifest format is documented
- manifest can represent agents, skills, hooks, docs, scripts, and settings fragments
- generated outputs can trace back to manifest entries

---

## Meta Phase M2 - Skill generation templates

### Goal
Add templates and generation logic for project-enrichment skills.

### Deliverables
- `templates/skill/`
- script(s) for generating skill folders from templates/manifest
- example generated skill folder(s)

### Acceptance criteria
- scaffold can generate at least one valid skill folder from template + manifest
- generated skill includes required structure
- generation is reproducible

---

## Meta Phase M2.5 — Claude startup file generation

### Goal
Support generation of downstream `.claude/CLAUDE.md` and optional rules files from templates and capability manifest data.

### Deliverables
- `templates/CLAUDE.template.md`
- generation logic or documented generation flow for downstream Claude startup files
- docs describing how downstream startup files are derived

### Acceptance criteria
- scaffold can generate a valid downstream `.claude/CLAUDE.md`
- generated file is concise and references project docs
- scaffold repo’s own `.claude/CLAUDE.md` remains generic and stable
- generation behavior is documented

---

## Meta Phase M3 - Skill validation and packaging

### Goal
Support validation and packaging of generated skills as deliverables.

### Deliverables
- script(s) to validate generated skills
- script(s) to package generated skills as `skill.zip`
- docs for packaging flow
- output directory conventions

### Acceptance criteria
- generated skills validate successfully
- packaging produces valid `skill.zip`
- packaging workflow is documented
- failures are reported clearly

---

## Meta Phase M4 - Downstream enrichment flow

### Goal
Make it easy to apply scaffold capabilities to another repo.

### Deliverables
- adoption workflow docs
- scripts to install/apply generated agents/skills/settings into a downstream repo
- examples for greenfield and existing-repo adoption

### Acceptance criteria
- a downstream repo can be enriched from scaffold outputs
- docs cover both new and existing repos
- lead can operate in audit mode on adopted repos

---

## Meta Phase M4.5 — Execution modes and non-interference

### Goal
Formalize interactive collaboration mode and containerized unattended mode.

### Deliverables
- docs describing execution modes
- settings strategy for project vs local vs command-line overrides
- wrapper updates if needed
- non-interference guidance for working on the scaffold repo itself

### Acceptance criteria
- interactive work on the scaffold repo uses conservative defaults
- unattended mode is opt-in
- mode selection is documented
- project settings do not force permissive execution for all users
- direct collaboration with Claude on the scaffold repo remains straightforward

---

## Meta Phase M5 — Containerized unattended execution

### Goal
Support running the scaffold inside an isolated container for permissive unattended operation.

### Deliverables
- containerization docs and initialization flow
- reference container configuration or integration strategy
- wrapper scripts that can run in-container
- safety guidance for trusted repositories
- optional setup flow to initialize local container-specific settings

### Acceptance criteria
- a user can initialize the containerized environment from docs/scripts
- unattended wrappers work in the container
- permissive mode is confined to local/container-specific configuration
- the approach is documented as opt-in
- the approach does not require VS Code as the only usage path

---

## Meta Phase M5.1 — Adopt Anthropic reference devcontainer

### Goal
Replace the custom container setup with one based on Anthropic's reference devcontainer, gaining firewall-based network isolation while preserving the existing phase-loop workflow and CLI-only usage path.

### Deliverables
- `.devcontainer/devcontainer.json` based on Anthropic's reference
- `.devcontainer/Dockerfile` adapted from Anthropic's reference (with scaffold-specific additions: Python 3, pyyaml)
- `.devcontainer/init-firewall.sh` from Anthropic's reference (domain-whitelisted outbound firewall)
- updated `scripts/container-setup.sh` to build from `.devcontainer/`
- removal or deprecation of `container/Dockerfile`
- updated `docs/CONTAINERIZATION.md`
- updated `capabilities/project-capabilities.yaml` manifest entries

### Acceptance criteria
- container uses Anthropic's firewall rules (default-deny + whitelisted domains)
- `run-until-done.sh` continues to work as the entrypoint
- `container-setup.sh build/run/shell` commands still work
- VS Code is not required (CLI-only path preserved)
- non-root user is preserved
- docs updated to reflect the new setup

---

## Meta Phase M5.5 — Reasoning profile guidance

### Goal
Define when deeper reasoning should be used for scaffold work without forcing it for all interactions.

### Deliverables
- guidance for lead/planner/reviewer roles
- docs describing when deeper reasoning is helpful
- non-interference guidance for lightweight interactive work

### Acceptance criteria
- deeper reasoning is recommended for complex design, architecture, and review work
- ordinary collaboration remains lightweight by default
- no global always-on heavy mode is required

---

## Meta Phase M6 - Planner and red-team integration

### Goal
Formalize plan/adversarial review for material architectural work.

### Deliverables
- updated `strategy-planner`
- updated `architecture-red-team`
- ADR template and usage docs
- lead workflow updated to require design memo + critique for major changes

### Acceptance criteria
- design-heavy work triggers planner + red-team
- decision memos are stored in docs or ADRs
- implementation tasks trace back to an approved decision

---

## Meta Phase M7 - Self-application and refinement

### Goal
Use the scaffold to improve itself under full quality gates.

### Deliverables
- audited subagent definitions
- improved templates/docs/scripts as needed
- self-application usage example
- refined guardrails for control-loop changes

### Acceptance criteria
- scaffold successfully improves at least one part of itself
- reviewer and red-team approve the changes
- workflow remains stable after self-application

---

## Meta Phase M8 - Generalization and compatibility

### Goal
Broaden the scaffold beyond the original project style without losing quality.

### Deliverables
- additional project-type examples
- generalized usage patterns
- compatibility notes
- migration/versioning guidance

### Acceptance criteria
- scaffold is clearly reusable for multiple project types
- compatibility guidance exists for older scaffold-based repos
- extension patterns are documented

---

## Meta Phase M9 — Install lifecycle and provenance

### Goal
Establish a provenance, upgrade, and uninstall contract for downstream projects enriched by the scaffold, so that scaffold-owned and project-owned files can be distinguished, scaffold versions can be tracked, and scaffold capabilities can be surgically upgraded or removed without coupling to or silently overwriting project work.

### Background
The current `enrich-project.py` engine copies scaffold assets into shared directories (`.claude/`, `docs/`, `scripts/`, `.devcontainer/`) without writing any provenance record. Downstream projects therefore cannot:
- identify which files came from the scaffold vs. local authorship
- know which scaffold version they were enriched from
- detect drift between scaffold canonical and local state
- be upgraded to a newer scaffold release without manual reconciliation
- be uninstalled cleanly

Several copy lists are also hardcoded in the engine (workflow scripts filtered to 2 of 8, container files, root workflow files, doc class filters) instead of declared in the capability manifest. This splits source of truth between data and code, and an implicit deny-list for meta-only files (`LICENSE`, `CONTRIBUTING.md`, root `README.md`, root `AGENTS.md`) only works because no copy path enumerates root files broadly — it is not robust to future contributions.

### Ownership classes
This phase formalizes three classes for every scaffold-installed file:
- **`scaffold`** — canonically owned by the scaffold; re-enrichment overwrites unless local content has drifted from manifest sha (then surfaces a diff and requires acknowledgement)
- **`scaffold-generated-once`** — installed once at bootstrap, then becomes project-owned; re-enrichment does not overwrite (e.g. `.claude/CLAUDE.md`, downstream `AGENTS.md`, the rendered SPEC/ARCHITECTURE/PROD_REQUIREMENTS templates)
- **`scaffold-internal`** — never copied to downstream projects under any code path (e.g. META_SPEC, META_PHASES, LICENSE, CONTRIBUTING.md, the scaffold's own root `AGENTS.md` and `README.md`)

### Self-application
The capability manifest must be expressive enough to classify every tracked file in this scaffold repo itself, not just files destined for downstream projects. This is the self-test of the schema: if it cannot describe its own contents cleanly, it is not yet sound. The `--self-check` command (see deliverables) walks the scaffold repo, classifies every tracked file against the manifest, and fails when any file is unclassified or mis-classified. This is the recursive self-application — the scaffold's process governs the scaffold's own contents — without inventing a circular `.scaffold/manifest.json` for the scaffold repo (which would not carry useful provenance, since the scaffold sources are the canonical originals, not copies).

### Deliverables
- `.scaffold/manifest.json` schema and example written into the docs
- Ownership classes declared in `capabilities/project-capabilities.yaml` for every installable asset (replaces hardcoded filters in `enrich-project.py`)
- `enrich-project.py` extended with:
  - `--upgrade` — 3-way reconciliation (manifest sha, scaffold-new sha, project-current sha) producing a planned diff before applying
  - `--uninstall` — removes only files marked `scaffold` and `scaffold-generated-once`
  - `--check` — drift detection; non-zero exit when scaffold-owned files diverge from manifest sha
  - `--reconcile` — one-time retrofit for projects enriched before M9; walks the project, computes shas, writes a retroactive manifest at the current scaffold version
  - `--self-check` — walks the scaffold repo itself, classifies every tracked file against the capability manifest's ownership taxonomy, and fails when any file is unclassified or mis-classified
- Explicit, declared deny-list of `scaffold-internal` files; any attempt to install one fails with a clear error
- `templates/AGENTS.template.md` for downstream projects (parameterized like `CLAUDE.template.md`), installed as `scaffold-generated-once`
- `docs/INSTALL_LIFECYCLE.md` documenting install / upgrade / uninstall / drift / reconciliation flows with a worked example
- Migration of hardcoded copy lists (workflow scripts, container files, root workflow files) from `enrich-project.py` into the capability manifest
- Updated `docs/CAPABILITY_MANIFEST.md` to document ownership classes and provenance fields
- Scaffold version field declared in `capabilities/project-capabilities.yaml` (or derived from git for unreleased states)

### Acceptance criteria
- Every successful enrichment writes or updates `.scaffold/manifest.json` recording scaffold version/commit, profile, timestamp, and the list of installed files with their ownership class and content sha256
- Re-running enrichment against a project with no local hand-modifications produces no diff to any scaffold-owned file (idempotent)
- Re-running enrichment against a project where a scaffold-owned file has drifted surfaces the drift and requires explicit acknowledgement before overwriting
- `--upgrade` produces a planned diff (added / removed / changed / locally-modified) and applies it only after user confirmation
- `--uninstall` removes files marked `scaffold` and `scaffold-generated-once` (with appropriate user acknowledgement for the once-generated class) and leaves files marked `project` untouched
- `--check` exits non-zero when scaffold-owned files have drifted from manifest sha
- `--reconcile` produces a valid manifest for an existing enriched project that did not previously have one
- `--self-check` succeeds against the scaffold repo with every tracked file classified by the manifest, and fails (non-zero exit) when a tracked file is unclassified, mis-classified, or matches more than one class
- `scaffold-internal` files cannot be installed by any code path; the engine refuses with a clear error
- `templates/AGENTS.template.md` is installed once, then becomes project-owned (re-enrichment does not overwrite)
- `docs/INSTALL_LIFECYCLE.md` is concise, has a worked example, and is referenced from `README.md` and `AGENTS.md`
- All copy lists previously hardcoded in `enrich-project.py` are now declared in `capabilities/project-capabilities.yaml`

### Required reviews
This phase changes the install / upgrade / uninstall contract and is subject to the planning gate and the control-loop change gate (see `docs/QUALITY_GATES.md`):
- `strategy-planner` for the manifest schema and CLI command surface
- `architecture-red-team` for failure modes (corrupt manifest, partial enrichment, schema migration over time, concurrent enrichment, hand-edits during upgrade, scaffold-uninstall recovery)
- `code-reviewer` for engine changes
- `artifacts/decision-memo.md` written before implementation
- ADR under `docs/adr/` capturing the ownership-class taxonomy and manifest schema

### Out of scope (see sub-phases below)
M9 ships the install/upgrade/uninstall contract. Eight follow-on sub-phases are articulated below. None gate M9 approval; each is independently plannable.

---

## Meta Phase M9.1 — Namespaced scaffold docs path

### Goal
Move scaffold-canonical docs (`QUALITY_GATES.md`, `USAGE_PATTERNS.md`, `EXECUTION_MODES.md`, `REASONING_PROFILES.md`, `ADR_TEMPLATE.md`, `CONTAINERIZATION.md`, `INSTALL_LIFECYCLE.md`) under `docs/scaffold/` so human readers can tell scaffold-territory from project-territory at a glance.

### Background
Today scaffold-canonical docs live alongside project-owned docs in the same `docs/` directory. The manifest distinguishes them by ownership class, but a human reading `ls docs/` cannot. M9.1 adds the directory-level discipline that complements the manifest's machine-readable taxonomy.

### Deliverables
- Move scaffold-canonical docs to `docs/scaffold/`
- Update `docs.<KEY>.path` entries in `capabilities/project-capabilities.yaml`
- Add a v1 → v2 manifest migration rewriting downstream entries' paths (with a fallback that detects pre-namespace `.scaffold/manifest.json` entries and migrates them)
- Update cross-references in all docs that link to scaffold-canonical paths
- Update `templates/CLAUDE.template.md` and `templates/AGENTS.template.md` to reference the new paths

### Acceptance criteria
- After running `--upgrade` against a downstream project enriched at v1, the manifest's `schema_version` is 2 and entries point at `docs/scaffold/<file>.md`; the actual files have been moved.
- Re-running `--check` reports clean.
- Project-original docs (e.g. `docs/SPEC.md`, `docs/RUNBOOK.md`) are untouched.
- `--self-check` passes against the new paths.

### Required reviews
Touch the install lifecycle and schema. Planning gate applies; ADR amendment to `ADR-0001` capturing the path-namespace decision.

### Out of scope
- Renaming or relocating non-doc scaffold paths (`scripts/`, `.claude/agents/`, etc.). Those serve Claude Code conventions and stay where they are.

---

## Meta Phase M9.2 — Scaffold versioning and changelog

### Goal
Replace the current `0.0.0+git.<commit>` development version with semantic versioning, a `CHANGELOG.md`, and `--upgrade --to vX.Y.Z` so downstream projects can pin to specific scaffold versions.

### Background
M9 records `scaffold_version` in `.scaffold/manifest.json` but the value is just a short commit hash. There's no notion of "release," no breaking-change signal, and no way to upgrade against a known target other than "whatever git checkout the scaffold is on right now." Real consumers need predictable upgrade UX.

### Deliverables
- Adopt semver scheme (`MAJOR.MINOR.PATCH`); document policy in `docs/COMPATIBILITY.md`
- Tag releases on master branch (e.g. `v1.0.0`)
- `CHANGELOG.md` at scaffold root, classified `scaffold-internal`
- `scripts/release.sh` (or equivalent) automating tag + changelog entry creation
- `--upgrade --to vX.Y.Z` flag: checks out the named scaffold tag in a temp clone, runs the upgrade against that version
- `enrich-project.py` derives `scaffold_version` from `git describe --tags --always` instead of just commit hash
- Link from downstream `INSTALL_LIFECYCLE.md` to the changelog

### Acceptance criteria
- A tagged scaffold release writes `scaffold_version: v1.2.3+git.<commit>` (or just `v1.2.3` if exactly on tag).
- `--upgrade --to v1.0.0` against a project on v1.2.0 produces a downgrade plan; `--upgrade --to v1.3.0` produces an upgrade plan; both are dry-runnable and require explicit confirmation.
- `CHANGELOG.md` is committed and updated as part of the release script.
- Manifest schema migrations are tied to MAJOR version bumps (a v2.x scaffold can ship migrations from v1.y manifests).

### Required reviews
Versioning policy is a control-loop change. Planning gate; red-team scrutiny for migration backwards-compatibility; updated `docs/COMPATIBILITY.md`.

### Out of scope
- Any release distribution beyond git tags (PyPI, npm, etc.) — those are M9.3 territory
- Backporting fixes to old MAJOR versions (single supported MAJOR for now)

---

## Meta Phase M9.3 — Plugin distribution (hybrid)

### Goal
Distribute the scaffold's reusable agents and skills as a Claude Code plugin (`.claude-plugin/plugin.json` + a marketplace) so users can install them into any existing project via `/plugin install`, while keeping the full scaffold (containerization, phase gates, lifecycle, capability profiles) as the bootstrap path for new projects.

### Background
The scaffold today has two distinct value propositions: (a) a *library* of subagents and skills (project-lead, strategy-planner, architecture-red-team, code-reviewer, autonomous-product-builder, etc.) and (b) an *operating system* (phase gates, container, provenance, profiles). The library is reusable in any project; the operating system is opinionated and needs the full scaffold layout. Plugin distribution gives users a low-friction path to (a) without forcing (b).

### Deliverables
- `.claude-plugin/plugin.json` declaring the plugin's metadata, agents, skills, and (optional) hooks
- `.claude-plugin/marketplace.json` if hosting our own marketplace, OR a PR to a community marketplace
- A subset of agents/skills selected for plugin distribution (default: all `scaffold` ownership class agents + `autonomous-product-builder` skill; never `bootstrap-*` files)
- Plugin install instructions in `README.md` ("install into any project via `/plugin install ...`") alongside the scaffold instructions
- A test that verifies the plugin manifest references files that actually exist
- Classification in `capabilities/project-capabilities.yaml`: `.claude-plugin/*.json` as `scaffold` (shipped to plugin marketplace; not installed into downstream projects via enrich-project.py)

### Acceptance criteria
- `/plugin marketplace add <github-url>` followed by `/plugin install <name>@<marketplace>` installs the plugin into a fresh project, exposing the agents.
- The plugin can be uninstalled via the plugin manager without affecting the scaffold's `enrich-project.py` engine (the two distribution paths are independent).
- A user enriched with the scaffold AND the plugin doesn't end up with duplicate agents — document the precedence (project-local `.claude/agents/<name>.md` from enrich wins; plugin agents are inert when a same-name file exists locally; or vice versa, decide explicitly).
- Plugin manifest is mechanically validated against the actual filesystem in CI (no broken references).

### Required reviews
Plugin distribution adds a second product surface; planning gate applies. Specific red-team concerns: precedence between scaffold-installed and plugin-installed agents, version skew (plugin version vs scaffold version), and security (the plugin distributes executable hooks).

### Out of scope
- Replacing the scaffold with the plugin. The scaffold remains the canonical bootstrap path.
- Distribution to non–Claude-Code targets (e.g., Cursor's extension marketplace). Possible in a future M9.3.x.
- Auto-updating downstream `.claude/agents/` from the plugin's newer version. Plugins manage their own files; the scaffold's `--upgrade` manages enrich-installed files. They don't cross-update.

---

## Meta Phase M9.4 — Subagent overlay mechanism

### Goal
Allow downstream projects to extend scaffold-installed subagent files (e.g. add project-specific test flows to `qa-playwright.md`) without permanently freezing the file under `--keep-local`. M9 reserves the schema (`overlays: []` per file entry, `*.project.md` ignored convention) but does not implement concat or conflict semantics.

### Background
The meewar2 retrofit (April 2026) revealed that 5 of 9 subagent files had project-specific append-only extensions. Today these surface as drift; `--keep-local` preserves them but also opts the file out of all future scaffold improvements. The result is permanent stranding: the team gets neither scaffold updates nor a way to keep their additions clean.

The M9 schema reserves the design space:
- `overlays: []` field is on every manifest file entry from `schema_version: 1`
- `*.project.md` files alongside `<name>.md` are skipped by `--check` and `--upgrade` in M9 (intended for M9.4 overlays)

### Deliverables
- Concat semantics: when an overlay file (e.g. `.claude/agents/code-reviewer.project.md`) exists, the agent at runtime sees the concatenation of the canonical file + overlay content
- `overlays: []` populated with overlay metadata (path, sha256) per manifest entry
- `--upgrade` updates the canonical file from scaffold-new while preserving overlay file unchanged
- `--check` flags drift in the canonical file and the overlay separately
- Conflict resolution policy: what happens when an overlay's parent file is removed across scaffold versions, what happens when an overlay introduces invalid content, etc.
- Documentation in `INSTALL_LIFECYCLE.md` and a worked example in the meewar2 history showing customization → upgrade preserving customization

### Acceptance criteria
- A downstream project with `.claude/agents/code-reviewer.md` (scaffold canonical) and `.claude/agents/code-reviewer.project.md` (project additions) can run `--upgrade` to update the canonical without touching the overlay; the agent at runtime sees both.
- `--keep-local` is documented as a stop-gap retired by M9.4; existing meewar2-shaped projects can migrate from `--keep-local` to overlays via a one-shot migration command.
- Overlay introduction does not require a `schema_version` bump (the field is reserved from v1).
- An overlay whose parent file no longer exists in scaffold produces a clear error during `--upgrade`, not silent acceptance.

### Required reviews
Highest priority per real-world evidence. Planning gate (concat semantics, conflict resolution); red-team scrutiny for runtime concat correctness (does Claude Code actually read concatenated files? if not, the design fails); ADR documenting the overlay model.

### Out of scope
- Free-form patching (substring replace, line edits). Overlays are append-only.
- Overlays for non-agent files (hooks, settings.json, docs). Possibly a future M9.4.x once the agent overlay model is proven.

---

## Meta Phase M9.5 — Manifest signing / tamper detection

### Goal
Add optional HMAC over `.scaffold/manifest.json` body so a downstream project can detect tampering or accidental corruption. Useful for environments with strict audit/compliance requirements.

### Background
Today the manifest is a plain JSON file. Anyone with write access can edit shas to mask drift or hide a hand-edit. For most teams this is fine (the manifest is a workflow artifact, not a security boundary). For regulated environments, an integrity check matters.

### Deliverables
- Optional `signature` field in the manifest, populated when a `SCAFFOLD_HMAC_KEY` env var is present
- HMAC-SHA256 over the canonical-form manifest body (sorted keys, no signature field)
- `--check` verifies signature when present, exits non-zero on mismatch
- `--migrate-only` adds the signature retroactively when the env var is set
- Documentation: when to use signing, key management responsibility (downstream owns the key; scaffold only provides the algorithm)

### Acceptance criteria
- A signed manifest with the wrong sha for any file is detected by `--check`.
- A manifest with a tampered `signature` field is detected.
- An unsigned manifest is accepted normally (signing is opt-in).
- The HMAC algorithm is documented (canonicalization rules, hash algorithm) so an independent script can verify.

### Required reviews
Cryptographic boundary; red-team scrutiny on canonicalization (subtle bugs here let attackers craft signature-equivalent manifests); ADR with algorithm spec.

### Out of scope
- Public-key signing (X.509, GPG). HMAC is sufficient and avoids key infrastructure.
- Signing the scaffold-side `capabilities/project-capabilities.yaml`. Scaffold integrity is a different threat model (git history is the audit trail).

---

## Meta Phase M9.6 — Multi-profile additive installs

### Goal
Let a downstream project enriched with profile A (e.g. `default`) add profile B's agents (e.g. `game-project`'s `engine-builder`, `frontend-builder`, `backend-builder`) without re-enriching everything or losing the existing manifest.

### Background
M9 supports a single profile per project (`profile` is a top-level field in the manifest). The meewar2 retrofit surfaced that the project actually has agents from multiple profiles' worth (5 drifted agents the audit found, but the `default` profile only tracks 2 of them). Today the only workaround is to switch profile entirely (which would track all of meewar2's agents but rewrite the whole install set).

### Deliverables
- `--enrich-profile <name>` flag: additive install of a named profile's incremental agents/docs/etc., merging with the existing manifest
- Manifest schema: `profile` becomes `profiles: [list]` (with v2 → v3 migration adding the wrapping); behavior: each profile's contributions are tracked, removal of a profile cleanly removes its files via `--remove-profile <name>`
- Conflict resolution: if profile A and profile B both declare the same path with different ownership, refuse with clear error
- Update `bootstrap-new-project.sh` and `adopt-existing-repo.sh` to support multi-profile install
- Documentation: when to use multi-profile vs. just switching profiles

### Acceptance criteria
- A project enriched with `default` can be incrementally enriched with `game-project`, adding `engine-builder` etc. without touching files from `default` that didn't change.
- Manifest reflects both profiles in `profiles: [default, game-project]` after schema v3 migration.
- `--remove-profile game-project` cleanly removes only that profile's exclusive files; shared files (those in both profiles) remain.
- `--upgrade` correctly handles multi-profile state.

### Required reviews
Schema change (v2 → v3 migration); planning gate applies; red-team scrutiny on conflict resolution (what happens when two profiles disagree on the same path).

### Out of scope
- Custom profile definitions per downstream project. Profiles still come from the scaffold's `capabilities/project-capabilities.yaml`. (A future M9.6.x might allow downstream profile overlays.)

---

## Meta Phase M9.7 — Settings template sync lint

### Goal
Mechanically enforce that the scaffold's own `.claude/settings.json` and `templates/settings.template.json` stay byte-identical, eliminating the documented invariant currently maintained by hand.

### Background
M9 introduced `templates/settings.template.json` to make `.claude/settings.json`'s `bootstrap-with-template-tracking` class honest (manifest's `template_sha` points at a real file). Today the two files are identical; `templates/settings.template.json` is essentially a copy. The scaffold's YAML comment admits "must stay in sync (enforced by future M9.x lint)." This phase implements that lint.

### Deliverables
- Extension to `--self-check`: assert byte-equality between `.claude/settings.json` and `templates/settings.template.json`
- Failure surfaces a diff and names both paths
- `Makefile` (or pre-commit hook in `.claude/hooks/`) running `--self-check` to catch the divergence at commit time
- Update `CONTRIBUTING.md` documenting the invariant and how the lint enforces it

### Acceptance criteria
- Editing one file but not the other causes `--self-check` to exit 1 with both paths and a diff.
- The lint passes today (the two files are byte-identical).
- The pre-commit hook (if installed) prevents committing a divergent state.

### Required reviews
Light. Lint is a non-mutating check; only `--self-check` semantics change. Code review only.

### Out of scope
- Generalizing to "every `.claude/X` should have a `templates/X.template`" — too aggressive. This phase narrowly enforces the existing invariant.

---

## Meta Phase M9.8 — Partial-failure manifest write polish

### Goal
When `--upgrade` is interrupted mid-flight (some files copied, then one fails), write a partial manifest reflecting the files that did succeed before returning. Currently the manifest is left at pre-upgrade state, so successful copies appear as drift on the next `--check` until the user re-runs.

### Background
M9 §5 documents recovery via re-run, and the engine handles it correctly: orphan tmps are swept, succeeded copies are seen as clean on rerun. But the period between the failure and the re-run is confusing — `--check` reports drift on files that are actually correct. Code-review F7 flagged this; M9 documented the recovery path (`INSTALL_LIFECYCLE.md` Troubleshooting section). This phase replaces documentation with mechanical correctness.

### Deliverables
- Wrap `apply_upgrade_plan` in a try/finally that writes a partial manifest reflecting whatever did succeed before propagating the exception
- Manifest carries a `partial: true` field plus `failed_at: <timestamp>` and `failed_path: <path>` when the partial state is recorded
- `--check` against a partial manifest prints a hint ("manifest is from a partial upgrade; re-run --upgrade to complete") and still verifies all listed entries
- `--upgrade` against a partial manifest resumes from the failure point (skips the files already recorded as canonical)
- Test: monkeypatch the third file copy to raise, verify the manifest after the exception reflects the first two as canonical and lists the failed file under `failed_path`

### Acceptance criteria
- A simulated failure during `--upgrade` produces a partial manifest with successful copies marked clean.
- `--check` against the partial manifest exits 0 for the succeeded files (no false drift).
- A subsequent `--upgrade` resumes correctly: failed file is retried, partial flag clears on full success.

### Required reviews
Touches the engine's atomic-write semantics. Code-reviewer pass; updated `INSTALL_LIFECYCLE.md` Troubleshooting section.

### Out of scope
- True transactional rollback (revert succeeded copies on failure). The current best-effort + recovery model is sufficient for the use case.

---

## Meta Phase M10 — Design artifact

### Goal
Add an opt-in `docs/DESIGN.md` artifact that documents the current system shape (subsystems, data flows, hot spots, boundaries) between formal decision memos. The aim is to catch scaling and integration concerns at design time rather than in QA or production, *without* forcing heavy design ceremony on small projects.

### Background
The scaffold ships four planning docs: `SPEC.md` (what users see), `ARCHITECTURE.md` (how code is organized), `PHASES.md` (when things land), `PROD_REQUIREMENTS.md` (what production needs). The gap: which subsystems exist, what data flows where, and where bottlenecks are expected. Without that artifact, scaling concerns surface as afterthoughts (N+1 queries in QA, synchronous coupling discovered under load, schema choices that make later features hard).

A heavy design phase would contradict the scaffold's audit-first ethos. The right shape is *one page, opt-in, complementary to existing artifacts.* `strategy-planner` and `architecture-red-team` already do design work via decision memos; M10 gives them a stable artifact to capture the steady-state shape between memos.

### Deliverables
- `templates/design.template.md` — one-page template with four sections, fitting on one screen:
  - **System sketch** — one ASCII or Mermaid diagram of subsystems and dependencies
  - **Data flows** — for the top 2–3 user actions, what touches what in what order (3–7 lines each)
  - **Hot spots** — where bottlenecks are expected (hot writes, large reads, external-call serializations)
  - **Boundaries** — async-vs-sync decisions, transaction boundaries, deploy-unit ownership
- `DESIGN` entry in `capabilities/project-capabilities.yaml` `docs:` section, classified `bootstrap-frozen`, NOT in `default` profile's `include_docs` (opt-in only)
- New `with-design` profile in `capabilities/project-capabilities.yaml` that `extends: default` and adds `DESIGN` to `include_docs`; alternatively a `--with-design` flag on `enrich-project.py` for ad-hoc opt-in
- One-line reference to `docs/DESIGN.md` in `templates/CLAUDE.template.md` and `templates/AGENTS.template.md`, conditional on the file existing locally
- New "When to use DESIGN.md" section in `docs/USAGE_PATTERNS.md` (one paragraph, with explicit "skip this for projects under N hours of work" guidance)
- `strategy-planner` agent description updated to include "produces or updates `docs/DESIGN.md` when one exists"
- `architecture-red-team` agent description updated to include "reviews the steady-state design in `docs/DESIGN.md` alongside decision memos"

### Acceptance criteria
- Fresh enrichment with `default` profile produces a project WITHOUT `docs/DESIGN.md` (no behavior change for existing users; M10 is purely additive).
- Fresh enrichment with `with-design` profile (or `--with-design`) produces a project WITH `docs/DESIGN.md` rendered from the template.
- The rendered template is under 60 lines and fits on one screen at 80-column width.
- An existing project can opt in by editing its `.scaffold/manifest.json` profile to `with-design` and running `--upgrade`; the new file installs and shows up in the manifest with `bootstrap-frozen` ownership.
- `docs/USAGE_PATTERNS.md` has a one-paragraph "When to use DESIGN.md" section that explicitly discourages design ceremony for trivial projects.
- `--self-check` passes after the manifest changes.
- 28 (or more) M9 tests still green.

### Required reviews
Light. Adding a template + profile + manifest entries; no engine changes; no install/upgrade contract changes. Code review only; planning gate does NOT apply (M10 is purely additive).

### Out of scope
- Replacing or reorganizing existing planning docs (`SPEC`, `ARCHITECTURE`, `PHASES`, `PROD_REQUIREMENTS`). M10 is additive.
- Making DESIGN.md required by default. The whole value is *opt-in* — small projects don't carry the weight.
- Mechanical enforcement that `DESIGN.md` stays consistent with implementation. Future M10.x if needed.
- A separate "scaling-design" doc. Scaling concerns belong in DESIGN.md's "Hot spots" section; splitting dilutes value.
- A new agent for design review. `architecture-red-team` already does this; it just needs an artifact to review.

---

## Sub-phase priority (informal)

M10 leads. Rationale: M10 raises quality for *every* project enriched by the scaffold (not just those that hit the meewar2-shaped pain points the M9.x sub-phases address). It's also small (additive, opt-in, no engine changes) and unblocks better intake from `strategy-planner` and `architecture-red-team` immediately. Catching scaling concerns at design time has more leverage than fixing any individual lifecycle gap. The M9.x sub-phases follow in evidence-driven order.

1. **M10 — Design artifact** — raises the ceiling on project quality at design time; opt-in keeps small projects unencumbered; one-page artifact pairs naturally with existing strategy-planner and architecture-red-team agents
2. **M9.4 — Subagent overlays** — meewar2 evidence shows 5 stranded agents; `--keep-local` is a real stop-gap that's actively biting users
3. **M9.3 — Plugin distribution** — broadens reach significantly with low effort; additive (no breaking changes)
4. **M9.6 — Multi-profile additive installs** — also surfaced by meewar2; medium effort; unlocks richer projects
5. **M9.2 — Scaffold versioning + changelog** — essential for predictable upgrade UX once external consumers exist
6. **M9.7 — Settings template sync lint** — small, removes a hand-maintained invariant
7. **M9.8 — Partial-failure manifest write polish** — small, improves UX after a failure
8. **M9.1 — Namespaced scaffold docs path** — cosmetic; nice-to-have
9. **M9.5 — Manifest signing** — only matters in regulated/hostile environments

This is informal; the project lead picks the actual next phase based on user pull and current bandwidth.

---

## Meta Phase completion rule

A meta-phase is not complete until:
- acceptance criteria pass
- required reviews pass
- skill generation/validation/packaging passes if relevant
- approval artifact is written
- the phase is committed before the next phase begins
