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
#   bash scripts/verify-in-container.sh                 # the boundary-state suite, the command guard, the declared surface
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
# reports itself rootless) → --user 0:0.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-scaffold-runner}"
JQ_MIN="${JQ_MIN:-1.7}"
TARGETS=("$@")
# v0.18.2: the command guard's parse runs on the image's python and git too.
# v0.18.3: the guard's scope, the scaffold-reads advisory and the declared
# facts' proofs (they run the loop's own functions on the image's jq/grep).
# v0.19.1: multi-phase landings (the plan parser, the evidence base).
# v0.19.3: the process-reads record, and the adopted gate checks on the image's
# own node (20: its piped reporter is TAP).
# v0.19.5: the learnings rotation, on the image's python3.
# v0.23.0: the UI checks' fixture set, in the image's pinned browser, axe-core
# and reference font (the only place its browser tier runs).
[[ ${#TARGETS[@]} -gt 0 ]] || TARGETS=("tests.test_boundary_state" "tests.test_loop_owns_commits"
  "tests.test_guard_scope" "tests.test_scaffold_reads" "tests.test_declared_surface"
  "tests.test_engine_outside" "tests.test_multiphase_invariance" "tests.test_project_tests"
  "tests.test_adopted_checks" "tests.test_learnings_budget" "tests.test_ui_check")

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
# v0.19.3: the browser is pinned (.devcontainer/Dockerfile CHROMIUM_VERSION);
# an image built before the pin, or from another Dockerfile, reports another
# browser — stale, rebuild it.
want="$(sed -n 's/^ARG CHROMIUM_VERSION=//p' /workspace/.devcontainer/Dockerfile | head -1)"
chrome="$(ls -d /home/node/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1 || true)"
got="$( [ -n "$chrome" ] && "$chrome" --version 2>/dev/null | tr -s ' ' | sed 's/ *$//' || true)"
echo "verify-in-container: browser ${got:-none} (pinned: Google Chrome for Testing $want)"
if [ -z "$want" ] || [ "$got" != "Google Chrome for Testing $want" ]; then
  echo "verify-in-container: FAIL — the image's browser is not the pinned one (rebuild the image: .devcontainer/Dockerfile pins CHROMIUM_VERSION)" >&2
  exit 3
fi
# v0.23.0: the UI checks' tools are pinned too (axe-core, playwright-core, the
# reference font); without them tests.test_ui_check's browser tier would SKIP,
# a green run that checked nothing — so a stale image fails here instead.
df=/workspace/.devcontainer/Dockerfile
want_ui="$(printf 'playwright-core %s\naxe-core %s\nfont %s' "$(sed -n 's/^ARG PLAYWRIGHT_CORE_VERSION=//p' $df | head -1)" \
  "$(sed -n 's/^ARG AXE_CORE_VERSION=//p' $df | head -1)" "$(sed -n 's/^ARG UI_FONT_SHA256=//p' $df | head -1)")"
got_ui="$(cat /opt/phasekit-ui/VERSIONS 2>/dev/null || true)"
echo "verify-in-container: ui tools: $(printf '%s' "${got_ui:-none}" | tr '\n' ' ')"
if [ "$got_ui" != "$want_ui" ]; then
  echo "verify-in-container: FAIL — the image's UI tools are not the pinned ones (rebuild the image: .devcontainer/Dockerfile pins AXE_CORE_VERSION)" >&2
  exit 3
fi
cd /workspace
python3 -m unittest "$@" -v
INNER
)

# v0.22.0: the argv comes from the one launcher (scripts/phasekit-launch.py,
# the `runner` role): its user rule (uid 0 under rootless docker) and its
# bounds, which this run lacked before.
launch=()
while IFS= read -r -d '' arg; do launch+=("$arg"); done < <(
  "${PHASEKIT_PYTHON:-python3}" "$ROOT_DIR/scripts/phasekit-launch.py" argv --role runner \
    --name "phasekit-verify-in-container-$$" --interactive --image "$IMAGE_NAME" \
    --entrypoint bash --workdir /workspace --mount "$ROOT_DIR:/workspace:ro" \
    --set "JQ_MIN=$JQ_MIN" --set "PHASEKIT_TEST_LAYOUT=${PHASEKIT_TEST_LAYOUT:-vendored}" \
    -- -s -- "${TARGETS[@]}")
if [[ ${#launch[@]} -lt 2 || "${launch[0]} ${launch[1]}" != "docker run" \
      || " ${launch[*]} " != *" --memory-swap "* ]]; then
  echo "verify-in-container: the launcher composed no bounded docker run argv" >&2
  exit 2
fi
run_user=""
for ((i = 0; i < ${#launch[@]} - 1; i++)); do
  [[ "${launch[i]}" == --user ]] && { run_user="${launch[i + 1]}"; break; }
done
echo "verify-in-container: $IMAGE_NAME, repo read-only at /workspace, user $run_user, targets: ${TARGETS[*]}"
exec "${launch[@]}" <<<"$IN_CONTAINER"
