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

## Meta Phase M4 — Execution modes and non-interference

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

## Meta Phase M6 — Reasoning profile guidance

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

## Meta Phase M7 - Planner and red-team integration

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

## Meta Phase M8 - Self-application and refinement

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

## Meta Phase M9 - Generalization and compatibility

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

## Meta Phase completion rule

A meta-phase is not complete until:
- acceptance criteria pass
- required reviews pass
- skill generation/validation/packaging passes if relevant
- approval artifact is written
- the phase is committed before the next phase begins
