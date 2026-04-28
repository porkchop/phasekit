# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Cursor, Copilot, OpenCode, Gemini CLI, Antigravity, etc.) when working with the **{{PROJECT_NAME}}** repository.

## Repository overview

{{PROJECT_NAME}} was scaffolded from the `competent-developer-template`. It uses a phase-gated, audit-first workflow: work progresses through defined phases with explicit approval artifacts and git commits between each. Adapt this section to describe what {{PROJECT_NAME}} actually does once the spec is filled in.

See `README.md` for product detail and `docs/SPEC.md` for the product specification.

## Core operating rules

- Work in **audit-first mode** — assume code may already exist; verify before rewriting.
- Start from the **earliest unapproved phase** in `docs/PHASES.md`.
- Prefer **minimal, backward-compatible** changes.
- **Stop after writing `artifacts/phase-approval.json`** — do not proceed past a phase until the repository has been committed externally.
- Treat containerized unattended mode as **opt-in**. Permissive execution must live in local/container-only configuration or explicit CLI overrides — never in shared project settings.

## Lifecycle

```
  AUDIT          PLAN           BUILD          VERIFY         REVIEW         APPROVE
 ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐
 │ Read │ ───▶ │ Memo │ ───▶ │ Code │ ───▶ │ Test │ ───▶ │  QA  │ ───▶ │ Gate │
 │Phase │      │  +   │      │ Diff │      │  +   │      │ Gate │      │ JSON │
 │Goal  │      │ Risk │      │      │      │ Lint │      │      │      │+Commit│
 └──────┘      └──────┘      └──────┘      └──────┘      └──────┘      └──────┘
```

Each phase ends with `artifacts/phase-approval.json` and an external commit. The next phase does not begin until the commit lands.

## Required references

Before non-trivial work, read:

- `docs/SPEC.md` — what {{PROJECT_NAME}} is supposed to do
- `docs/ARCHITECTURE.md` — technical architecture
- `docs/DESIGN.md` (if present) — steady-state system design: subsystems, data flows, hot spots, boundaries
- `docs/PHASES.md` — phase plan for this project
- `docs/QUALITY_GATES.md` — universal/testing/DRY/drift/planning/control-loop/commit gates
- `docs/PROD_REQUIREMENTS.md` — production/deployment requirements
- `docs/USAGE_PATTERNS.md` — workflow patterns
- `.claude/CLAUDE.md` — project instructions (loaded automatically by Claude Code)

## Subagents available

Subagents live in `.claude/agents/`. Use the right one for the task — do not improvise across roles.

| Agent | Use when |
|---|---|
| `project-lead` | Orchestrating phased delivery; deciding the next smallest verified step |
| `strategy-planner` | Major decisions; comparing options, tradeoffs, migration plan |
| `architecture-red-team` | Adversarial review of plans; surfacing hidden coupling, scaling, security holes |
| `code-reviewer` | Quality, architecture compliance, test sufficiency, duplication |
| `qa-playwright` | Browser verification of user-visible work |

Profile-specific agents (when present): `engine-builder`, `backend-builder`, `frontend-builder`, `release-hardening`. See `capabilities/project-capabilities.yaml` if it exists locally for the active set.

Multi-agent orchestration is allowed only in the **fan-out → merge** pattern (e.g. running `code-reviewer` + `architecture-red-team` in parallel and synthesizing their reports). Do not build router agents that delegate between roles.

## Interface contract for incoming tasks

When another agent or human hands work to a Claude Code session in this repo, the handoff should include:

- **Goal** in 1–3 bullets (what success looks like)
- **Non-goals** (what *not* to do)
- **Constraints** (perf, compat, deadlines, frozen deps)
- **Pointers to ground truth** (file paths with line numbers, related PRs, specs)
- **Definition of done + verification method** (tests, repro steps, screenshots)

What this repo's agents return:

- A plan or diff
- Explicit assumptions made
- A verification summary (commands run + results)
- Open questions / unknowns
- An approval artifact (for phase work)

## Escalation triggers

Stop and confirm with the human before:

- Destructive ops (`rm -rf`, `git reset --hard`, branch deletion)
- Schema migrations on shared/production data
- Anything touching auth, secrets, or public exposure
- Force-push or amending published commits
- Changing `.claude/settings.json`, hooks, or other shared scaffold-managed files (these are tracked by `.scaffold/manifest.json` and will surface as drift on upgrade)

## Anti-rationalization

Reject these excuses:

| Rationalization | Reality |
|---|---|
| "This is too small for the spec / phase model" | The phase model is what makes 'small' verifiable. Skipping it produces work that cannot be approved. |
| "I'll add the phase-approval artifact later" | Without it, the next agent has no audit trail and the loop cannot advance. The artifact *is* the gate. |
| "I can just quickly implement this" | Audit-first exists because existing code may already satisfy the goal. Re-implementing without auditing produces drift. |
| "The test isn't strictly necessary here" | The testing gate requires a test that would fail if the change were reverted. If you can't write one, you don't understand the change. |
| "I'll loosen project settings just for this run" | Project settings are shared. Permissive execution belongs in `settings.local.json` or CLI flags, never in committed config. |
| "I'll skip review since the change is obvious" | The review gate catches duplication, missed drift, and architecture violations the implementer is least likely to notice. |

## Scaffold provenance

This repo was enriched by the `competent-developer-template` scaffold. The provenance record is at `.scaffold/manifest.json` (committed, not gitignored). To audit, upgrade, or uninstall scaffold-installed files, use the scaffold's `enrich-project.py` engine. See `docs/INSTALL_LIFECYCLE.md` (if present) for the lifecycle contract.

## Cross-tool notes

- **Claude Code** is the primary target. Subagents in `.claude/agents/` are auto-discovered; skills under `.claude/skills/` follow Claude skill anatomy.
- **OpenCode / Cursor / Copilot / Gemini CLI**: read this file. Subagents map to your tool's equivalent (custom modes, agents, etc.). The phase-gate model is tool-agnostic.
- Hooks in `.claude/hooks/` are Claude-specific. Other tools should enforce equivalent boundaries via their own mechanisms before running Bash.
