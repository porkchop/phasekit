#!/usr/bin/env bash
# Pretty-printer for claude stream-json output. Reads JSONL from stdin and
# writes one human-readable line per event to stdout. Tolerates non-JSON
# lines (e.g. stderr text mixed in by `2>&1 | tee`) by passing them through
# unchanged. Schema is best-effort — unknown event shapes are dumped as
# compact JSON so information is never lost when the CLI changes.
#
# Usage:
#   bash scripts/phasekit-log-fmt.sh < log.jsonl
#   tail -F artifacts/logs/claude-iter-3.jsonl | bash scripts/phasekit-log-fmt.sh
#
# Used by scripts/run-phase.sh to convert the live claude --output-format
# stream-json stream into the *.log files under artifacts/logs/.
set -u

command -v jq >/dev/null 2>&1 || { echo "phasekit-log-fmt.sh: jq is required" >&2; exit 1; }

jq -R -r --unbuffered '
  def fmt_content:
    if .type == "text" then "[text] \(.text // "")"
    elif .type == "tool_use" then "[tool_use] \(.name // "?") \((.input // {}) | tojson)"
    elif .type == "thinking" then "[thinking] \(.thinking // "")"
    elif .type == "tool_result" then "[tool_result] \(.tool_use_id // "") \((.content // "") | tostring | .[0:1000])"
    else "[\(.type // "?")] \(. | tojson)"
    end;

  def fmt:
    if .type == "system" then "[system] \(.subtype // "") \(.session_id // "")"
    elif .type == "assistant" and ((.message.content // null) | type) == "array" then
      .message.content | map(fmt_content) | join("\n")
    elif .type == "user" and ((.message.content // null) | type) == "array" then
      .message.content | map(fmt_content) | join("\n")
    elif .type == "result" then "[result \(.subtype // "")] \(.result // .error // (. | tojson))"
    elif .type == "stream_event" and (.event.delta.text // null) != null then "[partial] \(.event.delta.text)"
    elif .type then "[\(.type)] \(. | tojson)"
    else . | tojson
    end;

  . as $line | try (fromjson | fmt) catch $line
'
