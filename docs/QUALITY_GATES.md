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

## Testing gate
A phase is not complete unless:
- every new module, endpoint, or public behavior has at least one test exercising its primary path
- every bug fix includes a regression test that would fail without the fix
- tests are written before or alongside implementation code, not retrofitted after
- test names describe the behavior under test, not implementation details
- edge cases and error paths are tested for any logic identified as risky in the spec or decision memo

When coverage tooling is available in the target project, aim for meaningful branch coverage of new code. Do not pursue a numeric target at the expense of test quality — a focused test that catches real regressions is worth more than broad shallow coverage.

"Relevant tests pass" (from the universal gate) means: tests exist that would fail if the feature were removed or the bug fix reverted.

## DRY and reuse gate
- business rules and validation logic must exist in exactly one place
- if the same logic appears in more than one layer, extract it to a shared module or justify the duplication in a decision memo
- builders must check for existing utilities and shared modules before creating new ones
- code-reviewer must reject changes that introduce unjustified duplication

## Drift detection gate
Before starting a new implementation phase, the assigned builder must:
- review code produced in prior phases for overlap with the current task
- flag any inconsistency between the current plan and already-approved code
- prefer extending existing modules over creating parallel implementations
- report discovered drift as a blocking issue for project-lead to resolve before proceeding

## Planning gate
Use a planning and adversarial review cycle before implementation when any of the following are true:
- the change crosses multiple layers
- persistence or schema choices are involved
- auth, security, or public internet exposure is involved
- the refactor could invalidate prior assumptions
- there are at least two plausible implementation strategies
- scaffold control flow, capability generation, or packaging behavior is affected (self-improvement mode)

Required outputs:
- `artifacts/decision-memo.md`
- optional ADR in `docs/adr/` (use `docs/ADR_TEMPLATE.md`; follow naming convention `ADR-NNNN-short-title.md`)

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
- subagent definitions (`.claude/agents/`)

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

For phases that required a planning gate, include a `decision_memo` field referencing the governing artifact:

```json
{
  "phase": "meta-M5.1",
  "approved": true,
  "summary": "...",
  "decision_memo": "artifacts/decision-memo.md",
  "suggested_commit_message": "meta-M5.1: ..."
}
```

This field is optional for phases that did not require a planning gate, and required for those that did.

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

## Discovered work and phase expansion

During execution, the lead may discover that the current phase is underspecified or that additional prerequisite work is required.

In that case:

### Minor discovered work
If the new work is small, backward-compatible, and does not require external input:
- update the relevant docs directly
- append or refine acceptance criteria
- continue the current phase without asking the user

### Major but self-resolvable discovered work
If the new work is substantial but can be resolved using existing repo context and subagents:
- invoke strategy-planner and architecture-red-team as needed
- update META_SPEC.md, META_PHASES.md, ADRs, or related docs
- split the phase into subphases or append follow-on phases as needed
- do not ask the user for confirmation merely to continue planning
- write `artifacts/phase-update.json` if the phase plan changed materially

### External blocker
If required information is genuinely missing from the repo and cannot be resolved autonomously:
- write `artifacts/phase-blocked.json`
- stop without writing `artifacts/phase-approval.json`

Approved phase numbering must remain stable. Do not renumber already-approved phases retroactively.