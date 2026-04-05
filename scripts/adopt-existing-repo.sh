#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAFFOLD_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_NAME="${1:-$(basename "$(pwd)")}"

mkdir -p artifacts docs/adr .claude

# Generate .claude/CLAUDE.md from template if it doesn't already exist
CLAUDE_TEMPLATE="$SCAFFOLD_ROOT/templates/CLAUDE.template.md"
CLAUDE_TARGET=".claude/CLAUDE.md"
if [[ -f "$CLAUDE_TEMPLATE" ]] && [[ ! -f "$CLAUDE_TARGET" ]]; then
  sed -e "s/{{PROJECT_NAME}}/$PROJECT_NAME/g" \
      -e "s/{{OPTIONAL_REFERENCES}}//" \
      "$CLAUDE_TEMPLATE" > "$CLAUDE_TARGET"
  echo "Generated $CLAUDE_TARGET from template."
fi

echo "Adoption mode ready. Update docs to reflect the current repo, then run the lead in audit mode."
