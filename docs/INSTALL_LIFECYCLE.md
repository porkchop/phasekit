# Install Lifecycle

This document describes how phasekit is installed on a machine, how a project is pinned to a phasekit release, how a pinned project is checked and upgraded, and how a vendored project is migrated. The pinned lifecycle is implemented in `scripts/phasekit-pin.py` (called by the `phasekit` CLI, `scripts/phasekit.sh`); the vendored lifecycle, kept for projects created before v0.19.0, in `scripts/enrich-project.py`.

## TL;DR

```bash
# Once per machine: the CLI, the engine store, the Claude Code plugin
curl -fsSL https://raw.githubusercontent.com/porkchop/phasekit/master/install.sh | bash

# From inside a project (verbs act on the current directory):
phasekit init [profile]     # a new pinned project: .phasekit-version + the project's own files, one commit
phasekit check              # pinned: pin installed? engine files tracked? stale hook wiring? plugin present?
phasekit upgrade            # pinned: a pin bump through the project's gate, one commit
phasekit migrate            # a vendored project -> pinned (exact, gated, one commit, never pushed)
phasekit engines list       # the engine store
phasekit plugin status      # the Claude Code plugin
phasekit status             # current phase state (derived from workflow artifacts)
phasekit channel            # show/set the self-update channel (stable|edge|<ref>)
phasekit self-update        # move the phasekit install along its channel (and install the release it lands on)
```

## Two layouts: pinned and vendored

| | Pinned (v0.19.0 and later) | Vendored (before v0.19.0) |
|---|---|---|
| What the project tracks | `.phasekit-version` (one release tag) and its own files | a copy of the engine: `scripts/run-until-done.sh`, `.claude/hooks/`, `.claude/agents/`, the process docs, `.devcontainer/`, `.scaffold/manifest.json`, ... |
| Where the engine runs from | the engine store, read-only, outside the tree | the project's own tree |
| Hooks and agents | the phasekit Claude Code plugin | wired in the project's `.claude/settings.json` |
| `phasekit upgrade` | a pin bump through the gate | a 3-way re-provision of the vendored files through the gate |
| `phasekit check` | exit 0 / 3 / 6 (below) | manifest drift |

The CLI decides by presence, and the pin wins: a project that carries `.phasekit-version` is pinned (a stray `scripts/run-until-done.sh` in its tree never runs, and `phasekit check` reports it as a leftover); one with `scripts/run-until-done.sh` and no pin is vendored. Every verb works in both. Vendored projects are legacy, supported until migrated; their lifecycle is in [Legacy: the vendored lifecycle](#legacy-the-vendored-lifecycle-supported-until-migrated).

Inside the engine, every script computes `ENGINE_DIR` from its own location and `ROOT_DIR` (the project) from `PHASEKIT_PROJECT_DIR` when set — the pinned case, which the CLI and the container set — else `ENGINE_DIR`. In a vendored project the two are the same directory, so nothing changes there.

## The canonical clone (source of truth)

`install.sh` clones phasekit to `${XDG_DATA_HOME:-~/.local/share}/phasekit` (`PHASEKIT_HOME` overrides) and writes a `phasekit` launcher to `~/.local/bin` that runs the CLI from that clone (under an isolated venv). That clone is the source of every engine in the store (its release tags) and the CLI you run. It never runs a project itself: it is writable and moves with `self-update`, so a project always runs from an immutable engine in the store — even when its pin names the tag the clone sits on. `phasekit self-update` (or re-running the installer) moves the clone along its channel (see below).

### Update channels (stable / edge)

The clone follows a **channel** that decides which ref `self-update` moves it to (full rationale in `docs/adr/ADR-0002-self-update-channels.md`):

- **`stable`** (default) — the latest `v*` release tag. Recommended for projects that consume phasekit; reproducible and reviewed.
- **`edge`** — the tip of the default branch (`origin/master`). For developing phasekit itself or riding fixes before a release. **Opt-in and loud:** `self-update` prints an "unreleased" warning, because on `edge` the CLI you run is unreleased. A pinned project still runs only release tags.
- **`<ref>`** — an explicit pin to a tag or sha; does not auto-advance.

```bash
phasekit channel            # print the current channel (default: stable)
phasekit channel edge       # follow origin/master from now on
phasekit channel stable     # back to release tags
phasekit self-update        # apply the current channel
```

The channel is persisted in `<clone>/.phasekit-channel` and is the single source of truth shared by `self-update` and `install.sh`. The installer also sets it from `PHASEKIT_REF`: the default branch → `edge`, a release tag → `stable`, anything else → a pin. With no `PHASEKIT_REF`, re-running the installer follows the persisted channel. Existing installs with no channel file default to `stable`, so behavior is unchanged until you opt in.

When `self-update` lands on a release tag, it also installs that release into the engine store, so projects can bump to it and pins to it resolve offline.

> Note: `phasekit check-version` is not yet channel-aware — on `edge` it still reports against release tags. Tracked as a fast-follow in ADR-0002.

## The pin: `.phasekit-version`

One line, one release tag (`vMAJOR.MINOR.PATCH`, `v0.19.0` or later — an older tag cannot run a project from outside its tree and is refused). It is the only engine fact the repository carries, and it is project state:

- **Commit it.** `git show <commit>:.phasekit-version` names the engine that built any commit; `phasekit engines install <tag>` gives back its exact bytes.
- **It moves only by `phasekit upgrade`.** The loop never commits a session's change to it: like `.claude/settings.json` and `.github/workflows/`, a staged change to `.phasekit-version` makes the loop refuse the commit (`artifacts/scope-refusal.json`). A session cannot choose its successor's engine.
- **The engine that started an iteration finishes it.** The loop puts a `phasekit` shim on PATH that runs its own engine (`PHASEKIT_ENGINE_DIR`), so a pin edited mid-run never switches engines.

## The engine store

Each installed release is one directory, `<store>/<tag>/`: a `git archive` of the tag's commit from the canonical clone, plus `.engine-version` (the tag) and `.engine-commit` (the full commit sha), with every file and directory read-only. The store is `<canonical clone>/engines/` — `~/.local/share/phasekit/engines/` for a standard install — or `PHASEKIT_ENGINE_STORE` when set.

```bash
phasekit engines install v0.19.0   # fill the store for one tag (idempotent)
phasekit engines list              # the installed engines
phasekit engines path v0.19.0      # print its directory (exit 6 when not installed)
```

An engine is about 3 MB. phasekit never prunes the store; keep every tag any branch of any project pins. To remove one by hand, make it writable first (`chmod -R u+w <store>/<tag>`).

## Auto-fetch, and what it trusts

A pinned release the store lacks is installed the first time a verb needs its engine (`loop`, `run`, `container`, `verify`, `scope`, `contracts`, `hook`, `docs`, `init`, `upgrade`, `migrate`), so a fresh clone of a pinned project runs with no extra step. `phasekit check` never fetches: it reports.

What is fetched, from where, and how it is checked:

- **Only release tags.** The pin must match `vMAJOR.MINOR.PATCH` and be `v0.19.0` or later; anything else is refused before git is touched.
- **From the canonical clone first.** If the clone already holds the tag, nothing touches the network: the engine is `git archive` of that tag's commit. Git's object store is content-addressed, so the bytes are exactly the tree the tag names.
- **Then from the clone's own `origin`.** If the clone lacks the tag, phasekit runs `git fetch --tags origin` in the clone — the remote you installed from (`PHASEKIT_URL` at install time) — and archives the tag from there. Nothing else is downloaded, and never from a URL a project names. A tag the clone already holds is never re-fetched, so a tag moved upstream does not change an installed engine.
- **Extracted defensively.** Members with absolute paths or `..`, anything but files, directories and symlinks, and symlinks that point outside the engine are refused. The engine is extracted into a temporary directory beside its final path, must contain `scripts/run-until-done.sh`, gets `.engine-version` and `.engine-commit`, is made read-only, and is renamed into place in one step, so no reader sees half an engine. A directory at the final path that is not a complete engine for that tag is refused (move it aside and install again).
- **Not checked:** tag signatures. A tag is trusted as the clone's `origin` serves it. Release tags are never moved or deleted (policy; the command guard also refuses `git tag -d` / `git tag -f` in sessions).

**Offline opt-out.** `PHASEKIT_NO_AUTO_FETCH=1` makes nothing implicit: a verb whose pin is not installed fails with exit 6 and names `phasekit engines install <tag>` (and, when the clone lacks the tag, `git -C <clone> fetch --tags`). An explicit `phasekit engines install` still archives a tag the clone holds.

## The Claude Code plugin

Interactive Claude Code sessions in a pinned project get phasekit's hooks and agents from the plugin in the engine's `plugin/` directory:

- **Hooks.** The same four hooks as a vendored project's `.claude/settings.json`: the command guard `deny-dangerous-commands` (PreToolUse, Bash), `wrapup-nudge` (PreToolUse and PostToolUse), `compact-reanchor` (SessionStart, compact) and `require-verdict` (Stop), all through `plugin/hooks/run-hook.sh`, plus a `guard-probe` hook on UserPromptSubmit that only the loop's self-check uses.
- **Agents.** Namespaced `phasekit:<agent>` (`phasekit:project-lead`, `phasekit:code-reviewer`, ...).
- **Scope.** The dispatcher is inert outside phasekit projects (a repository with no `.phasekit-version`, outside the loop). In a vendored project whose own `.claude/settings.json` wires a hook to a file that exists, the plugin steps aside for that hook, so nothing fires twice; a wiring whose file is gone does not make it step aside.

```bash
phasekit plugin install    # claude plugin marketplace add <canonical clone>; claude plugin install phasekit@phasekit
phasekit plugin status
```

The install is user scope and idempotent. The marketplace is the canonical clone as a directory marketplace, which Claude Code loads in place, so the plugin moves with `self-update`. `install.sh` and `phasekit init` install it; `PHASEKIT_NO_PLUGIN=1` (or `init --no-plugin`) skips that. Without the `claude` CLI the step is skipped with a note.

The loop does not depend on the installed plugin: it passes the engine's own plugin with `--plugin-dir` to every turn, and refuses a turn whose guard self-check fails (`docs/EXECUTION_MODES.md`).

## `phasekit init`

```bash
phasekit init [profile] [--pin TAG] [--no-commit] [--no-plugin]
```

Creates a pinned project in the current directory (running `git init` if it is not a repository):

- writes `.phasekit-version` — this phasekit's own release, or `--pin TAG` (an installed or fetchable release, `v0.19.0` or later); a phasekit that is not on a release tag requires `--pin`;
- installs that engine into the store if needed;
- writes the project's own files for the profile: `docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, `docs/PROD_REQUIREMENTS.md`, `docs/LEARNINGS.md`, `docs/CONVENTIONS.md` (stack profiles), `docs/project/QUALITY_GATES.md`, `.claude/CLAUDE.md`, `.claude/settings.json` (permissions only, no hooks), `AGENTS.md`, `scripts/phasekit-verify.sh` — never overwriting a file that exists (it prints `keep (exists)`), so it also adopts an existing repository;
- commits exactly those paths in one commit (`chore(phasekit): init vX.Y.Z (<profile>)`) unless `--no-commit`; staged changes in the index make it refuse first;
- installs the plugin unless `--no-plugin`.

It writes no engine file. On a vendored project it refuses and points at `phasekit migrate`; on a pinned one it says so and does nothing.

## `phasekit check` (pinned)

Read-only. Exit codes:

| Exit | Meaning |
|---|---|
| 0 | clean: the pin is installed, no engine path is tracked, no stale hook wiring |
| 3 | a leftover: a path the engine provides (scaffold class, or `.scaffold/`) is tracked in the project, or `.claude/settings.json` still wires vendored `.claude/hooks/` paths |
| 6 | the pinned release is not installed here; the message names `phasekit engines install <tag>` (`check` never fetches) |

A missing plugin is a loud `WARNING` (an interactive session in the project would run without the command guard), not an exit code: a CI machine runs no interactive sessions.

## `phasekit upgrade` is a pin bump

```bash
phasekit upgrade [--to TAG] [--push]
```

1. Picks the target: `--to TAG`, or the newest release (`v0.19.0` or later) known on this machine — the canonical clone's tags, the store, the running phasekit. Run `phasekit self-update` first to learn about a newer release. Nothing newer: it says so and exits 0.
2. Refuses when the index holds staged changes or `.phasekit-version` has uncommitted changes.
3. Installs the target engine (auto-fetch rules above).
4. Writes `.phasekit-version` and runs the project's own gate under the **new** engine — the v0.16 upgrade gate, unchanged: `PHASEKIT_UPGRADE_VERIFY` chooses `auto`, `container`, `host` or `off`, a gate that writes into the tree is red.
5. Green: commits one line, `chore(phasekit): pin vA -> vB`. Red: the pin is put back, nothing changes, exit 4.
6. Pushes only with `--push` (a failed push is a note; the commit stays local).

There is nothing to merge, no keep-local and no drift: the engine is not in the tree.

### The pin travels with the branch

A pin bump can land at any time, including during an open iteration. The running iteration keeps the engine it started with (its loop and its shim), and a pin bump committed on the integration branch reaches an open iteration's work branch only at that branch's merge-back. So there is no "upgrade only at rest" window for a pinned project.

## `phasekit migrate`

```bash
phasekit migrate [--dry-run] [--discard-local] [--force] [--pin TAG]
```

Converts a vendored project (one with `.scaffold/manifest.json`) to a pinned one:

1. **Preconditions.** At least one commit; a clean working tree (it refuses with the dirty paths, exit 2). The pin is this phasekit's own release, or `--pin TAG`.
2. **Local edits.** If an engine file the manifest records as `scaffold` class was edited since phasekit wrote it, or carries a standing keep-local, it lists them and refuses (exit 2): the engine's copy would replace them. Move what you need into project-owned files (`docs/project/<NAME>.md` is the companion of a process doc) and re-run, or pass `--discard-local`.
3. **The pre-flight (v0.19.2).** It scans the project's own tracked code files — tests, scripts and source, never the engine files it deletes — for reads of an engine path (the manifest's `scaffold` class, `contracts/interface.json` included; not `.scaffold/manifest.json`, whose name a supervisor's fixtures share — the gate catches a read of the project's own) by its in-tree path, with the scaffold-reads lexer (a path in prose, a comment, a planted sample, a forbidden-path list or `fixtures/` is data, not a read). Any found: it refuses (exit 2), changing nothing and running no gate, and prints each `file:line: path` and the remedy: read the contract through `$PHASEKIT_CONTRACT` (or `phasekit facts --path` by hand) — `docs/QUALITY_GATES.md` "Tests read the declared surface". `--force` skips the pre-flight; the gate then decides. `phasekit check` prints the same list in a vendored project as the `migration-readiness` hint, and `--dry-run` refuses exactly as the real run would. v0.19.3: the pre-flight also prints how many test files read process documents (the `process-reads` advisory, `docs/QUALITY_GATES.md` "A project's tests test the project") — information only; it never refuses on them.
4. **`--dry-run`** prints the plan — every file it would delete, whether `.claude/settings.json` changes, the pin — and changes nothing.
5. **The change.** Deletes exactly the manifest's `scaffold` entries (tracked or on disk) and `.scaffold/`, prunes directories left empty, strips the entries that wire phasekit's four engine hooks (`.claude/hooks/{deny-dangerous-commands,require-verdict,wrapup-nudge,compact-reanchor}.sh`) from `.claude/settings.json` (permissions, and the project's own hooks, unchanged), and writes `.phasekit-version`. Project-owned files are not touched.
6. **The gate.** Runs the project's gate under the engine, as `upgrade` does (with `PHASEKIT_CONTRACT` naming the engine's contract). Red: every byte is restored, nothing is committed, the tree is exactly as before, exit 4.
7. **One commit** (`chore(phasekit): migrate to the engine outside the repo (vX.Y.Z)`). It never pushes.

Idempotent: on a pinned project it says so and does nothing. Afterwards `phasekit check` should be clean.

**Supervised projects.** A project that a supervisor (an orchestrator) dispatches by running the project's own vendored `scripts/container-setup.sh` must stay vendored until the supervisor resolves pins: after the migration there is no vendored loop for it to start.

## What to commit in a pinned project

- **Commit:** `.phasekit-version`; the project's own files (`docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, `docs/PROD_REQUIREMENTS.md`, `docs/LEARNINGS.md`, `docs/CONVENTIONS.md`, `docs/project/`, `docs/adr/`, `docs/DESIGN.md` if used, `AGENTS.md`, `.claude/CLAUDE.md`, `.claude/settings.json`, `scripts/phasekit-verify.sh`); and the workflow artifacts (`artifacts/phase-approval.json`, `artifacts/decision-memo.md` and review documents, `artifacts/phase-blocked.json` and `artifacts/project-complete.json` when present).
- **Never commit an engine file.** `phasekit check` exits 3 on one. A non-Claude agent that needs a process doc can be pointed at `phasekit docs`.
- **Gitignore** `.claude/settings.local.json` (per-user overrides). The loop hides its own transient files through `.git/info/exclude`.

## Legacy: the vendored lifecycle (supported until migrated)

Everything below applies to vendored projects only — projects enriched with `phasekit bootstrap` / `phasekit adopt` (legacy) or created before v0.19.0. In a vendored project `phasekit check` and `phasekit upgrade` act on the recorded manifest as described here. `phasekit migrate` (above) converts one to the pinned layout.

```bash
# Legacy vendored verbs (from inside a vendored project):
phasekit bootstrap          # legacy: first-time vendored install (greenfield)
phasekit adopt              # legacy: adopt an existing project, vendored (no overwrites)
phasekit check              # vendored: audit current state against the recorded manifest
phasekit check-version      # vendored: is a newer scaffold release available?
phasekit upgrade            # vendored: re-provision against the current scaffold

# Raw-flag forms still work (and can target a path), e.g.:
phasekit --reconcile .      # rebuild the manifest from disk (pre-M9 projects)
phasekit --migrate-only .   # migrate the manifest schema forward, no other effects
```

`enrich-project.py` reads every scaffold source relative to its own location, so for a vendored project the canonical clone is the upgrade source; the project's own vendored `scripts/` are loop runtime, not the upgrade source.

### Provenance: `.scaffold/manifest.json` (vendored)

After every successful enrichment, the engine writes `.scaffold/manifest.json` in the downstream project. **This file MUST be committed to the project's git history** (not gitignored). The engine warns if it is gitignored.

The manifest records:

- `schema_version` — integer; the engine migrates older manifests in-memory before any operation
- `scaffold_version` and `scaffold_commit` — what version of the scaffold installed this state. `scaffold_version` is `git describe --tags --always --dirty` (e.g. `v0.1.0`, `v0.1.0-3-g0d9ee74`), falling back to the short commit when untagged. See `docs/RELEASING.md`.
- `origin_url` — the scaffold repo's `origin` remote, so a project can find its upstream for `--check-version` and the loop update nudge (may be `null` on older manifests)
- `profile` — which profile was active (`default`, `game-project`, etc.)
- `enriched_at` — UTC ISO-8601 timestamp
- `normalization` — the recipe used for content hashing (`lf-trim-trailing-ws-single-final-newline` v1)
- `files` — one entry per scaffold-installed path:
  - `path`, `ownership`, `text` (binary or text)
  - `sha256` (normalized) and `sha256_strict` (byte-exact)
  - `overlays: []` (reserved for M9.4)
  - `installed_at` (UTC)
  - For `bootstrap-with-template-tracking`: `rendered_from` and `template_sha`

### `--upgrade` commits its own work (vendored)

An upgrade commits the files it wrote, with the message `chore(scaffold): phasekit upgrade vX.Y.Z -> vA.B.C`, and pushes when the branch has an upstream. Opt out with `--no-commit`.

It leaves the tree clean because a dirty tree after an upgrade caused two distinct failures in one day:

- **Idle projects self-deadlocked.** The upgrade dirtied the tree and the orchestrator's on-ramp refuses a dirty tree, so a project that gets no sessions could never absorb its own upgrade: dirty tree → no session → still dirty. Three projects on the v0.7.1 rollout had to be fixed by hand.
- **Active projects filed false scaffold-drift signals.** Sessions that absorbed the upgrade committed scaffold-class files, tripping scope containment four times.

**Only the paths the upgrade wrote are committed.** It uses `git commit --only` on exactly those paths, so anything you already had staged or modified stays exactly where it was — an upgrade must never hand your in-flight work a commit message about the scaffold. Everything about the commit and push is non-fatal: no git identity, no remote, no upstream, or a rejected push each print a note and leave the installed files in place.

### `--upgrade` runs the project's gate first, and remembers keep-local (v0.16.0) (vendored)

- **The project's own gate runs on the upgraded tree before anything is committed.**
  Anything but a clean green — red, timed out, a gate that wrote into the tree, or a gate
  that could not run here — restores every file the upgrade wrote byte-for-byte, commits
  nothing, names the check and the files, and exits 4. Where it runs is
  `PHASEKIT_UPGRADE_VERIFY`: `auto` (the runner image when docker is reachable; the host
  only when there is no docker CLI at all — a daemon that fails or hangs, or a missing image,
  refuses and says what to do), `container`, `host`, `off`. A project whose gate was not configured before
  the upgrade (no script, or the stub) is skipped. `--no-verify` commits without it and
  says so in the commit subject; `--no-commit` never runs it.
- **The gate runs in a session's environment, not a bare container (v0.16.2).** A suite
  that is green in a session must not be refused at upgrade for what the runner lacked
  (foundry-orchestrator, 2026-09-27: 15 red on its landed main — no git identity, no
  contracts provider). So the container gate mirrors what `scripts/container-setup.sh`
  gives a session and a test can observe:
  - `HOME=/home/node` (the image's baked `.gitconfig` and Playwright cache) when the gate
    user is root or `node` (uid 0 or 1000, by number or name), as container-setup pins it
    for a `--user` override; any other uid gets a throwaway HOME, because the image's home
    is not its to write (a session there would run as `node` — a divergence recorded, not
    mirrored: set `PHASEKIT_CONTAINER_USER` to the session's user).
    `CLAUDE_CONFIG_DIR` is set as a session's is, and `IS_SANDBOX=1` as root.
  - A **global** git identity, written before the gate the way
    `.devcontainer/entrypoint.sh` writes one: `GIT_USER_NAME`/`GIT_USER_EMAIL` when set,
    else what the project repo resolves (`git config user.name`), else
    `phasekit upgrade <phasekit-upgrade@localhost>`. Global config, never
    `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env: env would outrank a test's own
    `git config user.name` in its scratch repo, which no session does. On the host the
    gate gets the same identity only when the host has none (a temporary
    `GIT_CONFIG_GLOBAL` that includes the real global files first).
  - The contracts provider: `PHASEKIT_CONTRACTS_MOUNT`, else `PHASEKIT_CONTRACTS_DIR` on
    the upgrading host, bind-mounted **read-only** at `/contracts` with
    `PHASEKIT_CONTRACTS_DIR=/contracts` inside — container-setup's mount. A set-but-unusable
    provider (not a directory, or no readable `index.json`) refuses the upgrade (exit 4,
    in either mode) exactly as it would refuse a session; unset mounts nothing — so a
    project whose suite needs a provider must be upgraded with one exported (Foundry: the
    orchestrator stages it with `orchestrator.contracts_mount`). A host-mode gate sees the
    same tree as `PHASEKIT_CONTRACTS_DIR` (the mount name wins when both are set).

  Not mirrored, on purpose: the entrypoint (firewall + dropped capabilities — it needs
  sudo and `NET_ADMIN`, and a gate with more network is never refused for it), the Claude
  credential volume (a gate gets no credentials), and the ssh agent and push tokens (a
  gate pushes nothing).
- **An interrupted upgrade is settled by the next one.** The exact pre-upgrade bytes of
  every path the upgrade may touch are kept outside the tree
  (`$XDG_STATE_HOME/phasekit/upgrade-pending/`) until it commits. Ctrl-C or SIGTERM
  restores on the spot (exit 130); after a SIGKILL the next `--upgrade` restores an
  unverified tree, or commits a verified one, before planning anything — touching only
  files still exactly as the upgrade left them (anything changed since is named and left
  alone) — and `--check`
  exits 3 while one is pending. A staging failure (a stale `.git/index.lock`) exits 5 and
  leaves the verified upgrade pending for the next run to commit.
- **`--keep-local PATH` is a standing decision.** It is recorded on the file's manifest
  entry (`"local": "kept"`) and every later upgrade keeps the file by default, marking
  it `(standing keep-local)` in the plan, until `--take-new PATH` releases it. (A
  drifted `bootstrap-*` file kept by default is not a decision and records nothing.) Before
  v0.16.0 the flag was forgotten after one upgrade, and the next scaffold update of the
  file silently replaced the project's version.

### Which files a project may edit (v0.17.0) (vendored)

Amendments are not merged on upgrade (the M9.4 overlay idea, declined 2026-09-27: a real
amendment usually rewrites an inherited rule in place, and a mechanical merge can leave
two rules that contradict each other). Instead every file has one owner:

- **Scaffold-owned** (`scaffold`) files are never edited in the project. What a project
  wants to add goes in the file's **companion**, `docs/project/<NAME>.md` for
  `docs/<NAME>.md`; what it wants changed goes upstream to phasekit. phasekit seeds one
  companion, `docs/project/QUALITY_GATES.md` (`bootstrap-frozen`): the quality gates are
  the one scaffold doc that is per-project policy, and the only one a fleet project ever
  amended. It is seeded once and never rewritten; a project that already has one keeps
  it (adopted, not refused). A standing `--keep-local` remains the escape hatch.
- **Project-owned** (`bootstrap-*`) files are seeded once and never overwritten.
  `docs/CONVENTIONS.md` joined them in v0.17.0 (it was `scaffold`): the stack's
  conventions are a starting point a project corrects. The upgrade that migrates it
  judges it one last time as the scaffold file it was — an unedited copy takes the
  release's text, an edited or kept one is kept (never refused) — and records the new
  class. A standing `"local": "kept"` on it is cleared with a note: nothing overwrites
  a project-owned file, so the mark means nothing there (it still means something on
  `scripts/phasekit-verify.sh`, where it stops a stub re-seed). A re-profile to another
  stack gives an unedited `docs/CONVENTIONS.md` (still byte-identical to the template it
  was seeded from) the new stack's text, and keeps and reports an edited one; leaving the
  stacks keeps the file project-owned, so a plain `--uninstall` never deletes it.
  The migration is `--upgrade`'s: an `enrich` or `--reconcile --force` run first
  re-records the file as project-owned without it, so an unedited copy keeps its old text
  (reported by `--include-templates`; `--take-new PATH` takes the new one).
- **Template changes are advisory.** A `bootstrap-with-template-tracking` entry records
  the template sha its file is based on (`template_sha`), and every later write carries
  it forward, so `--check --include-templates` keeps reporting a template change until the
  project acts: `--take-new PATH` re-renders from the template, `--keep-local PATH`
  records "seen, keeping ours". (Before v0.17.0 every upgrade re-stamped the current
  template's sha, so the advisory vanished at the next upgrade.) A file adopted from the
  scaffold class or from a collision is based on its own bytes. (A base recorded before
  v0.17.0 is the template as of that project's last v0.16 upgrade, not necessarily the
  one its file was first rendered from — template changes before then are not reported.)
- **Two in-place repairs of project-owned files**, the only writes an upgrade makes to them:
  missing scaffold hook registrations are added to `.claude/settings.json` (additive), and
  (v0.18.7) the template-seeded `- @docs/<NAME>.md` lines in `.claude/CLAUDE.md` are
  rewritten as named references (`` - `docs/<NAME>.md` ``). Claude Code resolves an `@`
  import relative to the file that holds it, so from `.claude/` those imports loaded
  nothing; they are not made live because an import loads the whole file into every
  session. Only those lines change (outside fenced code, bullet and trailing text kept);
  any other `@path` that resolves to nothing is only noted. `--dry-run` says what it would
  rewrite; a second upgrade finds nothing to do.

### What to commit (and what to gitignore) (vendored)

Phasekit installs files and produces runtime artifacts. Most of what it installs is project-shared (commit it); a few specific paths are runtime-only or per-user (gitignore them).

#### Always commit (vendored)

Everything the scaffold installs into the project, plus everything the workflow produces:

- **All scaffold-installed files** — `.claude/agents/`, `.claude/hooks/`, `.claude/skills/`, `.claude/settings.json`, `.claude/CLAUDE.md`, `docs/*.md` (both scaffold-canonical and rendered project docs), `scripts/run-phase.sh`, `scripts/run-until-done.sh`, `CONTINUE_PROMPT.txt`, `AGENTS.md`, `.devcontainer/*`, `scripts/container-setup.sh`, `scripts/verify-container.sh`. These define the project's workflow contract and must be shared with the team.
- **`.scaffold/manifest.json`** — the provenance record. **Must** be committed (engine warns when gitignored). This is how `--check` and `--upgrade` know what scaffold version installed what.
- **`artifacts/phase-approval.json`** — the gate every phase ends with.
- **`artifacts/decision-memo.md`** and any `artifacts/*.md` review documents (red-team reviews, code reviews) — the audit trail.
- **`artifacts/phase-blocked.json`** when present — surfaces blockers for the next session.
- **`artifacts/project-complete.json`** when present — final completion artifact.
- **`docs/adr/ADR-NNNN-*.md`** — your project's architectural decision records.
- **`docs/DESIGN.md`** if the `with-design` profile is active — the steady-state design (M10).

#### Always gitignore (vendored)

These are runtime-only or per-user artifacts:

- **`.claude/settings.local.json`** — per-user overrides (often permissive); not project-shared.
- **`.scaffold/manifest.json.lock`** — fcntl.flock advisory lockfile; runtime-only. Since v0.18.6 the engine excludes it itself (`.git/info/exclude`, where it creates the lock), the loop never stages it, and `phasekit upgrade` untracks a copy an older history committed (the file stays on disk). A `.gitignore` line for it is harmless.
- **`*.scaffold-tmp`** — orphan temp files from atomic copy interrupts; swept by the engine on next run, but might briefly exist.

#### Copy-pasteable `.gitignore` snippet (vendored)

If your project doesn't already have these entries, append:

```gitignore
# Phasekit — per-user and runtime-only artifacts
.claude/settings.local.json
.scaffold/manifest.json.lock
*.scaffold-tmp
```

Project-language gitignores (`node_modules/`, `.venv/`, build outputs, etc.) are the project's concern and aren't covered by phasekit. Keep them in your existing `.gitignore` alongside the snippet above.

#### Notes (vendored)

- **`AGENTS.md` at project root** is rendered with your project name and is `bootstrap-with-template-tracking` — write your project-specific guidance into it; future template improvements surface as advisory drift via `--check --include-templates`, never auto-overwriting your edits.
- **`docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/PHASES.md`, `docs/PROD_REQUIREMENTS.md`** are `bootstrap-frozen` — written once, never re-rendered. Customize freely.
- **`docs/CONVENTIONS.md`** (stack profiles) is `bootstrap-with-template-tracking` since v0.17.0 — seeded from the stack's template, then yours to amend.
- **`docs/project/QUALITY_GATES.md`** is the project-owned companion of the scaffold's `docs/QUALITY_GATES.md` — put the project's own gates there, never in the scaffold doc.
- **`.claude/agents/<name>.md`** files are `scaffold` class — project-specific rules belong in project-owned docs (`AGENTS.md`, `.claude/CLAUDE.md`, a companion); `--keep-local` on `--upgrade` preserves an in-place edit as a standing decision. (The M9.4 overlay mechanism was declined, v0.17.0.)
- **`artifacts/`** as a directory should always exist (the engine creates it during enrichment) but its contents accumulate over time as phases land. Each artifact you commit is a piece of the project's audit trail.

### Ownership classes (M9 §2) (vendored)

| Class | Behavior on upgrade |
|---|---|
| `scaffold` | Re-enrichment overwrites after acknowledged drift; default upgrade target |
| `bootstrap-frozen` | Never re-rendered, never re-checked under `--check --strict` |
| `bootstrap-with-template-tracking` | Never auto-overwritten; manifest stores `template_sha`; advisory drift when source template changes |
| `scaffold-template` | Lives only in the scaffold (`templates/`); rendered into downstream files |
| `scaffold-internal` | Lives only in the scaffold; never installable |
| `scaffold-orphan` | Downstream-only; assigned by `--upgrade` when the new scaffold no longer declares a previously-tracked path. Left on disk; `--uninstall` removes. |

See `docs/CAPABILITY_MANIFEST.md` for the full schema and per-class semantics.

### Worked example: detecting and resolving drift (vendored)

A team enriched a project with the `default` profile. Months later, they want to verify nothing has drifted from the canonical scaffold version.

```bash
$ python3 enrich-project.py --check ~/projects/myapp
--check: scaffold v0.1.0
  clean: 26
  drifted: 1
  missing: 0
  DRIFT: docs/QUALITY_GATES.md  (scaffold)
```

Exit code is `3` (drift detected). The team has three options:

1. **Take the scaffold's version** (e.g. their hand-edits were a mistake): re-enrich with `--force`, or wait for `--upgrade --take-new` (Slice C).
2. **Keep their local edits**: do nothing; the drift will continue surfacing on every `--check` until they either revert or run `--reconcile` to snapshot the current state as the new manifest baseline.
3. **Reconcile**: run `--reconcile --force` to record the current on-disk state as authoritative. Use this when the drift represents intentional project-specific work that should be tracked locally rather than reverted.

### Worked example: retrofitting an existing project (vendored)

A project was enriched before M9 and has no `.scaffold/manifest.json`. To bring it under the lifecycle contract:

```bash
$ python3 enrich-project.py --reconcile ~/projects/myapp
--reconcile: 27 files found on disk, 0 missing
Manifest written: ~/projects/myapp/.scaffold/manifest.json
```

After this, `--check` works normally, and future scaffold upgrades can be planned against the recorded baseline.

### Worked example: upgrading to a new schema version (vendored)

When the scaffold ships a new manifest schema (e.g. v1 → v2 in the future):

```bash
$ python3 enrich-project.py --check ~/projects/myapp
--check: ... runs cleanly even on a v1 manifest; the engine migrates in memory.

$ python3 enrich-project.py --migrate-only ~/projects/myapp
Migrated manifest from schema v1 to v2.
```

`--migrate-only` rewrites the on-disk manifest without other side effects. Migrations are linear-chain pure functions in `scripts/migrations/`; the engine composes them in order.

### Best practice: always `--upgrade --dry-run` first (vendored)

`--upgrade --yes` is convenient but it auto-takes scaffold-new for any file in the **`update-available`** state — i.e., the file matches the manifest sha (clean from the manifest's perspective) but the scaffold has a newer canonical version. This is correct as a default for files the team has not customized.

The trap: when a downstream project has customizations that pre-date the manifest baseline (e.g. agent files extended with project-specific rules before `--reconcile` snapshotted them), those files appear `update-available` rather than `drifted`. The default action overwrites them silently.

**The discipline:**

```bash
# 1. See the plan first; never apply blind
python3 enrich-project.py --upgrade --dry-run ~/projects/myapp

# 2. Identify any files in [take-new] that you have customized
#    (project-specific agent rules, hand-edits to bootstrap-* files,
#    anything you intentionally diverged from canonical scaffold)

# 3. Re-run with explicit --keep-local for each of those files
python3 enrich-project.py --upgrade --yes \
    --keep-local .claude/agents/code-reviewer.md \
    --keep-local .claude/agents/qa-playwright.md \
    ~/projects/myapp
```

If a project genuinely has no customizations, `--upgrade --yes` without flags is fine. The risk scales with how heavily the project has extended scaffold-installed files — which, since v0.17.0, it should not do: project content goes in companions and project-owned files (see "Which files a project may edit").

A quick way to inventory likely-customized files: run `python3 enrich-project.py --check ~/projects/myapp` first. Anything reported as `DRIFT:` is a definite candidate for `--keep-local`. The trickier cases are files in `update-available` (clean against manifest, behind canonical) — those don't surface in `--check`, only in `--upgrade --dry-run`.

### Reserved conventions (vendored)

These are reserved by M9 for future sub-phases. Do not use them yet:

- **`overlays: []`** per file entry — reserved; M9.4 (overlays) was declined in v0.17.0 in favour of explicit ownership, and the field stays empty.

Agent customizations show up as drift. Use `--check` to inventory them.

### Concurrency and locking (vendored)

The engine takes a per-target advisory lock (`fcntl.flock` on `.scaffold/manifest.json.lock`) before any mutating operation. Concurrent runs against the same target are rejected with exit code `2`.

For CI environments that have their own mutual exclusion, pass `--no-lock` to bypass the engine's lock.

The lock is per-target-directory, never per-scaffold-repo. Concurrent enrichment of *different* targets is fully supported.

### Things M9 does NOT yet do (vendored)

These are deferred to later sub-phases:

- `--upgrade` with plan-then-confirm and `--keep-local`/`--take-new` per-file overrides — **Slice C**
- `--uninstall` with `--include-once` and an uninstall log — **Slice C**
- Per-file atomic copy via `<dest>.scaffold-tmp` + `os.replace` for downstream files — **Slice C** (manifest writes are already atomic)
- Pre-install secrets regex scan (`AKIA*`, `BEGIN PRIVATE KEY`, `xox*`, real `sk-ant-*`) — **Slice C**
- Symlink refusal (refuse when target subdir realpath escapes the target) — **Slice C**
- Subagent overlay mechanism (`*.project.md` concat with conflict resolution) — **M9.4**
- Manifest signing / tamper detection — **M9.5**
- Multi-profile additive installs — **M9.6**

### Troubleshooting (vendored)

**`No .scaffold/manifest.json in <target>`** — This project was enriched before M9. Run `--reconcile` to build a retroactive manifest.

**Exit code 2 with "another enrich-project.py process is operating on..."** — The lock is held by a concurrent run. Wait for it, or use `--no-lock` if you have your own mutex.

**`--check` exits 3 on a file you intentionally edited** — That's the drift detection working. Either revert your edits, or run `--reconcile --force` to record the current state as the new baseline. A future `--upgrade --keep-local <path>` (Slice C) will let you preserve edits while still pulling in scaffold updates elsewhere.

**Manifest is committed but `git status` shows it changed after an enrich or upgrade** — It changed because a recorded sha moved (a file phasekit installed, or a project-owned file whose edits the manifest re-baselined). Since v0.14.1 the timestamps are not churn: `installed_at` is carried forward for every file the run did not write, and `enriched_at` moves only when a file was installed or removed or the scaffold version changed — so a re-run that changes nothing writes nothing, and a re-baseline diff is exactly the sha lines that moved. `--check` is the read-only alternative.

**`--upgrade` interrupted partway (disk full, SIGTERM, etc.)** — Files that were successfully copied are scaffold-canonical on disk; the manifest, written last, may still record pre-upgrade shas for them. A subsequent `--check` will report drift on those files even though they match the scaffold. Recovery: re-run `--upgrade --yes` (or with the same per-file flags). The second pass sees clean files and is a no-op for them; any tmp files from the interrupt are swept by the orphan sweep at engine startup.

## See also

- `docs/CAPABILITY_MANIFEST.md` — manifest schema and ownership taxonomy
- `artifacts/decision-memo.md` — full design rationale (planning gate output)
- `artifacts/red-team-review.md`, `artifacts/red-team-review-v2.md` — adversarial reviews of the design
