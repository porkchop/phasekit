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
    echo "Ensure the container was started with --cap-add=NET_ADMIN --cap-add=NET_RAW" >&2
    exit 1
fi

echo "Firewall active. Starting: $*"
exec "$@"
