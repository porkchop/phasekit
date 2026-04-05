# Scaffold unattended execution container
#
# This Dockerfile provides an isolated environment for running the scaffold's
# phase-gated workflow autonomously with permissive Claude Code execution.
#
# WARNING: This container runs Claude Code with bypassPermissions.
# Only use with trusted repositories.
#
# Build:
#   docker build -t scaffold-runner container/
#
# Run:
#   docker run --rm -it \
#     -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
#     -v "$(pwd)":/workspace \
#     scaffold-runner
#
# Or use the setup script:
#   bash scripts/container-setup.sh

FROM node:20-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    bash \
    curl \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install pyyaml for scaffold scripts
RUN python3 -m pip install --break-system-packages pyyaml

# Create non-root user for execution
RUN useradd -m -s /bin/bash --uid 1000 scaffold
USER scaffold

WORKDIR /workspace

# Default entrypoint runs the phase loop
ENTRYPOINT ["bash", "scripts/run-until-done.sh"]
