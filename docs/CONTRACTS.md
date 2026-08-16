# Cross-project contracts

*Introduced in phasekit v0.7.0.*

**Inert by default.** A repo without a `contracts.yaml` behaves exactly as it
did in v0.6.6. Nothing below applies until a repo declares something.

## The problem

Three times in one week, a component was built against an interface it had
*guessed*:

| what happened | shape |
|---|---|
| a loop consumed `PHASEKIT_SESSION_DEADLINE`; nothing emitted it | consumer built, producer missing |
| a supervisor exported `META_REPO_PATH`; nothing consumed it | producer built, consumer missing |
| a dashboard read `rec.id` from an endpoint that sends `iteration` | consumer guessed the wire format |

Each was found by a human's eye, late. The third survived 1079 tests, a
mutation run and two browser passes — because every one of them validated
against a fixture that encoded the same guess.

The fix is not more tests. It is giving the consumer a **machine-checkable
copy of the producer's real interface**, and making divergence a red gate.

## The two questions, deliberately separated

|  | question | where it runs | what it needs |
|---|---|---|---|
| **Conformance** | Does my code match the contract? | your ordinary test suite | the **committed** copy in your repo |
| **Authenticity** | Is that copy really the producer's current contract? | the phasekit verify gate | the **mount** |

You vendor the contract exactly as you commit a lockfile. `git clone && <your
test command>` then passes anywhere, hermetically, with no mount and no
provider. At build time phasekit asserts **vendored == mounted** and refuses on
any difference — whether the producer moved on, or someone edited the vendored
copy to go green.

A vendored copy *can* be forged. The forgery simply does not survive the next
phasekit build, which is where all real work lands. What is not acceptable is a
copy with **no pin**.

> **Stated boundary:** public CI can prove conformance but not freshness. A
> clone has the vendored contract and no provider, so a green CI badge is a
> weaker claim than a green phasekit gate. That is fine — every real build goes
> through the gate — but do not read one as the other.

## phasekit works with no provider at all

phasekit is a public tool. Nothing here may make an orchestrator a
prerequisite. So:

**The trigger for refusing is a repo-owned declaration, never the mount's
absence.**

* No `contracts.yaml` → nothing is expected and nothing complains.
* `contracts.yaml` declares a dependency → phasekit refuses to build when that
  contract is not readable. The repo said it needs it; building without it is
  building blind.

Because the declaration lives in the repo, it **travels**. A standalone user who
wants contract validation declares dependencies and points
`PHASEKIT_CONTRACTS_DIR` at a local directory by hand, and phasekit behaves
identically. An orchestrator is one possible *provider* of a documented
interface, not a requirement.

## The three locations

```
producer-repo/
  contracts/                      # what I PUBLISH (my own interface)
    openapi.json

consumer-repo/
  contracts.yaml                  # what I DEPEND ON  (project-owned)
  vendor/contracts/<slug>/        # my committed copy  (the cache)
    openapi.json

/contracts                        # the provider MOUNT, read-only
  index.json                      # the provider's manifest
  <slug>/
    openapi.json
```

`contracts/` (published) and `vendor/contracts/` (consumed) are separate on
purpose: a repo can be both producer and consumer.

## Declaring a dependency — `contracts.yaml`

At the repo root. **Project-owned**: phasekit never writes, renders or upgrades
this file, so `phasekit upgrade` can never erase your declarations.

```yaml
version: 1
depends_on:
  # Shorthand: the slug alone, with every default applied.
  - foundry-orchestrator

  # Long form, when you want a description or a non-default location.
  - slug: billing-api
    vendor: vendor/contracts/billing-api    # default: vendor/contracts/<slug>
    description: the REST surface we call from the checkout flow
```

`depends_on: []` is a valid declaration of *none*.

The parser is deliberately strict — unknown keys, a wrong `version`, a slug
containing a path separator, an absolute or `..` vendor path, and duplicate
slugs are all hard errors. A typo'd key that silently does nothing is the exact
bug class this feature exists to kill.

## The provider mount

A provider passes `PHASEKIT_CONTRACTS_MOUNT=<host path>` to
`scripts/container-setup.sh`, which bind-mounts it read-only at `/contracts` and
sets `PHASEKIT_CONTRACTS_DIR=/contracts` inside the container.

The mount must contain `index.json`:

```json
{
  "version": 1,
  "entries": [
    { "slug": "foundry-orchestrator", "path": "foundry-orchestrator" }
  ]
}
```

`path` is optional and defaults to the slug. Unknown fields are ignored —
`index.json` crosses a repo boundary and is versioned by a separate producer, so
additive fields must not break a consumer built before they existed. (The
opposite rule applies to `contracts.yaml`, which is phasekit's own format.)

**"No dependencies" is a manifest with zero entries, never an empty directory.**
An empty directory is indistinguishable from a broken bind mount; a manifest is
the provider *asserting* it checked. Silence and never-reached must not look
alike. `container-setup.sh` refuses to start when `PHASEKIT_CONTRACTS_MOUNT`
points at something with no readable `index.json`, rather than dropping it
silently — a passed-but-ignored path is precisely the `META_REPO_PATH` bug.

## The gate

```bash
python3 scripts/phasekit-contracts.py check
```

| exit | meaning | fix |
|---|---|---|
| 0 | nothing declared, or every contract matches | — |
| 2 | the declaration or the provider manifest is malformed | fix the file named in the message |
| 3 | **unobtainable** — no provider, or it does not offer the slug | fix the provider or the registry; refreshing cannot help |
| 4 | **drift** — the vendored copy is missing or differs | run the refresh command the message names |

Comparison is byte-exact: sha256 over every file in the tree, recursively.
mtimes, ownership and directory order are ignored, because a bind mount and a
git checkout will never agree on those and a pin that fires on them is noise
that teaches people to bypass the gate. Symlinks and other non-regular files are
rejected outright rather than skipped — a skipped entry is a hole an
inauthentic copy could hide in.

### Where the gate runs

* **`scripts/run-until-done.sh`** runs it before every phase commit. This is the
  authoritative call, and it deliberately lives in phasekit-owned code: a check
  that only lived in the project-owned `scripts/phasekit-verify.sh` could be
  edited away by the repo it polices.
* **`scripts/phasekit-verify.sh`** (seeded by every stack profile) runs it too,
  so a human or a CI job running the gate directly gets the same answer.

`VERIFY_SKIP=1` does **not** switch the contracts gate off. VERIFY_SKIP is the
routine per-iteration hatch for red TDD commits and docs-only phases; letting it
also disable contract authenticity would turn the gate off exactly when a red
gate is applying the pressure to cheat. The separate hatch is
`PHASEKIT_CONTRACTS_SKIP=1`, and it announces itself loudly.

## Fixing drift

```bash
python3 scripts/phasekit-contracts.py refresh
```

Re-vendors every declared contract from the mount, then commit the result
alongside whatever code and test changes it forces. It **replaces** rather than
merges, so a file the producer deleted disappears from your copy too; otherwise
the vendored copy slowly becomes a superset nobody notices.

Refresh is deliberately manual. Auto-refreshing at dispatch would dirty the tree
and trip the clean-tree guards; instead the gate fails, a human or the next
iteration refreshes, and the change lands in a commit where it is reviewable.

## Session awareness

When a repo declares dependencies, the loop prepends a block to the session
prompt naming them, stating the vendored contract is authoritative and that
guessing is forbidden. A mount nobody is told about goes unread — that is half
of why `META_REPO_PATH` dangled for months.

## Inspecting

```bash
python3 scripts/phasekit-contracts.py status     # what this repo declares
python3 scripts/phasekit-contracts.py provider   # what the mount offers
```

Both accept `--json`.

## Producer responsibilities

The producer publishes its contract to `contracts/` in its own repo, and its
acceptance criteria must include refreshing it. **A producer iteration that
changes an interface without refreshing its contract is incomplete by
definition** — without a producer-side pin, a hand-written contract rots exactly
like prose and the problem has merely moved.

## Environment reference

| variable | side | meaning |
|---|---|---|
| `PHASEKIT_CONTRACTS_MOUNT` | host | Host path a provider passes to `container-setup.sh`; bind-mounted read-only at `/contracts`. |
| `PHASEKIT_CONTRACTS_DIR` | in-process | Where the contracts tree is readable. Set by `container-setup.sh`; set it yourself to run the gate outside a container. Default `/contracts`. |
| `PHASEKIT_CONTRACTS_SKIP` | in-process | `1` bypasses the gate for one run. Loud, operator-only, never a committed setting. |
