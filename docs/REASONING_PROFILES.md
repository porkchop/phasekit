# Reasoning Profiles

## Overview

Different scaffold roles benefit from different reasoning depths. This document defines when deeper, more deliberate reasoning is appropriate and when standard lightweight reasoning is sufficient — and establishes that no global always-on heavy mode is required.

See also: `docs/EXECUTION_MODES.md` for execution mode strategy and the non-interference principle.

## Principle

Deeper reasoning should be used when the quality of the decision matters more than the speed of the response. Routine work should remain fast and lightweight.

## Role guidance

### project-lead

**Standard reasoning** (default) for:
- Phase selection and audit decisions on well-understood phases
- Approving existing work that clearly satisfies acceptance criteria
- Delegating to subagents for scoped, well-defined tasks

**Deeper reasoning** recommended for:
- Novel phase decomposition with ambiguous scope
- Resolving conflicts between acceptance criteria and discovered state
- Deciding whether to split a phase, append a follow-on, or write a blocker artifact
- Any decision that changes the roadmap or approved phase numbering rules

### strategy-planner

**Standard reasoning** for:
- Straightforward implementation breakdowns with one clear approach
- Scoped tasks with well-understood tradeoffs

**Deeper reasoning** recommended for:
- Comparing three or more plausible approaches with non-obvious tradeoffs
- Migration planning where rollback paths are unclear
- Estimating hidden scope or coupling in a proposed design
- Any scenario where choosing the wrong approach creates difficult-to-reverse state

### architecture-red-team

**Standard reasoning** for:
- Routine structural critiques of contained, low-risk changes
- Reviewing docs-only or additive changes with no production impact

**Deeper reasoning** recommended for:
- Security-sensitive, public-internet, or auth-related changes
- Multi-layer changes that cross persistence, API, and client boundaries
- Control-loop, autonomy, or capability-generation changes
- Any change where the failure mode is hard to detect or reverse

### code-reviewer

**Standard reasoning** for:
- Style, clarity, and maintainability feedback
- Checking that changes match documented acceptance criteria
- Routine test coverage review

**Deeper reasoning** recommended for:
- Subtle concurrency, race condition, or state mutation issues
- Security-sensitive validation, sanitization, or auth logic
- Complex logic where incorrect behavior may only manifest under edge conditions

## When deeper reasoning is not needed

- Direct interactive collaboration on day-to-day tasks
- Simple file reads, edits, and incremental commits
- Phase audits for phases with clear, already-satisfied acceptance criteria
- Tasks with a single well-understood approach and no irreversible consequences

## Non-interference rule

No role or agent should force deeper reasoning as a global always-on default. Reasoning depth is:
- **role-specific**: different agents have different thresholds
- **task-specific**: complexity triggers deeper reasoning, not general policy
- **opt-in for interactive work**: humans collaborating interactively with Claude should not be required to use extended thinking modes

Project settings (`.claude/settings.json`) must not contain any setting that globally forces extended or deep reasoning for all interactions. If a session requires deeper analysis, it should be invoked explicitly for that task.

## Usage in practice

When invoking strategy-planner or architecture-red-team for high-stakes work, the project-lead should include context that signals the decision complexity:
- describe the competing options
- enumerate the irreversible or high-risk aspects
- state which acceptance criteria are hardest to satisfy

This naturally cues the subagent to reason more carefully without requiring a global mode toggle.
