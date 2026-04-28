# {{PROJECT_NAME}}

This repository uses the competent developer workflow.

## Core operating rules
- Work in audit-first mode
- Start from the earliest unapproved phase
- Prefer minimal, backward-compatible changes unless a rewrite is explicitly justified
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally

## Required references
- @docs/SPEC.md
- @docs/ARCHITECTURE.md
- @docs/PHASES.md
- @docs/QUALITY_GATES.md
- @docs/PROD_REQUIREMENTS.md

## Optional references
- @docs/DESIGN.md — steady-state system design (subsystems, data flows, hot spots, boundaries). Read first if present; not every project has one.

{{OPTIONAL_REFERENCES}}