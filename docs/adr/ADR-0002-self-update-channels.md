# ADR-0002: Self-update channels (stable / edge)

## Status

Proposed (2026-06-05). Not yet implemented. Supersedes the implicit
"self-update always tracks the latest release tag" behavior in
`scripts/phasekit.sh` if accepted.

## Context

The canonical clone (`${XDG_DATA_HOME:-~/.local/share}/phasekit`) is the single
source for both running and upgrading downstream projects (ADR-0001,
`docs/INSTALL_LIFECYCLE.md`). How that clone moves to a newer phasekit is split
across two entrypoints today, with no memory of intent:

- `phasekit self-update` (`scripts/phasekit.sh`) is hardcoded to the latest
  `v*` tag — `git fetch --tags` then `checkout` the highest `sort -V` tag.
- `install.sh` *can* target any ref via `PHASEKIT_REF` (tag, branch, sha), but
  persists nothing — every run re-infers the ref from scratch.

This asymmetry produced concrete, repeated friction. While shipping the
rootless-Docker fix, a maintainer iterating on phasekit from an unattended agent
worker could not get `self-update` to ride `master` at all (it only knows tags),
and the documented workaround misfired twice:

1. `PHASEKIT_REF=master curl … | bash` silently fell back to the latest tag —
   the `VAR=value cmd` prefix binds to `curl`, not the piped `bash`, so the
   installer never saw the variable.
2. Once corrected, the installer's explicit-ref path did a bare
   `git checkout master` with no fast-forward, stranding the clone one commit
   behind `origin/master` (fixed by the branch fast-forward in `install.sh`; the
   *persistence* gap this ADR addresses remained).

Underlying problem: there is no first-class, durable notion of *which line of
development this install follows*. `install.sh` can branch-track; `self-update`
cannot; neither remembers the choice.

Constraints:

- phasekit provisions **other** projects. Tracking `master` means unreleased
  scaffold can flow into downstream repos via `phasekit upgrade`. The ADR-0001
  provenance machinery still records whatever is on disk — `git describe
  --tags --always` yields `vX.Y.Z-N-gSHA` for a post-tag checkout, so version
  traceability holds — but provisioning mid-flight code is a real risk that must
  be opt-in, never the default.
- No persisted install state exists today, so the default with no state **must**
  remain "latest tag" — existing installs cannot change behavior silently.
- The mechanism must ride the existing bash entrypoints (`install.sh`,
  `scripts/phasekit.sh`); no new distribution channel or package format.
- The channel must be a single source of truth consulted by *both* entrypoints,
  or they will drift back into the asymmetry above.

## Options considered

**A. Status quo.** `self-update` = latest tag; branch tracking only via
`install.sh` + `PHASEKIT_REF`, non-persistent. Rejected — it is the friction
this ADR exists to remove: no memory of intent, asymmetric entrypoints, and the
pipe/precedence and no-fast-forward traps documented above.

**B. Infer the channel from git HEAD state** (detached at a tag ⇒ stable; on a
branch ⇒ edge). No new state file. Rejected — fragile: HEAD is mutated by
routine git operations, it conflates *where the clone currently sits* with *what
it should track*, and a deliberate pin (detached at a sha) is indistinguishable
from a transient detached checkout.

**C. Env var only (`PHASEKIT_CHANNEL`).** Rejected for the stated goal: an env
var is not durable, so it re-introduces the same "set it every time, forget it
once" trap (and the same shell-precedence footgun) that bit us with
`PHASEKIT_REF`.

**D. A persisted channel file in the clone, one verb to set it, consulted by
both `install.sh` and `self-update`.** Adopted. Durable, explicit, single source
of truth, backward compatible (absent ⇒ stable).

**E. Full package-manager-style multi-channel with signed/published releases.**
Out of scope — over-engineered for a single-canonical-clone tool; revisit only
if phasekit moves to a marketplace/plugin distribution (ADR-0001 option D).

## Decision

Adopt option D with the following concrete decisions:

1. **Two named channels plus a pin form.**
   - `stable` (default) — the latest `v*` release tag. Identical to today's
     `self-update`.
   - `edge` — the tip of the remote default branch (`origin/master`).
   - `<ref>` (a tag or sha) — an explicit **pin** that does not auto-advance.

2. **State is one file: `$PHASEKIT_HOME/.phasekit-channel`**, holding a single
   token (`stable` | `edge` | `<ref>`). Absent ⇒ `stable`. This file is the
   single source of truth; both entrypoints read it and `self-update`/installer
   own writes.

3. **CLI: a single `channel` verb.** `phasekit channel` prints the current
   channel; `phasekit channel stable|edge|<ref>` sets it. Chosen over a trio of
   verbs (`track-latest`/`track`/`stable`) for discoverability and
   extensibility.

4. **`self-update` resolution by channel:**
   - `stable` → `git fetch --tags`; checkout highest `v*` tag *(unchanged
     behavior)*.
   - `edge` → `git fetch`; checkout the default branch; `git merge --ff-only
     origin/<branch>` — the exact logic added to `install.sh` in the branch
     fast-forward fix. Factor it into a shared bash helper so the two paths
     cannot diverge (single source of truth, per the project's anti-drift rule).
   - `<ref>` pin → `git fetch`; checkout the ref; advance only if it names a
     branch.

5. **`install.sh` writes the channel from how `PHASEKIT_REF` resolved:**
   default-branch ⇒ `edge`; tag or empty ⇒ `stable`; any other explicit ref ⇒
   pin to that ref. So the channel stays consistent whether it was set by the
   installer or the CLI.

6. **Edge is loud.** On `edge` (and on a pin to a non-tag commit), `self-update`
   prints a one-line notice — e.g. `tracking unreleased phasekit
   (v0.3.0-3-gSHA); downstream 'phasekit upgrade' may provision pre-release
   scaffold` — so the operator always knows they are off stable.

7. **Default is unchanged.** No channel file ⇒ `stable` ⇒ byte-for-byte the
   current `self-update`. Existing installs are unaffected until a user opts in.

## Consequences

Positive:

- Removes the friction and the entrypoint asymmetry. "Ride master" becomes
  `phasekit channel edge && phasekit self-update`; "go back to releases" is
  `phasekit channel stable`.
- One persisted source of truth consulted by both `install.sh` and
  `self-update`, reusing the shared branch fast-forward helper so the two cannot
  drift.
- Backward compatible: absent channel file = `stable` = today's behavior; no
  change for any existing install until it opts in.
- Provenance is preserved: `git describe --tags --always` already yields a
  meaningful tag-or-sha version that ADR-0001's manifest records, so `edge`
  installs remain traceable.

Negative / accepted tradeoffs:

- Introduces the first persisted config file for the install
  (`.phasekit-channel`). It must be created and managed; the installer and the
  `channel` verb own its lifecycle.
- `edge` provisions unreleased scaffold into downstream projects via `upgrade`.
  Mitigated by: opt-in only, the loud notice (decision 6), and ADR-0001's drift
  detection and plan-then-confirm upgrade. Not the default.
- A pin (`channel=<sha>`) does not auto-advance; a user can strand the install
  on an old ref and forget. `phasekit channel` (print form) surfaces the current
  state to make this visible.

Follow-up:

- **v1 implementation:** the `.phasekit-channel` file, the `phasekit channel`
  verb, channel-aware `self-update`, the `install.sh` writer, the edge notice,
  the shared branch fast-forward helper, and tests (mirroring the offline
  fast-forward regression test added for `install.sh`).
- **Fast-follow — make `--check-version` channel-aware.** Today it answers "is
  there a newer tag?". On `edge` the meaningful question is "is `origin/master`
  ahead of `HEAD`?". Left out of v1 to keep the first cut small; documented as a
  known limitation until shipped.
- **Docs:** add a "Channels" section to `docs/INSTALL_LIFECYCLE.md` and update
  the `self-update` references in `README.md` / `docs/CONTAINERIZATION.md`.
- Decide whether the in-container loop's "newer phasekit available" update nudge
  should respect the channel.
