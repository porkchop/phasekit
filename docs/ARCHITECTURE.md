# Architecture

## System overview
Document the intended technical architecture.

## Layer boundaries
- domain / engine
- application / service
- persistence / messaging
- UI / presentation

## Shared constraints
- keep domain rules out of UI
- keep persistent schema evolution explicit
- keep security-sensitive behavior server-authoritative
- keep test strategy aligned with risk

## Preferred stack
Document the chosen stack and any alternatives ruled out.

## Testing strategy
- unit
- integration
- end-to-end
- visual / browser verification

## Operational concerns
- deployment model
- migrations
- monitoring
- backups / restore
- incident response hooks


## Capability manifest

The scaffold uses `capabilities/project-capabilities.yaml` as the single source of truth for which agents, docs, hooks, scripts, and skills should be present or generated for downstream projects.

The manifest schema, field definitions, and output-mapping rules are documented in `docs/CAPABILITY_MANIFEST.md`.
