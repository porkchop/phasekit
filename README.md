# competent developer template

A reusable Claude Code project scaffold for methodical, phase-gated software delivery. Works in two modes: **interactive collaboration** (default) where you work directly with Claude, and **autonomous containerized execution** where a wrapper script drives Claude through phases unattended.

## What it provides

- **Phase-gated workflow** — work progresses through defined phases with explicit approval artifacts and git commits between each
- **Subagent roles** — specialized agents for planning, building, reviewing, QA, and hardening
- **Safety by default** — conservative settings, a `deny-dangerous-commands` hook, and permissive execution only via opt-in CLI flags
- **Enrichment scripts** — bootstrap new projects or adopt existing repos with a single command
- **Capability profiles** — named bundles (`default`, `game-project`, `saas-project`, or custom) that control which agents, docs, hooks, and scripts a project receives
- **Skill packaging** — validates and packages reusable Claude Code skills as `skill.zip` deliverables

## Quick start: interactive mode

This is the default and most common way to use a scaffolded project. Open the repo in Claude Code and work normally — the scaffold provides structure without getting in the way.

### New project

```bash
mkdir my-project && cd my-project && git init
bash /path/to/scaffold/scripts/bootstrap-new-project.sh        # uses "default" profile
# or with a specific profile:
bash /path/to/scaffold/scripts/bootstrap-new-project.sh game-project
```

Then customize the generated docs for your project:
- `docs/SPEC.md` — product specification
- `docs/ARCHITECTURE.md` — technical architecture
- `docs/PHASES.md` — phase plan
- `docs/PROD_REQUIREMENTS.md` — production/deployment requirements

Start Claude Code in the project directory and work interactively. The `project-lead` agent can orchestrate phased delivery, or you can just use Claude directly for design, implementation, and review.

### Existing repo

```bash
cd /path/to/existing-repo
bash /path/to/scaffold/scripts/adopt-existing-repo.sh           # uses "default" profile
```

This copies agents, hooks, scripts, and doc templates **without overwriting existing files**. Adapt the generated `docs/` to your current codebase state, then start the lead in audit mode to re-vet existing work.

### Re-enriching a project

To update a previously scaffolded project with newer scaffold assets:

```bash
python3 /path/to/scaffold/scripts/enrich-project.py /path/to/project --profile default
# preview first with --dry-run; overwrite with --force
```

## Quick start: autonomous mode (containerized)

For unattended phase-loop execution in an isolated container with network firewall and Playwright MCP browser automation. **Opt-in only — do not use on repos you don't fully trust.**

```bash
# Build the container image
bash scripts/container-setup.sh build

# Option A: subscription auth (flat rate, recommended for heavy workloads)
bash scripts/container-setup.sh setup    # interactive login, one-time
bash scripts/container-setup.sh run      # autonomous loop

# Option B: API key auth (pay-per-token)
ANTHROPIC_API_KEY='sk-ant-...' bash scripts/container-setup.sh run
```

### How the autonomous loop works

1. `container-setup.sh run` starts the container with firewall + `NET_ADMIN` capabilities
2. `entrypoint.sh` runs `init-firewall.sh` (default-deny + whitelisted domains)
3. `run-until-done.sh` reads `CONTINUE_PROMPT.txt` and invokes `run-phase.sh` in a loop
4. Each phase: Claude reads the prompt, finds the earliest unapproved phase, executes it, writes `artifacts/phase-approval.json`
5. The wrapper commits, then starts the next iteration
6. Loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` (default 50) is reached

The wrapper passes `--permission-mode bypassPermissions` — this flag only applies inside the container and does not affect interactive users.

See `docs/CONTAINERIZATION.md` for full details on security model, firewall, environment variables, and troubleshooting.

## Profiles

Profiles define which agents, docs, hooks, and scripts a project receives. Defined in `capabilities/project-capabilities.yaml`.

| Profile | Agents added beyond default | Best for |
|---|---|---|
| `default` | project-lead, strategy-planner, architecture-red-team, code-reviewer, qa-playwright | General web apps, services, CLIs |
| `game-project` | + engine-builder, frontend-builder, backend-builder, release-hardening | Games, interactive simulations |
| `saas-project` | + frontend-builder, backend-builder, release-hardening | SaaS products with API/persistence |

Create custom profiles by adding entries to `capabilities/project-capabilities.yaml` with `extends: default`. See `docs/EXTENSION_PATTERNS.md` for patterns and a profile selection guide.

## Included roles

| Agent | Purpose |
|---|---|
| `project-lead` | Orchestrates phased delivery and approvals |
| `strategy-planner` | Writes implementation strategy memos for major decisions |
| `architecture-red-team` | Challenges design choices, exposes risks |
| `backend-builder` | APIs, persistence, auth, server-authoritative validation |
| `frontend-builder` | UI and client workflows, separated from domain logic |
| `engine-builder` | Deterministic core logic and rules engines |
| `code-reviewer` | Code quality, architecture compliance, test sufficiency |
| `qa-playwright` | Browser verification of user-visible functionality |
| `release-hardening` | Production readiness, observability, operational safety |

Agent definitions live in `.claude/agents/`. The `autonomous-product-builder` skill (`.claude/skills/autonomous-product-builder/`) packages the full workflow as a reusable Claude Code skill.

## Core workflow

1. Write or adapt the product spec in `docs/`
2. Start the lead in audit mode if code already exists
3. Require a strategy memo and adversarial review for major design decisions (`artifacts/decision-memo.md`)
4. Require code review and QA before phase approval
5. The outer wrapper (or you) creates a git commit between approved phases
6. Continue until `artifacts/project-complete.json` is written

### Artifacts produced

| Artifact | When |
|---|---|
| `artifacts/phase-approval.json` | After each approved phase |
| `artifacts/decision-memo.md` | After planning-gate decisions |
| `artifacts/phase-blocked.json` | When external input is needed |
| `artifacts/project-complete.json` | When all phases are done |

## Settings and safety

### Settings layering (later wins)

1. **Project settings** (`.claude/settings.json`) — checked in, conservative, shared with all users
2. **Local settings** (`.claude/settings.local.json`) — gitignored, per-user overrides
3. **CLI flags** (`--permission-mode bypassPermissions`) — used only by wrapper scripts in containers

### Safety hooks

The `deny-dangerous-commands` hook (`.claude/hooks/deny-dangerous-commands.sh`) runs as a `PreToolUse` hook on all `Bash` calls in interactive mode. It blocks dangerous operations like `rm -rf /`, `chmod 777`, etc.

In containerized mode with `bypassPermissions`, hooks are bypassed — the container's network firewall and isolation are the safety boundary instead.

**Rule: never make project settings permissive to support autonomous mode.** Use local settings or CLI flags instead.

## Project structure

```
.claude/
  CLAUDE.md                       # scaffold-specific Claude instructions
  settings.json                   # conservative shared settings + hooks
  agents/                         # subagent role definitions (9 agents)
  hooks/deny-dangerous-commands.sh
  skills/autonomous-product-builder/  # packaged workflow skill
.devcontainer/                    # Anthropic reference devcontainer + firewall
artifacts/                        # phase approval and completion artifacts
capabilities/
  project-capabilities.yaml       # single source of truth for profiles and capabilities
docs/                             # specs, phases, quality gates, guides
examples/
  game-project/                   # example enrichment output
  saas-project/                   # example enrichment output
scripts/
  bootstrap-new-project.sh        # enrich a new project from a profile
  adopt-existing-repo.sh          # enrich an existing repo (no-overwrite)
  enrich-project.py               # profile-based enrichment engine
  run-phase.sh                    # run a single phase via Claude CLI
  run-until-done.sh               # autonomous phase loop
  container-setup.sh              # build/run/shell for Docker container
  generate-skill.py               # generate skill folders from templates
  validate-skill.py               # validate skill structure
  package-skill.py                # package skills as skill.zip
  verify-container.sh             # post-build container health check
templates/                        # templates for specs, skills, CLAUDE.md, ADRs
CONTINUE_PROMPT.txt               # prompt used by the autonomous loop
KICKOFF.md                        # entrypoint documentation
```

## Key documentation

| Doc | Purpose |
|---|---|
| `docs/EXECUTION_MODES.md` | Interactive vs. autonomous mode details |
| `docs/QUALITY_GATES.md` | Phase approval rules, planning gates, commit gates |
| `docs/CAPABILITY_MANIFEST.md` | Manifest schema and profile resolution |
| `docs/EXTENSION_PATTERNS.md` | Adding profiles, agents, hooks, skills, docs |
| `docs/CONTAINERIZATION.md` | Container setup, firewall, auth, troubleshooting |
| `docs/COMPATIBILITY.md` | Versioning policy and upgrade guidance |
| `docs/REASONING_PROFILES.md` | When to use deeper reasoning per role |
| `docs/USAGE_PATTERNS.md` | Workflow patterns for different project types |
| `docs/META_SPEC.md` | Scaffold self-improvement specification |
| `docs/META_PHASES.md` | Self-improvement phase plan |

## Scaffold self-improvement

This scaffold can improve itself under the same workflow it provides to downstream repos.

1. Review `docs/META_SPEC.md`, `docs/META_PHASES.md`, and `capabilities/project-capabilities.yaml`
2. Start in audit mode from the earliest unapproved meta-phase
3. Use planner + red-team for control-loop or packaging changes
4. Require reviewer approval and skill validation for relevant phases

See `docs/SELF_APPLICATION_EXAMPLE.md` for a worked example.

## Prerequisites

- `claude` CLI installed
- `git` and `jq` available
- Python 3 with `pyyaml` (for enrichment/skill scripts)
- Docker (only for containerized mode)

## Operating notes

- Commits in autonomous mode are created by the outer shell wrapper, not by Claude
- Keep `.claude/settings.local.json` out of version control (gitignored by default)
- The wrapper scripts assume a clean git repository or that you accept automated commits
- `CONTINUE_PROMPT.txt` drives the autonomous loop — it instructs Claude to find and execute the next unapproved phase
- Downstream `.claude/CLAUDE.md` files are generated from `templates/CLAUDE.template.md`, not copied from the scaffold