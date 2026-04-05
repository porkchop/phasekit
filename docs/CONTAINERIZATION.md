# Containerized Unattended Execution

This document describes how to run the scaffold's phase-gated workflow autonomously inside an isolated container.

**This is opt-in.** Interactive collaboration mode (see `docs/EXECUTION_MODES.md`) is the default. Use containerized execution only when you want fully autonomous phase loops.

## Safety warning

The container runs Claude Code with `--permission-mode bypassPermissions`. This means:
- Claude can execute any command without prompting for approval
- **Hooks do not apply** — `deny-dangerous-commands.sh` and other PreToolUse hooks are bypassed entirely
- **Network is unrestricted** — Claude can reach any endpoint (required for the Anthropic API, but also means it can download or upload data freely)
- **Full repo access** — the bind mount gives Claude read/write access to the entire repository, including `.git` history
- **`git add -A`** — the wrapper's commit function stages all changes; the only defense against committing unexpected files is `.gitignore`

**Only use this with trusted repositories and code you are willing to have fully modified.** Do not mount sensitive directories alongside the repo.

## Prerequisites

- Docker installed and running
- `ANTHROPIC_API_KEY` environment variable set
- The scaffold repository cloned locally

## Quick start

```bash
# Build the container image
bash scripts/container-setup.sh build

# Run the autonomous phase loop
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" bash scripts/container-setup.sh run

# Or open an interactive shell inside the container
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" bash scripts/container-setup.sh shell
```

## Container contents

The Dockerfile (`container/Dockerfile`) provides:
- Node.js 20 (for Claude Code CLI)
- Python 3 with pyyaml (for scaffold scripts)
- Git, jq, bash, curl
- Claude Code CLI installed globally
- Non-root `scaffold` user for execution

The working directory (`/workspace`) is bind-mounted from the host, so all changes are visible on both sides.

## How it works

1. `container-setup.sh build` builds the Docker image
2. `container-setup.sh run` mounts the repo at `/workspace` and runs `scripts/run-until-done.sh`
3. `run-until-done.sh` calls `run-phase.sh` in a loop, each invocation using `--permission-mode bypassPermissions`
4. Each phase writes `artifacts/phase-approval.json`, which the wrapper commits before the next iteration
5. The loop stops when `artifacts/project-complete.json` appears, a blocker is written, or `MAX_ITERATIONS` is reached

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `MAX_ITERATIONS` | `50` | Phase loop iteration limit |
| `CLAUDE_MODE` | `new` | Set to `continue` to resume a session |
| `IMAGE_NAME` | `scaffold-runner` | Docker image name |

## Permissive mode confinement

Permissive execution is activated only inside the container:
- The project's `.claude/settings.json` remains conservative (checked in)
- `bypassPermissions` is passed via CLI flag inside the wrapper scripts only
- No `.claude/settings.local.json` is generated or committed
- The container's filesystem is isolated from the host except for the bind mount

### What is NOT confined
- **Network access**: the container has full network access (no `--network none`)
- **Hooks**: `PreToolUse` hooks (like `deny-dangerous-commands.sh`) are bypassed by `bypassPermissions` mode
- **Git operations**: Claude can run any git command including push, reset, and rebase
- **File staging**: the commit wrapper uses `git add -A`, so anything Claude creates gets committed unless `.gitignore` excludes it

For stronger isolation, add `--network none` to the `docker run` command (you will need to pre-authenticate or use a proxy for API access).

## VS Code is not required

This approach uses Docker directly. You do not need VS Code, devcontainers, or any IDE to use containerized execution. The `container-setup.sh` script handles build and run from any terminal.

If you prefer VS Code devcontainers, you can adapt `container/Dockerfile` into a `.devcontainer/devcontainer.json` configuration, but that is not required or provided by default.

## Extending the container

To add project-specific dependencies:
1. Create a `container/Dockerfile.project` that extends the base image
2. Add your dependencies
3. Override `IMAGE_NAME` when running the setup script

Example:
```dockerfile
FROM scaffold-runner
RUN apt-get update && apt-get install -y postgresql-client
```

```bash
docker build -t my-project-runner -f container/Dockerfile.project container/
IMAGE_NAME=my-project-runner bash scripts/container-setup.sh run
```

## Troubleshooting

- **"ANTHROPIC_API_KEY is not set"**: Export the variable before running
- **Permission errors on /workspace**: Ensure the host directory is readable by UID 1000 (the `scaffold` user)
- **Claude CLI not found**: Rebuild the image to pick up the latest CLI version
- **Phase loop exits immediately**: Check that `KICKOFF_PROMPT.txt` and `CONTINUE_PROMPT.txt` exist in the repo root
