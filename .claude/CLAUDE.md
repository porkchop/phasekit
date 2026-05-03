# Phasekit

This repository is a reusable development operating system.

## Core operating rules
- Work in audit-first mode
- Start from the earliest unapproved phase or meta-phase
- Prefer minimal, backward-compatible changes
- Stop after writing `artifacts/phase-approval.json`
- Do not proceed past a phase until the repository has been committed externally

## Execution mode rules
- Prefer normal interactive collaboration mode by default
- Treat containerized unattended mode as opt-in
- Keep permissive execution confined to local/container-specific configuration or explicit command-line overrides
- Do not make ordinary direct work on this repository cumbersome

## Required references
- @docs/META_SPEC.md
- @docs/META_PHASES.md
- @docs/QUALITY_GATES.md
- @docs/CAPABILITY_MANIFEST.md
- @capabilities/project-capabilities.yaml
- @docs/USAGE_PATTERNS.md