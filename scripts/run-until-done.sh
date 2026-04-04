#!/usr/bin/env bash
set -euo pipefail

MAX_ITERATIONS="${MAX_ITERATIONS:-100}"
ITERATION=1
PROMPT_FILE="./KICKOFF_PROMPT.txt"
DONE_ARTIFACT="./artifacts/project-complete.json"

while [[ "$ITERATION" -le "$MAX_ITERATIONS" ]]; do
  echo "=== Iteration $ITERATION ==="

  if [[ -f "$DONE_ARTIFACT" ]]; then
    echo "Project completion artifact already present."
    exit 0
  fi

  if [[ "$ITERATION" -eq 1 ]]; then
    CLAUDE_MODE=new ./scripts/run-phase.sh "$PROMPT_FILE"
  else
    CLAUDE_MODE=continue ./scripts/run-phase.sh ./CONTINUE_PROMPT.txt
  fi

  if [[ -f "$DONE_ARTIFACT" ]]; then
    echo "Project complete."
    exit 0
  fi

  ITERATION=$((ITERATION + 1))
done

echo "Reached MAX_ITERATIONS without project completion." >&2
exit 4
