# Decision Memo: M5.1 — Adopt Anthropic Reference Devcontainer

## Problem statement
The M5 container has unrestricted network access. Anthropic publishes a reference devcontainer with firewall rules (default-deny + whitelisted domains). M5.1 adopts those rules while preserving the CLI-only workflow.

## Constraints
- Firewall requires `NET_ADMIN` + `NET_RAW` capabilities and iptables/ipset packages
- `init-firewall.sh` must run as root before the entrypoint
- Python 3 + pyyaml must be added (scaffold-specific)
- Non-root user must be preserved
- VS Code must not be required
- `container-setup.sh build/run/shell` must all work with firewall active

## Recommendation: Option A — Overlay on Anthropic's Dockerfile

Create `.devcontainer/` with Anthropic's files + scaffold additions. Use an entrypoint wrapper for CLI-only firewall init.

## Red-team resolutions

| Blocker | Resolution |
|---|---|
| B1: Firewall ordering | Confirmed safe — Anthropic's script resolves domains before applying DROP |
| B2: Missing cap-drop=ALL | Accepted — add `--cap-drop=ALL` before NET_ADMIN/NET_RAW |
| B3: Claude can disable firewall | Documented as best-effort network hygiene, not a hard security boundary |
| B4: Shell mode skips firewall | Fixed — entrypoint.sh used for all modes |

## Risks and mitigations
- Upstream drift of init-firewall.sh → document source commit, add header comment
- Migration from container/Dockerfile.project pattern → document in CONTAINERIZATION.md
- Username change scaffold→node (same UID 1000) → document in commit and docs
- Firewall whitelist completeness → verify runtime domains are covered
- Firewall failure observability → entrypoint.sh fails fast with clear error

## Implementation slice
1. Create `.devcontainer/` files (Dockerfile, init-firewall.sh, entrypoint.sh, devcontainer.json)
2. Update `scripts/container-setup.sh`
3. Remove `container/Dockerfile`
4. Update `docs/CONTAINERIZATION.md`
5. Update `capabilities/project-capabilities.yaml`

## Acceptance criteria
- Container uses Anthropic's firewall rules (default-deny + whitelisted domains)
- `run-until-done.sh` continues to work as the entrypoint
- `container-setup.sh build/run/shell` commands still work with firewall active
- VS Code is not required (CLI-only path preserved)
- Non-root user preserved (node, UID 1000)
- Docs updated to reflect new setup, firewall threat model, and migration notes
