#!/usr/bin/env bash
set -euo pipefail

# Verify that the scaffold container environment is correctly set up.
# Run this inside the container after building:
#
#   bash scripts/container-setup.sh shell
#   bash scripts/verify-container.sh
#
# Or from the host (--entrypoint bypasses firewall which needs extra caps):
#
#   docker run --rm --entrypoint bash -v "$(pwd)":/workspace -w /workspace scaffold-runner scripts/verify-container.sh
#
# Checks: core tools, Playwright MCP server, Chromium, MCP settings injection.
# Exit code 0 = all checks pass, non-zero = at least one failure.

PASS=0
FAIL=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok  $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $label"
    FAIL=$((FAIL + 1))
  fi
}

check_output() {
  local label="$1"
  shift
  local output
  if output=$("$@" 2>&1); then
    echo "  ok  $label — $output"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $label"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Scaffold container verification ==="
echo ""

# --- Core tools ---
echo "Core tools:"
check "claude CLI" command -v claude
check "git" command -v git
check "jq" command -v jq
check "python3" command -v python3
check "python3 pyyaml" python3 -c "import yaml"
echo ""

# --- Playwright MCP ---
echo "Playwright MCP:"
check "playwright-mcp binary" command -v playwright-mcp

# Test that the MCP server responds to an initialize handshake.
# Send a JSON-RPC initialize request via stdin and check for a valid response.
MCP_INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"0.1"}}}'
check "MCP server responds to initialize" bash -c "echo '$MCP_INIT' | timeout 15 playwright-mcp --headless --no-sandbox 2>/dev/null | head -1 | jq -e '.result.serverInfo' >/dev/null 2>&1"
echo ""

# --- Chromium ---
echo "Chromium:"
# Playwright manages its own browser binaries in ~/.cache/ms-playwright/.
# The MCP server handshake above proves the browser works end-to-end.
# This check just confirms the cache directory exists with content.
check "playwright browser cache exists" bash -c "ls /home/node/.cache/ms-playwright/chromium-*/chrome-linux*/chrome >/dev/null 2>&1 || ls /home/node/.cache/ms-playwright/chromium_headless_shell-*/headless_shell >/dev/null 2>&1"
echo ""

# --- MCP settings injection ---
echo "MCP settings injection:"
SETTINGS_LOCAL="/workspace/.claude/settings.local.json"
if [[ -f "$SETTINGS_LOCAL" ]]; then
  check "settings.local.json has mcpServers.playwright" jq -e '.mcpServers.playwright' "$SETTINGS_LOCAL"
  check "playwright command is playwright-mcp" bash -c "jq -e -r '.mcpServers.playwright.command' '$SETTINGS_LOCAL' | grep -q 'playwright-mcp'"
  check "args include --headless" bash -c "jq -e '.mcpServers.playwright.args | index(\"--headless\")' '$SETTINGS_LOCAL'"
  check "args include --no-sandbox" bash -c "jq -e '.mcpServers.playwright.args | index(\"--no-sandbox\")' '$SETTINGS_LOCAL'"
else
  echo "  skip  settings.local.json not found (entrypoint may not have run yet)"
fi
echo ""

# --- Summary ---
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
