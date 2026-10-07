# Containerized Unattended Execution

This document describes how to run the scaffold's phase-gated workflow autonomously inside an isolated container with network-level firewall protection.

**This is opt-in.** Interactive collaboration mode (see `docs/EXECUTION_MODES.md`) is the default. Use containerized execution only when you want fully autonomous phase loops.

The commands below are the `phasekit` CLI, run from inside the project: `phasekit container build|setup|run|shell`, with `phasekit run` short for `phasekit container run`. They run the engine's `scripts/container-setup.sh` against the project. In a **pinned** project (v0.19.0 and later: `.phasekit-version`, no engine files in the tree) that is the pinned engine's copy from the engine store, with `PHASEKIT_PROJECT_DIR` naming the project; see [Pinned projects: the mounted engine](#pinned-projects-the-mounted-engine). In a **vendored** project (the engine in its own tree) it is the project's own copy, and everything behaves exactly as in v0.18.8; see [Legacy: vendored projects](#legacy-vendored-projects).

## Security model

The container runs Claude Code with `--permission-mode bypassPermissions`. This means:
- Claude can execute any command without prompting for approval
- **Hooks still apply** — permission prompts are skipped, but the project's hooks run on every tool call (probed in scaffold-runner, claude 2.1.285, 2026-09-30): a PreToolUse hook that exits 2 refuses the call. `deny-dangerous-commands.sh` receives its payload as JSON on stdin; until v0.18.1 it read only `CLAUDE_TOOL_INPUT`, which the harness never sets, so it blocked nothing. Since v0.18.2 it is live, and under the loop it refuses the model's git writes to this repository — the loop owns every commit (docs/QUALITY_GATES.md "The loop owns every commit")
- **The engine is read-only** — in a pinned project the engine is mounted read-only at `/opt/phasekit`, so a session cannot edit its own loop, hooks or guard; the hooks come from the engine's plugin, which the loop passes to every turn and checks before every turn (the guard self-check, `docs/EXECUTION_MODES.md`)
- **Full repo access** — the bind mount gives Claude read/write access to the entire repository, including `.git` history (the guard refuses git writes by command; a program that runs git itself is caught by the loop's whole-tree check at every completion)
- **`git add -A`** — the wrapper's commit function stages all changes; `.gitignore` and the loop's own exclusions (artifacts/logs/, artifacts/scratch/) are the only defense against committing unexpected files — scratch goes in `artifacts/scratch/` or `/tmp`

### Network firewall (best-effort)

The container uses Anthropic's reference firewall (`init-firewall.sh`) to restrict outbound network access:
- **Default-deny policy** — all outbound traffic is blocked by default
- **Whitelisted domains** — only necessary services are allowed: npm registry, GitHub, Anthropic API, Sentry, Statsig, VS Code marketplace
- **DNS and SSH** — outbound DNS (UDP 53) and SSH (TCP 22) are allowed
- **Host network** — the Docker host network is allowed for API access
- **Verification** — the script verifies that `example.com` is blocked and `api.github.com` is allowed

**Important: The firewall is best-effort network hygiene, not a hard security boundary.** Claude with `bypassPermissions` runs as a non-root user with `NET_ADMIN` capability, which means it could theoretically modify or disable the firewall rules. The firewall prevents accidental or unintended network access, not a determined adversarial agent.

**Only use this with trusted repositories and code you are willing to have fully modified.** Do not mount sensitive directories alongside the repo.

## Authentication

The container supports two authentication methods. Choose whichever fits your billing preference.

### Option A: Claude Code subscription (recommended for heavy workloads)

Subscription plans (Pro/Max) include usage at a flat monthly rate, making them more cost-effective for autonomous loops that consume many tokens.

This uses a 2-phase workflow — first log in interactively, then run headless:

```bash
# Phase 1: One-time setup — log in with your subscription
phasekit container setup
# Inside the container, run:
claude login
# A URL is displayed — open it in your browser to complete OAuth.
# Once logged in, exit the container.

# Phase 2: Run the autonomous loop using stored credentials
phasekit run
```

Credentials are stored in a named Docker volume (`scaffold-claude-config`) and persist between container runs. You only need to repeat the setup phase if the credentials expire or the volume is deleted. The login is shared: `setup` mounts the whole volume (in a pinned project too), and every project's sessions use the one `.credentials.json` in it.

**Important:** Do NOT set `ANTHROPIC_API_KEY` when using subscription auth — if set, it takes precedence over stored credentials.

### Option B: API key (pay-per-token)

For pay-per-token billing via the Anthropic API:

1. Go to the [Anthropic Console](https://console.anthropic.com/)
2. Create an API key under Settings → API Keys
3. Set it in your host shell:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
phasekit run
```

The key is passed into the container at runtime via `docker run -e` and never stored in any repo file.

## Prerequisites

- Docker installed and running (Engine 26+ / API 1.45+ for the per-project session directory and, in a pinned project, the per-project config root, which refuses older Docker; see [Per-project session directory](#per-project-session-directory))
- Subscription credentials from `phasekit container setup`, or `ANTHROPIC_API_KEY` set (see above)
- phasekit installed (`install.sh`): the `phasekit` CLI and the engine store

## Quick start

```bash
# Build the container image
phasekit container build

# One-time setup: log in with your Claude subscription
phasekit container setup
# Inside the container, run: claude login

# Run the autonomous phase loop (using stored subscription credentials)
phasekit run

# Or with an API key instead:
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" phasekit run

# Open an interactive shell inside the container
phasekit container shell
```

The `run`, `setup` and `shell` commands initialize the firewall inside the container before executing the main command. There is no way to accidentally bypass the firewall through `container-setup.sh`.

## Container contents

The Dockerfile (`.devcontainer/Dockerfile`) is based on [Anthropic's reference devcontainer](https://github.com/anthropics/claude-code/tree/main/.devcontainer) with scaffold-specific additions:

- Node.js 20 (for Claude Code CLI)
- Python 3 with pyyaml (for scaffold scripts)
- Git, jq, bash, curl, and other development tools
- iptables, ipset, iproute2, dnsutils, aggregate (for firewall)
- Claude Code CLI installed globally
- Playwright MCP server (`@playwright/mcp`) and Chromium for browser automation
- Firewall initialization script (`init-firewall.sh`)
- Entrypoint wrapper (`entrypoint.sh`) that runs firewall and injects MCP config before the main command
- Non-root `node` user (UID 1000) for execution

The working directory (`/workspace`) is bind-mounted from the host, so all changes are visible on both sides. In a pinned project the engine is also mounted, read-only, at `/opt/phasekit`.

## Pinned projects: the mounted engine

*v0.19.0.* For a pinned project, `container-setup.sh` (the engine's copy, run with `PHASEKIT_PROJECT_DIR` set to the project — `phasekit run` does this) starts the container with:

- the project at `/workspace`, read-write, as before;
- the engine directory from the store at `/opt/phasekit`, **read-only**;
- `-e PHASEKIT_PROJECT_DIR=/workspace`, so the engine's loop works on the project;
- `/opt/phasekit/bin` first on `PATH`, so `phasekit` inside the container is the mounted engine (the loop also puts its own `phasekit` shim first, which runs the loop's own engine);
- the loop started as `/opt/phasekit/scripts/run-until-done.sh`;
- the session's own Claude config root (next section), not the whole config volume.

**The image is never built or retagged per dispatch.** One shared image (`IMAGE_NAME`, default `scaffold-runner`) serves every project; a project on an older engine must not retag it for all the others. `run` and `shell` use the existing image; only when it does not exist at all are they allowed to build it once, from the engine's `.devcontainer/` (the first run of a fresh install). `PHASEKIT_IMAGE_PREBUILT=1` skips even the existence check. Rebuild deliberately with `phasekit container build` (it builds from the engine's `.devcontainer/`), for example after a release that changes the image.

## Playwright MCP server (browser automation)

The container includes Chromium and the `@playwright/mcp` server, giving the `qa-playwright` subagent direct browser tools (`browser_navigate`, `browser_snapshot`, `browser_take_screenshot`, `browser_click`, etc.) for verifying user-visible functionality during autonomous execution.

### How it works

1. Chromium and `@playwright/mcp` are pre-installed in the Docker image during build
2. The entrypoint injects MCP server configuration into `.claude/settings.local.json` (gitignored) before Claude starts
3. Claude receives the Playwright MCP tools as available tools during the session
4. The `qa-playwright` agent uses these tools for browser verification

The MCP server runs in headless mode with `--no-sandbox` (standard for Docker containers). It communicates with Claude via stdio — no network ports are opened.

### Disabling

To skip Playwright MCP injection (e.g., for non-browser projects):

```bash
SKIP_PLAYWRIGHT_MCP=1 phasekit run
```

### Interactive mode setup

The container's MCP injection only applies when running the container. For interactive use, register the server with a one-liner:

```bash
claude mcp add playwright -- npx @playwright/mcp@latest
```

This registers the server in your user-level Claude configuration. Add `--headless` if you don't have a display.

## How it works

1. `phasekit container build` builds the Docker image from the engine's `.devcontainer/`
2. `phasekit run` starts the container with firewall capabilities (pinned: the engine mounted read-only at `/opt/phasekit`, the image reused)
3. `entrypoint.sh` runs `init-firewall.sh` (default-deny + whitelisted domains)
4. `entrypoint.sh` injects Playwright MCP server config into `.claude/settings.local.json`
5. After firewall and MCP init, the entrypoint executes the engine's `run-until-done.sh` (pinned: `/opt/phasekit/scripts/run-until-done.sh`)
6. `run-until-done.sh` calls `run-phase.sh` in a loop, each invocation using `--permission-mode bypassPermissions` (pinned: also `--plugin-dir /opt/phasekit/plugin`, after the guard self-check)
7. Each phase writes `artifacts/phase-approval.json`, which the wrapper commits before the next iteration
8. The loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` is reached

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (optional) | API key for pay-per-token auth; omit to use stored subscription credentials |
| `MAX_ITERATIONS` | `50` | Phase loop iteration limit |
| `CLAUDE_MODE` | `new` | Set to `continue` to resume this project's last recorded conversation by id (forwarded into the container; see EXECUTION_MODES.md, "Session continuity") |
| `PHASEKIT_ITER_RETRY` | `1` | Retry budget per iteration on a transient `claude` CLI failure; see `docs/EXECUTION_MODES.md` |
| `PHASEKIT_TRACE` | (unset) | Set to `1` to enable `set -x` xtrace in the wrapper scripts (host and inside the container); see `docs/EXECUTION_MODES.md` |
| `IMAGE_NAME` | `scaffold-runner` | Docker image name |
| `CLAUDE_VOLUME` | `scaffold-claude-config` | Named Docker volume for `~/.claude` credential persistence. A host directory path (starting with `/`) is also accepted; a pinned session then gets bind mounts of its parts instead of volume-subpath mounts |
| `GIT_USER_NAME` | `Scaffold Runner` | Git author name for commits |
| `GIT_USER_EMAIL` | `scaffold-runner@localhost` | Git author email for commits |
| `SKIP_PLAYWRIGHT_MCP` | (empty) | Set to `1` to skip Playwright MCP injection |
| `PLAYWRIGHT_MCP_VERSION` | `0.0.70` | Override `@playwright/mcp` version at build time |
| `PHASEKIT_ROOTLESS_DOCKER` | (unset) | Set to `1` to run the container as UID 0 for rootless Docker bind mounts; see [Rootless Docker](#rootless-docker) |
| `PHASEKIT_CONTAINER_USER` | (unset) | Lower-level override for `docker run --user` (`root` or `uid:gid`); takes precedence over `PHASEKIT_ROOTLESS_DOCKER` |
| `PHASEKIT_CONTRACTS_MOUNT` | (unset) | Host path to a provider's contracts tree; bind-mounted read-only at `/contracts`; see [Cross-project contracts mount](#cross-project-contracts-mount) |
| `PHASEKIT_PROJECT_DIR` | (unset) | The project directory for an engine outside the tree (pinned). Set by `phasekit run` / `phasekit container`; unset means the vendored layout (the script's own checkout is the project) |
| `PHASEKIT_IMAGE_PREBUILT` | (unset) | Pinned projects: `1` skips the check that the image exists (and the one-time build when it does not) |

## Per-project session directory

*v0.18.8 (auto-memory alone since v0.18.7).* Every session mounts its project at `/workspace`
and shares the `CLAUDE_VOLUME`, and Claude Code keys its per-directory state by the working
directory — `projects/-workspace/` in the config directory: the session transcripts
(`<id>.jsonl`), each session's own files, and the auto-memory (`memory/`). So before v0.18.7
every project on a host shared ONE memory, and before v0.18.8 one transcript directory, where
`claude -c` ("the most recent conversation in this directory") could resume ANOTHER project's
conversation. Measured 2026-10-06: 15 shared transcripts held two projects' turns, interleaved by
concurrent sessions appending to one file. The loop now resumes by explicit session id (see
[EXECUTION_MODES.md](EXECUTION_MODES.md), "Session continuity"); this is the storage half.

`container-setup.sh` gives each session its own directory of the same volume,
`project-sessions/<key>`, mounted over the WHOLE `/home/node/.claude/projects/-workspace`
(one mount — Docker's `volume-subpath`, Docker Engine 26+ / API 1.45+; it replaces v0.18.7's
memory-only mount). `<key>` is the project directory's name (characters outside
`A-Za-z0-9._-` become `_`, leading dots are dropped, an empty name is `_default`), so two
checkouts with the same directory name share one directory. Before the session starts, a
throwaway container (no network, the session's own user, the volume mounted where the session
mounts it) creates the directory; the first time, its `memory/` is carried over: a copy of
v0.18.7's `project-memory/<key>` when the project has one, else of the shared memory. Nothing
is moved or deleted: the shared directory (its transcripts are legacy — never resumed again)
and `project-memory/<key>` stay where they are, and a project still on an older phasekit keeps
using them. Nothing else changes: `/workspace`, the volume and `CLAUDE_CONFIG_DIR` are as
before, and a session on the host never runs this script.

Own directory or none, never another project's: if the directory cannot be prepared, or the
docker client or daemon is older than API 1.45, the session gets an empty throwaway directory
(tmpfs) and a warning. Its transcripts then last the container's life: the loop's later turns
still resume within it, and the next run (or a `CLAUDE_MODE=continue` start) begins a new
conversation that re-anchors from the tree.

Known limit (vendored projects): the whole config volume is still mounted at
`/home/node/.claude` (it holds the login), so a session that goes looking can read
`project-sessions/<other key>/` and `project-memory/` there. The CLI never resumes or remembers
from those paths — only from `projects/-workspace`, which is this project's own — but a model
running a shell command is not the CLI. A pinned project's sessions close this with a config
root per project (next section).

## Per-project config root (pinned projects)

*v0.19.0.* A pinned project's container session sees, of the config volume (`CLAUDE_VOLUME`),
exactly three things — never the whole volume:

| In the volume | Mounted at | What it is |
|---|---|---|
| `project-config/<key>` | `/home/node/.claude` | the project's own config root: its settings, `.claude.json`, history, shell snapshots and caches |
| `project-sessions/<key>` | `/home/node/.claude/projects/-workspace` | its transcripts and memory, exactly as in v0.18.8 (carried over; nothing moved or deleted) |
| `.credentials.json` | `/home/node/.claude/.credentials.json` | the **shared** login, one read-write file mount |

`<key>` is the project directory's name (the same key as the session directory). The first time,
the root is created (atomically, in the same throwaway preparation container) and seeded with a
copy of the shared `settings.json` and a minimal `.claude.json` that marks `/workspace` trusted and
onboarding done — no other state.

**The login.** Refresh tokens rotate (measured 2026-10-07: a refresh replaced the shared refresh
token), so the credentials are never copied and never read-only: a copy or a read-only file would
strand a rotated token in one session and log everyone else out. They are one read-write FILE
mount, and a file mount pins the file's inode — while every whole-volume writer (a vendored
project's session, `setup`, a supervisor's own containers) replaces the file by renaming a staging
file over it. A pinned session therefore **never refreshes the login itself**: it starts only when
the shared access token outlives it (until `PHASEKIT_SESSION_DEADLINE`, else two hours, plus 15
minutes). When the token expires sooner, the login is refreshed **first**: a throwaway, networked
container gives a copy of the login (marked expired) to a tiny `claude -p` (haiku) in a throwaway
config directory, then writes the refreshed login back **in place** (same inode, so every running
pinned session sees it; Claude Code itself also writes in place over a mount point, its rename
failing with `EBUSY`). No credential is ever printed; only the new expiry is read back. Refreshes are serialised by a lock file in the volume (`.phasekit-login.lock`) and the expiry is re-checked under it, so concurrent pinned preps refresh once. An unbounded pinned session (`phasekit container shell`, or a run without `PHASEKIT_SESSION_DEADLINE`) is guaranteed two hours; beyond that it may refresh the login itself, which is safe only while no whole-volume writer has replaced the file since the session started.
`PHASEKIT_LOGIN_REFRESH=0` refuses the session instead of refreshing. With no shared login in the
volume, the session needs `ANTHROPIC_API_KEY` (or run `phasekit container setup`).

This needs Docker's `volume-subpath` (Engine 26+ / API 1.45+). On older Docker, or when the root
cannot be prepared, a pinned session is **refused** — it never falls back to the whole volume.
Alternatively set `CLAUDE_VOLUME` to a host directory (an absolute path): the same three parts are
then bind-mounted, which works on any Docker version. A `CLAUDE_VOLUME` containing a comma is
refused (a `--mount` value cannot carry it). `phasekit container setup` still mounts the whole
volume, because the login it creates is the shared one.

## Cross-project contracts mount

*v0.7.0. Optional — unset is the normal case and changes nothing.*

A provider (the Foundry orchestrator, or a human working by hand) sets
`PHASEKIT_CONTRACTS_MOUNT` to a host directory containing `index.json` plus one
directory per dependency slug. `container-setup.sh` bind-mounts it **read-only**
at the fixed container path `/contracts` and sets `PHASEKIT_CONTRACTS_DIR` so
the in-container tooling can find it.

```bash
PHASEKIT_CONTRACTS_MOUNT=/srv/foundry/contracts \
  phasekit run
```

Two rules govern the mount, and they pull in opposite directions on purpose:

- **Unset is a no-op.** phasekit works with no orchestrator at all, so a missing
  mount can never be an error here. The refusal is triggered by the consumer
  repo's own `contracts.yaml`, never by the mount's absence.
- **Set-but-unusable is a hard error**, before the container starts. If the path
  is not a directory, or has no readable `index.json`, `container-setup.sh`
  exits non-zero. A provider passing a path the consumer silently drops is
  exactly the `META_REPO_PATH` failure — an export with no consumer, unnoticed
  for months — and this feature exists to prevent that class.

A provider with nothing to offer still ships an `index.json` with zero entries.
An empty directory is indistinguishable from a broken bind mount; a manifest is
the provider asserting it checked.

Full mechanism: `docs/CONTRACTS.md`.

## Container capabilities

The container runs with a minimal capability set:
- `--cap-drop=ALL` — drops all default Linux capabilities
- `--cap-add=NET_ADMIN` — required for iptables firewall configuration
- `--cap-add=NET_RAW` — required for raw socket operations used by the firewall
- `--cap-add=SETUID` — required for `sudo` to run the firewall init as root
- `--cap-add=SETGID` — required for `sudo` to switch group identity

## Rootless Docker

By default the container runs as the non-root `node` user (UID 1000). Under
standard ("rootful") Docker this is correct: container UID 1000 is the same as
host UID 1000, so files written into the bind-mounted `/workspace` are owned by
your host user.

**Under [rootless Docker](https://docs.docker.com/engine/security/rootless/) this
breaks.** The daemon runs inside a user namespace, so container UID 1000 maps to
a high *subordinate* UID on the host (from `/etc/subuid`) that your login user
cannot chown or modify. Files the container writes to `/workspace` then appear
owned by that unmapped UID, producing errors such as:

```
/usr/local/bin/entrypoint.sh: line 40: /workspace/.claude/settings.local.json: Permission denied
mkdir: cannot create directory '/workspace/artifacts/logs': Permission denied
```

`chmod -R a+rwX .` does not reliably fix this, because some files end up owned by
mapped UIDs your host user cannot touch.

### The fix: run as container root

Set `PHASEKIT_ROOTLESS_DOCKER=1`:

```bash
PHASEKIT_ROOTLESS_DOCKER=1 phasekit run
```

This runs the container process as UID 0. Because Docker is rootless, **container
root maps back to your unprivileged host user**, not real host root — so
`/workspace` writes land as your user and the permission errors disappear. The
rootless security boundary is preserved: the "root" inside the container has no
elevated privileges on the host.

`HOME` is kept at `/home/node` in this mode so the image's prebuilt, node-owned
assets stay reachable as UID 0: the Claude credential volume, the Playwright
browser cache, the default git identity, and the ssh `known_hosts` mount.

For finer control (e.g. matching a specific host UID/GID), use the lower-level
override instead — it accepts `root` or a raw `uid:gid`:

```bash
PHASEKIT_CONTAINER_USER=root        phasekit run
PHASEKIT_CONTAINER_USER=1001:1001   phasekit run
```

`PHASEKIT_CONTAINER_USER` takes precedence over `PHASEKIT_ROOTLESS_DOCKER`.

> Note: rootless Docker may also restrict the `NET_ADMIN`/`NET_RAW` capabilities
> the firewall needs. If `init-firewall.sh` fails to initialize in your rootless
> setup, that is a separate capability concern from the bind-mount ownership fix
> above; see the firewall note in [Troubleshooting](#troubleshooting).

## VS Code devcontainer support (optional)

The `.devcontainer/devcontainer.json` file (in the phasekit repository, and in a vendored project) provides optional VS Code integration. If you open such a repo in VS Code with the Dev Containers extension, it will offer to reopen in the container. This is entirely optional — the CLI-only path via `container-setup.sh` does not require VS Code.

When using VS Code, the firewall runs via `postStartCommand` instead of the entrypoint wrapper.

## Extending the container

To add project-specific dependencies, create a Dockerfile that extends the base image:

```dockerfile
FROM scaffold-runner
USER root
RUN apt-get update && apt-get install -y postgresql-client
USER node
```

```bash
docker build -t my-project-runner -f Dockerfile.project .
IMAGE_NAME=my-project-runner phasekit run
```

## Firewall maintenance

The `init-firewall.sh` script is vendored from Anthropic's reference devcontainer. To check for upstream updates:

```bash
# Compare vendored version against upstream
curl -s https://raw.githubusercontent.com/anthropics/claude-code/main/.devcontainer/init-firewall.sh | diff - .devcontainer/init-firewall.sh
```

## Migration from M5

If you previously used the `container/Dockerfile` from M5:
- The build context changed from `container/` to `.devcontainer/`
- The container user changed from `scaffold` to `node` (same UID 1000, no permission issues)
- Network is now firewalled by default (was unrestricted)
- `--cap-add=NET_ADMIN --cap-add=NET_RAW` are now required (added automatically by `container-setup.sh`)
- Custom `container/Dockerfile.project` files should be updated to extend `scaffold-runner` directly

## Verifying the container

After building, run the verification script to check that all tools are correctly installed:

```bash
# From inside the container (after phasekit container shell; pinned: the engine's copy)
bash /opt/phasekit/scripts/verify-container.sh

# Or directly from the host, from the phasekit checkout or a vendored project
# (--entrypoint bypasses firewall which needs extra caps)
docker run --rm --entrypoint bash -v "$(pwd)":/workspace -w /workspace scaffold-runner scripts/verify-container.sh
```

The script checks: core tools (claude, git, jq, python3+pyyaml), Playwright MCP server binary and handshake, Chromium headless launch, and MCP settings injection.

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"**: Export the variable before running
- **"Firewall initialization failed"**: Ensure Docker supports `--cap-add=NET_ADMIN` (rootless Docker may not)
- **Permission errors on /workspace**: Under standard Docker, ensure the host directory is readable by UID 1000 (the `node` user). Under **rootless Docker** these errors are expected with the default user — run with `PHASEKIT_ROOTLESS_DOCKER=1` (see [Rootless Docker](#rootless-docker))
- **`fatal: detected dubious ownership in repository at '/workspace'`** (git exits 128): the bind-mounted workspace is owned by a different UID than the container user. The image marks `/workspace` as a git `safe.directory`, so **rebuild** to pick up the fix (`phasekit container build`). If extending the image with your own Dockerfile, re-apply `git config --global --add safe.directory /workspace`
- **Claude CLI not found**: Rebuild the image to pick up the latest CLI version
- **Phase loop exits immediately**: In a vendored project, check that `CONTINUE_PROMPT.txt` exists in the repo root; in a pinned one the prompt is the engine's — check `phasekit check` (the pin is installed) and that the engine mount at `/opt/phasekit` is present
- **`phasekit: REFUSING the turn — the guard self-check failed`** (pinned): the engine's plugin did not load in the session, so the loop will not start a model turn without its command guard (`run-phase.sh` exits 7). Check that `/opt/phasekit/plugin/` exists in the container and that `claude` and `jq` work there
- **`container: config: … refusing`** (pinned): the per-project config root could not be mounted alone — upgrade Docker (Engine 26+), or set `CLAUDE_VOLUME` to a host directory; see [Per-project config root](#per-project-config-root-pinned-projects)
- **Firewall blocking needed domains**: Check `init-firewall.sh` whitelist; add domains if your workflow requires additional network access
- **Stale DNS in long-running containers**: The firewall resolves domain IPs at startup; CDN-backed services may rotate IPs over time. Restart the container or re-run `sudo /usr/local/bin/init-firewall.sh` to refresh

## Legacy: vendored projects

A vendored project (the engine in its own tree; every project created before v0.19.0) runs its
own copy of the script, so `phasekit container <cmd>` and `phasekit run` behave exactly as the
direct forms below, as in v0.18.8:

```bash
bash scripts/container-setup.sh build    # vendored: build the image from the project's own .devcontainer/
bash scripts/container-setup.sh setup    # vendored: interactive login
bash scripts/container-setup.sh run      # vendored: rebuild the image, then run scripts/run-until-done.sh
bash scripts/container-setup.sh shell    # vendored: a shell in the container
```

Differences from a pinned project: the project's own tree is the engine (nothing at
`/opt/phasekit`, no `PHASEKIT_PROJECT_DIR`); `run` and `shell` rebuild the image from the project's
`.devcontainer/` every time; the whole config volume is mounted at `/home/node/.claude` with the
per-project session directory over `projects/-workspace` (and the tmpfs fallback on older
Docker, rather than a refusal); and the hooks are the project's own, wired in its
`.claude/settings.json` (an installed plugin steps aside for them).
