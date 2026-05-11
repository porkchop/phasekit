# Execution Modes

The scaffold supports two execution modes. Interactive collaboration is the default; unattended mode is opt-in.

## Mode 1: Interactive collaboration (default)

Used when a human is directly collaborating with Claude on this repository or a downstream project.

### Behavior
- `.claude/settings.json` is the active settings file, with conservative allow/deny lists
- Claude prompts before running unapproved tools
- Hooks (`deny-dangerous-commands.sh`) block dangerous operations
- No assumption of permissive execution

### When to use
- Direct work on the scaffold repo
- Design, implementation, and review conversations
- Any session where a human is actively participating

### Settings strategy
Project settings (`.claude/settings.json`) are checked into the repo and apply to all users. They must remain conservative:
- Allow only safe read/build/test commands
- Deny destructive git operations, secret file reads, and broad deletes
- Hooks provide an additional safety layer

## Mode 2: Containerized unattended (opt-in)

Used when the scaffold runs autonomous phase-gated work inside an isolated container.

### Behavior
- Wrapper scripts (`run-phase.sh`, `run-until-done.sh`) invoke Claude with `--permission-mode bypassPermissions`
- Claude executes without interactive approval prompts
- Phase-gated workflow and approval artifacts still apply
- The container provides isolation boundaries

### When to use
- Automated multi-phase builds
- CI/CD-triggered scaffold runs
- Batch processing of scaffold phases

### How to enable
Unattended mode is activated by running the wrapper scripts:
```bash
# Single phase
./scripts/run-phase.sh ./CONTINUE_PROMPT.txt

# Multi-phase loop
MAX_ITERATIONS=50 ./scripts/run-until-done.sh
```

These scripts pass `--permission-mode bypassPermissions` to Claude. This flag only takes effect when explicitly invoked — it does not change the project settings for interactive users.

### Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_MODE` | `new` | Set to `continue` to resume a previous session. Honored both for direct `run-until-done.sh` invocation and when forwarded through `container-setup.sh run`. |
| `MAX_ITERATIONS` | `50` | Maximum phase iterations for `run-until-done.sh` |
| `PHASEKIT_ITER_RETRY` | `1` | Per-iteration retry budget when the `claude` CLI exits non-zero (e.g. an API-side content-filter trip mid-response, a 5xx, or a transient network failure). Retries reuse the current session via `continue` mode and do not advance the iteration counter. Set to `0` to disable. |
| `AUTO_PUSH` | (unset) | Set to `1` to push after each phase commit. Useful when the project needs CI to fire on each phase, github-pages-as-progress-mirror, or deploy previews. Pushes to the current branch's upstream (`git push` with no args). Push failures are non-fatal — the loop continues; the commit is already local. |
| `SSH_AUTH_SOCK` | (host's value) | When invoked via `container-setup.sh run`, the host's SSH agent socket is forwarded into the container so `git push` to SSH remotes works. Run `ssh-add` on the host first. |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset) | Passed through to the container if set, for HTTPS-remote push workflows that use a Personal Access Token. |

## Settings layering

Claude Code resolves settings in this order (later wins):
1. **Project settings** (`.claude/settings.json`) — checked in, conservative, shared
2. **Local settings** (`.claude/settings.local.json`) — gitignored, user-specific overrides
3. **Command-line flags** (`--permission-mode`) — used by wrapper scripts for unattended mode

### Override guidance
- **Never** make project settings permissive to support unattended mode
- Use `.claude/settings.local.json` for per-user tweaks (gitignored by default)
- Use command-line flags in wrapper scripts for unattended execution
- Container-specific configuration should live in the container setup, not the repo

## Non-interference principle

The scaffold must not make ordinary human collaboration cumbersome.

This means:
- Project settings remain conservative by default
- Permissive behavior lives in local/container config or CLI overrides
- The repo works naturally with Claude for design, implementation, and review
- Autonomous workflow behavior is opt-in, not always-on
- No global heavy-mode is forced on interactive sessions
