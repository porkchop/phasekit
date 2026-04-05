#!/bin/bash
set -euo pipefail

# Initialize the firewall before running the main command.
# This replaces devcontainer.json's postStartCommand for CLI-only usage.
#
# If firewall init fails (e.g., missing --cap-add=NET_ADMIN), the container
# will exit immediately rather than running without network isolation.

echo "Initializing firewall..."
if ! sudo /usr/local/bin/init-firewall.sh; then
    echo "ERROR: Firewall initialization failed." >&2
    echo "Ensure the container was started with --cap-add=NET_ADMIN --cap-add=NET_RAW --cap-add=SETUID --cap-add=SETGID" >&2
    exit 1
fi

# Override default git identity if env vars are set
if [[ -n "${GIT_USER_NAME:-}" ]]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

echo "Firewall active. Starting: $*"
exec "$@"
