#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${1:-default}"

# Enrich the current directory. Delegates to the wrapper so the enrich
# invocation lives in exactly one place (scripts/phasekit.sh `adopt`).
bash "$SCRIPT_DIR/phasekit.sh" adopt "$PROFILE"

echo ""
echo "Adoption mode ready. Update docs to reflect the current repo, then run the lead in audit mode."
