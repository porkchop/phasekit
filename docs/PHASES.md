# Phase plan

## Current repo status

This scaffold supports both greenfield work and adoption of an existing codebase.

Lead-agent instruction:
- start from phase 0 in audit mode
- review phases sequentially
- approve existing work when it satisfies acceptance criteria
- patch minimally when gaps are found
- do not begin a later phase until the current phase is approved

Status handling:
- if already implemented, audit and approve rather than rebuild
- if partially implemented, patch minimally to reach acceptance
- if missing, implement normally

## Phase 0 - project bootstrap
Deliverables:
- repo structure
- core docs in `docs/`
- subagents and settings in `.claude/`
- CI or at least repeatable local lint/test commands

Acceptance:
- project can be bootstrapped locally
- docs are coherent enough to guide further work

## Phase 1 - domain model and core engine
Deliverables:
- domain model
- core state transitions
- test harness
- deterministic rules where applicable

Acceptance:
- core rules can be exercised without the UI
- unit tests cover primary state changes

## Phase 2 - critical business logic
Deliverables:
- main behaviors implemented
- edge cases covered
- decision memo for any rule changes

Acceptance:
- major domain workflows pass
- no hidden constants or duplicated rules

## Phase 3 - orchestration and persistence
Deliverables:
- application services
- persistence layer
- migration strategy
- recovery / replay strategy where relevant

Acceptance:
- state survives reloads / restarts as required
- server-side validation exists for critical actions

## Phase 4 - user-facing workflow
Deliverables:
- primary interface
- happy-path end-to-end flow
- QA verification with browser automation if relevant

Acceptance:
- a user can complete the main workflow without outside explanation
- QA pass artifact exists

## Phase 5 - production readiness
Deliverables:
- auth or session ownership
- observability
- deployment and rollback notes
- rate limiting and abuse controls where relevant
- release hardening review

Acceptance:
- product satisfies `docs/PROD_REQUIREMENTS.md`
- release-hardening approves

## Phase 6 - completion
Deliverables:
- final docs and cleanup
- known limitations documented
- `artifacts/project-complete.json`

Acceptance:
- all required phases approved
- no blocking quality gate failures remain
