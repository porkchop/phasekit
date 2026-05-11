# Containerized Unattended Execution

This document describes how to run the scaffold's phase-gated workflow autonomously inside an isolated container with network-level firewall protection.

**This is opt-in.** Interactive collaboration mode (see `docs/EXECUTION_MODES.md`) is the default. Use containerized execution only when you want fully autonomous phase loops.

## Security model

The container runs Claude Code with `--permission-mode bypassPermissions`. This means:
- Claude can execute any command without prompting for approval
- **Hooks do not apply** — `deny-dangerous-commands.sh` and other PreToolUse hooks are bypassed entirely
- **Full repo access** — the bind mount gives Claude read/write access to the entire repository, including `.git` history
- **`git add -A`** — the wrapper's commit function stages all changes; the only defense against committing unexpected files is `.gitignore`

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
bash scripts/container-setup.sh setup
# Inside the container, run:
claude login
# A URL is displayed — open it in your browser to complete OAuth.
# Once logged in, exit the container.

# Phase 2: Run the autonomous loop using stored credentials
bash scripts/container-setup.sh run
```

Credentials are stored in a named Docker volume (`scaffold-claude-config`) and persist between container runs. You only need to repeat the setup phase if the credentials expire or the volume is deleted.

**Important:** Do NOT set `ANTHROPIC_API_KEY` when using subscription auth — if set, it takes precedence over stored credentials.

### Option B: API key (pay-per-token)

For pay-per-token billing via the Anthropic API:

1. Go to the [Anthropic Console](https://console.anthropic.com/)
2. Create an API key under Settings → API Keys
3. Set it in your host shell:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
bash scripts/container-setup.sh run
```

The key is passed into the container at runtime via `docker run -e` and never stored in any repo file.

## Prerequisites

- Docker installed and running
- `ANTHROPIC_API_KEY` environment variable set (see above)
- The scaffold repository cloned locally

## Quick start

```bash
# Build the container image
bash scripts/container-setup.sh build

# One-time setup: log in with your Claude subscription
bash scripts/container-setup.sh setup
# Inside the container, run: claude login

# Run the autonomous phase loop (using stored subscription credentials)
bash scripts/container-setup.sh run

# Or with an API key instead:
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" bash scripts/container-setup.sh run

# Open an interactive shell inside the container
bash scripts/container-setup.sh shell
```

All three modes (build, run, shell) initialize the firewall before executing the main command. There is no way to accidentally bypass the firewall through `container-setup.sh`.

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

The working directory (`/workspace`) is bind-mounted from the host, so all changes are visible on both sides.

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
SKIP_PLAYWRIGHT_MCP=1 bash scripts/container-setup.sh run
```

### Interactive mode setup

The container's MCP injection only applies when running the container. For interactive use, register the server with a one-liner:

```bash
claude mcp add playwright -- npx @playwright/mcp@latest
```

This registers the server in your user-level Claude configuration. Add `--headless` if you don't have a display.

## How it works

1. `container-setup.sh build` builds the Docker image from `.devcontainer/`
2. `container-setup.sh run` starts the container with firewall capabilities
3. `entrypoint.sh` runs `init-firewall.sh` (default-deny + whitelisted domains)
4. `entrypoint.sh` injects Playwright MCP server config into `.claude/settings.local.json`
5. After firewall and MCP init, the entrypoint executes `scripts/run-until-done.sh`
6. `run-until-done.sh` calls `run-phase.sh` in a loop, each invocation using `--permission-mode bypassPermissions`
7. Each phase writes `artifacts/phase-approval.json`, which the wrapper commits before the next iteration
8. The loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` is reached

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (optional) | API key for pay-per-token auth; omit to use stored subscription credentials |
| `MAX_ITERATIONS` | `50` | Phase loop iteration limit |
| `CLAUDE_MODE` | `new` | Set to `continue` to resume a previous session (forwarded into the container) |
| `PHASEKIT_ITER_RETRY` | `1` | Retry budget per iteration on a transient `claude` CLI failure; see `docs/EXECUTION_MODES.md` |
| `IMAGE_NAME` | `scaffold-runner` | Docker image name |
| `CLAUDE_VOLUME` | `scaffold-claude-config` | Named Docker volume for `~/.claude` credential persistence |
| `GIT_USER_NAME` | `Scaffold Runner` | Git author name for commits |
| `GIT_USER_EMAIL` | `scaffold-runner@localhost` | Git author email for commits |
| `SKIP_PLAYWRIGHT_MCP` | (empty) | Set to `1` to skip Playwright MCP injection |
| `PLAYWRIGHT_MCP_VERSION` | `0.0.70` | Override `@playwright/mcp` version at build time |

## Container capabilities

The container runs with a minimal capability set:
- `--cap-drop=ALL` — drops all default Linux capabilities
- `--cap-add=NET_ADMIN` — required for iptables firewall configuration
- `--cap-add=NET_RAW` — required for raw socket operations used by the firewall
- `--cap-add=SETUID` — required for `sudo` to run the firewall init as root
- `--cap-add=SETGID` — required for `sudo` to switch group identity

## VS Code devcontainer support (optional)

The `.devcontainer/devcontainer.json` file provides optional VS Code integration. If you open this repo in VS Code with the Dev Containers extension, it will offer to reopen in the container. This is entirely optional — the CLI-only path via `container-setup.sh` does not require VS Code.

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
IMAGE_NAME=my-project-runner bash scripts/container-setup.sh run
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
# From inside the container (e.g. after container-setup.sh shell)
bash scripts/verify-container.sh

# Or directly from the host (--entrypoint bypasses firewall which needs extra caps)
docker run --rm --entrypoint bash -v "$(pwd)":/workspace -w /workspace scaffold-runner scripts/verify-container.sh
```

The script checks: core tools (claude, git, jq, python3+pyyaml), Playwright MCP server binary and handshake, Chromium headless launch, and MCP settings injection.

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"**: Export the variable before running
- **"Firewall initialization failed"**: Ensure Docker supports `--cap-add=NET_ADMIN` (rootless Docker may not)
- **Permission errors on /workspace**: Ensure the host directory is readable by UID 1000 (the `node` user)
- **Claude CLI not found**: Rebuild the image to pick up the latest CLI version
- **Phase loop exits immediately**: Check that `CONTINUE_PROMPT.txt` exists in the repo root
- **Firewall blocking needed domains**: Check `init-firewall.sh` whitelist; add domains if your workflow requires additional network access
- **Stale DNS in long-running containers**: The firewall resolves domain IPs at startup; CDN-backed services may rotate IPs over time. Restart the container or re-run `sudo /usr/local/bin/init-firewall.sh` to refresh
