# Adversarial checklist

Use this to challenge a plan before implementation.

## Architecture
- Does the plan duplicate logic across layers?
- Does it create hidden coupling that will be painful later?
- Is the proposed abstraction justified now?

## Security and ownership
- Are user-owned actions validated server-side?
- Are secrets, tokens, or privileged operations handled safely?
- Could the plan expose unauthenticated or unauthorized behavior?

## Persistence and migration
- Can the data model evolve safely?
- What happens if deployment fails halfway through?
- Is rollback or forward-fix realistic?

## Testing and verification
- Are the highest-risk behaviors directly tested?
- Is browser QA required?
- Are there acceptance criteria that could still be interpreted ambiguously?

## Operations
- Will the plan be observable in production?
- Does it require rate limiting, abuse controls, or health checks?
- Is anything missing that would make incidents harder to debug?
