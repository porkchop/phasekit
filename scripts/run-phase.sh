#!/usr/bin/env bash
set -euo pipefail

PROMPT_FILE="${1:-./KICKOFF_PROMPT.txt}"
MODE="${CLAUDE_MODE:-new}"
ARTIFACT="./artifacts/phase-approval.json"
DONE_ARTIFACT="./artifacts/project-complete.json"

mkdir -p ./artifacts
rm -f "$ARTIFACT"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Prompt file not found: $PROMPT_FILE" >&2
  exit 1
fi

PROMPT_CONTENT="$(cat "$PROMPT_FILE")"

if [[ "$MODE" == "continue" ]]; then
  claude -c --permission-mode bypassPermissions -p "$PROMPT_CONTENT"
else
  claude -p --permission-mode bypassPermissions "$PROMPT_CONTENT"
fi

if [[ -f "$DONE_ARTIFACT" ]]; then
  echo "Project completion artifact detected."
  exit 0
fi

if [[ ! -f "$ARTIFACT" ]]; then
  echo "Phase approval artifact not found: $ARTIFACT" >&2
  exit 2
fi

jq empty "$ARTIFACT" >/dev/null
PHASE="$(jq -r '.phase' "$ARTIFACT")"
APPROVED="$(jq -r '.approved' "$ARTIFACT")"
SUMMARY="$(jq -r '.summary' "$ARTIFACT")"
COMMIT_MSG="$(jq -r '.suggested_commit_message' "$ARTIFACT")"

if [[ "$APPROVED" != "true" ]]; then
  echo "Phase not approved: $PHASE" >&2
  exit 3
fi

if [[ -z "$COMMIT_MSG" || "$COMMIT_MSG" == "null" ]]; then
  COMMIT_MSG="${PHASE}: approved"
fi

git add -A
git commit -m "$COMMIT_MSG"

echo "Committed approved phase: $PHASE"
echo "$SUMMARY"
