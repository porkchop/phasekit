# Example profile: game project

This folder shows how to use the `game-project` profile to enrich a game repo.

## Setup

```bash
mkdir my-game && cd my-game
git init
bash /path/to/scaffold/scripts/bootstrap-new-project.sh game-project
```

## What gets created

After enrichment with the `game-project` profile, the project will contain:

```
my-game/
  .claude/
    CLAUDE.md                  # generated from template
    agents/
      project-lead.md          # orchestrates phased delivery
      strategy-planner.md      # writes implementation strategy
      architecture-red-team.md # challenges design choices
      code-reviewer.md         # enforces code quality
      qa-playwright.md         # browser verification
      engine-builder.md        # deterministic core logic
      frontend-builder.md      # UI without business-rule leaks
      backend-builder.md       # persistence, APIs, auth
      release-hardening.md     # production readiness
    hooks/
      deny-dangerous-commands.sh
  docs/
    SPEC.md                    # customize for your game
    ARCHITECTURE.md            # customize for your stack
    PHASES.md                  # phase plan (from scaffold)
    QUALITY_GATES.md           # quality gates (from scaffold)
    PROD_REQUIREMENTS.md       # customize for your deployment
    USAGE_PATTERNS.md          # usage patterns (from scaffold)
    adr/                       # architecture decision records
  artifacts/                   # phase approval artifacts go here
  scripts/
    run-phase.sh               # run a single phase
    run-until-done.sh          # run all phases
```

## Next steps

1. Edit `docs/SPEC.md` with your game's product spec
2. Edit `docs/ARCHITECTURE.md` with your engine/stack choices
3. Start the project-lead agent in audit mode
