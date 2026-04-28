# Usage patterns

## Pattern 1 - Greenfield product build
Use when a repository is new or nearly empty.

Workflow:
1. from the new project directory, run:
   ```
   bash /path/to/scaffold/scripts/bootstrap-new-project.sh [PROFILE]
   ```
   This copies agents, docs, hooks, and generates `.claude/CLAUDE.md` using the manifest profile (default: `default`).
2. customize `docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, and `docs/PROD_REQUIREMENTS.md`
3. let `project-lead` start at phase 0
4. require planning memos for architecture choices
5. iterate phase by phase until `project-complete.json`

## Pattern 2 - Existing repo adoption
Use when code already exists and needs methodical continuation.

Workflow:
1. from the existing project directory, run:
   ```
   bash /path/to/scaffold/scripts/adopt-existing-repo.sh [PROFILE]
   ```
   This copies agents, hooks, and doc templates without overwriting existing files.
2. adapt `docs/` to the current state of the codebase
3. start the lead in audit mode
4. re-vet already-built phases before continuing
5. patch minimally rather than rewriting by default

## Pattern 3 - Hardening sprint
Use when the product mostly exists and the focus is quality.

Workflow:
1. prioritize `code-reviewer`, `architecture-red-team`, `qa-playwright`, and `release-hardening`
2. require explicit defect lists and mitigation plans
3. avoid new product scope unless it blocks readiness

## Pattern 4 - High-risk design change
Use when persistence, auth, scaling, or cross-layer refactors are involved.

Workflow:
1. require `strategy-planner`
2. require `architecture-red-team`
3. write `artifacts/decision-memo.md`
4. only then assign a builder

## Pattern 5 - Browser-heavy product
Use when UI behavior is complex or prone to drift.

Workflow:
1. make `qa-playwright` mandatory for each user-visible phase
2. require screenshots for critical states
3. treat console errors as defects unless explicitly waived

## Pattern 6 - Scaffold self-improvement
Use when this repository is evolving its own agents, hooks, settings, workflow docs, or generation logic.

Workflow:
1. read `docs/META_SPEC.md` and `docs/META_PHASES.md`
2. start in audit mode
3. use planner and red-team for any control-loop or packaging changes
4. require reviewer approval and, when relevant, skill validation + packaging before acceptance

## Pattern 7 - Capability-packager mode
Use when the scaffold should generate enrichment assets for another repository.

Workflow:
1. update `capabilities/project-capabilities.yaml`
2. map requested capabilities to agents, docs, hooks, scripts, and skills
3. generate or refine the required assets
4. validate and package project skills as deliverables
5. document downstream installation and usage

## Pattern 8 - Concept-to-spec ideation
Use *before* Pattern 1, when you have a fuzzy idea and need to land on a SPEC, an architecture, and a phase plan good enough to start building. Pattern 1 assumes the SPEC already exists; this pattern gets you there.

The scaffold deliberately doesn't build its own ideation tool — conversation with Claude *is* the powerful core. The optimization is structuring the conversation and combining it with the right external tools.

### Tool selection: claude.ai vs Claude Code

Pattern 8 uses both Claude surfaces. They're better at different phases:

| Phase | Best surface | Why |
|---|---|---|
| **Divergent — "is this idea worth pursuing?"** | [claude.ai](https://claude.ai) | Lightweight (open a tab); cheap to branch a new chat for "what if X?"; Artifacts render Markdown/React/Mermaid live so you can iterate on a SPEC draft visually; Projects pin templates + a system prompt across conversations; works on mobile |
| **External tooling — UI mockups, diagrams** | v0 / Figma+AI / Excalidraw | Specialty surfaces; Claude can describe but not render |
| **Convergent — "let's structure this into a buildable spec"** | Claude Code | The scaffold's `strategy-planner` and `architecture-red-team` agents only exist here; addyosmani/agent-skills' `idea-refine` and `spec-driven-development` skills run here when the plugin is installed; direct file writes into the eventual project location |
| **Bootstrap + phase 0** | Claude Code | File system, verification, git integration are the value prop |

The seam: **switch from claude.ai to Claude Code once you have a SPEC draft you'd be embarrassed to throw away.** Before that, you're exploring (claude.ai is right). After that, you're producing (Claude Code is right). Most people skip claude.ai entirely and start in Claude Code; that's correct once you have direction but wastes an hour of structured tool overhead on the "is this idea even good?" exploration.

Workflow (each step labelled with its preferred surface):
1. **Set up a structured Claude session.** *(claude.ai)* No project directory exists yet — ideation happens in claude.ai, not in a repo. You need the scaffold's templates available to Claude somehow:
   - **Local clone of the scaffold (most reliable today):** in [claude.ai](https://claude.ai), create a Project and upload these files as project knowledge from your local scaffold checkout: `templates/spec.template.md`, `templates/architecture.template.md`, `templates/design.template.md` (optional), and the canonical workflow docs (`docs/QUALITY_GATES.md`, `docs/USAGE_PATTERNS.md`, `docs/EXECUTION_MODES.md`, `docs/REASONING_PROFILES.md`).
   - **Scaffold published publicly (once it is):** paste the raw GitHub/Bitbucket URLs into the Project's custom-instructions field so Claude can fetch them, or attach via a connector if your Claude surface supports it.
   - **No-tool fallback:** paste the templates' contents directly into the Project's custom-instructions field. Less elegant but works anywhere.

   Every conversation in that Project starts with the templates in context. If you're already inside an existing scaffolded project running Claude Code, you can do this even more directly — `@`-reference the scaffold's template paths in conversation. Pattern 8 doesn't require Claude Code; claude.ai works fine.
2. **Walk the spec sections in order.** *(claude.ai — divergent phase; lightweight is right here)* Prompt Claude with: "I want to build X. Walk me through `docs/SPEC.md` section by section — ask one question at a time and update the spec as we go. Render the current draft as an Artifact so I can see it grow." Iterate freely; spawn new chats inside the Project to explore "what if I did Y instead?" without losing your main thread. Output is a SPEC draft good enough that you'd be embarrassed to throw it away — that's your signal to switch surfaces.
3. **Mock the UI if it matters.** *(external tools — claude.ai can describe but not render high-fidelity UI)* For UI-heavy products, generate top 2–3 screens using:
   - **[v0](https://v0.dev)** for fast React mockups (lower fidelity, fastest iteration)
   - **Figma + Figma AI / Galileo AI / Builder.io's Figma plugin** for high-fidelity designs
   - **Whimsical AI / Excalidraw / Eraser.io** for sketch-style flow diagrams
   Drop screenshots or links into `docs/SPEC.md` rather than committing the design tool itself. Skip this step entirely for CLIs, libraries, or services without a UI.
4. **Architecture pass.** *(Claude Code — convergent phase begins; you need the real `strategy-planner` agent now)* Move your SPEC draft into Claude Code (paste it into a working file, or run Pattern 1 first and paste into the rendered `docs/SPEC.md`). Ask `strategy-planner` to compare 2–4 plausible architectures and recommend one. Output: a draft `docs/ARCHITECTURE.md` with the chosen layering, technology choices, and the rejected options.
5. **(Optional) Design pass.** *(Claude Code)* If the project's complexity warrants — multiple subsystems, scaling concerns, async/sync decisions worth being explicit about — opt into the M10 design artifact via the `with-design` profile. `strategy-planner` produces the initial `docs/DESIGN.md`; `architecture-red-team` reviews it. See "When to use docs/DESIGN.md" above.
6. **Phase plan.** *(Claude Code)* Ask `strategy-planner` to break the SPEC into phases such that each phase ends in a verifiable, deployable, reviewable increment. Output: `docs/PHASES.md`.
7. **Adversarial review.** *(Claude Code)* Run `architecture-red-team` against SPEC + ARCHITECTURE (+ DESIGN if present) + PHASES. Address blocking concerns; record non-blocking ones as `Open questions` in DESIGN.md if used, or in a planning memo. The red-team should flag scaling concerns, missing non-goals, weak phase acceptance criteria, and integration risks.
8. **Initialize the project.** *(Claude Code)* Run Pattern 1 (`bootstrap-new-project.sh`). The SPEC, ARCHITECTURE, PHASES, and PROD_REQUIREMENTS.md you've drafted slot in directly — the scaffold's templates are *defaults*, not *requirements*. Override the rendered files with what you've already written. (If you ran Pattern 1 earlier in step 4 to use Claude Code's filesystem, you've already done this.)

Notes:
- **Steps 1–3** happen *outside* any specific repo. claude.ai for the conversation, external tools for UI, scratch notes for everything else.
- **Steps 4–8** happen *inside* Claude Code. The scaffold's `strategy-planner` and `architecture-red-team` agents are the value here — claude.ai cannot replicate them faithfully even if you describe the personas in a system prompt. The real agent files in `.claude/agents/*.md` carry specific instructions and output formats that took adversarial reviews to settle.
- **The exact handoff point** is flexible. Some teams cross between steps 3 and 4 (claude.ai for SPEC + UI, Claude Code for ARCH + DESIGN + PHASES). Others run Pattern 1 immediately after step 2, paste the SPEC draft into the rendered `docs/SPEC.md`, and do steps 3–7 inside Claude Code with file references. Both are fine. The single hard rule: don't try to run `strategy-planner` or `architecture-red-team` from claude.ai by play-acting — you lose what makes them work.
- **If the concept genuinely fits in 30 minutes of conversation**, skip the formal walk-through entirely. This pattern is for ideas worth at least a half-day of work.
- **For ongoing ideation on an existing project**, prefer `decision-memo.md` artifacts under `artifacts/` rather than reopening SPEC. SPEC describes the steady-state product; decision memos describe each material change.

## When to use docs/DESIGN.md

The optional `docs/DESIGN.md` artifact (M10) documents the steady-state system shape: subsystems, data flows, hot spots, and boundaries. Pair it with `SPEC.md` (what users see) and `ARCHITECTURE.md` (how code is organized).

Use it when:
- the project will have more than one subsystem with non-trivial dependencies between them
- scaling concerns matter (hot writes, large reads, external-call serialization, queueing)
- there are sync-vs-async decisions, transaction boundaries, or trust boundaries worth being explicit about
- multiple agents will collaborate on the project and need a shared mental model

Skip it for:
- prototypes, single-file scripts, throwaway experiments, or projects under a few hours of work
- pure CRUD apps with one obvious shape and no scaling concerns

Enable it via the `with-design` profile at enrichment time, or by editing the project's `.scaffold/manifest.json` profile to `with-design` and running `--upgrade`. Keep `DESIGN.md` under one screen — push detail into per-decision memos in `artifacts/` rather than letting the design itself grow. `strategy-planner` produces and updates the design; `architecture-red-team` reviews it alongside decision memos.

## Companion plugins

Scaffolded projects compose well with workflow-skill plugins. The scaffold owns the *project structure layer* (phases, gates, provenance, capability profiles); plugins typically own the *workflow technique layer* (skills with anti-rationalization tables, slash commands, process recipes). Combining them is additive — pick what's useful from each.

### Known-compatible: addyosmani/agent-skills

The [agent-skills plugin](https://github.com/addyosmani/agent-skills) ships 21 skills (spec-driven-development, incremental-implementation, test-driven-development, debugging-and-error-recovery, code-review-and-quality, etc.), three personas (code-reviewer, security-auditor, test-engineer), and seven slash commands (`/spec`, `/plan`, `/build`, `/test`, `/review`, `/code-simplify`, `/ship`). Install it inside a scaffolded project:

```
/plugin marketplace add addyosmani/agent-skills
/plugin install agent-skills@addy-agent-skills
```

**Resolution rules** (handled by Claude Code):

- Project-local `.claude/agents/<name>.md` (installed by our enrich) wins over plugin agents. Their `code-reviewer` persona is shadowed by our `.claude/agents/code-reviewer.md`; this is intentional — local customizations (like meewar2-style project extensions) take precedence.
- Project-local skills under `.claude/skills/<name>/` similarly win over plugin skills with the same name.
- Slash commands from the plugin become available globally inside the project. There is no override mechanism today.

**Caveat — slash commands are not phase-aware.** Their `/build`, `/ship`, etc. are workflow shortcuts that do not read `artifacts/phase-approval.json` or write approval artifacts. Treat the plugin's slash commands as *workflow aids inside phase work*, not as a substitute for the phase model. The phase model still owns the approval lifecycle; the plugin's skills enrich the work each phase does.

**What composes well in practice:**

| Plugin contribution | Scaffold partner | How they reinforce each other |
|---|---|---|
| `spec-driven-development` skill | `docs/SPEC.md` template | Skill says spec-before-code; scaffold installs the template |
| `incremental-implementation` skill | `engine-builder` / `frontend-builder` / `backend-builder` agents | Skill says how to slice; agent says who builds each slice |
| `test-driven-development` skill | Testing gate in `QUALITY_GATES.md` | Skill is the workflow; gate is the acceptance check |
| `code-review-and-quality` skill | `code-reviewer` agent + review gate | Skill is reviewer process; agent is the role that runs the gate |
| `debugging-and-error-recovery` skill | Used inside any build phase | Pure technique; orthogonal to phase model |
| `documentation-and-adrs` skill | `docs/adr/` + `templates/adr.template.md` | Skill says write an ADR; scaffold says where they go |
| Anti-rationalization tables (their authoring style) | Already adopted in `QUALITY_GATES.md` (M9) | Same idea; reinforces ours |

If you adopt the plugin, mention it in your project's `docs/AGENTS.md` (rendered downstream from the M10 template) so other agents and humans entering the repo know which workflow conventions are active.
