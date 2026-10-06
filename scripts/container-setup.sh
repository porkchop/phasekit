#!/usr/bin/env bash
set -euo pipefail
# PHASEKIT_TRACE=1 turns on bash xtrace so every wrapper command is visible.
# Forwarded into the container so the in-container scripts also trace.
[[ "${PHASEKIT_TRACE:-}" == "1" ]] && set -x

# Container setup script for scaffold unattended execution.
#
# Builds from .devcontainer/ using Anthropic's reference devcontainer
# with scaffold-specific additions (Python, pyyaml, entrypoint wrapper).
#
# The container includes a firewall (init-firewall.sh) that restricts
# outbound network access to whitelisted domains only. The firewall is
# best-effort network hygiene, not a hard security boundary.
#
# Usage:
#   bash scripts/container-setup.sh [build|setup|run|shell]
#
# Commands:
#   build   Build the container image (default)
#   setup   Build and open an interactive shell for claude login (no API key required)
#   run     Build and run the phase loop
#   shell   Build and open an interactive shell
#
# Authentication (pick one):
#   Option A — API key (pay-per-token):
#     ANTHROPIC_API_KEY   Set this env var before running 'run' or 'shell'
#
#   Option B — Claude Code subscription (flat-rate):
#     1. Run 'setup' and execute 'claude login' inside the container
#     2. Run 'run' or 'shell' without ANTHROPIC_API_KEY
#     Credentials persist in a named Docker volume between runs.
#
# Environment:
#   ANTHROPIC_API_KEY   Optional — API key for pay-per-token auth
#   MAX_ITERATIONS      Phase loop iteration limit (default: run-until-done.sh's
#                       — 50 standard, 2 light; forwarded only when set)
#   PHASEKIT_ITERATION_MODE  "light" = reduced-ceremony loop for small triaged
#                       tasks (v0.6.0; see docs/EXECUTION_MODES.md)
#   PHASEKIT_SESSION_DEADLINE  Epoch seconds of the supervisor's hard kill;
#                       enables deadline-aware iteration pacing (v0.6.1)
#   PHASEKIT_CONTRACTS_MOUNT  HOST path to a provider's contracts tree
#                       (index.json + one directory per dependency slug).
#                       Bind-mounted read-only at /contracts and announced to
#                       the in-container tooling via PHASEKIT_CONTRACTS_DIR.
#                       Optional: unset means "no provider", which is the
#                       normal standalone case and changes nothing (v0.7.0;
#                       see docs/CONTRACTS.md)
#   IMAGE_NAME          Docker image name (default: scaffold-runner)
#   CLAUDE_VOLUME       Named volume for ~/.claude credentials (default: scaffold-claude-config)
#   GIT_USER_NAME       Git author name (default: Scaffold Runner)
#   GIT_USER_EMAIL      Git author email (default: scaffold-runner@localhost)
#   SKIP_PLAYWRIGHT_MCP Set to 1 to skip Playwright MCP injection (default: inject)
#   PLAYWRIGHT_MCP_VERSION  Override @playwright/mcp version for builds (default: in Dockerfile)
#
# Rootless Docker support:
#   PHASEKIT_ROOTLESS_DOCKER  Set to 1 to run the container as UID 0 (container
#                             root). Under rootless Docker this maps back to the
#                             unprivileged host user, so files written to the
#                             bind-mounted /workspace are owned by that host user
#                             instead of an unmapped subordinate UID. Fixes
#                             "Permission denied" on /workspace under rootless.
#   PHASEKIT_CONTAINER_USER   Lower-level override for the container user passed
#                             to `docker run --user`. Accepts "root" or a raw
#                             "uid:gid" (e.g. "1001:1001"). Takes precedence over
#                             PHASEKIT_ROOTLESS_DOCKER. Default: unset (image's
#                             built-in non-root `node` user, UID 1000).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-scaffold-runner}"
CLAUDE_VOLUME="${CLAUDE_VOLUME:-scaffold-claude-config}"
COMMAND="${1:-build}"

# Container-side mount point for cross-project contracts (v0.7.0). Fixed, not
# configurable: the in-container tooling learns the path from the
# PHASEKIT_CONTRACTS_DIR env var we set alongside the mount, so there is one
# way to name it. Must match DEFAULT_MOUNT_DIR in scripts/phasekit-contracts.py.
CONTRACTS_CONTAINER_DIR="/contracts"

# Per-project auto-memory (v0.18.7). Every project is mounted at /workspace and
# every session shares one config volume, so Claude Code's auto-memory —
# <config>/projects/-workspace/memory/, keyed by the working directory — was
# ONE directory for every project: each project's sessions read and wrote the
# others' notes (2026-10-06: 531 files, 2.5 MB, written by the whole fleet).
# The volume is shared by concurrent sessions, so a symlink inside it cannot
# send different containers to different places. Instead each session gets
# its own directory of the same volume MOUNTED over that path (docker's
# volume-subpath, Docker >= 26 / API 1.45): project-memory/<key>, where <key>
# is this checkout's directory name. /workspace, the volume and
# CLAUDE_CONFIG_DIR are unchanged, and a host session never runs this script.
#
# The shared directory stays where it is, as the legacy copy: never moved,
# never deleted. A project's own directory is seeded with a copy of it the
# first time the project runs (atomically: a killed seed leaves no half
# directory), and the copies diverge from then on. Own memory or none, never
# another project's: when the directory cannot be prepared, or docker is too
# old for a subpath, the session gets an empty throwaway memory (tmpfs) and a
# warning. The key is the directory NAME, so two checkouts with one name share
# a memory (the fleet keeps one checkout per project under one directory).
CLAUDE_CONFIG_CONTAINER_DIR="/home/node/.claude"
CLAUDE_MEMORY_CONTAINER_DIR="$CLAUDE_CONFIG_CONTAINER_DIR/projects/-workspace/memory"
PROJECT_MEMORY_SUBDIR="project-memory"
PROJECT_MEMORY_MIN_API="1.45"
# Run by `sh` in a throwaway container: $1 = the key, $2 = where the config
# volume is mounted. Prints `seeded` or `exists`.
PROJECT_MEMORY_SEED_SH='set -eu
key=$1; vol=$2
legacy="$vol/projects/-workspace/memory"
base="$vol/'"$PROJECT_MEMORY_SUBDIR"'"
dst="$base/$key"
# The mountpoint must exist before docker mounts over it: docker would create
# it as the daemon user, and a non-root session could then not write beside it.
mkdir -p "$legacy" "$base"
if [ -d "$dst" ]; then
  echo exists
else
  work="$base/.seed/$key"
  mkdir -p "$work"
  # A killed seed leaves its directory here: cleared once it is an hour old,
  # never while another run may still be filling it.
  find "$work" -mindepth 1 -maxdepth 1 -mmin +60 -exec rm -rf {} +
  tmp=$(mktemp -d "$work/XXXXXX")
  cp -R "$legacy/." "$tmp/"
  chmod 755 "$tmp"
  if mv -T "$tmp" "$dst" 2>/dev/null; then
    echo seeded
  else
    rm -rf "$tmp"
    [ -d "$dst" ]
    echo exists
  fi
  rmdir "$work" "$base/.seed" 2>/dev/null || true
fi'
PROJECT_MEMORY_ARGS=()

project_memory_key() {
  local k
  k="$(basename "$ROOT_DIR")"
  k="${k//[^A-Za-z0-9._-]/_}"
  k="${k#"${k%%[!.]*}"}"   # no leading dot: never `.`, `..` or a hidden name
  [[ -n "$k" ]] || k="_default"
  printf '%s' "$k"
}

# True unless an API version docker reports — the client's or the server's,
# whichever is lower — is older than the subpath minimum (an unreadable one is
# tried; docker then reports its own error).
docker_supports_volume_subpath() {
  local v maj min
  for v in $(docker version --format '{{.Client.APIVersion}} {{.Server.APIVersion}}' \
               2>/dev/null || true); do
    [[ "$v" =~ ^([0-9]+)\.([0-9]+)$ ]] || continue
    maj="${BASH_REMATCH[1]}"; min="${BASH_REMATCH[2]}"
    if (( maj < ${PROJECT_MEMORY_MIN_API%%.*} \
          || (maj == ${PROJECT_MEMORY_MIN_API%%.*} && min < ${PROJECT_MEMORY_MIN_API#*.}) )); then
      return 1
    fi
  done
  return 0
}

# Sets PROJECT_MEMORY_ARGS to this session's memory mount. $1 = the docker
# --user spec ("" = the image's own user): the directory is created by the
# user the session runs as, with the volume where the session mounts it (so a
# fresh volume is first populated, and owned, as the session sees it).
project_memory_mount() {
  local user_spec="$1" key out
  key="$(project_memory_key)"
  local fallback=(--mount "type=tmpfs,dst=$CLAUDE_MEMORY_CONTAINER_DIR,tmpfs-mode=1777")
  local prep=(--rm --network none --cap-drop=ALL
              -v "$CLAUDE_VOLUME":"$CLAUDE_CONFIG_CONTAINER_DIR" --entrypoint /bin/sh)
  if [[ -n "$user_spec" ]]; then
    prep+=(--user "$user_spec")
    if [[ "$user_spec" == "0:0" ]]; then
      prep+=(--cap-add=DAC_OVERRIDE)
    fi
  fi
  # Prepared even when the mount below cannot be used: it also creates the
  # mountpoint the tmpfs needs, as the session's user.
  if ! out="$(docker run "${prep[@]}" "$IMAGE_NAME" -c "$PROJECT_MEMORY_SEED_SH" sh "$key" \
                "$CLAUDE_CONFIG_CONTAINER_DIR" 2>&1)"; then
    echo "container: auto-memory: could not prepare $PROJECT_MEMORY_SUBDIR/$key in '$CLAUDE_VOLUME' (${out:0:200}); this session's memory is empty and not kept" >&2
    PROJECT_MEMORY_ARGS=("${fallback[@]}")
    return 0
  fi
  if ! docker_supports_volume_subpath; then
    echo "container: auto-memory: docker's API is older than $PROJECT_MEMORY_MIN_API (no volume-subpath); this session's memory is empty and not kept — upgrade Docker for per-project memory" >&2
    PROJECT_MEMORY_ARGS=("${fallback[@]}")
    return 0
  fi
  if [[ "$CLAUDE_VOLUME" == *,* ]]; then
    echo "container: auto-memory: '$CLAUDE_VOLUME' contains a comma, which a --mount value cannot carry; this session's memory is empty and not kept" >&2
    PROJECT_MEMORY_ARGS=("${fallback[@]}")
    return 0
  fi
  if [[ "$CLAUDE_VOLUME" == /* ]]; then
    # A host directory used as the config "volume": the same layout, bound.
    PROJECT_MEMORY_ARGS=(--mount "type=bind,src=$CLAUDE_VOLUME/$PROJECT_MEMORY_SUBDIR/$key,dst=$CLAUDE_MEMORY_CONTAINER_DIR")
  else
    PROJECT_MEMORY_ARGS=(--mount "type=volume,src=$CLAUDE_VOLUME,dst=$CLAUDE_MEMORY_CONTAINER_DIR,volume-subpath=$PROJECT_MEMORY_SUBDIR/$key")
  fi
  case "$out" in
    *seeded*) echo "container: auto-memory: $PROJECT_MEMORY_SUBDIR/$key (first run: seeded with a copy of the shared memory)" ;;
    *)        echo "container: auto-memory: $PROJECT_MEMORY_SUBDIR/$key" ;;
  esac
}

# Resolve the container user (see header docs).
#
# The image normally runs as the non-root `node` user (UID 1000). That is the
# right default under standard ("rootful") Docker, where container UID 1000 ==
# host UID 1000. But under *rootless* Docker the daemon runs in a user
# namespace: container UID 1000 maps to a high subordinate UID on the host that
# the launching user cannot chown/modify. Files the container writes into the
# bind-mounted /workspace then appear owned by that unmapped UID, producing
# errors like "Permission denied" on .claude/settings.local.json or
# artifacts/logs that even `chmod -R a+rwX` can't fully repair.
#
# Running as container root (UID 0) sidesteps this: under rootless Docker,
# container UID 0 maps back to the unprivileged *host* user who started the
# daemon, so /workspace writes are owned by that host user. The rootless
# security boundary is preserved — "root" here is not real host root.
#
# PHASEKIT_CONTAINER_USER (explicit) wins over the PHASEKIT_ROOTLESS_DOCKER=1
# convenience flag, which is just shorthand for PHASEKIT_CONTAINER_USER=root.
CONTAINER_USER="${PHASEKIT_CONTAINER_USER:-}"
if [[ -z "$CONTAINER_USER" && "${PHASEKIT_ROOTLESS_DOCKER:-}" == "1" ]]; then
  CONTAINER_USER="root"
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: $1 is required but not found." >&2
    exit 1
  }
}

require_cmd docker

build_image() {
  echo "Building container image: $IMAGE_NAME"
  local build_args=()
  if [[ -n "${PLAYWRIGHT_MCP_VERSION:-}" ]]; then
    build_args+=(--build-arg "PLAYWRIGHT_MCP_VERSION=$PLAYWRIGHT_MCP_VERSION")
  fi
  docker build "${build_args[@]}" -t "$IMAGE_NAME" "$ROOT_DIR/.devcontainer/"
  echo "Image built successfully: $IMAGE_NAME"
}

check_auth() {
  # Warn if neither API key nor persisted credentials are available.
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    return 0
  fi

  # Check if the named volume exists and has credentials.
  if docker volume inspect "$CLAUDE_VOLUME" >/dev/null 2>&1; then
    return 0
  fi

  echo "Warning: No authentication found." >&2
  echo "Either set ANTHROPIC_API_KEY or run 'setup' first to log in with your Claude subscription." >&2
  echo "Continuing anyway — Claude will fail if no credentials are available at runtime." >&2
}

run_container() {
  local cmd=("$@")
  echo "Starting container from: $ROOT_DIR"

  local docker_args=(
    --rm
    --cap-drop=ALL
    --cap-add=NET_ADMIN
    --cap-add=NET_RAW
    --cap-add=SETUID
    --cap-add=SETGID
    -v "$CLAUDE_VOLUME":/home/node/.claude
    -v "$ROOT_DIR":/workspace
    -e CLAUDE_CONFIG_DIR=/home/node/.claude
  )

  # Forward MAX_ITERATIONS only when the caller set it — run-until-done.sh
  # owns the default (50 standard, 2 light). Unconditionally injecting 50 here
  # would silently defeat light mode's iteration cap.
  if [[ -n "${MAX_ITERATIONS:-}" ]]; then
    docker_args+=(-e MAX_ITERATIONS="$MAX_ITERATIONS")
  fi

  # Optional deterministic container name. An outer supervisor (e.g. the
  # foundry orchestrator's run-session.sh) sets PHASEKIT_CONTAINER_NAME so it
  # can `docker kill` the container if the bounded session times out — GNU
  # `timeout` kills the wrapper process tree but NOT the docker-run child's
  # container, which otherwise keeps running (and spending) as an orphan.
  # Unset → docker assigns a random name, exactly as before.
  if [[ -n "${PHASEKIT_CONTAINER_NAME:-}" ]]; then
    docker_args+=(--name "$PHASEKIT_CONTAINER_NAME")
  fi

  # Allocate an interactive TTY only when we actually have one. Under
  # non-interactive invocation — OpenClaw over `ssh host '…'`, a cron/systemd
  # unit, or any pipe — stdin/stdout are not terminals, and `docker run -t`
  # aborts with "the input device is not a TTY" (requiring the `ssh -tt`
  # workaround). The autonomous loop reads its prompt from a file (run-phase.sh)
  # and pipes claude's output, so it needs neither -i nor -t. Interactive
  # terminal use still gets both, preserving Ctrl-C and live output.
  if [[ -t 0 && -t 1 ]]; then
    docker_args+=(-it)
  fi

  # Optional rootless-Docker mode: run as the requested container user (UID 0
  # for "root"). We deliberately keep HOME pointed at /home/node so every asset
  # baked into the image for the node user stays reachable under the new UID:
  #   - the Claude credential volume mounted at /home/node/.claude
  #   - CLAUDE_CONFIG_DIR (already /home/node/.claude)
  #   - the Playwright browser cache (/home/node/.cache/ms-playwright)
  #   - the default git identity (/home/node/.gitconfig)
  #   - the ssh known_hosts mount (/home/node/.ssh/known_hosts, below)
  # Without this, a root process would default to HOME=/root and silently miss
  # all of them (lost login, no browser, "Author identity unknown" on commit).
  if [[ -n "$CONTAINER_USER" ]]; then
    local user_spec="$CONTAINER_USER"
    [[ "$user_spec" == "root" ]] && user_spec="0:0"
    docker_args+=(--user "$user_spec")
    docker_args+=(-e HOME=/home/node)
    echo "Container user override: running as '$user_spec' (HOME=/home/node)"
    # The image bakes /home/node (and its .gitconfig, caches, .claude mount)
    # owned by the `node` user (UID 1000). Running as UID 0 does NOT bypass
    # those permissions here because --cap-drop=ALL removes CAP_DAC_OVERRIDE —
    # so container-root cannot write its own $HOME and git aborts with
    # "could not lock config file /home/node/.gitconfig: Permission denied".
    # Restore just that one capability for the root case. In rootless Docker,
    # container UID 0 maps to the unprivileged host user, so DAC_OVERRIDE stays
    # contained to that user's namespace and grants no host privilege.
    if [[ "$user_spec" == "0:0" ]]; then
      docker_args+=(--cap-add=DAC_OVERRIDE)
      # Claude Code refuses --permission-mode bypassPermissions (the autonomous
      # loop's mode, set in run-phase.sh) when running as UID 0, unless
      # IS_SANDBOX=1 declares the environment sandboxed. This container is
      # genuinely sandboxed (default-deny firewall + --cap-drop=ALL), so the
      # declaration is honest and is required for root-mode autonomous runs.
      docker_args+=(-e IS_SANDBOX=1)
    fi
  fi

  # Pass API key only if set — omitting it lets Claude use stored subscription credentials.
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    # v0.18.2: by NAME — docker reads the value from its own environment; a
    # value on docker's command line is readable in the host process table.
    export ANTHROPIC_API_KEY
    docker_args+=(-e ANTHROPIC_API_KEY)
  fi

  if [[ -n "${GIT_USER_NAME:-}" ]]; then
    docker_args+=(-e GIT_USER_NAME="$GIT_USER_NAME")
  fi
  if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    docker_args+=(-e GIT_USER_EMAIL="$GIT_USER_EMAIL")
  fi
  if [[ -n "${SKIP_PLAYWRIGHT_MCP:-}" ]]; then
    docker_args+=(-e SKIP_PLAYWRIGHT_MCP="$SKIP_PLAYWRIGHT_MCP")
  fi

  # AUTO_PUSH=1 enables `git push` after every phase commit inside the loop.
  # See scripts/run-until-done.sh and docs/EXECUTION_MODES.md for the contract.
  if [[ -n "${AUTO_PUSH:-}" ]]; then
    docker_args+=(-e AUTO_PUSH="$AUTO_PUSH")
  fi

  # Per-project model choice (v0.4.5): forward so run-phase.sh can pass
  # `--model` to the claude CLI. Empty/unset = CLI default.
  if [[ -n "${ANTHROPIC_MODEL:-}" ]]; then
    docker_args+=(-e ANTHROPIC_MODEL="$ANTHROPIC_MODEL")
  fi

  # Per-iteration execution mode (v0.6.0): light = reduced ceremony for small
  # triaged tasks. Forwarded like ANTHROPIC_MODEL — set per-session by the
  # orchestrator, never a committed setting. See docs/EXECUTION_MODES.md.
  if [[ -n "${PHASEKIT_ITERATION_MODE:-}" ]]; then
    docker_args+=(-e PHASEKIT_ITERATION_MODE="$PHASEKIT_ITERATION_MODE")
  fi

  # Session deadline for iteration pacing (v0.6.1): epoch seconds of the
  # supervisor's hard kill (run-session computes start + MAX_MINUTES). The
  # loop refuses to start an iteration it likely can't finish before this.
  if [[ -n "${PHASEKIT_SESSION_DEADLINE:-}" ]]; then
    docker_args+=(-e PHASEKIT_SESSION_DEADLINE="$PHASEKIT_SESSION_DEADLINE")
  fi
  # Deadline-watchdog tuning knobs (v0.13.0) ride beside the deadline they
  # qualify; defaults are computed in-loop, so forwarding is only needed when
  # an operator overrides them.
  if [[ -n "${PHASEKIT_WRAPUP_LEAD_SECONDS:-}" ]]; then
    docker_args+=(-e PHASEKIT_WRAPUP_LEAD_SECONDS="$PHASEKIT_WRAPUP_LEAD_SECONDS")
  fi
  if [[ -n "${PHASEKIT_LASTRESORT_LEAD_SECONDS:-}" ]]; then
    docker_args+=(-e PHASEKIT_LASTRESORT_LEAD_SECONDS="$PHASEKIT_LASTRESORT_LEAD_SECONDS")
  fi
  # Branch-per-iteration + squash-to-target (v0.14.0): the supervisor names
  # the integration branch and (optionally) the iteration's work branch per
  # session. Unset = the loop's commit paths are unchanged.
  if [[ -n "${PHASEKIT_SQUASH_TARGET:-}" ]]; then
    docker_args+=(-e PHASEKIT_SQUASH_TARGET="$PHASEKIT_SQUASH_TARGET")
  fi
  if [[ -n "${PHASEKIT_WORK_BRANCH:-}" ]]; then
    docker_args+=(-e PHASEKIT_WORK_BRANCH="$PHASEKIT_WORK_BRANCH")
  fi

  # Project build environment (v0.14.3). A supervisor names, in
  # PHASEKIT_FORWARD_ENV, the comma-separated NAMES of project-specific keys it
  # has already placed in this script's process env — an operator-held env
  # file the orchestrator reads, e.g. the backend URL a client bakes at build
  # time. This is the one door for non-PHASEKIT_ project keys: the allowlist
  # above stays fixed, the names travel as a list, the VALUES travel only in
  # the process env (never a file, never a log), and the witness below names
  # keys only. A listed name that is unset is skipped silently — the project's
  # own build then fails honestly; a malformed name is skipped loudly. The list
  # itself is forwarded too, so the loop can say which keys it was given.
  if [[ -n "${PHASEKIT_FORWARD_ENV:-}" ]]; then
    local _fwd_list _fwd_name _fwd_names=() _fwd_shown
    # Newlines are separators too (a YAML block scalar is a natural source).
    IFS=',' read -r -a _fwd_list <<< "${PHASEKIT_FORWARD_ENV//$'\n'/,}"
    for _fwd_name in "${_fwd_list[@]}"; do
      # Trim the EDGES only: interior whitespace makes a name malformed, and
      # malformed is loud, never silently repaired into a different name.
      _fwd_name="${_fwd_name#"${_fwd_name%%[![:space:]]*}"}"
      _fwd_name="${_fwd_name%"${_fwd_name##*[![:space:]]}"}"
      [[ -n "$_fwd_name" ]] || continue
      if [[ ! "$_fwd_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        # The loud path must not become the leak: a token written as
        # NAME=value (env-file syntax pasted into the list) is shown by its
        # name part only, and any token is cut at 40 characters.
        _fwd_shown="${_fwd_name%%=*}"
        [[ "$_fwd_shown" == "$_fwd_name" ]] || _fwd_shown="$_fwd_shown=…"
        echo "container: PHASEKIT_FORWARD_ENV: skipping malformed name '${_fwd_shown:0:40}' (names only — no '=', no spaces)" >&2
        continue
      fi
      case "$_fwd_name" in
        HOME|PATH|CLAUDE_CONFIG_DIR|IS_SANDBOX)
          # docker's last -e wins: a project key with one of these names would
          # silently override what this script set for the container.
          echo "container: PHASEKIT_FORWARD_ENV: refusing '$_fwd_name' — the container script owns that name" >&2
          continue ;;
      esac
      if [[ -n "${!_fwd_name:-}" ]]; then
        # v0.18.2: `-e NAME` — docker reads the value from its own
        # environment. `-e NAME=value` put the value on docker's command
        # line, readable by every account on the host in the process table
        # (seen on foundry-worker, 2026-09-30); the promise above is "values
        # travel only in the process env".
        export "${_fwd_name?}"
        docker_args+=(-e "$_fwd_name")
        _fwd_names+=("$_fwd_name")
      fi
    done
    docker_args+=(-e PHASEKIT_FORWARD_ENV="$PHASEKIT_FORWARD_ENV")
    if [[ ${#_fwd_names[@]} -gt 0 ]]; then
      echo "container: forwarding project env: ${_fwd_names[*]}"
    else
      echo "container: PHASEKIT_FORWARD_ENV is set but none of its names are set in the environment — nothing forwarded" >&2
    fi
  fi

  # Cross-project contracts (v0.7.0). A provider — the Foundry orchestrator, or
  # a human running standalone — points PHASEKIT_CONTRACTS_MOUNT at a host
  # directory holding index.json plus one directory per dependency slug. We
  # bind-mount it read-only at the fixed container path /contracts and tell the
  # in-container tooling where it landed.
  #
  # Unset is the ordinary case and must stay a no-op: phasekit is a public tool
  # that works with no orchestrator at all, so "no mount" can never be an error
  # here. The refusal lives on the consumer side, triggered by the repo's own
  # contracts.yaml declaration — never by the mount's absence.
  #
  # Set-but-unusable IS an error, and a loud one. A provider that passes a path
  # we silently drop is exactly the META_REPO_PATH failure — an export with no
  # consumer, unnoticed for months — reproduced inside the feature built to
  # prevent it. Fail before spending a single token.
  if [[ -n "${PHASEKIT_CONTRACTS_MOUNT:-}" ]]; then
    if [[ ! -d "$PHASEKIT_CONTRACTS_MOUNT" ]]; then
      echo "Error: PHASEKIT_CONTRACTS_MOUNT is set to '$PHASEKIT_CONTRACTS_MOUNT' but that is not a directory." >&2
      echo "       A provider must pass a readable contracts tree (index.json + one dir per slug)," >&2
      echo "       or leave the variable unset. See docs/CONTRACTS.md." >&2
      exit 1
    fi
    if [[ ! -r "$PHASEKIT_CONTRACTS_MOUNT/index.json" ]]; then
      # "No dependencies" is a manifest with zero entries, never an empty
      # directory: an empty dir is indistinguishable from a broken bind mount,
      # whereas a manifest is the provider ASSERTING that it checked.
      echo "Error: PHASEKIT_CONTRACTS_MOUNT '$PHASEKIT_CONTRACTS_MOUNT' has no readable index.json." >&2
      echo "       A provider with no dependencies to offer must still ship an index.json with" >&2
      echo "       zero entries — silence and never-reached must not look alike." >&2
      exit 1
    fi
    docker_args+=(-v "$PHASEKIT_CONTRACTS_MOUNT":"$CONTRACTS_CONTAINER_DIR":ro)
    docker_args+=(-e PHASEKIT_CONTRACTS_DIR="$CONTRACTS_CONTAINER_DIR")
    echo "Contracts mount: $PHASEKIT_CONTRACTS_MOUNT -> $CONTRACTS_CONTAINER_DIR (read-only)"
  fi

  # CLAUDE_MODE controls whether the inner loop starts a fresh session (`new`,
  # default) or resumes the most recent one (`continue`). Forward so users can
  # manually continue a run that crashed mid-iteration without editing scripts:
  #   CLAUDE_MODE=continue bash scripts/container-setup.sh run
  if [[ -n "${CLAUDE_MODE:-}" ]]; then
    docker_args+=(-e CLAUDE_MODE="$CLAUDE_MODE")
  fi

  # PHASEKIT_ITER_RETRY caps per-iteration retries on transient claude CLI
  # failures (filter trips, 5xx, etc.). See scripts/run-until-done.sh.
  if [[ -n "${PHASEKIT_ITER_RETRY:-}" ]]; then
    docker_args+=(-e PHASEKIT_ITER_RETRY="$PHASEKIT_ITER_RETRY")
  fi

  # PHASEKIT_TRACE=1 enables bash xtrace inside the wrappers (loud but useful
  # for diagnosing loop behavior).
  if [[ -n "${PHASEKIT_TRACE:-}" ]]; then
    docker_args+=(-e PHASEKIT_TRACE="$PHASEKIT_TRACE")
  fi

  # Forward the host's SSH agent so `git push` to SSH remotes (git@github.com:…)
  # works inside the container. No keys are copied — the container only sees
  # the agent socket. Requires a running ssh-agent on the host with the right
  # key loaded (`ssh-add ~/.ssh/id_ed25519`).
  if [[ -n "${SSH_AUTH_SOCK:-}" ]] && [[ -S "$SSH_AUTH_SOCK" ]]; then
    docker_args+=(-v "$SSH_AUTH_SOCK:/ssh-agent")
    docker_args+=(-e SSH_AUTH_SOCK=/ssh-agent)
  fi

  # Mount known_hosts so the container trusts the same host keys as the host
  # (avoids prompts for `git@github.com` etc. on first connection).
  if [[ -f "$HOME/.ssh/known_hosts" ]]; then
    docker_args+=(-v "$HOME/.ssh/known_hosts:/home/node/.ssh/known_hosts:ro")
  fi

  # GH_TOKEN / GITHUB_TOKEN for HTTPS-with-PAT push workflows. Pass through
  # if set; container uses it via gh CLI or git credential helper.
  if [[ -n "${GH_TOKEN:-}" ]]; then
    export GH_TOKEN
    docker_args+=(-e GH_TOKEN)
  fi
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    export GITHUB_TOKEN
    docker_args+=(-e GITHUB_TOKEN)
  fi

  # Per-project auto-memory (v0.18.7; see PROJECT_MEMORY_SEED_SH above). Last,
  # after every refusal above, so a refused session prepares nothing.
  local memory_user="$CONTAINER_USER"
  if [[ "$memory_user" == "root" ]]; then
    memory_user="0:0"
  fi
  project_memory_mount "$memory_user"
  docker_args+=("${PROJECT_MEMORY_ARGS[@]}")

  docker run "${docker_args[@]}" "$IMAGE_NAME" "${cmd[@]}"
}

case "$COMMAND" in
  build)
    build_image
    ;;
  setup)
    build_image
    echo ""
    echo "=== Claude Code Login Setup ==="
    echo "Run 'claude login' inside the container to authenticate with your subscription."
    echo "The login flow will display a URL — open it in your browser to complete auth."
    echo "Credentials are stored in the '$CLAUDE_VOLUME' Docker volume and persist between runs."
    echo ""
    run_container bash
    ;;
  run)
    build_image
    check_auth
    run_container bash scripts/run-until-done.sh
    ;;
  shell)
    build_image
    check_auth
    run_container bash
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    echo "Usage: $0 [build|setup|run|shell]" >&2
    exit 1
    ;;
esac
