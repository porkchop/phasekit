# Example profile: saas project

This folder shows how to use the `saas-project` profile to enrich a SaaS repo.

## Setup

```bash
mkdir my-saas && cd my-saas
git init
bash /path/to/scaffold/scripts/bootstrap-new-project.sh saas-project
```

## What gets created

After enrichment with the `saas-project` profile, the project will contain:

```
my-saas/
  .claude/
    CLAUDE.md                  # generated from template
    agents/
      project-lead.md          # orchestrates phased delivery
      strategy-planner.md      # writes implementation strategy
      architecture-red-team.md # challenges design choices
      code-reviewer.md         # enforces code quality
      qa-playwright.md         # browser verification
      frontend-builder.md      # UI without business-rule leaks
      backend-builder.md       # persistence, APIs, auth
      release-hardening.md     # production readiness
    hooks/
      deny-dangerous-commands.sh
  docs/
    SPEC.md                    # customize for your SaaS product
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

1. Edit `docs/SPEC.md` with your SaaS product spec
2. Edit `docs/ARCHITECTURE.md` with your stack choices
3. Add auth and billing requirements to `docs/PROD_REQUIREMENTS.md`
4. Start the project-lead agent in audit mode
