#!/usr/bin/env bash
set -euo pipefail

# Container setup script for scaffold unattended execution.
#
# This script builds the container image and optionally runs it.
#
# Usage:
#   bash scripts/container-setup.sh [build|run|shell]
#
# Commands:
#   build   Build the container image (default)
#   run     Build and run the phase loop
#   shell   Build and open an interactive shell
#
# Environment:
#   ANTHROPIC_API_KEY   Required for 'run' and 'shell' commands
#   MAX_ITERATIONS      Phase loop iteration limit (default: 50)
#   IMAGE_NAME          Docker image name (default: scaffold-runner)

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
  docker build -t "$IMAGE_NAME" "$ROOT_DIR/container/"
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
  local entrypoint=("$@")
  check_api_key
  echo "Starting container from: $ROOT_DIR"
  docker run --rm -it \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -e MAX_ITERATIONS="${MAX_ITERATIONS:-50}" \
    -v "$ROOT_DIR":/workspace \
    ${entrypoint:+--entrypoint "${entrypoint[0]}"} \
    "$IMAGE_NAME" \
    ${entrypoint:+"${entrypoint[@]:1}"}
}

case "$COMMAND" in
  build)
    build_image
    ;;
  run)
    build_image
    run_container
    ;;
  shell)
    build_image
    run_container bash
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    echo "Usage: $0 [build|run|shell]" >&2
    exit 1
    ;;
esac
