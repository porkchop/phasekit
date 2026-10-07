# Execution Modes

The scaffold supports two execution modes. Interactive collaboration is the default; unattended mode is opt-in.

Both modes work in both project layouts. A **pinned** project (v0.19.0 and later) tracks `.phasekit-version` and none of phasekit's engine files: the loop, the hooks, the agents and the process docs run read-only from the engine store, and interactive sessions get the hooks and agents from the phasekit Claude Code plugin. A **vendored** project (before v0.19.0, legacy, supported until migrated) carries the engine in its own tree and wires its hooks in its own `.claude/settings.json`. See "Pinned runs" below and `docs/INSTALL_LIFECYCLE.md`.

## Mode 1: Interactive collaboration (default)

Used when a human is directly collaborating with Claude on this repository or a downstream project.

### Behavior
- `.claude/settings.json` is the active settings file, with conservative allow/deny lists (in a pinned project it holds permissions only)
- Claude prompts before running unapproved tools
- Hooks (`deny-dangerous-commands.sh`) block dangerous operations — in a pinned project through the phasekit plugin (`phasekit plugin install`, done by `install.sh` and `phasekit init`; `phasekit check` warns loudly when it is missing), in a vendored project through its own settings
- Subagents are the plugin's, namespaced `phasekit:<agent>`, in a pinned project; `.claude/agents/` in a vendored one
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
Unattended mode is activated by running the loop through the CLI, from inside the project:
```bash
# Multi-phase loop on the host
MAX_ITERATIONS=50 phasekit loop

# The same loop in the isolated container (docs/CONTAINERIZATION.md)
phasekit run

# A single iteration
MAX_ITERATIONS=1 phasekit loop
```

In a vendored project (legacy) the CLI runs the project's own copies, and the direct script forms still work there: `./scripts/run-until-done.sh`, and `./scripts/run-phase.sh ./CONTINUE_PROMPT.txt` for a single phase.

These scripts pass `--permission-mode bypassPermissions` to Claude. This flag only takes effect when explicitly invoked — it does not change the project settings for interactive users.

### Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_MODE` | `new` | Set to `continue` to resume this project's last recorded conversation by id (`artifacts/logs/claude-session-id`; a new session that re-anchors from the tree when there is none). Honored both for direct `run-until-done.sh` invocation and when forwarded through `container-setup.sh run`. See "Session continuity" below. |
| `MAX_ITERATIONS` | `50` | Maximum phase iterations for `run-until-done.sh` |
| `PHASEKIT_ITER_RETRY` | `1` | Per-iteration retry budget when the `claude` CLI exits non-zero (e.g. an API-side content-filter trip mid-response, a 5xx, or a transient network failure). Retries resume the run's conversation by id (`continue` mode) and do not advance the iteration counter. Set to `0` to disable. |
| `PHASEKIT_TRACE` | (unset) | Set to `1` to enable `set -x` xtrace in the wrapper scripts (`container-setup.sh`, `run-until-done.sh`, `run-phase.sh`). Every shell command is printed before execution — loud, but useful when diagnosing why the loop took an unexpected branch. Forwarded into the container by `container-setup.sh run`. |
| `AUTO_PUSH` | (unset) | Set to `1` to push after each phase commit. Useful when the project needs CI to fire on each phase, github-pages-as-progress-mirror, or deploy previews. Pushes to the current branch's upstream (`git push` with no args). Push failures are non-fatal — the loop continues; the commit is already local. |
| `PHASEKIT_ITERATION_MODE` | `standard` | Set to `light` for the reduced-ceremony loop (v0.6.0) — see "Light execution mode" below. Set per-session by the outer supervisor (forwarded into the container like `ANTHROPIC_MODEL`); never a committed setting. |
| `PHASEKIT_WRAPUP_SENTINEL` | `artifacts/wrapup-requested` | Path of the soft wrap-up sentinel (v0.6.0). An outer supervisor touches this file a few minutes before its hard session kill; between iterations the loop honors it — commits what stands (verify-gated) and exits 0 instead of starting an iteration the kill would truncate. Stale sentinels are cleared at loop start. |
| `PHASEKIT_SESSION_DEADLINE` | (unset) | Epoch seconds of the supervisor's hard kill (v0.6.1; run-session computes start + MAX_MINUTES and forwards it). Enables deadline-aware pacing: between iterations, if remaining time < max(1.2 × average pass duration this run, the cost ledger's pass P90, 3 min) — the two estimates once a pass has run — the loop takes the wrap-up path instead of starting an iteration it likely can't finish. Also arms the deadline watchdog: the measured wrap-up lead, the take-control point and the last-resort commit (v0.13.0/v0.18.0, "Session efficiency" below). Unset ⇒ behavior unchanged. |
| `VERIFY_MAX_ATTEMPTS` | `3` (standard), `2` (light) | Circuit breaker for the pre-commit verify gate: after this many consecutive failures the loop writes `phase-blocked.json` (light: escalates) and stops. |
| `PHASEKIT_CONTRACTS_DIR` | `/contracts` | Where the provider's contracts tree is readable (v0.7.0). Set by `container-setup.sh` alongside the read-only bind mount it creates from `PHASEKIT_CONTRACTS_MOUNT`; set it yourself to run the gate outside a container. Only consulted when this repo has a `contracts.yaml`. See `docs/CONTRACTS.md`. |
| `PHASEKIT_CONTRACTS_SKIP` | (unset) | Set to `1` to bypass the cross-project contracts gate for one run (v0.7.0). Deliberately separate from `VERIFY_SKIP`, which does **not** disable it: VERIFY_SKIP is the routine hatch for red TDD commits, and letting it also switch off contract authenticity would disarm the gate exactly when a red gate applies the pressure to cheat. Announces itself on stderr; operator-only, never a committed setting. |
| `SSH_AUTH_SOCK` | (host's value) | When invoked via `container-setup.sh run`, the host's SSH agent socket is forwarded into the container so `git push` to SSH remotes works. Run `ssh-add` on the host first. |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset) | Passed through to the container if set, for HTTPS-remote push workflows that use a Personal Access Token. |
| `PHASEKIT_PROJECT_DIR` | (unset) | The project an engine outside the tree works on (v0.19.0). Set by `phasekit loop` / `phasekit run` / `phasekit verify` for a pinned project (`/workspace` in the container); unset means the vendored layout. Not exported to the model's own commands or the project's gate. |
| `PHASEKIT_ENGINE_DIR` | (set by the loop) | The engine a running loop's `phasekit` shim runs (v0.19.0); a pin edited mid-run never switches it. |
| `PHASEKIT_ENGINE_DOCS` | (set by the loop, pinned) | The engine's `docs/` directory, exported to a pinned run's sessions. |
| `PHASEKIT_GUARD_PROBE_TIMEOUT` | `120` | Seconds the pinned guard self-check may take before the turn is refused. (`PHASEKIT_GUARD_PROBE` / `PHASEKIT_GUARD_PROBE_TOKEN` are the probe's own channel, set by `run-phase.sh`; never set them yourself.) |
| `PHASEKIT_NO_AUTO_FETCH` | (unset) | `1`: a pinned release the engine store lacks is not installed implicitly; the verb exits 6 naming `phasekit engines install <tag>` (`docs/INSTALL_LIFECYCLE.md`). |

### Pinned runs: the engine, the plugin and the guard self-check (v0.19.0)

Every engine script computes `ENGINE_DIR` from its own location and the project, `ROOT_DIR`, from `PHASEKIT_PROJECT_DIR` when set, else `ENGINE_DIR`. In a vendored project the two are the same directory and nothing below applies except the shim. In a pinned run:

- **The plugin is passed, not installed.** `run-phase.sh` starts every turn with `--plugin-dir <engine>/plugin`, so the hooks and agents come from the read-only engine — the only copy a session can reach; it cannot edit its own guard. The installed plugin is not needed for the loop.
- **The guard self-check (fail closed).** Before EVERY model turn, `run-phase.sh` starts a throwaway, model-free `claude -p --plugin-dir <engine>/plugin` with `PHASEKIT_GUARD_PROBE` set. The plugin's `guard-probe` hook (UserPromptSubmit) checks that all four hooks are present and executable, that the command guard refuses `git reset --hard` (and, under the loop, `git commit`), writes the probe's token to the probe file, and blocks the prompt — so there is no model call and no cost. No token means the plugin did not load: the turn is REFUSED and `run-phase.sh` exits 7 (`phasekit: REFUSING the turn — the guard self-check failed …`). A pinned session never runs without its command guard and stop hook.
- **The prompt names the engine.** The session prompt starts with a `PHASEKIT ENGINE` preamble (the engine's version and read-only path, where its docs are, that `phasekit <verb>` is on PATH, that the subagents are `phasekit:<agent>`, never to copy engine files into the repository), and every `docs/<NAME>.md` it mentions that the project does not have is rewritten to the engine's real path. `PHASEKIT_ENGINE_DOCS` is exported.
- **The pin is not the session's.** The loop never commits a change to `.phasekit-version`: like `.claude/settings.json` and `.github/workflows/`, a staged change to it makes the loop refuse the commit (`artifacts/scope-refusal.json`). It moves only by `phasekit upgrade`. The engine that started an iteration finishes it, and a pin bump on the integration branch reaches an open iteration's work branch only at its merge-back (the pin travels with the branch), so a pin bump needs no rest window.

In **both** layouts the prompts name the CLI — `phasekit verify`, `phasekit scope`, `phasekit facts --json`, `phasekit contracts refresh` — never a path into the tree, and the loop puts a `phasekit` shim on PATH for its sessions that runs that loop's own engine (`PHASEKIT_ENGINE_DIR`); in a vendored project that is the project's own copy, exactly what the old script path ran.

### Session continuity (v0.18.8)

A run is ONE conversation. Its first turn starts a new session — the id is chosen up front
(`claude --session-id`) and recorded in `artifacts/logs/claude-session-id` before the model
starts — and every later turn (the next iteration, a CLI retry, the no-verdict request)
resumes that id with `claude --resume <id>`. The loop never uses `claude -c`: "the most recent
conversation in this directory" is not this run's conversation when several projects share one
config volume and one `/workspace` path (2026-10-06: three xmeo-v3 turns had resumed
foundry-orchestrator sessions). After each turn, the id the CLI reports in its `system`/`init`
event is recorded (it wins if the two ever differ).

Fail safe, never another conversation: when there is no usable recorded id, or the CLI cannot
honour the resume (exit 1, no `init`, and the CLI's "No conversation found with session ID" —
an id from elsewhere, a transcript directory that was lost), the turn starts a NEW session whose
prompt first says so
and points it back at the tree (`artifacts/session-handoff.json`, `docs/PHASES.md`, `git
status`/`diff`/`log`); both attempts stay in the turn's logs. Nothing else is read that way: a
turn ended by a signal, an auth or credit failure (the run's conversation is kept for the
retry), or a refusal after the loop began taking the session back (the wrap-up sentinel
`artifacts/wrapup-requested`, a take-control or completion yield) — a turn the loop ended stays
ended. The light-mode final review is a side conversation: a new session that records
nothing. `CLAUDE_MODE=continue` at a run's start resumes the last run's recorded conversation the
same way. The id file is the loop's own state: never committed, never in `git status`.

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
bash "$(phasekit docs)/../scripts/phasekit-log-fmt.sh" < artifacts/logs/claude-iter-1.jsonl | less   # the engine's formatter
jq -c 'select(.type == "assistant")' artifacts/logs/claude-iter-1.jsonl
```

(In a vendored project the formatter is also at `scripts/phasekit-log-fmt.sh` in the tree.)

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

## The Stop hook, and its off switch (v0.8.0)

`.claude/hooks/require-verdict.sh` blocks an autonomous session from ending
its turn without a verdict artifact (`phase-approval.json`,
`phase-update.json`, `phase-blocked.json`, `project-complete.json`, or another
terminal signal). It exists because a session that returns a final message
kills every background task it started and — before this hook — could walk
away leaving green work uncommitted; three consecutive sessions did exactly
that on 2026-08-16. On a healthy session it blocks zero times.

**Operational lever: `PHASEKIT_STOP_BLOCK_LIMIT`** — how many times the hook
may block per iteration before stepping aside (default `2`).

- **`PHASEKIT_STOP_BLOCK_LIMIT=0` disables the behavior entirely** (the guard
  is `blocks >= limit`, so zero steps aside on the first check). Reach for
  this if the hook ever blocks a legitimate stop or fights the loop.
- **The env var is the off switch — file surgery is not.** In a pinned
  project the hook lives in the read-only engine's plugin and cannot be
  removed per project. In a vendored project, deleting the hook file or its
  `Stop` entry in `.claude/settings.json` silently un-deletes itself: the
  vendored `phasekit upgrade` re-syncs missing scaffold hook registrations by
  design (that sync is what fixes the shipped-but-unwired failure class, and
  it is deliberately not overridable per project).
- The hook is inert outside the autonomous loop: it exits immediately unless
  the loop's `PHASEKIT_VERDICT_ARTIFACTS` / `PHASEKIT_ARTIFACTS_DIR` /
  `PHASEKIT_ITER_MARKER` environment is present, so interactive sessions
  never see it.

## Branch-per-iteration + squash-to-target (v0.14.0)

Opt-in, per session, via `PHASEKIT_SQUASH_TARGET=<integration branch>`
(`main`, `master`, …). Unset — the default, and what every standalone user
gets — leaves every commit path exactly as before.

**Why.** On a long-running autonomous project the integration branch fills
with checkpoints, wrap-ups, strand commits and heals: nothing on it is a
bisect or revert unit. With the target set, the loop keeps all of that on a
**work branch** and the integration branch gains **one commit per approved
phase**.

**How it works.**

- **Loop start:** HEAD must be on a work branch. Standing on the target, the
  loop creates one — `PHASEKIT_WORK_BRANCH` if given (a supervisor passes
  `iter/<N>-<slug>`), else `iter/<UTC stamp>` — and checks it out. Already on
  a work branch: nothing happens. On some other branch than the one named:
  the loop blocks rather than guess.
- **Every commit the loop makes lands on the work branch** — checkpoints
  (`phase-update.json`), wrap-up, the deadline watchdog's strand commit,
  upgrade and heal commits. Off-box durability is the supervisor's push of
  that branch, as today (`AUTO_PUSH=1` pushes both the branch and the target).
- **At an approval-class commit** (`phase-approval.json`,
  `project-complete.json`), after the branch commit passes the usual gates,
  the loop squashes: one commit on the target whose tree is the branch tree
  and whose message is the artifact's `suggested_commit_message` plus a
  trailer `phasekit-squash: <work-branch>@<short-sha>`; then a **merge-back**
  commit on the work branch (`chore(workflow): merge-back <target> after
  squash (phasekit v0.14.0)`) recording the target's new tip as a parent, so
  the next squash diffs only the next phase. Both are plumbing operations
  (`commit-tree` + old-value-guarded `update-ref`): the index and working
  tree are never touched, nothing is rewritten, nothing is force-pushed, and
  the branch keeps its full checkpoint history.
- **At completion** HEAD rests on the target (trees are identical, so no file
  changes); the work branch is kept for forensics — retention is the
  supervisor's business.

**Squash-integrity guard (fails closed).** The target may only move through
the loop's own squash, so its tip must be an ancestor of the work branch. A
hand commit, a hotfix, or a hand-merge on the target — or, best-effort, on
`origin/<target>` (the loop fetches when it can; the last-seen remote tip
counts when it cannot) — makes the next squash **refuse**: the branch commit
stays, the target is untouched, `artifacts/phase-blocked.json` is written
with `blocker_kind: branch-integrity` and a `next_step`, and the loop exits
2. Every later loop start re-attempts the squash **before spending a token**
and blocks again until an operator merges the target into the work branch
(or resets it). A squash the target is still owed for a different reason —
an approval that landed through a wrap-up or strand commit — is caught up at
the next loop start behind the verify gate.

**Interrupted mid-squash** (a kill between the two ref updates: target
advanced, merge-back missing) is recognised at the next boundary by the
squash commit's own trailer — the named branch commit must be an ancestor of
HEAD and carry the target's tree — and completed idempotently, even if the
branch gained commits in between (an intake, a strand).

**Known limitation.** If an approval's squash was refused and the completion
was then swept into the same branch commit, the catch-up squashes both under
the completion's message — one commit for the last phase plus completion,
not two. Recorded rather than solved (v0.14.0 review); it only follows an
operator-resolved integrity block.

**Supervisor contract** (pinned in `contracts/interface.json` conventions,
`branch-per-iteration-squash`): *fully merged* ⇔ `git diff --quiet <target>
<work-branch>`; a completed iteration whose final squash was refused is not
resting. The supervisor pushes both refs after a session (`git push -u origin
HEAD` + `git push origin <target>` — a plain `git push` fails on a fresh
work branch with no upstream and leaves the session's commits host-local),
fetches `origin/<target>` before a session so the remote guard sees fresh
data (inside a credential-less container the fetch always fails), and decides
branch retention.

## Boundary state (v0.14.5)

Every "did this phase land?" question the loop or a supervisor used to answer
by inference — file presence, mtimes, git status, the squash trailer — is
answered by **one record**, `artifacts/boundary-state.json` (transient for
git: never committed, hidden from `git status`), written before and advanced
after each step of **one landing sequence**:

| step | name | proven by |
|---|---|---|
| 0 | `idle` | a new iteration began; nothing approved yet |
| 1 | `approved` | an approval-class artifact is on disk (`final_phase` read here) |
| 2 | `committed` | `phase-approval.json` is clean in git — committed under its own message |
| 3 | `recorded` | `project-complete.json` is committed (final boundaries only; the loop writes it from a `final_phase: true` approval when the session did not) |
| 4 | `squashed` | the target carries the approval-class blobs (squash mode; trivial otherwise) |
| 5 | `merged-back` | the target tip is an ancestor of the work branch |
| 6 | `armed` | `ready-to-deploy.json` observed — presence and mtime recorded; the loop never writes it |
| 7 | `rested` | tree clean; on a final boundary HEAD is on the target and the consumed batons are gone |

`land_boundary` in `scripts/run-until-done.sh` is the only code that advances
the record. It walks the steps from the one its caller can prove; each step
has a *proof* (git and disk alone) and an *action* (the mechanism that used to
decide for itself — `commit_pending_approval_first`, `commit_from_artifact`,
`squash_to_target`, `repair_half_squash`, `rest_on_target`,
`clear_consumed_batons_at_completion`). A proven step is recorded and skipped;
an unproven one gets its action and must then prove. Every entry point — the
iteration commit, the completion commit, a stranded artifact at loop start,
the catch-up squash — is a call to this function, so a session killed at any
instant leaves a tree the proofs describe exactly, and **recovery is the same
call at the next loop start, before any model turn.** A red verify stops the
walk at the last proven step with `phase-verify-failed.json` on disk; a
refused squash stops it with `phase-blocked.json` (branch-integrity) — never
dirty-and-silent. The deadline watchdog and the wrap-up fall-through record
`killed_after` (the step observed at the kill) so the next session's first
line says where the sequence stood. Every function the watchdog fork reaches
is defined before the fork — the watchdog is a background subshell armed
before the first model turn, and a helper defined after the arm site is
`command not found` on the kill path (run 682, 2026-09-09) — pinned by
construction in `tests/test_deadline_watchdog.py` (`ForkVisibility`, v0.14.6).
v0.14.7: the record step 3 synthesizes carries `iteration` verbatim from the
approval (`null` when the approval names none — the key is always present), and
the watchdog's last-resort commit no longer deletes a session-authored
`project-complete.json` that HEAD does not yet carry — "restore to HEAD" of a
record HEAD lacks was a deletion; it now stays on disk (when it parses — a torn
file from an interrupted writer is still deleted), uncommitted, exactly as
the never-landed verdict the next loop start's recovery looks for first
(orchestrator iteration 122, three kills, three erased records). An untracked
`ready-to-deploy.json` is still deleted: it is a deploy claim no landing step
owns, and an unverified one must not survive into a later commit.

Fields a supervisor reads (pinned in `contracts/interface.json`): `step`,
`step_name`, `final`, `phase`, `pass`, `iteration`, `sha_at_step` (`"2"`/`"3"`/`"5"`/`"7"`
→ the work-branch commit, `"4"` → the target tip), `verify_memo` / `verify_red`,
`deploy`, `killed_after`/`killed_mode`, and `previous` (the boundary that
ended before the current idle record began — what last landed). `pass` is this
session's index inside the `MAX_ITERATIONS` loop (1, 2, …) — it was written as
`iteration` until v0.14.7, so every live record read `iteration: 1`. `iteration`
(v0.14.8, `schema: 2`) is the supervising iteration's label copied verbatim from
`artifacts/iteration-mode.json`'s `iteration` when the supervisor declared one,
else `null` — never derived from the branch name, because phasekit does not
define iterations; `pass` is 0 only when loop-start recovery opened the record
before the first pass. A schema-1 record has no `pass` and its `iteration` is
the pass counter; `previous` keeps the archived boundary's own `schema`, so a
schema-1 `previous` can sit under a schema-2 record until the next boundary
lands — a consumer branches on the schema of the block it reads. *Step 7 with `final: true` is "the completion
landed and HEAD rests"; step 7 with `final: false` is a phase boundary landed;
step < 7 is a boundary the previous session did not finish.* A record whose
shas neither HEAD, the target nor the recorded work branch reach (an operator
moved HEAD) is named, discarded, and only git's own evidence counts.

**Completion is terminal (v0.14.9).** A final boundary whose completion has
landed on the target — the record says `final: true` at step 6 or 7 — is the
iteration's terminal state: the session exits 0 at rest and nothing else runs.
No next pass (a pass that begins with the record already complete for this
iteration — same supervisor label on a schema-2 record and the marker, else the
same work branch — exits at once, with zero model turns), no wrap-up or pacing
commit, no watchdog last-resort commit, no `phase-blocked.json`, no baton. Step
7 (`rested`) is hygiene, not completion: a verify gate that rewrites tracked
files after the completion commit staged them leaves the tree dirty, and the
loop names that dirt and leaves it exactly as it is — it never re-enters the
loop over it (the two live shapes: xmeo iteration 50's pacing wrap-up committed
the gate's re-measurement straight onto the target; iteration 56's next pass
found no next phase and wrote `phase-blocked.json`). **v0.14.10 closes that
cause at the source:** the verify gate is read-only over the tree — a rewrite
is the gate's *footprint*, restored and red at the commit, never at rest (see
`docs/QUALITY_GATES.md` "Pre-commit verification gate"), so the completion
never lands over gate noise and the project never rests with it. Step 7
unproven now means dirt of another origin — still named, still left exactly
as it is, never re-entered over. To resume
work on a complete project, remove `artifacts/project-complete.json` (the
completion record) and re-run; a supervisor's next-iteration intake does exactly
that, and a standalone squash-mode run without `PHASEKIT_WORK_BRANCH` gets a
fresh work branch and is never "this iteration" anyway. Step 7 with
`final: false` is a phase boundary and the loop continues to the next phase
exactly as before.

**Nothing written after the completion commit rests (v0.18.1).** The completion
is terminal, so nothing written after the commit that lands it is the
iteration's work (foundry-orchestrator iteration 142: the model committed the
record itself, then its turn — and the light review's after it — kept editing
a tracked test file; the tree could not rest and a human settled it). Two
halves: (a) every model turn runs under a **completion guard** — when a commit
made during the turn lands a completion record that claims (a carried record a
checkpoint swept in unchanged claims nothing) and the tree is clean (the commit
carried the turn's work — a record-only commit is not the end: the model may
commit the rest next), the loop ends the turn (the
take-control SIGTERM, below; `completion guard: …` in the log) and the model
gets no further tool call in it; in branch-per-iteration mode the light review,
which precedes the final commit, does not run over a completion the build turn
committed with the whole tree (over a record-only commit, and in plain mode, it
runs as before, and its re-written record lands the work through the
verify-gated completion commit). (b) the walk snapshots the tree at the
completion commit (the guard's observation, or its own commit) and, at its
start, at step 3
(before the squash's gate) and again before the rest is proven, restores every
path written AFTER it — tracked paths to the committed bytes, untracked ones
removed — names them (`post_completion` in the record, stderr) and keeps their
bytes under `artifacts/logs/post-completion/<stamp>/` (a path whose bytes
cannot be kept is not restored). "After" is known by observation, never by a
file's time: the loop snapshots the tree only when it knows the state — the
guard when it sees the completion committed on a clean tree, the walk right
after its own commit — so whatever appears after that (a write racing the
signal, a process the turn left running) is after. A turn the loop dispatches
(a repair pass, the review) drops the snapshot first and is never residue. A
completion never seen clean (a record-only commit over older work) is not
judged — its dirt is named by the step-7 line and left as is. It corrects and
never refuses: a landing is never blocked by it.

**The loop owns every commit (v0.18.2).** Under the loop the command guard
refuses a model's git writes to this repository (docs/QUALITY_GATES.md "The
loop owns every commit"), so the completion is the loop's to commit — and
whoever committed it, the walk checks the whole tree: step 3 (`recorded`) is
proven only on a clean tree; work the committed record's commit did not carry
lands through the loop's own verify-gated completion commit (the body names
the commit it completes), or stays in the tree, named (`unlanded` in the
record, stderr), with the walk stopped at step 3 for the next turn to repair.
The catch-up squash judges only a worktree that equals HEAD's tree (decided:
commit first, then squash — a difference waits for the next verify-gated
commit), and in plain mode step 4 means "HEAD's tree passed the verify gate":
the loop's own green verdict recorded for exactly HEAD's tree (the verify
memo), else the gate runs on HEAD's exact tree before the boundary counts.

The generated test `tests/test_boundary_state.py` SIGKILLs a real session of
the shipped loop at every step boundary (before and after the record
advances), for every entry point, in both modes, for final and non-final
boundaries, with a green and a red gate at the resume — and asserts the
sequence's owed properties each time. `PHASEKIT_BOUNDARY_KILL_PROBE` is that
test's fault-injection knob; it is inert unless set.

## Session efficiency (v0.18.0)

foundry-meta `designs/DESIGN-session-efficiency.md` (approved 2026-09-28). Sessions ran out of time while closing out — every timeout since 09-14 logged "sentinel never observed" — because close-out cost was never measured, the wrap-up lead was a fixed percentage, the loop depended on the model hearing a nudge, recovery after the kill broke the tree in a way the project's own checks caught, and the iteration's facts were rebuilt by the model from copies. One pass over the chain:

**The cost ledger.** `artifacts/logs/cost-ledger.json` (under the loop's logs/: never committed, persists across sessions; priors on a fresh clone) keeps the last 20 samples of: the gate's wall time per tier (`g_full`, `g_fast`), the landing's cost minus its gate (`w`), the model's close-out from nudge to yield (`m`), the light review (`r`), a pass (`pass`), and the last 20 sessions. Every session under a deadline prints `phasekit-cost: {…}` — the P90s, the lead in force, `at_cap`, and `heavy` (the full tier's P90 over 12.5% of the bound, or the needed lead over the cap). A supervisor reads that line; it never recomputes the numbers.

**The lead, one formula, one home.** `T_y = G_full + W + 60 s` (take-control), `L = T_y + M (+ R in light mode)` (the sentinel), clamped to [300 s, 25% of the span] — the cap is a doctrine boundary: above it the project gets push-back (the `heavy` fact), never a wider lead (fork F1); the loop never changes the session bound (fork F2). `deadline watchdog: armed — sentinel at T-Ls, last-resort commit at T-60s (span Ss), take control at T-Tys` is the line a supervisor reads the lead from.

**The loop takes control back.** At T-T_y (light mode: the build turn at T-(T_y+R), so the review still fits) a model turn that has not yielded is ENDED — SIGTERM to the `claude` process whose pid `run-phase.sh` writes to `artifacts/logs/claude.pid` — after an `artifacts/logs/.deadline-yield` marker. The loop reads that as a wrap-up at an iteration boundary: no CLI retry, no verdict retry; a verdict the turn left lands through the usual verify-gated path, and the session wraps up (verify-gated; red falls through to the labelled wip). The session log says `deadline watchdog: took control`; if the watchdog could not write its marker, a turn that ended non-zero across its take-control instant is still read as taken (`took control (inferred …`, v0.18.1), never retried as a CLI failure. Probed before it was built (scaffold-runner, claude 2.1.282): SIGTERM ends the turn in under a second, kills the running tool child with it, keeps every completed write, leaves no `index.lock`. The last-resort commit at T-60 s stays the SIGKILL stage.

**The model's verify counts.** `phasekit verify` runs the gate exactly as the commit will (same preparation and staging, the contracts gate, the footprint, the memo). On a locked index it runs nothing and exits 2, naming the `index.lock` and what to do (v0.18.1). Close-out order: memory writes first, the verdict, `phasekit verify`, end the turn — the commit then reuses the green verdict for the exact tree instead of running the full tier again (light mode: the builder runs the fast tier before writing the record; the reviewer the full one).

**The nudge reaches the right agent.** The wrap-up nudge is once per iteration **per agent** (the hook payload names a subagent's calls; a subagent can no longer spend the main agent's nudge — xmeo run 999), and PostToolUse repeats it at most once per 60 s.

**Recovery keeps the tree coherent.** On every unverified commit path (the watchdog's kill, the wrap-up fall-through, the gate-pending index commit) `ready-to-deploy.json` and `project-complete.json` are UNSTAGED — never rewritten, never deleted: the wip carries HEAD's copies, the session's stay on disk, and the next start's verify-gated landing stages them with the rest of their coherent tree (restoring one half of a coherent pair is what reddened xmeo's consistency checks). A refused landing never leaves a verdict artifact staged; a refused COMPLETION record is carried on disk into the repair turn (claiming nothing until the session re-writes it), never deleted into an `AD` state.

**Iteration facts are the loop's.** Every loop commit carries `Phasekit-Iteration:` / `Phasekit-Phase:` / `Phasekit-Kind:` trailers (read them with `git log --format='%(trailers:key=Phasekit-Iteration,valueonly)'`, never the subject); approval-class subjects get a generated `iteration N phase P:` prefix (standalone `phase P:`), a stale one corrected; records are stamped (`iteration`, `base`, `final_phase`) and their deferrals composed from `artifacts/deferrals.json` (QUALITY_GATES "The deferral ledger"); every approval landing commits `artifacts/iterations/<N>/<phase>.json`, the phase's changed paths with content hashes; `phasekit scope [--iteration N] [--phase P] [--json]` answers "what changed" from the base and HEAD. A fact that cannot be derived is left as written with a WARN — never a refusal (fork F5).

**Hermetic tests.** A test reads the tree, never git history (QUALITY_GATES "Hermetic tests"); after the gate the loop names test files that appear to read history, once per session, advisory only.

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
3. **Command-line flags** (`--permission-mode`, and `--plugin-dir` in a pinned run) — used by wrapper scripts for unattended mode

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
