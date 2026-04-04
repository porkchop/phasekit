# Usage patterns

## Pattern 1 - Greenfield product build
Use when a repository is new or nearly empty.

Workflow:
1. adapt the templates in `docs/`
2. let `project-lead` start at phase 0
3. require planning memos for architecture choices
4. iterate phase by phase until `project-complete.json`

## Pattern 2 - Existing repo adoption
Use when code already exists and needs methodical continuation.

Workflow:
1. adapt `docs/` to the current state of the codebase
2. start the lead in audit mode
3. re-vet already-built phases before continuing
4. patch minimally rather than rewriting by default

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
