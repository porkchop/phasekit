#!/usr/bin/env bash
set -euo pipefail

# Run phasekit's own suite INSIDE the runtime image (v0.14.12) — the proof
# that the suite's jq is the runtime's jq.
#
# Why: the fleet's runtime image once shipped jq 1.6 while developer hosts ran
# 1.8; jq 1.6 rejects `$label` as a variable name, so every loop filter that
# bound it failed silently in-container from v0.14.5 to v0.14.10 with the
# whole host suite green (v0.14.11 renamed the word; this run makes the class
# visible before a tag). A pre-tag step of docs/RELEASING.md, not a pre-commit
# gate: it needs Docker.
#
# Usage (from the phasekit repo root, host with Docker):
#   bash scripts/verify-in-container.sh                 # tests.test_boundary_state
#   bash scripts/verify-in-container.sh tests.test_x …  # other unittest targets
#   IMAGE_NAME=scaffold-runner JQ_MIN=1.7 bash scripts/verify-in-container.sh
#
# The repo is bind-mounted READ-ONLY at /workspace (nothing the run does can
# touch the tree — scratch repos live under the container's /tmp), HOME is a
# throwaway inside the container, and the entrypoint's firewall is bypassed
# (--entrypoint bash: no extra capabilities needed). The container's jq must
# be >= JQ_MIN (default 1.7) and must accept `$label` — asserted before the
# suite runs, so a stale image fails here, loudly, not by a green suite that
# never exercised the filters.
#
# Container user: the host uid/gid on a rootful daemon (the developer host);
# under ROOTLESS Docker the host user IS container root, while the host uid
# names an unrelated subuid inside the container (no passwd entry; a
# different owner in git's eyes), so the run is uid 0 there — the same knob
# container-setup.sh uses: PHASEKIT_ROOTLESS_DOCKER=1 (or a daemon that
# reports rootless in its SecurityOptions) → --user 0:0.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-scaffold-runner}"
JQ_MIN="${JQ_MIN:-1.7}"
TARGETS=("$@")
[[ ${#TARGETS[@]} -gt 0 ]] || TARGETS=("tests.test_boundary_state")

command -v docker >/dev/null 2>&1 || { echo "verify-in-container: docker not found" >&2; exit 2; }
docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || {
  echo "verify-in-container: image '$IMAGE_NAME' not found — build it first (bash scripts/container-setup.sh build)" >&2
  exit 2
}

# The in-container half: assert the jq, then run the targets. Passed as a
# single script on stdin so no quoting crosses the docker boundary.
IN_CONTAINER=$(cat <<'INNER'
set -euo pipefail
export HOME=/tmp/phasekit-verify-home PYTHONDONTWRITEBYTECODE=1 GIT_CONFIG_NOSYSTEM=1
# The throwaway HOME bypasses the image's baked `safe.directory /workspace`,
# so a target that runs git against the checkout itself (tests.test_installer)
# would hit the dubious-ownership guard whenever the container uid is not the
# mount's owner — declare it in the environment instead (git >= 2.31).
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/workspace
[ -n "${JQ_MIN:-}" ] || JQ_MIN=1.7
mkdir -p "$HOME"
ver="$(jq --version 2>/dev/null | sed 's/^jq-//')"
echo "verify-in-container: jq $ver (minimum $JQ_MIN), python $(python3 --version 2>&1 | cut -d' ' -f2), $(bash --version | head -1 | cut -d' ' -f2-4)"
if [ "$(printf '%s\n%s\n' "$JQ_MIN" "$ver" | sort -V | head -1)" != "$JQ_MIN" ]; then
  echo "verify-in-container: FAIL — the image's jq $ver is older than $JQ_MIN (rebuild the image: .devcontainer/Dockerfile pins JQ_VERSION)" >&2
  exit 3
fi
# The reserved-word regression (v0.14.11): a modern jq accepts `$label`.
if ! jq -n --arg label x '{l: $label}' >/dev/null 2>&1; then
  echo "verify-in-container: FAIL — the image's jq rejects \$label as a variable (jq 1.6 behaviour)" >&2
  exit 3
fi
cd /workspace
python3 -m unittest "$@" -v
INNER
)

run_user="$(id -u):$(id -g)"
if [[ "${PHASEKIT_ROOTLESS_DOCKER:-}" == "1" ]] \
   || docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
  run_user="0:0"
fi
echo "verify-in-container: $IMAGE_NAME, repo read-only at /workspace, user $run_user, targets: ${TARGETS[*]}"
exec docker run --rm -i \
  --entrypoint bash \
  --user "$run_user" \
  -e JQ_MIN="$JQ_MIN" \
  -v "$ROOT_DIR:/workspace:ro" \
  -w /workspace \
  "$IMAGE_NAME" -s -- "${TARGETS[@]}" <<<"$IN_CONTAINER"
