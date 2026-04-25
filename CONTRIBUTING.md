# Contributing

Thanks for your interest. This repo is a phase-gated development scaffold; it improves itself under the same workflow it gives to downstream projects. Please read this before opening a PR.

## Before you start

1. Read `README.md`, `AGENTS.md`, and `docs/META_SPEC.md`.
2. Check `docs/META_PHASES.md` to see if your idea fits an existing phase or needs a new one.
3. For non-trivial proposals, open an issue first describing the goal, tradeoffs, and rollback path.

## Ground rules

- **Audit first.** Look for existing code or capability before adding new ones.
- **Minimal, backward-compatible changes.** Additive over breaking.
- **One source of truth.** Don't duplicate facts across docs, manifest, and templates.
- **No secrets, ever.** Not in code, not in docs, not in commit messages, not in tests.
- **No permissive shared settings.** Permissive execution belongs in `.claude/settings.local.json` (gitignored) or CLI flags. `.claude/settings.json` stays conservative.

## What changes need extra review

These changes are subject to the **control-loop change gate** (`docs/QUALITY_GATES.md`) and require rationale, tradeoffs, and a rollback path:

- `project-lead` behavior or any subagent definition
- Quality gates, hooks, settings
- Generation, validation, or packaging scripts
- Capability manifest schema
- Skill packaging flow

These should also include a planning memo (`artifacts/decision-memo.md`) and ideally an ADR under `docs/adr/`.

## Adding a new phase, profile, agent, or skill

- **New phase:** append to `docs/META_PHASES.md`. Do not renumber existing approved phases.
- **New profile:** add to `capabilities/project-capabilities.yaml` with `extends: default`. See `docs/EXTENSION_PATTERNS.md`.
- **New agent:** add to `.claude/agents/` and register in `capabilities/project-capabilities.yaml`. Follow the role-description style of existing agents.
- **New skill:** follow the skill anatomy (Overview / When to Use / Process / Common Rationalizations / Red Flags / Verification). Place under `.claude/skills/<name>/SKILL.md`. Validate via `python3 scripts/validate-skill.py`.

## Pull request expectations

- A clear description of *why* (motivation) before *what* (mechanics).
- Tests where applicable. The testing gate is in `docs/QUALITY_GATES.md` — a fix needs a regression test that would fail without it.
- Updated docs for any rule, gate, or architecture change.
- A `decision_memo` reference if the planning gate applied.
- A green CI run.
- Conventional-style commit subjects (`feat:`, `fix:`, `chore:`, `docs:`, etc.) — match existing history.

## What is out of scope

- Generic application framework features (the scaffold is not a framework for app code).
- Project-specific product specs (those belong in downstream repos).
- Tool integrations that significantly broaden surface area without a phase plan.
- Always-on permissive execution. Autonomous behavior must remain opt-in.

## Reporting issues

When filing a bug, include:

- The scaffold commit / version
- The execution mode (interactive vs containerized)
- The command or workflow that failed
- Expected vs observed behavior
- Logs or artifact contents (`artifacts/phase-approval.json`, `artifacts/phase-blocked.json`) when relevant

## Code of conduct

Be kind. Assume good intent. Critique ideas, not people. Reviewers and contributors are both volunteers donating attention — make every exchange worth it.
