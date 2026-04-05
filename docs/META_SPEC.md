---
title: Scaffold Self-Improvement Spec
---

# Scaffold Self-Improvement Spec

## Purpose

This repository is a reusable development operating system for Claude Code-driven projects.

It must do two things well:

1. Provide a high-quality, phase-gated, multi-agent development workflow for product repositories
2. Generate, validate, and package the reusable project-enrichment assets needed to apply that workflow to downstream projects

These assets include:
- Claude subagents in `.claude/agents/`
- Claude settings and hooks in `.claude/`
- Claude/ChatGPT skills in `.claude/skills/`
- packaged `skill.zip` deliverables
- supporting workflow docs, templates, and usage instructions

## Core goals

- Remain reusable across many project types
- Improve itself safely through audited, phase-gated changes
- Support both greenfield setup and adoption of existing repos
- Generate capability assets from a clear source of truth
- Preserve backward compatibility where practical
- Keep all workflow behavior inspectable, versioned, and testable

## Non-goals

- Becoming a generic code framework for app logic
- Replacing project-specific product specs
- Fully autonomous deployment to production without explicit configuration
- Silently rewriting its own workflow without review and approval

## Product modes

### Mode 1: Scaffold mode
The repo is used as a reusable project scaffold for a new or existing codebase.

### Mode 2: Capability-packager mode
The repo generates reusable enrichment assets for downstream projects, including agents, settings, hooks, skills, packaged skill zips, and usage docs.

### Mode 3: Self-improvement mode
The repo improves its own templates, agents, quality gates, packaging flow, and skill-generation pipeline using the same disciplined workflow it provides to downstream repos.

## Execution modes

The scaffold must support at least two execution modes:

### Interactive collaboration mode
Used when a human is directly collaborating with Claude on this repository or a downstream project.

Requirements:
- conservative shared project settings
- no assumption of permissive execution
- minimal interference with normal repo work
- easy direct use of Claude on the repository itself

### Containerized unattended mode
Used when the scaffold runs autonomous phase-gated work with permissive execution.

Requirements:
- isolated container environment
- documented initialization flow
- local-only permissive settings or command-line overrides
- wrapper scripts able to run inside the container
- clear warning that this mode is for trusted repositories only

The scaffold should prefer interactive collaboration mode by default.
Containerized unattended mode should be opt-in.

## Containerization support

The scaffold must be able to assist users in setting up a containerized execution environment suitable for unattended Claude Code runs.

This support must include:
- reference container configuration based on Anthropic's reference devcontainer
- firewall rules restricting outbound network access to whitelisted domains only (default-deny policy)
- initialization scripts
- documentation for starting the container
- wrapper scripts that operate inside the container
- safety guidance for permissive execution

The scaffold should adopt Anthropic's reference devcontainer approach (https://github.com/anthropics/claude-code/tree/main/.devcontainer) as the basis for its container configuration, adapting it with scaffold-specific additions as needed. The approach must work without Visual Studio Code — users must be able to build and run the container via CLI tools (`docker build`, `devcontainer` CLI, or wrapper scripts).

## Non-interference principle

The scaffold must not make ordinary human collaboration on the repository cumbersome.

Requirements:
- project-scoped settings should remain conservative by default
- permissive unattended behavior should live in local/container-only configuration or explicit command-line overrides
- the repository should remain comfortable to use directly with Claude for design, implementation, and review work
- autonomous workflow behavior should be opt-in rather than always-on

## Design principles

- Audit before rewrite
- Prefer additive changes
- Keep one source of truth for project capabilities
- Keep generated artifacts reproducible
- Keep workflow rules explicit and documented
- Treat skill generation as a first-class deliverable, not an afterthought
- Require review and validation before accepting control-loop changes

## Required deliverables

The scaffold must support generation and maintenance of:

- `.claude/agents/*`
- `.claude/settings.json`
- `.claude/hooks/*`
- `.claude/skills/<skill-name>/...`
- packaged `skill.zip` artifacts
- capability manifest files
- docs describing usage, adoption, and extension
- templates for new downstream projects
- downstream Claude startup files such as `.claude/CLAUDE.md`
- optional downstream `.claude/rules/*.md` files

## Safety rules for self-improvement

Any change to the scaffold’s control loop, quality gates, subagent definitions, or generation logic must include:

- an explicit rationale
- a description of tradeoffs
- a rollback path
- updated docs
- reviewer approval
- red-team scrutiny if the change affects architecture or autonomy

## Quality expectations

A self-improvement phase is only complete when:

- acceptance criteria are met
- relevant tests pass
- `code-reviewer` approves
- `strategy-planner` and `architecture-red-team` are used for material design changes
- skill validation/packaging passes for any skill-related outputs
- docs are updated
- an approval artifact is written
- the outer wrapper commits the approved phase before the next phase begins

## Success criteria

This scaffold is successful when it can:

- set up a new downstream project consistently
- adopt an existing repo in audit mode
- generate project skills and supporting assets from a manifest
- package those skills reliably
- improve its own workflow safely over time

## Downstream Claude startup files

The scaffold must support generation of downstream Claude startup files, including:
- `.claude/CLAUDE.md`
- optional `.claude/rules/*.md`

These files must be concise, project-oriented, and derived from the selected capability profile and project docs.

### Generation flow

1. Source template: `templates/CLAUDE.template.md`
2. Placeholders: `{{PROJECT_NAME}}`, `{{OPTIONAL_REFERENCES}}` (currently unused; reserved for profile-driven references)
3. Rendered by: `scripts/bootstrap-new-project.sh` or `scripts/adopt-existing-repo.sh`
4. Output: `.claude/CLAUDE.md` in the downstream project (skipped if file already exists)

The scaffold repo’s own `.claude/CLAUDE.md` should remain generic and stable.

## Skill validation and packaging flow

Skills declared in `capabilities/project-capabilities.yaml` can be validated and packaged using dedicated scripts.

### Validation

```
python3 scripts/validate-skill.py              # validate all skills with validate=true
python3 scripts/validate-skill.py --skill KEY  # validate a specific manifest skill
python3 scripts/validate-skill.py --source DIR # validate any skill directory
```

Validation checks:
- `SKILL.md` exists with YAML frontmatter containing `name` and `description`
- `SKILL.md` has at least one markdown heading
- `agents/openai.yaml` exists with a valid `interface` key

### Packaging

```
python3 scripts/package-skill.py               # package all skills with package=true
python3 scripts/package-skill.py --skill KEY   # package a specific skill
python3 scripts/package-skill.py --force        # overwrite existing archives
```

Packaging behavior:
- Runs validation before packaging; fails if validation fails
- Writes to the `package_output` path from the manifest (e.g. `dist/skills/<name>/skill.zip`)
- Excludes dotfiles and `__pycache__`
- Reports file count and archive size

### Output conventions

- Generated skill folders: `generated/skills/<name>/` (from `generation.output_root`)
- Packaged archives: `dist/skills/<name>/skill.zip` (from `generation.skills_output_root`)