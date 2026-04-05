# competent developer template

A reusable Claude Code project scaffold for methodical, phase-gated software delivery.

It is designed for:
- greenfield projects
- adopting an existing repository in audit-first mode
- long-running containerized Claude Code sessions
- subagent-based execution with review, QA, and commit gates
- scaffold self-improvement under the same workflow it provides to downstream repos
- capability packaging for downstream project enrichment

## Core workflow

1. Write or adapt the product spec in `docs/`.
2. Start the lead in audit mode if code already exists.
3. Require a strategy memo and adversarial review for major design decisions.
4. Require code review and QA before phase approval.
5. Let the host wrapper create a git commit between approved phases.
6. Continue until `artifacts/project-complete.json` is written.

## Meta workflow

This scaffold can also operate as its own product.

When doing that:
- use `docs/META_SPEC.md` and `docs/META_PHASES.md`
- keep the lead in audit mode
- treat capability generation and skill packaging as first-class deliverables
- require planner + red-team for control-loop and packaging changes

## Included roles

- `project-lead`
- `strategy-planner`
- `architecture-red-team`
- `backend-builder`
- `frontend-builder`
- `engine-builder`
- `code-reviewer`
- `qa-playwright`
- `release-hardening`

## Quick start

### Greenfield project

1. Copy this scaffold into your repo root.
2. Customize the docs in `docs/` and the templates in `templates/`.
3. In an isolated container, copy `.claude/settings.container.example.json` to `.claude/settings.local.json`.
4. Run:

```bash
./scripts/run-until-done.sh
```

### Existing repo adoption

1. Copy this scaffold into your repo root.
2. Adapt `docs/SPEC.md`, `docs/ARCHITECTURE.md`, and `docs/PHASES.md` to the current codebase.
3. Keep the lead in audit mode until current phases are re-vetted.
4. Run:

```bash
./scripts/run-until-done.sh
```

### Scaffold self-improvement

1. Copy or open this scaffold repo in an isolated container.
2. Review `docs/META_SPEC.md`, `docs/META_PHASES.md`, and `capabilities/project-capabilities.yaml`.
3. Use the meta kickoff prompt.
4. Keep work scoped to the earliest unapproved meta-phase.

## Files to customize first

- `docs/SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/PHASES.md`
- `docs/PROD_REQUIREMENTS.md`
- `docs/META_SPEC.md`
- `docs/META_PHASES.md`
- `capabilities/project-capabilities.yaml`
- `.claude/settings.json`
- `.claude/agents/project-lead.md`

## Important operating notes

- The wrappers assume `claude`, `git`, and `jq` are installed.
- The wrappers assume a clean git repository or that you are comfortable with automated commits.
- Commits are created by the outer shell wrapper, not by Claude.
- Keep `.claude/settings.local.json` out of version control.

## Claude startup files

This scaffold includes a generic `.claude/CLAUDE.md` for the scaffold repo itself.

Downstream project-specific Claude startup files should be generated from templates rather than copied from the scaffold repo unchanged.

## Deliverables produced by the workflow

- `artifacts/decision-memo.md`
- `artifacts/phase-approval.json`
- `artifacts/project-complete.json`
- packaged skill artifacts where relevant
- one git commit per approved phase

## Execution modes

This scaffold supports two ways of working:

### Interactive collaboration mode
Use Claude directly on the repository with conservative shared settings. This is the default mode and is intended for normal development, design, review, and iteration.

### Containerized unattended mode
Use the wrapper scripts in an isolated container when you intentionally want autonomous phase-gated work with permissive execution. This mode is opt-in and intended only for trusted repositories.

The scaffold is designed so that unattended behavior does not interfere with normal direct work on the repo.

## Containerized unattended runs

For unattended permissive execution, prefer an isolated container environment with local-only overrides or explicit command-line permission flags.

Do not enable permissive execution as the default shared project setting for all users.