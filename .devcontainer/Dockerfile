# Scaffold execution container
#
# Based on Anthropic's Claude Code reference devcontainer:
# https://github.com/anthropics/claude-code/tree/main/.devcontainer
#
# Scaffold-specific additions marked with "# SCAFFOLD:" comments.
#
# WARNING: This container runs Claude Code with bypassPermissions.
# Only use with trusted repositories.

FROM node:20

ARG TZ
ENV TZ="$TZ"

ARG CLAUDE_CODE_VERSION=latest

# Install basic development tools and iptables/ipset for firewall
RUN apt-get update && apt-get install -y --no-install-recommends \
  less \
  git \
  procps \
  sudo \
  fzf \
  zsh \
  man-db \
  unzip \
  gnupg2 \
  gh \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  nano \
  vim \
  bash \
  curl \
  ca-certificates \
  # SCAFFOLD: Python 3 for scaffold scripts (validate-skill, package-skill, etc.)
  python3 \
  python3-pip \
  python3-venv \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# SCAFFOLD: jq from the upstream static release, sha256-pinned (v0.14.12).
# Debian bookworm's apt jq is 1.6, which rejects `$label` / `$__loc__` as
# variable names; the loop's filters were written on jq 1.8 hosts and failed
# silently in-container from phasekit v0.14.5 to v0.14.10 while the host suite
# stayed green. The build fails on a checksum mismatch, asserts the version,
# and runs the reserved-word filter. Verify a built image with
# scripts/verify-in-container.sh (the suite under the runtime's jq).
ARG JQ_VERSION=1.7.1
ARG JQ_SHA256_AMD64=5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5
ARG JQ_SHA256_ARM64=4dd2d8a0661df0b22f1bb9a1f9830f06b6f3b8f7d91211a1ef5d7c4f06a8b4a5
RUN ARCH="$(dpkg --print-architecture)" && \
  case "$ARCH" in \
    amd64) JQ_SHA256="$JQ_SHA256_AMD64" ;; \
    arm64) JQ_SHA256="$JQ_SHA256_ARM64" ;; \
    *) echo "unsupported architecture for the pinned jq: $ARCH" >&2; exit 1 ;; \
  esac && \
  curl -fsSL --retry 3 --retry-all-errors -o /usr/local/bin/jq "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${ARCH}" && \
  echo "${JQ_SHA256}  /usr/local/bin/jq" | sha256sum -c - && \
  chmod 0755 /usr/local/bin/jq && \
  test "$(jq --version)" = "jq-${JQ_VERSION}" && \
  jq -n --arg label x '{l: $label}' >/dev/null

# SCAFFOLD: Install pyyaml for scaffold scripts
RUN python3 -m pip install --break-system-packages pyyaml

# SCAFFOLD: uv + a baked managed Python (for Python projects whose verify
# gate runs `uv sync` — e.g. foundry-orchestrator requires-python >=3.12,
# newer than bookworm's 3.11). Python is downloaded at image-build time so
# sessions never need GitHub at runtime; wheels still need PyPI, which
# init-firewall.sh allowlists. World-readable so both node and UID-0
# (rootless) sessions can use it.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
# copy mode: .venv lives on the /workspace bind mount (different fs from the
# uv cache), where hardlinks fail; copy avoids per-session warnings/fallback.
ENV UV_LINK_MODE=copy
RUN uv python install 3.12 && chmod -R a+rX /opt/uv

# Ensure default node user has access to /usr/local/share
RUN mkdir -p /usr/local/share/npm-global && \
  chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist bash history
RUN SNIPPET="export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  && mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R $USERNAME /commandhistory

# Set DEVCONTAINER environment variable to help with orientation
ENV DEVCONTAINER=true

# Create workspace and config directories and set permissions
RUN mkdir -p /workspace /home/node/.claude && \
  chown -R node:node /workspace /home/node/.claude

WORKDIR /workspace

ARG GIT_DELTA_VERSION=0.18.2
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  sudo dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Set up non-root user
USER node

# Install global packages
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin

# Set the default shell to bash (scaffold preference over zsh)
ENV SHELL=/bin/bash

# SCAFFOLD: Default git identity for unattended commits (overridable via env vars)
RUN git config --global user.name "Scaffold Runner" && \
  git config --global user.email "scaffold-runner@localhost"

# SCAFFOLD: Trust the bind-mounted workspace.
# /workspace is a bind mount, so its files are owned by the host user's UID. When
# that differs from the container user — always the case under rootless Docker,
# and common otherwise — git's "dubious ownership" guard aborts every command
# with exit 128 ("detected dubious ownership in repository at '/workspace'"),
# which would break the loop's between-phase commit step. /workspace is our own
# controlled mount, so marking it safe is correct. Baked into /home/node/.gitconfig,
# which is read in both the default (node) and rootless-root (HOME=/home/node) modes.
RUN git config --global --add safe.directory /workspace

# Install Claude Code
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# SCAFFOLD: Playwright MCP server for browser automation in container.
# Install the npm package as node user, then switch to root to install
# Chromium's OS dependencies, then back to node for the browser binary.
ARG PLAYWRIGHT_MCP_VERSION=0.0.70
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION}
USER root
RUN npx playwright-core install-deps chromium
USER node
RUN npx playwright-core install chromium

# Copy and set up firewall script
COPY init-firewall.sh /usr/local/bin/
# SCAFFOLD: Copy entrypoint wrapper for CLI-only firewall init
COPY entrypoint.sh /usr/local/bin/
USER root
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/entrypoint.sh && \
  echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-firewall && \
  chmod 0440 /etc/sudoers.d/node-firewall
USER node

# SCAFFOLD: Default entrypoint runs firewall init then the provided command
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash", "scripts/run-until-done.sh"]
