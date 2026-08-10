# Extension Patterns

How to extend the scaffold with custom profiles, agents, hooks, skills, and docs.

## Adding a custom profile

1. Open `capabilities/project-capabilities.yaml`
2. Add a new entry under `profiles:`:
   ```yaml
   profiles:
     my-project:
       extends: default
       include_agents:
         - project-lead
         - strategy-planner
         - backend-builder
         - release-hardening
       include_skills:
         - autonomous-product-builder
   ```
3. Use `extends` to inherit from an existing profile and add or narrow the capability set
4. Reference only keys that exist in the corresponding top-level sections (`agents`, `skills`, `docs`, etc.)
5. Enrich a downstream project with: `python3 scripts/enrich-project.py /path/to/project --profile my-project`

## Adding a custom agent

1. Create a markdown file in `.claude/agents/` (e.g. `.claude/agents/data-engineer.md`)
2. Define the agent's role, scope, and tools in the file
3. Register it in the manifest:
   ```yaml
   agents:
     data-engineer:
       enabled: true
       source: .claude/agents/data-engineer.md
       purpose: build and maintain data pipelines
   ```
4. Include it in relevant profiles:
   ```yaml
   profiles:
     data-project:
       extends: default
       include_agents:
         - project-lead
         - data-engineer
         - code-reviewer
   ```

## Adding a custom hook

1. Create a shell script in `.claude/hooks/` (e.g. `.claude/hooks/lint-on-save.sh`)
2. Register it in the manifest:
   ```yaml
   hooks:
     lint-on-save:
       path: .claude/hooks/lint-on-save.sh
       required: false
   ```
3. Include it in relevant profiles via `include_hooks`

## Adding a custom skill

1. Create a skill directory (e.g. `.claude/skills/my-skill/`)
2. Add `SKILL.md` with YAML frontmatter (`name`, `description`) and at least one heading
3. Add `agents/openai.yaml` with a valid `interface` key
4. Register it in the manifest:
   ```yaml
   skills:
     my-skill:
       enabled: true
       source_dir: .claude/skills/my-skill
       package_output: dist/skills/my-skill/skill.zip
       validate: true
       package: true
       purpose: description of the skill
   ```
5. Validate: `python3 scripts/validate-skill.py --skill my-skill`
6. Package: `python3 scripts/package-skill.py --skill my-skill`

## Adding a custom doc

1. Create the document file (e.g. `docs/DATA_PIPELINE.md`)
2. Register it in the manifest:
   ```yaml
   docs:
     DATA_PIPELINE:
       path: docs/DATA_PIPELINE.md
       required: false
   ```
3. Include it in relevant profiles via `include_docs`

## Profile selection guide

Use this matrix to choose a starting profile based on your project characteristics:

| Project type | Recommended profile | Key agents | Notes |
|---|---|---|---|
| Python service, API, or tool (uv) | `python-uv` | default set | Stack profile: seeds a uv/ruff/mypy/pytest verify gate + conventions doc |
| Static site / vanilla-JS web app | `static-web` | default set | Stack profile: seeds node:test + no-dependency + import-graph gate + conventions doc |
| Browser/canvas game | `game-canvas` | adds engine-builder, frontend-builder, backend-builder, release-hardening | Stack profile: game agents + static-web gate; successor to `game-project` |
| Documentation repo | `docs-only` | default set | Stack profile: seeds a markdown internal link/reference checker |
| General web app or service | `default` | project-lead, strategy-planner, code-reviewer, qa-playwright | Good starting point when no stack profile fits |
| Game or interactive simulation (non-canvas) | `game-project` | adds engine-builder, frontend-builder, backend-builder, release-hardening | Deterministic core logic + UI separation; no stack contract |
| SaaS product | `saas-project` | adds frontend-builder, backend-builder, release-hardening | API/persistence focus, no engine-builder |
| CLI tool or library | Create custom extending `default` | project-lead, strategy-planner, code-reviewer | Extend default without qa-playwright if no browser UI |
| Data pipeline | Create custom | project-lead, code-reviewer + custom data-engineer | Extend default, add domain-specific agents |

Stack profiles (v0.5.0) add a per-stack contract on top of agents: a working pre-commit verify gate (`PHASEKIT_VERIFY_CONFIGURED=1` out of the box, project-owned after seeding, re-seeded on upgrade only while still the stub) and a scaffold-owned `docs/CONVENTIONS.md`. A custom profile opts in by setting `stack: python-uv | static-web | game-canvas | docs-only` (inherited through `extends`; child overrides parent).

**When to create a custom profile:**
- Your project needs agents not in any existing profile
- You want to exclude agents that an existing profile includes
- You have domain-specific skills, hooks, or docs to bundle

**When to use `default` as-is:**
- Your project fits a general development workflow
- You don't yet know what specialization you need
- You can always switch profiles later by re-running enrichment

## Downstream adoption checklist

After enriching a downstream project:

1. **Verify agents copied** — check that `.claude/agents/` contains the expected agent files for your profile
2. **Verify docs templated** — check that `docs/` contains SPEC.md, ARCHITECTURE.md, PHASES.md, and PROD_REQUIREMENTS.md (customize these for your project)
3. **Verify hooks installed** — check that `.claude/hooks/` contains the expected hook scripts
4. **Verify CLAUDE.md generated** — check that `.claude/CLAUDE.md` exists and references your project docs
5. **Test the workflow** — start the project-lead in audit mode to confirm the phase-gated workflow operates correctly

For detailed workflows, see patterns 1 and 2 in `docs/USAGE_PATTERNS.md`.
