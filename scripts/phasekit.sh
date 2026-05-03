#!/usr/bin/env bash
# phasekit — thin wrapper around enrich-project.py.
#
# Forwards all arguments to the Python engine. Use any flag the engine
# supports: --check, --upgrade, --uninstall, --reconcile, --migrate-only,
# --self-check, --include-templates, --keep-local, --take-new, --adopt,
# --rename-local, --accept-removal, --interactive, --strict, --no-lock,
# --dry-run, --yes, --force, --profile, --include-once.
#
# Usage:
#   bash scripts/phasekit.sh --check .
#   bash scripts/phasekit.sh --upgrade --yes .
#   bash scripts/phasekit.sh --upgrade --dry-run --include-templates .
#
# For frequent use, alias it:
#   alias phasekit='bash /path/to/phasekit/scripts/phasekit.sh'
#   phasekit --check .

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/enrich-project.py" "$@"
