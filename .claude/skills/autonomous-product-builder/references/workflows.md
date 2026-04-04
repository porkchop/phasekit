# Workflow modes

## Mode 1: Greenfield build

Use when the repository is new or nearly empty.

1. Read the product docs.
2. Start at phase 0.
3. Use planning for major architecture choices.
4. Build phase by phase.
5. Stop at each approval artifact.

## Mode 2: Existing repo adoption

Use when code already exists.

1. Start in audit mode.
2. Review phases in order.
3. Approve what already passes.
4. Patch minimally where gaps exist.
5. Continue only after the current phase is approved.

## Mode 3: Hardening sprint

Use when the main product exists and quality/readiness are the focus.

1. Prioritize reviewer, red-team, QA, and release hardening.
2. Gather defects first.
3. Fix blockers narrowly.
4. Re-run review and QA before approving the phase.

## Mode 4: Scaffold self-improvement

Use when the repository is improving its own agents, hooks, settings, workflow docs, or packaging flow.

1. Read `docs/META_SPEC.md` and `docs/META_PHASES.md`.
2. Start in audit mode.
3. Use planner and red-team for control-loop or packaging changes.
4. Require reviewer approval.
5. Require skill validation/packaging when relevant.

## Mode 5: Capability-packager

Use when the repository should generate enrichment assets for another project.

1. Read `capabilities/project-capabilities.yaml`.
2. Map requested capabilities to generated outputs.
3. Generate or refine the required assets.
4. Validate and package skills if present.
5. Document downstream usage.
