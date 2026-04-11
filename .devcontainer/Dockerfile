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
  jq \
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

# SCAFFOLD: Install pyyaml for scaffold scripts
RUN python3 -m pip install --break-system-packages pyyaml

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

# Install Claude Code
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# SCAFFOLD: Playwright MCP server + Chromium for browser automation in container
ARG PLAYWRIGHT_MCP_VERSION=0.0.70
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} && \
    npx playwright install --with-deps chromium

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
