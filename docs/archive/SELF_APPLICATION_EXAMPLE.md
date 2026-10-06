# Self-Application Example

> **Scope:** This document is scaffold-internal. It describes how this repository improves itself (Mode 3). It is not included in downstream enrichment profiles.

This document demonstrates how the scaffold uses its own phase-gated workflow to improve itself. It is a worked example from meta-M7 (Self-application and refinement).

## What self-application means

Self-application is Mode 3 of the scaffold (from `docs/META_SPEC.md`): the repository improves its own templates, agents, quality gates, packaging flow, and skill-generation pipeline using the same disciplined workflow it provides to downstream repos.

The scaffold treats itself as a product, applies audit-first mode, and uses its own subagents (strategy-planner, architecture-red-team, code-reviewer) to plan and review every change.

## When to use it

Use self-application when:
- an agent definition is missing guidance on a common edge case
- a quality gate does not cover a change type that demonstrably affects scaffold behavior
- a meta-phase is starting and requires deliverable planning
- a doc is found to be inconsistent with actual behavior
- a new capability is being added to `capabilities/project-capabilities.yaml`

Do **not** use self-application for:
- stylistic preference changes to agent prompts
- adding agents without a clear capability gap
- changing quality gates without a concrete, named failure mode
- repackaging skills when the skill source has not changed

## Step-by-step: the M7 improvement cycle

This is the actual cycle executed during meta-M7.

### 1. Lead reads META_PHASES.md and identifies the current phase

The lead reads `docs/META_PHASES.md` and `artifacts/phase-approval.json` (or git log when the artifact is absent) to determine the earliest unapproved phase. For M7, the last approved phase was meta-M6.

### 2. Lead checks whether the planning gate applies

Meta-M7 changes agent definitions and quality gates — both are listed in the control-loop change gate in `docs/QUALITY_GATES.md`. The planning gate fires.

### 3. Lead invokes strategy-planner

The lead asks `strategy-planner` to write an implementation memo covering:
- which agents need changes and why (audit results)
- what the guardrail gap is (named specifically)
- what the self-application usage example should contain
- the narrowest implementation slice

The planner compared four options and recommended Option C: targeted audit with minimal patches and one new doc. See `artifacts/decision-memo.md` for the full memo.

### 4. Lead invokes architecture-red-team

The lead asks `architecture-red-team` to critique the memo. The red-team identified:
- **Blocking:** circular approval risk (an agent should not be the sole approver of changes to its own definition)
- **Scope bound required:** guardrail change needs a named gap, not speculative rule-adding
- **Scope bound required:** usage example must reference real M7 artifacts, not just narrative

### 5. Lead resolves the critique into a decision memo

The blocking concern (circularity) was resolved by restricting the scope: only `qa-playwright.md` is modified; it is reviewed by `code-reviewer` (unchanged). The guardrail change is reviewed by `architecture-red-team` (unchanged). No circular path exists.

The resolved decision is written to `artifacts/decision-memo.md`.

### 6. Lead issues a narrow implementation task

Based on the resolved memo, the implementation makes exactly three changes:
1. Add a Scope section to `.claude/agents/qa-playwright.md`
2. Add "subagent definitions" to the control-loop gate in `docs/QUALITY_GATES.md`
3. Write this document (`docs/SELF_APPLICATION_EXAMPLE.md`)

### 7. code-reviewer approves

`code-reviewer` reviews the three changed files against quality gates and architecture compliance. No agent being reviewed is the same agent performing the review.

### 8. Phase approval artifact is written

The lead writes `artifacts/phase-approval.json` for meta-M7 and stops. The external commit gate fires and creates the git commit before the next phase begins.

## The control-loop change gate — applied to agent definitions

The `docs/QUALITY_GATES.md` control-loop change gate (now including "subagent definitions") requires:

- **Rationale:** why the current definition is insufficient
- **Tradeoffs:** what is lost or risked by the change
- **Rollback path:** how to undo if behavior regresses
- **Updated docs:** at minimum this file and the decision memo
- **Reviewer approval:** from an agent whose definition is not being changed in the same phase

For M7, this table documents the agent-definition change:

| Field | Value |
|---|---|
| File changed | `.claude/agents/qa-playwright.md` |
| Rationale | The definition gave no guidance on when NOT to invoke the agent; future leads could gate meta-phase work on browser verification incorrectly |
| Tradeoff | Adds a constraint that slightly narrows agent scope; no behavior change for browser work |
| Rollback | `git revert` the M7 commit; the Scope section is purely additive |
| Reviewer | `code-reviewer` (unchanged in this phase) |

## What is out of scope

The following were explicitly considered and rejected for M7:
- Rewriting agents for stylistic improvement — no deficiency was identified
- Adding a new meta-lead agent — project-lead already handles self-improvement mode
- Changing `code-reviewer`, `architecture-red-team`, or `strategy-planner` definitions — would create circular approval paths
- Re-running skill packaging — `autonomous-product-builder` source did not change

## Artifacts produced during M7

- `artifacts/decision-memo.md` — planning gate output, strategy memo + red-team resolutions
- `artifacts/phase-approval.json` — written at phase end (see `docs/QUALITY_GATES.md` for schema)

## References

- `docs/META_SPEC.md` — self-improvement safety rules, product modes
- `docs/META_PHASES.md` — M7 deliverables and acceptance criteria
- `docs/QUALITY_GATES.md` — control-loop change gate, meta-project gates
- `docs/USAGE_PATTERNS.md` — Pattern 6 (Scaffold self-improvement)
- `capabilities/project-capabilities.yaml` — canonical agent list
