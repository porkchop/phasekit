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

## Adding a new tracked file (script, devcontainer asset, doc, template, …)

Every file under version control must be classified by the install manifest, otherwise the file silently fails to provision downstream on `phasekit --upgrade` even though the local tree looks fine.

Procedure for any new file you commit:

1. **Pick its ownership class** — see `docs/CAPABILITY_MANIFEST.md`. The common ones:
   - `scaffold` — copied verbatim into downstream projects.
   - `scaffold-internal` — lives in this repo only; never installed downstream.
   - `scaffold-template` — rendered through the engine, not copied verbatim.
   - `bootstrap-frozen` / `bootstrap-with-template-tracking` — downstream-only, write-once at install.
2. **Register the file** in the right place:
   - **Scripts** that should ship downstream — add an entry under `scripts:` in `capabilities/project-capabilities.yaml`, *and* add the path to `ALWAYS_INSTALLED_FILE_PATHS` in `scripts/enrich-project.py` (unless it's already covered by `WORKFLOW_SCRIPTS` + an `include_scripts` profile entry).
   - **Docs, agents, hooks, templates** — add an entry under the matching typed section in `capabilities/project-capabilities.yaml` and, if needed, the profile's `include_*` list.
   - **Anything else** — add an entry under the top-level `files:` map in `capabilities/project-capabilities.yaml` with an explicit `ownership:` class, or extend the `ignore:` globs if the file is genuinely build-time only and never tracked.
3. **Verify** with `python3 scripts/enrich-project.py --self-check`. Every tracked file must classify into exactly one class.

The pre-commit hook in `.githooks/pre-commit` runs `--self-check` automatically. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

This catches the most common failure (forgetting to register a new file) at the moment of commit instead of weeks later when a downstream `phasekit --upgrade` is missing the file.

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
