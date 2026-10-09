# phasekit

A reusable Claude Code project scaffold for methodical, phase-gated software delivery. Works in two modes: **interactive collaboration** (default) where you work directly with Claude, and **autonomous execution** (on the host or in a container) where phasekit's loop drives Claude through phases unattended.

```
  AUDIT          PLAN           BUILD          VERIFY         REVIEW         APPROVE
 ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐
 │ Read │ ───▶ │ Memo │ ───▶ │ Code │ ───▶ │ Test │ ───▶ │  QA  │ ───▶ │ Gate │
 │Phase │      │  +   │      │ Diff │      │  +   │      │ Gate │      │ JSON │
 │Goal  │      │ Risk │      │      │      │ Lint │      │      │      │+Commit│
 └──────┘      └──────┘      └──────┘      └──────┘      └──────┘      └──────┘
```

Each phase ends with `artifacts/phase-approval.json` and an external commit. The next phase does not begin until the commit lands.

Since v0.19.0 phasekit's engine (the loop, the hooks, the agents, the process docs, the container setup) lives **outside** your repository. A project carries one line, `.phasekit-version`, naming the release it runs on, plus its own documents and gate. Projects created before v0.19.0 carry a copy of the engine in their own tree ("vendored"); they keep working unchanged and can be converted with `phasekit migrate` (see [Migrating from vendored](#migrating-from-vendored)).

## Quickstart

### 1. Install (once per machine)

```bash
curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh | bash
```

This installs three things:

- the `phasekit` command (a canonical phasekit clone at `~/.local/share/phasekit`, with an isolated venv, and a launcher in `~/.local/bin`);
- the **engine store**: the release you installed, checked out read-only at `~/.local/share/phasekit/engines/<tag>/`;
- the **phasekit Claude Code plugin**, so interactive Claude Code sessions in a phasekit project get its command guard, hooks and agents (skip with `PHASEKIT_NO_PLUGIN=1`; install later with `phasekit plugin install`).

It does not touch any project. Requires `git` and `python3` with venv support; the plugin step needs the `claude` CLI (without it the step is skipped with a note).

### 2. Start a project

```bash
mkdir my-project && cd my-project
phasekit init                   # "default" profile
phasekit init python-uv         # or a named profile (python-uv, static-web, game-canvas, docs-only, game-project, ...)
```

`phasekit init` runs `git init` if needed, writes `.phasekit-version` (this phasekit's release) and the project's own files — `docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, `docs/PROD_REQUIREMENTS.md`, `docs/LEARNINGS.md`, `docs/CONVENTIONS.md` (stack profiles), `docs/project/QUALITY_GATES.md`, `.claude/CLAUDE.md`, `.claude/settings.json` (permissions only; the hooks come from the plugin), `AGENTS.md` and `scripts/phasekit-verify.sh` — and commits them in one commit. It never overwrites a file that already exists, so it works in an existing repository too. Options: `--pin TAG` (pin another release, v0.19.0 or later), `--no-commit`, `--no-plugin`.

Then fill in `docs/SPEC.md` and `docs/PHASES.md` (and the other docs as needed).

### 3. Use it

```bash
claude                          # interactive: work with Claude directly; the plugin supplies the guard and the phasekit:* agents
phasekit loop                   # the autonomous phase loop on this machine
phasekit run                    # the autonomous phase loop in the isolated container (see below)
```

The `project-lead` agent (`phasekit:project-lead` in a pinned project) can orchestrate phased delivery interactively, or you can use Claude directly for design, implementation, and review.

### 4. Upgrade

```bash
phasekit self-update            # move the phasekit install to the newest release (also installs it into the engine store)
phasekit upgrade                # bump this project's pin to the newest release known here, through its gate
```

`phasekit upgrade` is a pin bump: it installs the new engine, writes `.phasekit-version`, runs the project's own gate under the new engine, and commits one line, `chore(phasekit): pin vA -> vB`. A red gate leaves everything as it was (exit 4). It does not push unless you pass `--push`; `--to TAG` picks a specific release.

## What it provides

- **Phase-gated workflow** — work progresses through defined phases with explicit approval artifacts and git commits between each
- **Subagent roles** — specialized agents for planning, building, reviewing, QA, and hardening
- **Safety by default** — conservative settings, a `deny-dangerous-commands` command guard, and permissive execution only via opt-in CLI flags
- **An engine outside the repository** — a project pins a release; the engine runs read-only from the engine store, so there are no engine files to drift, merge or review in the project
- **Capability profiles** — named bundles (`default`, `game-project`, `saas-project`, stack profiles, or custom) that control which docs, agents and gate a project receives
- **Skill packaging** — validates and packages reusable Claude Code skills as `skill.zip` deliverables

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh | bash
```

This installs a canonical phasekit clone to `~/.local/share/phasekit` (with an isolated venv for its one dependency), puts a `phasekit` command on your PATH, installs the release it sits on into the engine store, and installs the Claude Code plugin. It tracks the latest release tag (the `stable` channel) and **does not touch any project**. Re-run it (or `phasekit self-update`) to move to the newest release; `phasekit channel edge` switches to tracking `origin/master` (see `docs/INSTALL_LIFECYCLE.md`).

Prefer to read before running? Download and inspect first:

```bash
curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh -o install.sh
less install.sh && bash install.sh
```

Overridable via env: `PHASEKIT_HOME` (install dir), `PHASEKIT_BIN` (launcher dir), `PHASEKIT_REF` (pin a tag/branch), `PHASEKIT_NO_PLUGIN=1` (skip the plugin). Requires `git` and `python3` with venv support.

## How a pinned project runs

- **The pin.** `.phasekit-version` holds one release tag (`v0.19.0` or later). It is the only engine fact the repository carries. It moves only by `phasekit upgrade`; the loop never commits a session's change to it.
- **The engine store.** Each pinned release is a read-only `git archive` checkout at `~/.local/share/phasekit/engines/<tag>/` (override the location with `PHASEKIT_ENGINE_STORE`), with `.engine-version` and `.engine-commit` recording what it is. The canonical clone itself never runs a project: it is writable and moves with `self-update`.
- **Fetched on first use.** A pinned release the store lacks is installed automatically the first time a verb needs it, from the canonical clone's own git tags (fetching tags from the clone's `origin` first if the tag is not there yet). `PHASEKIT_NO_AUTO_FETCH=1` turns that off for offline use: the verb then fails with exit 6 and names `phasekit engines install <tag>`. Details and the safety rules are in `docs/INSTALL_LIFECYCLE.md`.
- **The plugin.** Interactive Claude Code sessions get phasekit's hooks and agents from the plugin (`phasekit:<agent>`); the loop passes the engine's own copy of the plugin to every turn it starts and refuses a turn whose guard did not load (see [Safety hooks](#safety-hooks)).
- **The engine's docs.** `docs/QUALITY_GATES.md`, `docs/USAGE_PATTERNS.md` and the other process docs are in the engine, not the project: `phasekit docs` prints the directory.

### Lifecycle commands

Run these from **inside** a project (they act on the current directory):

```bash
phasekit init [profile]          # a new pinned project (see Quickstart)
phasekit status                  # current phase state (derived from artifacts)
phasekit check                   # pinned: pin installed, no engine files tracked, no stale hook wiring, plugin present
phasekit upgrade [--to TAG]      # pinned: a gated pin bump, one commit (--push to push it)
phasekit loop                    # the phase loop on the host
phasekit run                     # the phase loop in the container
phasekit container build|setup|run|shell   # the container commands for this project
phasekit verify                  # run this project's gate exactly as the loop's commit will (a green run is reused)
phasekit scope [--phase P]       # what this iteration (or one phase) changed, from its base and evidence
phasekit facts [--json]          # the facts phasekit declares for downstream tests
phasekit contracts check|refresh|status   # the cross-project contracts checker
phasekit engines install|list|path [TAG]  # the engine store
phasekit plugin install|status   # the Claude Code plugin
phasekit docs                    # print the engine's docs directory
phasekit hook <name>             # run one of the engine's hooks (stdin passed through)
phasekit migrate [--dry-run]     # convert a vendored project to a pinned one
phasekit channel [name]          # show/set self-update channel (stable|edge|<ref>)
phasekit self-update             # move the phasekit install along its channel
```

`phasekit check` exits 0 when clean, 3 when a phasekit engine path is still tracked in the project or `.claude/settings.json` still wires vendored hook paths, and 6 when the pinned release is not installed (it names `phasekit engines install <tag>`; `check` never fetches). A missing plugin is a loud warning, not an exit code.

See `docs/INSTALL_LIFECYCLE.md` for the full lifecycle contract and the "What to commit" guidance.

## Migrating from vendored

A project enriched before v0.19.0 carries the engine in its own tree (`scripts/run-until-done.sh`, `.claude/hooks/`, `.claude/agents/`, the process docs, `.scaffold/manifest.json`, ...). It keeps working exactly as before under every verb. To convert it:

```bash
cd my-old-project
phasekit migrate --dry-run      # print the plan: every file it would delete, the settings change, the pin (refuses as migrate would)
phasekit migrate                # do it: one commit, never pushed
```

What `phasekit migrate` does, exactly:

1. **Preconditions.** The project has `.scaffold/manifest.json` and at least one commit, and the working tree is clean (commit or stash first). The installed phasekit must sit on a release tag, or you pass `--pin TAG` (v0.19.0 or later).
2. **Refuses to discard local edits.** If any engine file the manifest names was edited since phasekit wrote it, or carries a standing `--keep-local`, it lists them and stops (exit 2). Move what you need into project-owned files (`docs/project/<NAME>.md` is the companion for a process doc) and re-run, or pass `--discard-local` to let the engine's copy replace them.
   **Refuses project files that read engine paths (v0.19.2).** Before it deletes anything or runs the gate, it scans the project's own code (tests, scripts, source) for reads of an engine file by its in-tree path — `contracts/interface.json` included — and stops (exit 2) with each `file:line: path`. Read the contract through `$PHASEKIT_CONTRACT` instead (exported in both layouts; `phasekit facts --path` by hand; see `docs/QUALITY_GATES.md` "Tests read the declared surface"). `--force` skips this pre-flight and leaves the verdict to the gate. `phasekit check` shows the same list ahead of time, as the `migration-readiness` hint.
3. **Deletes exactly** the files the manifest records as `scaffold` class, and `.scaffold/`. Project-owned files (SPEC, PHASES, CONVENTIONS, `AGENTS.md`, `.claude/CLAUDE.md`, the gate, ...) are not touched.
4. **Strips phasekit's hook wiring** (the four engine hooks under `.claude/hooks/`) from `.claude/settings.json`; its permissions and any hooks of the project's own are unchanged. The plugin supplies the hooks from now on.
5. **Writes** `.phasekit-version`.
6. **Runs the project's gate under the engine** (the same gate `phasekit upgrade` runs; `PHASEKIT_UPGRADE_VERIFY` chooses host or container). Red: every byte is restored, nothing is committed, exit 4.
7. **Commits** the result in one commit (`chore(phasekit): migrate to the engine outside the repo (vX.Y.Z)`). It never pushes: push when you are ready.

`phasekit migrate` is idempotent: on a project that is already pinned it says so and does nothing. After migrating, `phasekit check` should print `clean`.

**If a supervisor dispatches the project** (an orchestrator that starts sessions by running the project's own `scripts/container-setup.sh`), migrate only after that supervisor understands pins: a migrated project has no vendored loop for it to start.

## Autonomous mode

For unattended phase-loop execution. **Opt-in only — do not use on repos you don't fully trust.**

### In the container (recommended)

An isolated container with a network firewall and Playwright MCP browser automation:

```bash
# Build the container image (once; rebuild after a phasekit release that changes the image)
phasekit container build

# Option A: subscription auth (flat rate, recommended for heavy workloads)
phasekit container setup        # interactive login, one-time
phasekit run                    # autonomous loop

# Option B: API key auth (pay-per-token)
ANTHROPIC_API_KEY='sk-ant-...' phasekit run
```

In a pinned project the container mounts the project at `/workspace` and the engine **read-only** at `/opt/phasekit`, and each project's sessions get their own Claude configuration directory (`docs/CONTAINERIZATION.md`). `phasekit run` never rebuilds the shared image per run; `phasekit container build` does.

### On the host

```bash
phasekit loop
```

The same loop, run directly on your machine with your own git credentials. It invokes Claude with `--permission-mode bypassPermissions`, so use it only where you would accept that.

### How the autonomous loop works

1. `phasekit run` starts the container with firewall + `NET_ADMIN` capabilities (`phasekit loop` skips steps 1–2)
2. `entrypoint.sh` runs `init-firewall.sh` (default-deny + whitelisted domains)
3. The engine's `run-until-done.sh` reads its `CONTINUE_PROMPT.txt` and invokes `run-phase.sh` in a loop
4. Each phase: Claude reads the prompt, finds the earliest unapproved phase, executes it, writes `artifacts/phase-approval.json`
5. The loop commits (verify-gated), then starts the next iteration
6. Loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` (default 50) is reached

To cap the run at a small number of iterations — handy for smoke-testing a setup, validating a single phase, or stopping after a fixed billable budget — set `MAX_ITERATIONS` on the command line:

```bash
MAX_ITERATIONS=1 phasekit loop                  # one phase then stop
MAX_ITERATIONS=2 AUTO_PUSH=1 phasekit loop      # two phases with push
MAX_ITERATIONS=3 phasekit run                   # also works in container mode
```

The loop exits 0 on a clean stop, 2 on a blocker, 3 if it hits `MAX_ITERATIONS` without project completion.

### Optional: auto-push after each phase

For projects whose feedback loop depends on CI / deploy previews / github-pages-as-progress-mirror, set `AUTO_PUSH=1` to have the loop `git push` after every phase commit. Failures are non-fatal — the commit is already local; the next iteration continues.

```bash
# Host-side loop (simplest — uses your existing git credentials):
AUTO_PUSH=1 phasekit loop
```

For container-mode auto-push, the container also needs git auth. The setup script forwards both:

- **SSH remotes** (`git@github.com:...`): the container forwards your host's SSH agent if one is running. Run `ssh-add ~/.ssh/id_ed25519` on the host before invoking.
- **HTTPS remotes with a token**: set `GH_TOKEN` (or `GITHUB_TOKEN`) on the host; it's passed through.

```bash
# SSH-remote case:
eval "$(ssh-agent -s)" && ssh-add
AUTO_PUSH=1 phasekit run

# Token-remote case:
GH_TOKEN=ghp_... AUTO_PUSH=1 phasekit run
```

Default off because pushes cascade side effects (CI runs, deploys, notifications). Enable per-project, not globally. If `git push` inside the container fails (no agent forwarded, no token, branch protection, etc.), the warning logs and the loop continues — you don't lose work.

The loop passes `--permission-mode bypassPermissions` — this flag only applies to the loop's own Claude invocations and does not affect interactive users.

### Optional: rootless Docker

The container runs as the non-root `node` user by default. Under [rootless Docker](https://docs.docker.com/engine/security/rootless/) that user maps to an unmapped subordinate UID, so writes to the bind-mounted `/workspace` fail with `Permission denied`. Set `PHASEKIT_ROOTLESS_DOCKER=1` to run as UID 0 instead — under rootless Docker that maps back to your host user, so `/workspace` is owned correctly while the rootless security boundary is preserved.

```bash
PHASEKIT_ROOTLESS_DOCKER=1 phasekit run
```

Confirm it's active from the startup line `Container user override: running as '0:0'`. Default behavior is unchanged when unset. See `docs/CONTAINERIZATION.md` → "Rootless Docker" for the full rationale and the `PHASEKIT_CONTAINER_USER` (`root` or `uid:gid`) override.

### Pre-commit verification gate

The loop runs `scripts/phasekit-verify.sh` before every phase commit (whether or not AUTO_PUSH is set). A non-zero exit blocks the commit, writes `artifacts/phase-verify-failed.json`, and the next iteration directs Claude to fix the failure before doing new work. After three consecutive failures the loop stops with `phase-blocked.json`. `phasekit verify` runs the same gate by hand.

A **stack profile** (`python-uv`, `static-web`, `game-canvas`, `docs-only`) seeds a working gate for that stack out of the box (plus a project-owned `docs/CONVENTIONS.md` seeded from the stack's template); the file is project-owned after seeding. Under other profiles, `scripts/phasekit-verify.sh` starts as a stub — edit it to call your stack's fast checks (lint, typecheck, unit tests). Aim for under ~30 seconds; full E2E belongs to the verification-sprint gate, not this one.

Overrides:
- `PHASEKIT_VERIFY_CMD="..."` — one-shot override, bypasses the script
- `VERIFY_SKIP=1` — skip the gate for one iteration (docs-only or TDD red-commit phases)
- `VERIFY_MAX_ATTEMPTS=N` — change the circuit-breaker threshold (default 3)

Until you customize the stub it no-ops with a warning, so existing projects keep working with no change.

### Optional: cross-project contracts

If your project depends on another project's interface — an HTTP API, a file
format, a set of env vars and exit codes — you can stop guessing at it. Declare
the dependency in a `contracts.yaml` at your repo root, commit a vendored copy
of the producer's contract under `vendor/contracts/<slug>/`, and phasekit
refuses any phase commit where that copy has drifted from the producer's
authoritative one.

The split that makes this work anywhere: **your ordinary test suite validates
against the vendored copy**, so `git clone && <your tests>` passes with no
provider and no mount, exactly like a lockfile. Only the pre-commit gate needs a
provider, and only when your repo declares one. A repo with no `contracts.yaml`
behaves exactly as it did before. The checker is `phasekit contracts check`.

```yaml
# contracts.yaml
version: 1
depends_on:
  - billing-api
```

See `docs/CONTRACTS.md`.

See `docs/CONTAINERIZATION.md` for full details on security model, firewall, environment variables, and troubleshooting.

## Profiles

Profiles define which docs, agents, hooks, and scripts a project receives. Defined in `capabilities/project-capabilities.yaml`. In a pinned project the agents come from the engine's plugin; the profile chooses the project's own files (the gate, the conventions, the docs).

| Profile | Agents added beyond default | Best for |
|---|---|---|
| `default` | project-lead, strategy-planner, architecture-red-team, code-reviewer, qa-playwright | General web apps, services, CLIs |
| `game-project` | + engine-builder, frontend-builder, backend-builder, release-hardening | Games, interactive simulations |
| `saas-project` | + frontend-builder, backend-builder, release-hardening | SaaS products with API/persistence |
| `with-mutation` | (none) | Adds the opt-in mutation-testing protocol + harness — see `docs/MUTATION_TESTING.md` |

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

Agent definitions live in the engine's `.claude/agents/`. In a pinned project they come from the plugin and are namespaced: `phasekit:project-lead`, `phasekit:code-reviewer`, and so on. The `autonomous-product-builder` skill (`.claude/skills/autonomous-product-builder/`) packages the full workflow as a reusable Claude Code skill.

## Core workflow

1. Write or adapt the product spec in `docs/`
2. Start the lead in audit mode if code already exists
3. Require a strategy memo and adversarial review for major design decisions (`artifacts/decision-memo.md`)
4. Require code review and QA before phase approval
5. The loop (or you) creates a git commit between approved phases
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

1. **Project settings** (`.claude/settings.json`) — checked in, conservative, shared with all users. In a pinned project it holds permissions only.
2. **Local settings** (`.claude/settings.local.json`) — gitignored, per-user overrides
3. **CLI flags** (`--permission-mode bypassPermissions`, `--plugin-dir`) — used only by the loop's own Claude invocations

### Safety hooks

The `deny-dangerous-commands` command guard runs as a `PreToolUse` hook on every `Bash` call, interactive or autonomous (hooks still run under `bypassPermissions`). In every session it refuses the destructive commands — `git reset --hard`, `git clean -fd`, force or ref-deleting pushes, tag deletion/overwrite, `sudo`, `shred`, a recursive `rm` of the repository root or its `.git`. Under the loop it also refuses every git write to the repository and any `git push` (the loop owns every commit); an interactive session may push and commit normally. See the engine's `docs/QUALITY_GATES.md` "The loop owns every commit". Three more hooks complete the set: `require-verdict` (Stop), `wrapup-nudge` (PreToolUse and PostToolUse) and `compact-reanchor` (SessionStart after compaction).

In a pinned project the hooks come from the **plugin**, not from `.claude/settings.json`:

- **Interactive sessions** get them from the plugin installed for Claude Code (`phasekit plugin install`; `install.sh` and `phasekit init` do it). Without the plugin an interactive session in the project runs without the guard; `phasekit check` warns about it loudly. The plugin is inert outside phasekit projects.
- **The loop** passes the engine's own plugin (`--plugin-dir <engine>/plugin`) to every turn, and before every model turn runs a **guard self-check**: a throwaway, model-free `claude -p` whose prompt the plugin's `guard-probe` hook blocks after proving that the plugin loaded, that its hooks.json wires every hook on its event, and that the command guard — called through the same dispatcher a tool call takes — refuses `git reset --hard` (and `git commit` under the loop). It cannot fire the PreToolUse or Stop events themselves (that would need a model turn). No proof, no turn: `run-phase.sh` refuses the turn (exit 7; the loop retries per `PHASEKIT_ITER_RETRY`, then exits 7, committing nothing). A pinned session never runs without its guard.

**Rule: never make project settings permissive to support autonomous mode.** Use local settings or CLI flags instead.

## Project structure

A pinned project holds only its own files:

```
.phasekit-version                 # the engine pin: one release tag
AGENTS.md                         # guidance for any agent entering the repo
.claude/
  CLAUDE.md                       # project instructions for Claude Code
  settings.json                   # conservative permissions (no hooks: the plugin supplies them)
docs/
  SPEC.md ARCHITECTURE.md PHASES.md PROD_REQUIREMENTS.md LEARNINGS.md
  CONVENTIONS.md                  # stack profiles
  project/QUALITY_GATES.md        # this project's own gates (companion of the engine's)
scripts/
  phasekit-verify.sh              # this project's pre-commit gate
artifacts/                        # phase approval and completion artifacts
```

The phasekit repository is the engine (an engine in the store is a read-only copy of one release of it):

```
.claude/
  CLAUDE.md                       # phasekit's own Claude instructions
  settings.json                   # phasekit's own settings + hooks
  agents/                         # subagent role definitions (9 agents)
  hooks/                          # deny-dangerous-commands, require-verdict, wrapup-nudge, compact-reanchor
  skills/autonomous-product-builder/  # packaged workflow skill
.claude-plugin/marketplace.json   # the directory marketplace the plugin installs from
plugin/                           # the Claude Code plugin: hooks.json, run-hook.sh, agents (links into .claude/)
bin/phasekit                      # the engine's own CLI entry (on PATH in a pinned container run)
.devcontainer/                    # Anthropic reference devcontainer + firewall
capabilities/
  project-capabilities.yaml       # single source of truth for profiles and capabilities
contracts/interface.json          # the declared surface: artifacts, env, conventions, facts
docs/                             # process docs: quality gates, execution modes, guides
examples/                         # example enrichment output
scripts/
  phasekit.sh                     # the CLI behind `phasekit`
  phasekit-pin.py                 # pins, the engine store, init/migrate/check/upgrade, the plugin
  enrich-project.py               # profile resolution, templates, the vendored lifecycle engine
  run-phase.sh                    # run a single phase via Claude CLI
  run-until-done.sh               # autonomous phase loop
  container-setup.sh              # build/setup/run/shell for the Docker container
  generate-skill.py               # generate skill folders from templates
  validate-skill.py               # validate skill structure
  package-skill.py                # package skills as skill.zip
  verify-container.sh             # post-build container health check
templates/                        # templates for specs, skills, CLAUDE.md, ADRs, the gate
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
| `docs/CONTRACTS.md` | Cross-project contract dependencies: declaring, vendoring, the drift gate |
| `docs/MUTATION_TESTING.md` | Opt-in mutation-testing protocol: designing mutants, chunking, the audit record |
| `docs/COMPATIBILITY.md` | Versioning policy and upgrade guidance |
| `docs/REASONING_PROFILES.md` | When to use deeper reasoning per role |
| `docs/USAGE_PATTERNS.md` | Workflow patterns for different project types |
| `docs/RELEASING.md` | How a phasekit release is built, gated and tagged |
| `docs/INSTALL_LIFECYCLE.md` | Install / pin / upgrade / migrate contract; "What to commit" guidance; the vendored lifecycle |

## Developing phasekit

phasekit is developed by hand-built releases: every change reaches every downstream project, so a release is built, gated and reviewed by hand and shipped as a tag (`docs/RELEASING.md`). No phase loop runs on this repository. The retired self-improvement plan is kept in `docs/archive/` for provenance.

## Prerequisites

- `claude` CLI installed
- `git` and `jq` available
- Python 3 with `pyyaml` (for enrichment/skill scripts)
- Docker (only for containerized mode; Engine 26+ for a pinned project's per-project configuration)

## Operating notes

- Commits in autonomous mode are created by the loop, not by Claude
- Keep `.claude/settings.local.json` out of version control (gitignored by default)
- The loop assumes a clean git repository or that you accept automated commits
- `CONTINUE_PROMPT.txt` (in the engine) drives the autonomous loop — it instructs Claude to find and execute the next unapproved phase
- Downstream `.claude/CLAUDE.md` files are generated from `templates/CLAUDE.template.md`, not copied from the scaffold
- `.phasekit-version` is project state: commit it, and change it only with `phasekit upgrade`

## Legacy (vendored projects, supported until migrated)

Projects enriched before v0.19.0 carry the engine in their own tree and keep working exactly as before. Everything in this section applies to them only; [Migrating from vendored](#migrating-from-vendored) converts one.

### Legacy: creating a vendored project

`adopt` and `bootstrap` still produce a vendored project (use `phasekit init` for a new one):

```bash
phasekit bootstrap              # legacy vendored enrich, "default" profile
phasekit bootstrap game-project # or a specific profile
phasekit adopt                  # legacy vendored adopt of an existing repo (no overwrites)
```

These copy agents, hooks, scripts, and doc templates into the project **without overwriting existing files**, and record them in `.scaffold/manifest.json`.

> **From a clone (contributors / no install):** the path-based scripts still work for the legacy vendored enrich — `bash /path/to/phasekit/scripts/bootstrap-new-project.sh [profile]` and `adopt-existing-repo.sh [profile]` do the same thing from a checkout.

### Legacy: vendored lifecycle commands

In a vendored project `check` and `upgrade` act on the recorded manifest:

```bash
phasekit check                  # vendored: detect file drift vs the recorded manifest
phasekit check-version          # vendored: is a newer scaffold release available?
phasekit upgrade --dry-run      # vendored: plan an upgrade
phasekit upgrade --yes          # vendored: apply scaffold updates (3-way, gated, one commit)
phasekit upgrade --keep-local docs/X.md   # vendored: preserve specific project edits
```

Anything the vendored engine supports is still available in raw-flag form (forwarded verbatim), which also lets you target a project by path from anywhere:

```bash
phasekit --reconcile .                       # retrofit a pre-M9 project
phasekit --uninstall --include-once --yes .  # remove all scaffold files
phasekit --check-version ~/projects/myapp    # explicit target instead of cwd
```

Without the global install, the same commands work through the wrapper in a checkout: `bash /path/to/phasekit/scripts/phasekit.sh <verb-or-flags>` (legacy, vendored projects).

Best practice for a vendored project: always run `--upgrade --dry-run` first to inspect the plan, then re-invoke with `--keep-local PATH` for any scaffold-class file you've customized (better: move the customization into a project-owned file — `docs/project/QUALITY_GATES.md` is the companion for project gates — which also clears the way for `phasekit migrate`). See `docs/INSTALL_LIFECYCLE.md` for the vendored lifecycle contract and the ownership-class table.

### Legacy: running a vendored project's own scripts

`phasekit loop`, `phasekit run` and `phasekit container <cmd>` work in a vendored project too: they run the project's own copies. The direct script forms remain equivalent there:

```bash
bash scripts/container-setup.sh build    # vendored: same as phasekit container build
bash scripts/container-setup.sh setup    # vendored: same as phasekit container setup
bash scripts/container-setup.sh run      # vendored: same as phasekit run
bash scripts/run-until-done.sh           # vendored: same as phasekit loop
```

A vendored project's container mounts its own tree at `/workspace` (engine included), rebuilds the image from its own `.devcontainer/` on `run`, and mounts the whole Claude config volume with a per-project session directory — exactly as in v0.18.8. Its hooks are wired in its own `.claude/settings.json` to its own `.claude/hooks/`; if the plugin is also installed, the plugin steps aside for every hook the project wires itself, so no hook fires twice.
