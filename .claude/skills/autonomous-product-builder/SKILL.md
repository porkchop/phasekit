---
name: autonomous-product-builder
description: phase-gated product development workflow for claude code with audit-first repo adoption, planning, adversarial review, implementation, qa, approval artifacts, and capability packaging. use when a repository should be built, continued, hardened, or improved methodically by coordinating subagents, specs, tests, browser verification, skill generation, and commit gates.
---

# Autonomous Product Builder

Use this skill to run a methodical software delivery workflow inside a repository that contains specs, phase docs, subagents, capability manifests, and host-side commit wrappers.

## Operating model

Work in this order unless the repository docs say otherwise:
1. Read the product docs and inspect repo state.
2. Decide whether the repo is greenfield or should start in audit mode.
3. For major design decisions, create a short plan and adversarial critique before implementation.
4. Delegate implementation to the narrowest appropriate builder.
5. Require review and QA before phase approval.
6. Write approval artifacts and stop for the external commit gate.
7. Continue with the next earliest unapproved phase.

## Read first

Always read these if they exist:
- `docs/SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/PHASES.md`
- `docs/QUALITY_GATES.md`
- `docs/PROD_REQUIREMENTS.md`
- `docs/USAGE_PATTERNS.md`
- `docs/META_SPEC.md`
- `docs/META_PHASES.md`
- `capabilities/project-capabilities.yaml`

If the repository includes `.claude/agents/`, use those project agents rather than inventing a new role structure.

If the repository is missing required scaffold files, run `scripts/check_scaffold.py` from this skill and then patch the missing pieces using the nearest templates already present in the repo.

## Decision workflow

Use the planning loop for any risky change:
- persistence or schema design
- auth or public-internet exposure
- cross-layer refactors
- performance-sensitive architecture choices
- capability generation, packaging, or scaffold self-improvement
- any case with two or more plausible approaches

For those cases:
1. write a concise implementation memo using `references/decision-memo-template.md`
2. perform an adversarial review using `references/adversarial-checklist.md`
3. resolve the result into `artifacts/decision-memo.md`
4. only then assign implementation work

## Phase discipline

- Never assume existing code is approved just because it exists.
- Prefer audit and approval over rewrite.
- Patch minimally when a phase is close to passing.
- Do not proceed to the next phase until the current phase passes the documented gates.
- For browser-visible work, require browser verification and screenshots.
- For skill-producing work, require validation and packaging.
- At phase completion, write `artifacts/phase-approval.json` and stop.
- At full completion, write `artifacts/project-complete.json`.

## Output requirements

### Phase approval artifact
Use this shape:

```json
{
  "phase": "phase-4",
  "approved": true,
  "summary": "Primary workflow verified and accepted.",
  "suggested_commit_message": "phase-4: approve primary workflow"
}
```

### Project completion artifact
Use this shape:

```json
{
  "done": true,
  "summary": "Required phases completed and production requirements satisfied.",
  "final_notes": [
    "known limitation 1",
    "known limitation 2"
  ]
}
```

## Reusable patterns

See:
- `references/workflows.md` for greenfield, adoption, hardening, and self-improvement modes
- `references/adversarial-checklist.md` for plan critique
- `references/decision-memo-template.md` for planning output format

## What not to do

- do not let builders skip review and qa
- do not embed product rules in UI code when shared logic exists
- do not silently carry forward undocumented rule decisions
- do not continue past an approved phase without the external commit gate
- do not approve a phase when code-reviewer has flagged missing tests as blocking
- do not allow builders to skip the testing gate or DRY gate defined in QUALITY_GATES.md
