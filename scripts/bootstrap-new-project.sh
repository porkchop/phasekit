#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${1:-default}"

# Enrich the current directory. Delegates to the wrapper so the enrich
# invocation lives in exactly one place (scripts/phasekit.sh `bootstrap`).
bash "$SCRIPT_DIR/phasekit.sh" bootstrap "$PROFILE"

echo ""
echo "Bootstrap complete. Customize docs/SPEC.md, docs/ARCHITECTURE.md, docs/PHASES.md, and docs/PROD_REQUIREMENTS.md next."
