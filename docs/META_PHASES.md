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

### Out of scope (consider as future sub-phases)
- M9.1 — moving existing scaffold-canonical docs under a namespaced path (e.g. `docs/scaffold/`) for additional human-readable separation
- M9.2 — semantic versioning + changelog for the scaffold itself, enabling `--upgrade --to vX.Y.Z`
- M9.3 — distribution as a Claude Code plugin (`.claude-plugin/marketplace.json`) as an alternative to clone-and-run

---

## Meta Phase completion rule

A meta-phase is not complete until:
- acceptance criteria pass
- required reviews pass
- skill generation/validation/packaging passes if relevant
- approval artifact is written
- the phase is committed before the next phase begins
