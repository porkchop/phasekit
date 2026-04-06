# Decision Memo: Meta-M7 Self-Application and Refinement

**Date:** 2026-04-06  
**Phase:** meta-M7  
**Planning gate triggered by:** scaffold control flow and agent definitions affected

---

## Summary

Meta-M7 uses the scaffold's own workflow to improve itself. The planning gate was triggered and resolved as follows: strategy-planner produced an implementation memo; architecture-red-team critiqued it and identified one blocking concern (circular approval risk) and two scope-bounding requirements. The outcome is a narrow, reviewable set of changes that satisfies all four M7 deliverables without circular approval risk or scope inflation.

---

## Options Considered

**Option A — Audit only, no changes**  
Approve all 9 agents as-is, confirm existing guardrails are sufficient. Fails the acceptance criterion "scaffold successfully improves at least one part of itself."

**Option B — Broad agent rewrite**  
Rewrite all 9 agent definitions with richer prompting. High regression risk; violates "prefer minimal changes" rule; circular approval risk if reviewer agent definitions change.

**Option C — Targeted audit with minimal patches and one new doc (selected)**  
Audit all 9 agents against fitness-for-purpose criteria. Make only changes with a named, specific gap. Write the self-application example document as the primary deliverable.

**Option D — Add a new meta-lead agent**  
Add a tenth agent for self-improvement orchestration. Unnecessary; project-lead already has a self-improvement mode section.

---

## Recommended Approach

### Subagent audit results

| Agent | Disposition | Rationale |
|---|---|---|
| project-lead | approved as-is | Self-improvement mode section is present and accurate |
| strategy-planner | approved as-is | Focus and output format align with planning gate requirements |
| architecture-red-team | approved as-is | Adversarial focus and output format correct |
| code-reviewer | approved as-is | Scope and verdict format are actionable |
| qa-playwright | **minimal patch** | Missing scope clarification: no guidance on when NOT to invoke |
| engine-builder | approved as-is | Scope is correctly narrow |
| frontend-builder | approved as-is | Scope is correctly narrow |
| backend-builder | approved as-is | Scope is correctly narrow |
| release-hardening | approved as-is | Scope is correctly narrow |

### Changes in scope

1. **`qa-playwright.md`** — add a Scope section stating the agent applies only to browser/user-visible work and must not be invoked for docs-only or agent-definition changes. Reviewed by `code-reviewer` (not circular: code-reviewer.md is unchanged).

2. **`docs/QUALITY_GATES.md`** — add "subagent definitions (`.claude/agents/`)" to the control-loop change gate trigger list. Named gap: agents directly shape scaffold behavior but were not listed as gated triggers. Reviewed by `architecture-red-team` (not circular: architecture-red-team.md is unchanged).

3. **`docs/SELF_APPLICATION_EXAMPLE.md`** — new document providing a worked example of the M7 self-application cycle. References real M7 artifacts (`artifacts/decision-memo.md`, `artifacts/phase-approval.json`). Reviewed by `code-reviewer`.

### Changes explicitly out of scope

- No changes to `code-reviewer.md`, `architecture-red-team.md`, or `strategy-planner.md` — these agents approved the plan and must not be co-modified.
- No changes to builder agents (`engine-builder`, `frontend-builder`, `backend-builder`, `release-hardening`) — no deficiency found.
- No skill repackaging — `autonomous-product-builder` skill source is not changing.
- No new ADR — changes are below the ADR threshold (additive, no schema/persistence/auth impact).

---

## Circular Approval Risk Mitigation

The red-team identified that an agent should not be the sole approver of changes to its own definition. This is addressed by the scope above: the only modified agent definition is `qa-playwright.md`, which is reviewed by `code-reviewer` (unchanged). The guardrail change in `QUALITY_GATES.md` is reviewed by `architecture-red-team` (unchanged). No circular approval path exists.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Prompt drift from agent changes | Patch is additive only (new Scope section); no behavior change, only clarification |
| Scope inflation ("as needed" is unbounded) | All doc/script changes require a named deficiency from the audit |
| Stale usage example | Example references real M7 artifacts that must exist before phase-approval |
| Guardrail duplication | New guardrail text adds "subagent definitions" to an existing list; does not create a parallel gate |

---

## Rollback Path

All changes are in tracked files. The M6 commit (`c6ca510`) is the rollback target. To revert:
```
git revert HEAD  # after M7 commit
```
No schema, persistence, or external API is involved; rollback is low-risk.

---

## Acceptance Criteria for This Phase

1. All 9 agents audited; explicit disposition recorded for each (done in this memo).
2. `qa-playwright.md` has a Scope section added.
3. `docs/QUALITY_GATES.md` control-loop gate includes "subagent definitions."
4. `docs/SELF_APPLICATION_EXAMPLE.md` exists and references M7 artifacts.
5. `code-reviewer` approves all file changes.
6. No modified agent is used as the sole reviewer of its own changes.
7. `artifacts/phase-approval.json` written with `approved: true` for meta-M7.
