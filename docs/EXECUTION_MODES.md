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
| `PHASEKIT_TRACE` | (unset) | Set to `1` to enable `set -x` xtrace in the wrapper scripts (`container-setup.sh`, `run-until-done.sh`, `run-phase.sh`). Every shell command is printed before execution — loud, but useful when diagnosing why the loop took an unexpected branch. Forwarded into the container by `container-setup.sh run`. |
| `AUTO_PUSH` | (unset) | Set to `1` to push after each phase commit. Useful when the project needs CI to fire on each phase, github-pages-as-progress-mirror, or deploy previews. Pushes to the current branch's upstream (`git push` with no args). Push failures are non-fatal — the loop continues; the commit is already local. |
| `PHASEKIT_ITERATION_MODE` | `standard` | Set to `light` for the reduced-ceremony loop (v0.6.0) — see "Light execution mode" below. Set per-session by the outer supervisor (forwarded into the container like `ANTHROPIC_MODEL`); never a committed setting. |
| `PHASEKIT_WRAPUP_SENTINEL` | `artifacts/wrapup-requested` | Path of the soft wrap-up sentinel (v0.6.0). An outer supervisor touches this file a few minutes before its hard session kill; between iterations the loop honors it — commits what stands (verify-gated) and exits 0 instead of starting an iteration the kill would truncate. Stale sentinels are cleared at loop start. |
| `PHASEKIT_SESSION_DEADLINE` | (unset) | Epoch seconds of the supervisor's hard kill (v0.6.1; run-session computes start + MAX_MINUTES and forwards it). Enables deadline-aware pacing: between iterations, if remaining time < max(1.2 × average pass duration this run, 3 min), the loop takes the wrap-up path instead of starting an iteration it likely can't finish. Unset ⇒ behavior unchanged. |
| `VERIFY_MAX_ATTEMPTS` | `3` (standard), `2` (light) | Circuit breaker for the pre-commit verify gate: after this many consecutive failures the loop writes `phase-blocked.json` (light: escalates) and stops. |
| `PHASEKIT_CONTRACTS_DIR` | `/contracts` | Where the provider's contracts tree is readable (v0.7.0). Set by `container-setup.sh` alongside the read-only bind mount it creates from `PHASEKIT_CONTRACTS_MOUNT`; set it yourself to run the gate outside a container. Only consulted when this repo has a `contracts.yaml`. See `docs/CONTRACTS.md`. |
| `PHASEKIT_CONTRACTS_SKIP` | (unset) | Set to `1` to bypass the cross-project contracts gate for one run (v0.7.0). Deliberately separate from `VERIFY_SKIP`, which does **not** disable it: VERIFY_SKIP is the routine hatch for red TDD commits, and letting it also switch off contract authenticity would disarm the gate exactly when a red gate applies the pressure to cheat. Announces itself on stderr; operator-only, never a committed setting. |
| `SSH_AUTH_SOCK` | (host's value) | When invoked via `container-setup.sh run`, the host's SSH agent socket is forwarded into the container so `git push` to SSH remotes works. Run `ssh-add` on the host first. |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset) | Passed through to the container if set, for HTTPS-remote push workflows that use a Personal Access Token. |

### Visibility and logs

`run-phase.sh` invokes the claude CLI with `--output-format stream-json --include-partial-messages --verbose`, so every assistant message, tool call, tool result, and even partial in-flight chunks are emitted as JSONL events in real time. The default `-p text` mode is silent until the final response, which is useless when claude crashes mid-stream (e.g. an API content-filter trip).

Two files are produced per attempt under `artifacts/logs/`:

```
claude-iter-<N>.jsonl           raw stream-json events (full fidelity, forensics)
claude-iter-<N>.log             human-readable rendering of the same stream
claude-iter-<N>-retry<M>.jsonl  M-th retry of iteration N (raw)
claude-iter-<N>-retry<M>.log    M-th retry of iteration N (rendered)
```

The `*.log` file is produced by `scripts/phasekit-log-fmt.sh`, a small jq pretty-printer that turns each JSON event into a labelled line (`[text] ...`, `[tool_use] Bash {"command":"..."}`, `[tool_result] ...`, `[partial] ...`, `[result success] ...`). Non-JSON lines from stderr (such as `API Error: Output blocked by content filtering policy`) pass through unchanged so they still land in the log next to the events.

For a live view of a long-running loop (e.g. one started in a remote tmux session), open a second pane and `tail -F` the current iteration's `.log`:

```bash
tail -F artifacts/logs/claude-iter-3.log
```

After a crash, the most recent `claude-iter-*.log` files contain the rendered transcript of what claude was generating when it failed. If you need more detail than the rendering exposes, run the raw JSONL through the formatter (or `jq`) directly:

```bash
bash scripts/phasekit-log-fmt.sh < artifacts/logs/claude-iter-1.jsonl | less
jq -c 'select(.type == "assistant")' artifacts/logs/claude-iter-1.jsonl
```

`PHASEKIT_TRACE=1` additionally enables `set -x` in the wrapper scripts themselves, so every shell command they run (git commits, verify-gate invocations, artifact cleanup) is printed before execution.

## Light execution mode (v0.6.0)

`PHASEKIT_ITERATION_MODE=light` turns one loop run into the reduced-ceremony
path for small, pre-triaged tasks (single-surface change, low blast radius,
acceptance stateable in a few bullets). Triage happens upstream (the
orchestrator's scoping session); phasekit only executes the grade.

Semantics, relative to a standard run:

- **One collapsed phase.** The prompt is prefixed at runtime with light-mode
  overrides: build + verify + review in a single pass, no strategy-planner or
  architecture-red-team subagents, the code-reviewer still runs inside the
  phase. The session finishes by writing `artifacts/project-complete.json`.
- **Iteration cap 2, verify breaker 2** (`MAX_ITERATIONS` / `VERIFY_MAX_ATTEMPTS`
  defaults; both still overridable).
- **Model split.** Build iterations run whatever `ANTHROPIC_MODEL` the
  supervisor set (typically a cheaper model). Before the final commit, exactly
  one review pass runs on the **default** model (`ANTHROPIC_MODEL` dropped for
  that invocation, logged as `claude-iter-light-review.*`). The reviewer may
  fix defects in place or withdraw the completion.
- **Eligibility requires a real verify gate.** If `scripts/phasekit-verify.sh`
  is absent or still the stub (`PHASEKIT_VERIFY_CONFIGURED` sentinel at `0`),
  light mode is refused with one log line and the run proceeds in standard
  mode. Reduced ceremony only where mechanical verification is strong.
- **Escalation, never grinding.** Two verify-gate failures, any
  `phase-blocked.json`, an out-of-scope (scaffold-class) edit, or the
  iteration cap ends the run with `artifacts/light-escalation.json`
  (trigger + reason + detail + model + iterations used). The orchestrator
  re-queues the remainder as a standard full-ceremony iteration; phasekit just
  stops honestly and leaves the record. Exit codes keep their usual meanings
  (2 = blocked-class, 3 = cap).
- **The verify gate itself is unchanged and mandatory.** Promote gate,
  secret-lint, and scope containment all stay on.

## Loop integrity (v0.6.0)

Two guarantees added to `run-until-done.sh`:

- **Phase-commit atomicity.** `phase-approval.json` persists on disk as the
  durable record of the last approved phase; the loop now commits only
  artifacts (re)written during the current iteration (mtime marker), so a
  stale approval can never sweep later in-flight work into a commit under the
  wrong phase's message. The one exception is deliberate: retrying an
  approval whose verify gate failed last iteration — that staged work belongs
  to the same phase. An iteration that writes no fresh artifact now trips the
  loop contract (exit 1) instead of committing mislabeled work.
- **Soft wrap-up.** See `PHASEKIT_WRAPUP_SENTINEL` above — sessions get a
  chance to end cleanly (commit what stands, verify-gated) instead of only
  ever ending by the supervisor's hard kill.

Two timeout-waste levers added in v0.6.1, both riding the wrap-up path:

- **Deadline-aware pacing.** With `PHASEKIT_SESSION_DEADLINE` set (see the
  env table), the loop refuses to start an iteration it likely can't finish:
  remaining time below max(1.2 × the average pass duration this run, 3 min)
  triggers the same commit-what-stands wrap-up. Simple by design — pass
  durations are tracked per-run only, retried attempts count as passes (a
  conservative average is the right direction), and a missing or malformed
  deadline changes nothing.
- **Wrap-up handoff note.** Every wrap-up that leaves standing work writes
  `artifacts/session-handoff.json` first — `stopped_at_phase` (the last
  *approved* phase), `in_flight` (a one-line summary of the standing paths),
  `verified` (whether the wrap-up verify passed), `next_step` — composed by
  the loop itself, zero extra tokens. When the wrap-up commit happens the
  note lands inside it; when the commit is refused (verify failure, security
  pair) it stays on disk where the next session needs it most. It is an
  ephemeral baton: the next session's orientation (CONTINUE_PROMPT step 1)
  reads it, then deletes it. Durable learnings belong in `docs/LEARNINGS.md`;
  `cleanup_artifacts` deliberately leaves the note alone.

One recovery added in v0.6.3, closing the trap the atomicity fix created:

- **Stranded-artifact recovery.** A session killed after writing
  `phase-approval.json` (or `project-complete.json`) but before its commit
  leaves the artifact stranded: the atomicity gate rightly refuses it in every
  later session, but nothing told the model to rewrite it, so sessions
  re-validated the finished work and exited 1 uncommitted (five sessions
  burned this way on 2026-08-11). At loop start the wrapper now detects the
  stranded signature — the artifact has uncommitted changes in git (a landed
  one is clean; mtimes are deliberately not trusted) — and recovers
  mechanically: a stranded approval is scheduled onto the existing
  verify-gated retry path and lands at the first iteration boundary under its
  own message; a stranded completion is committed immediately (all the usual
  gates apply), finishing the run with zero claude calls. A landed-but-stale
  artifact, clean or with a dirty tree, never triggers recovery — that is the
  v0.6.0 guarantee, unchanged.

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
