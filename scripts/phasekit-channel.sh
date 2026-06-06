#!/usr/bin/env bash
# phasekit channel library — sourced by install.sh and scripts/phasekit.sh.
#
# Single source of truth for self-update channel resolution (see
# docs/adr/ADR-0002-self-update-channels.md). A "channel" selects which git ref
# the canonical clone tracks when it updates:
#
#   stable  -> latest v* release tag (the default; pre-ADR-0002 behavior)
#   edge    -> tip of the remote default branch (origin/<HEAD>, usually master)
#   <ref>   -> an explicit pin (a tag or sha); no auto-advance
#
# State is a single token in <repo>/.phasekit-channel; absent => stable. This
# file is sourced, not executed — it only defines functions and one variable, so
# it is safe under `set -euo pipefail`. Functions use explicit `if` rather than
# `cond && stmt` to avoid set -e aborting on a falsey guard.

PHASEKIT_CHANNEL_DEFAULT="stable"

# Path of the channel state file for a repo.
pk_channel_file() { printf '%s/.phasekit-channel' "$1"; }

# Echo the channel token for a repo (default stable). Whitespace-trimmed.
pk_channel_read() {
  local f v
  f="$(pk_channel_file "$1")"
  if [[ -f "$f" ]]; then
    v="$(tr -d '[:space:]' < "$f")"
    printf '%s' "${v:-$PHASEKIT_CHANNEL_DEFAULT}"
  else
    printf '%s' "$PHASEKIT_CHANNEL_DEFAULT"
  fi
}

# Persist a channel token for a repo.
pk_channel_write() {
  printf '%s\n' "$2" > "$(pk_channel_file "$1")"
}

# Remote default branch name for a repo (fallback: master).
pk_default_branch() {
  local b
  b="$(git -C "$1" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
  printf '%s' "${b:-master}"
}

# Echo the git ref a channel resolves to:
#   stable -> latest v* tag, or the default branch when no tags exist yet
#   edge   -> default branch name
#   <ref>  -> the ref verbatim (pin)
pk_channel_resolve_ref() {
  local repo="$1" channel="$2" tag
  case "$channel" in
    stable)
      tag="$(git -C "$repo" tag -l 'v*' | sort -V | tail -n1)"
      if [[ -n "$tag" ]]; then printf '%s' "$tag"; else pk_default_branch "$repo"; fi
      ;;
    edge) pk_default_branch "$repo" ;;
    *)    printf '%s' "$channel" ;;
  esac
}

# Classify an explicit ref into the channel that should be persisted for it
# (ADR-0002 decision 5): the default branch -> edge; an existing release tag ->
# stable; anything else (sha, feature branch) -> a pin to that ref.
pk_channel_classify_ref() {
  local repo="$1" ref="$2"
  if [[ "$ref" == "$(pk_default_branch "$repo")" ]]; then
    printf 'edge'
  elif git -C "$repo" rev-parse -q --verify "refs/tags/$ref" >/dev/null 2>&1; then
    printf 'stable'
  else
    printf '%s' "$ref"
  fi
}

# Check out the ref for a channel and, when it is a branch, fast-forward to the
# remote tip. Assumes the caller already fetched. No-op fast-forward for tags and
# SHAs (origin/<tag-or-sha> does not exist, so the merge fails and is swallowed).
# Returns non-zero when the channel resolves to no ref (stable with no tags and
# no default branch — only possible on an empty remote).
pk_channel_checkout() {
  local repo="$1" channel="$2" ref
  ref="$(pk_channel_resolve_ref "$repo" "$channel")"
  if [[ -z "$ref" ]]; then return 1; fi
  git -C "$repo" checkout --quiet "$ref"
  git -C "$repo" merge --ff-only --quiet "origin/$ref" 2>/dev/null || true
}

# True when a channel tracks unreleased code (edge, or a pin to a non-release
# ref). A pin to a vN.N tag is considered released.
pk_channel_is_edge() {
  case "$1" in
    stable)  return 1 ;;
    edge)    return 0 ;;
    v[0-9]*) return 1 ;;
    *)       return 0 ;;
  esac
}
