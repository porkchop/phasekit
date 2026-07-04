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

# Trust the bind-mounted workspace for git. When the container user differs from
# the /workspace owner — e.g. UID 0 in rootless mode (see
# scripts/container-setup.sh) — git otherwise aborts with "detected dubious
# ownership in repository at '/workspace'". Idempotent across restarts.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qx /workspace; then
    git config --global --add safe.directory /workspace
fi

# Override default git identity if env vars are set
if [[ -n "${GIT_USER_NAME:-}" ]]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Inject Playwright MCP server configuration for containerized mode.
# Writes to settings.local.json (gitignored) so it does not affect
# the checked-in project settings or interactive users.
#
# Note: this write targets the bind-mounted /workspace. Under rootless Docker
# the default non-root `node` user (UID 1000) maps to an unmapped subordinate
# host UID and this write can fail with "Permission denied". Launch with
# PHASEKIT_ROOTLESS_DOCKER=1 (see scripts/container-setup.sh) to run as UID 0,
# which maps back to the host user and makes /workspace writable again.
if [[ "${SKIP_PLAYWRIGHT_MCP:-}" != "1" ]]; then
    SETTINGS_DIR="/workspace/.claude"
    SETTINGS_LOCAL="$SETTINGS_DIR/settings.local.json"
    MCP_CONFIG='{"mcpServers":{"playwright":{"command":"playwright-mcp","args":["--headless","--no-sandbox"]}}}'

    if [[ -d "$SETTINGS_DIR" ]]; then
        if [[ -f "$SETTINGS_LOCAL" ]]; then
            # Additive merge — preserves all existing keys
            MERGED=$(jq --argjson new "$MCP_CONFIG" '. * $new' "$SETTINGS_LOCAL")
            echo "$MERGED" > "$SETTINGS_LOCAL"
            echo "Playwright MCP: merged into $SETTINGS_LOCAL"
        else
            echo "$MCP_CONFIG" | jq '.' > "$SETTINGS_LOCAL"
            echo "Playwright MCP: created $SETTINGS_LOCAL"
        fi
    else
        echo "Playwright MCP: skipped ($SETTINGS_DIR not found)"
    fi
fi

echo "Firewall active. Starting: $*"
exec "$@"
