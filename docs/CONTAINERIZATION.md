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

## Prerequisites

- Docker installed and running
- `ANTHROPIC_API_KEY` environment variable set
- The scaffold repository cloned locally

## Quick start

```bash
# Build the container image
bash scripts/container-setup.sh build

# Run the autonomous phase loop (with firewall)
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" bash scripts/container-setup.sh run

# Or open an interactive shell inside the container (with firewall)
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" bash scripts/container-setup.sh shell
```

All three modes (build, run, shell) initialize the firewall before executing the main command. There is no way to accidentally bypass the firewall through `container-setup.sh`.

## Container contents

The Dockerfile (`.devcontainer/Dockerfile`) is based on [Anthropic's reference devcontainer](https://github.com/anthropics/claude-code/tree/main/.devcontainer) with scaffold-specific additions:

- Node.js 20 (for Claude Code CLI)
- Python 3 with pyyaml (for scaffold scripts)
- Git, jq, bash, curl, and other development tools
- iptables, ipset, iproute2, dnsutils, aggregate (for firewall)
- Claude Code CLI installed globally
- Firewall initialization script (`init-firewall.sh`)
- Entrypoint wrapper (`entrypoint.sh`) that runs firewall before the main command
- Non-root `node` user (UID 1000) for execution

The working directory (`/workspace`) is bind-mounted from the host, so all changes are visible on both sides.

## How it works

1. `container-setup.sh build` builds the Docker image from `.devcontainer/`
2. `container-setup.sh run` starts the container with firewall capabilities
3. `entrypoint.sh` runs `init-firewall.sh` (default-deny + whitelisted domains)
4. After firewall init, the entrypoint executes `scripts/run-until-done.sh`
5. `run-until-done.sh` calls `run-phase.sh` in a loop, each invocation using `--permission-mode bypassPermissions`
6. Each phase writes `artifacts/phase-approval.json`, which the wrapper commits before the next iteration
7. The loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` is reached

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `MAX_ITERATIONS` | `50` | Phase loop iteration limit |
| `CLAUDE_MODE` | `new` | Set to `continue` to resume a session |
| `IMAGE_NAME` | `scaffold-runner` | Docker image name |

## Container capabilities

The container runs with a minimal capability set:
- `--cap-drop=ALL` — drops all default Linux capabilities
- `--cap-add=NET_ADMIN` — required for iptables firewall configuration
- `--cap-add=NET_RAW` — required for raw socket operations used by the firewall

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

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"**: Export the variable before running
- **"Firewall initialization failed"**: Ensure Docker supports `--cap-add=NET_ADMIN` (rootless Docker may not)
- **Permission errors on /workspace**: Ensure the host directory is readable by UID 1000 (the `node` user)
- **Claude CLI not found**: Rebuild the image to pick up the latest CLI version
- **Phase loop exits immediately**: Check that `KICKOFF_PROMPT.txt` and `CONTINUE_PROMPT.txt` exist in the repo root
- **Firewall blocking needed domains**: Check `init-firewall.sh` whitelist; add domains if your workflow requires additional network access
- **Stale DNS in long-running containers**: The firewall resolves domain IPs at startup; CDN-backed services may rotate IPs over time. Restart the container or re-run `sudo /usr/local/bin/init-firewall.sh` to refresh
