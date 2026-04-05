#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAFFOLD_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROFILE="${1:-default}"

# Enrich the current directory using the manifest profile
python3 "$SCAFFOLD_ROOT/scripts/enrich-project.py" "$(pwd)" --profile "$PROFILE"

echo ""
echo "Bootstrap complete. Customize docs/SPEC.md, docs/ARCHITECTURE.md, docs/PHASES.md, and docs/PROD_REQUIREMENTS.md next."
