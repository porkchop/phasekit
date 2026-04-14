#!/usr/bin/env bash
set -euo pipefail

# Container setup script for scaffold unattended execution.
#
# Builds from .devcontainer/ using Anthropic's reference devcontainer
# with scaffold-specific additions (Python, pyyaml, entrypoint wrapper).
#
# The container includes a firewall (init-firewall.sh) that restricts
# outbound network access to whitelisted domains only. The firewall is
# best-effort network hygiene, not a hard security boundary.
#
# Usage:
#   bash scripts/container-setup.sh [build|setup|run|shell]
#
# Commands:
#   build   Build the container image (default)
#   setup   Build and open an interactive shell for claude login (no API key required)
#   run     Build and run the phase loop
#   shell   Build and open an interactive shell
#
# Authentication (pick one):
#   Option A — API key (pay-per-token):
#     ANTHROPIC_API_KEY   Set this env var before running 'run' or 'shell'
#
#   Option B — Claude Code subscription (flat-rate):
#     1. Run 'setup' and execute 'claude login' inside the container
#     2. Run 'run' or 'shell' without ANTHROPIC_API_KEY
#     Credentials persist in a named Docker volume between runs.
#
# Environment:
#   ANTHROPIC_API_KEY   Optional — API key for pay-per-token auth
#   MAX_ITERATIONS      Phase loop iteration limit (default: 50)
#   IMAGE_NAME          Docker image name (default: scaffold-runner)
#   CLAUDE_VOLUME       Named volume for ~/.claude credentials (default: scaffold-claude-config)
#   GIT_USER_NAME       Git author name (default: Scaffold Runner)
#   GIT_USER_EMAIL      Git author email (default: scaffold-runner@localhost)
#   SKIP_PLAYWRIGHT_MCP Set to 1 to skip Playwright MCP injection (default: inject)
#   PLAYWRIGHT_MCP_VERSION  Override @playwright/mcp version for builds (default: in Dockerfile)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-scaffold-runner}"
CLAUDE_VOLUME="${CLAUDE_VOLUME:-scaffold-claude-config}"
COMMAND="${1:-build}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: $1 is required but not found." >&2
    exit 1
  }
}

require_cmd docker

build_image() {
  echo "Building container image: $IMAGE_NAME"
  local build_args=()
  if [[ -n "${PLAYWRIGHT_MCP_VERSION:-}" ]]; then
    build_args+=(--build-arg "PLAYWRIGHT_MCP_VERSION=$PLAYWRIGHT_MCP_VERSION")
  fi
  docker build "${build_args[@]}" -t "$IMAGE_NAME" "$ROOT_DIR/.devcontainer/"
  echo "Image built successfully: $IMAGE_NAME"
}

check_auth() {
  # Warn if neither API key nor persisted credentials are available.
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    return 0
  fi

  # Check if the named volume exists and has credentials.
  if docker volume inspect "$CLAUDE_VOLUME" >/dev/null 2>&1; then
    return 0
  fi

  echo "Warning: No authentication found." >&2
  echo "Either set ANTHROPIC_API_KEY or run 'setup' first to log in with your Claude subscription." >&2
  echo "Continuing anyway — Claude will fail if no credentials are available at runtime." >&2
}

run_container() {
  local cmd=("$@")
  echo "Starting container from: $ROOT_DIR"

  local docker_args=(
    --rm -it
    --cap-drop=ALL
    --cap-add=NET_ADMIN
    --cap-add=NET_RAW
    --cap-add=SETUID
    --cap-add=SETGID
    -v "$CLAUDE_VOLUME":/home/node/.claude
    -v "$ROOT_DIR":/workspace
    -e MAX_ITERATIONS="${MAX_ITERATIONS:-50}"
    -e CLAUDE_CONFIG_DIR=/home/node/.claude
  )

  # Pass API key only if set — omitting it lets Claude use stored subscription credentials.
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    docker_args+=(-e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")
  fi

  if [[ -n "${GIT_USER_NAME:-}" ]]; then
    docker_args+=(-e GIT_USER_NAME="$GIT_USER_NAME")
  fi
  if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    docker_args+=(-e GIT_USER_EMAIL="$GIT_USER_EMAIL")
  fi
  if [[ -n "${SKIP_PLAYWRIGHT_MCP:-}" ]]; then
    docker_args+=(-e SKIP_PLAYWRIGHT_MCP="$SKIP_PLAYWRIGHT_MCP")
  fi

  docker run "${docker_args[@]}" "$IMAGE_NAME" "${cmd[@]}"
}

case "$COMMAND" in
  build)
    build_image
    ;;
  setup)
    build_image
    echo ""
    echo "=== Claude Code Login Setup ==="
    echo "Run 'claude login' inside the container to authenticate with your subscription."
    echo "The login flow will display a URL — open it in your browser to complete auth."
    echo "Credentials are stored in the '$CLAUDE_VOLUME' Docker volume and persist between runs."
    echo ""
    run_container bash
    ;;
  run)
    build_image
    check_auth
    run_container bash scripts/run-until-done.sh
    ;;
  shell)
    build_image
    check_auth
    run_container bash
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    echo "Usage: $0 [build|setup|run|shell]" >&2
    exit 1
    ;;
esac
