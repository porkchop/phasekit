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
#   bash scripts/container-setup.sh [build|run|shell]
#
# Commands:
#   build   Build the container image (default)
#   run     Build and run the phase loop (with firewall)
#   shell   Build and open an interactive shell (with firewall)
#
# Environment:
#   ANTHROPIC_API_KEY   Required for 'run' and 'shell' commands
#   MAX_ITERATIONS      Phase loop iteration limit (default: 50)
#   IMAGE_NAME          Docker image name (default: scaffold-runner)
#   GIT_USER_NAME       Git author name (default: Scaffold Runner)
#   GIT_USER_EMAIL      Git author email (default: scaffold-runner@localhost)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-scaffold-runner}"
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
  docker build -t "$IMAGE_NAME" "$ROOT_DIR/.devcontainer/"
  echo "Image built successfully: $IMAGE_NAME"
}

check_api_key() {
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "Error: ANTHROPIC_API_KEY is not set." >&2
    echo "Export it before running: export ANTHROPIC_API_KEY='sk-...'" >&2
    exit 1
  fi
}

run_container() {
  local cmd=("$@")
  check_api_key
  echo "Starting container from: $ROOT_DIR"
  docker run --rm -it \
    --cap-drop=ALL \
    --cap-add=NET_ADMIN \
    --cap-add=NET_RAW \
    --cap-add=SETUID \
    --cap-add=SETGID \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -e MAX_ITERATIONS="${MAX_ITERATIONS:-50}" \
    ${GIT_USER_NAME:+-e GIT_USER_NAME="$GIT_USER_NAME"} \
    ${GIT_USER_EMAIL:+-e GIT_USER_EMAIL="$GIT_USER_EMAIL"} \
    -v "$ROOT_DIR":/workspace \
    "$IMAGE_NAME" \
    "${cmd[@]}"
}

case "$COMMAND" in
  build)
    build_image
    ;;
  run)
    build_image
    # Entrypoint (entrypoint.sh) runs firewall init, then executes the command.
    # Default CMD is "bash scripts/run-until-done.sh", so no args needed.
    run_container bash scripts/run-until-done.sh
    ;;
  shell)
    build_image
    # Entrypoint runs firewall init, then drops to bash.
    run_container bash
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    echo "Usage: $0 [build|run|shell]" >&2
    exit 1
    ;;
esac
