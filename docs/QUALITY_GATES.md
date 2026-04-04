# Quality gates

## Universal gate
A phase is only complete when all of the following are true:
- documented acceptance criteria pass
- relevant tests pass
- `code-reviewer` approves
- `qa-playwright` approves for user-visible or browser-based work
- docs are updated for any rule or architecture decision
- `artifacts/phase-approval.json` is written with `approved: true`
- the outer orchestration layer creates a git commit before the next phase begins

## Planning gate
Use a planning and adversarial review cycle before implementation when any of the following are true:
- the change crosses multiple layers
- persistence or schema choices are involved
- auth, security, or public internet exposure is involved
- the refactor could invalidate prior assumptions
- there are at least two plausible implementation strategies

Required outputs:
- `artifacts/decision-memo.md`
- optional ADR in `docs/adr/`

## Meta-project gates
For scaffold self-improvement work, a phase is not complete until:
- the current meta-phase acceptance criteria pass
- `strategy-planner` is used for material design changes
- `architecture-red-team` is used for architectural or autonomy-affecting changes
- `code-reviewer` approves
- generated skills validate and package successfully for any skill-related phase
- docs and manifest entries are updated
- `artifacts/phase-approval.json` is written
- the outer wrapper commits the approved phase before the next one begins

## Control-loop change gate
Any change affecting:
- project-lead behavior
- quality gates
- commit flow
- hooks
- settings
- generation scripts
- skill packaging flow

must include:
- rationale
- tradeoffs
- rollback path
- updated docs
- explicit reviewer approval

## Commit gate
Claude must stop after writing `artifacts/phase-approval.json`.
The host-side wrapper is responsible for:
1. reading the approval artifact
2. creating a git commit
3. resuming Claude for the next phase

## Suggested phase approval artifact

```json
{
  "phase": "phase-4",
  "approved": true,
  "summary": "Primary browser workflow verified and accepted.",
  "suggested_commit_message": "phase-4: approve browser workflow"
}
```

## Final completion artifact

```json
{
  "done": true,
  "summary": "Required phases completed and production requirements satisfied.",
  "final_notes": [
    "Known limitation 1",
    "Known limitation 2"
  ]
}
```
