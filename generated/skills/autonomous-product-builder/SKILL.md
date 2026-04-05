---
name: autonomous-product-builder
description: reusable workflow skill for project buildout, audit-first execution, and scaffold self-improvement
---

# Autonomous Product Builder

reusable workflow skill for project buildout, audit-first execution, and scaffold self-improvement

## Operating model

<!-- Customize this section for the specific skill's workflow -->

1. Read the project docs and inspect repo state.
2. Identify the current phase or task.
3. Execute the skill's core behavior.
4. Validate results against acceptance criteria.
5. Report outcomes clearly.

## Read first

Always read these if they exist:
- `docs/SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/PHASES.md`
- `docs/QUALITY_GATES.md`

## What not to do

- Do not skip validation steps.
- Do not silently override project conventions.
- Do not proceed past errors without reporting them.
