# Releasing phasekit

This describes how phasekit cuts a release and how downstream projects discover
that a newer one exists. It is about the **scaffold release version**, which is
a different axis from the **manifest schema version** (`version: 1` in
`capabilities/project-capabilities.yaml`) — see `docs/COMPATIBILITY.md` for the
schema-compatibility contract.

Since v0.19.0 a release reaches projects in two ways. A **pinned** project names
its release in `.phasekit-version` and runs that release's engine read-only from
the engine store; it moves to a new release by a gated pin bump
(`phasekit upgrade`), at any time. A **vendored** project (legacy, supported
until migrated) carries the engine in its own tree and is upgraded the old way.
See "Rolling a release out" below.

## The two version axes

| Axis | Where | Changes when | Consumed by |
| --- | --- | --- | --- |
| Manifest schema version | `version: 1` / `SCHEMA_VERSION_CURRENT` | backward-incompatible manifest change | `enrich-project.py` migrations |
| Scaffold release version | git tag `vX.Y.Z` → `.phasekit-version` (pinned) / `scaffold_version` (vendored) | every release | the engine store, `phasekit upgrade`; `--check-version` and the loop update nudge (vendored) |

## Scaffold release version

`scaffold_version` is computed by `get_scaffold_version()` in
`scripts/enrich-project.py` as `git describe --tags --always --dirty`:

- On a tagged commit: `v0.1.0`
- Past a tag: `v0.1.0-3-g0d9ee74` (3 commits ahead, at `0d9ee74`)
- Dirty tree: `…-dirty`
- No tags at all (or git unavailable): falls back to the short commit, or
  `0.0.0+git.unknown`

In a vendored project it is recorded in `.scaffold/manifest.json` alongside
`scaffold_commit` and `origin_url`, so the project knows what it was built
from and where upstream lives. A pinned project records only the tag, in
`.phasekit-version`; an installed engine records its tag and full commit in
`.engine-version` and `.engine-commit`. Only exact `vMAJOR.MINOR.PATCH` tags,
`v0.19.0` or later, can be pinned — a describe string is never a pin.

## Pre-tag: the suite under the runtime's jq (v0.14.12)

The unit suite runs on the developer host; the fleet runs the loop inside
`scaffold-runner`. Those two once disagreed on `jq` (host 1.8, image 1.6 — jq
1.6 rejects `$label` as a variable name), and every loop filter that bound it
failed silently in-container from v0.14.5 to v0.14.10 with the host suite
green. So before tagging a release that touches `scripts/run-until-done.sh`,
the hooks, or the image, run the boundary-state suite **inside the image**:

```bash
phasekit container build                  # if the image is stale (from the phasekit checkout)
bash scripts/verify-in-container.sh       # tests.test_boundary_state, repo mounted read-only
```

It asserts the image's `jq` is >= 1.7 and accepts `$label`, then runs the
suite with the repo bind-mounted read-only and a throwaway `HOME`. It runs as
the host uid on a rootful daemon and as container root under rootless Docker
(`PHASEKIT_ROOTLESS_DOCKER=1`, or auto-detected), the same mapping
`container-setup.sh` uses — a subuid cannot read the mount. Exit 0 is
the proof; exit 3 means the image is stale (rebuild — `.devcontainer/Dockerfile`
pins `JQ_VERSION` and its sha256). This is a release step, not a pre-commit
gate: it needs Docker.

## Pre-tag: shipped Python passes a downstream linter (v0.15.1)

Every `.py` file in `ALWAYS_INSTALLED_FILE_PATHS` lands inside downstream
repositories, and a downstream project whose verify gate lints its WHOLE tree
(`ruff check .`, say) will lint phasekit's file as if it were its own — while
`phasekit upgrade` commits run no gate. v0.15.0 shipped
`scripts/phasekit-roadmap.py` with 12 violations under a common strict config
(line-length 100, `E,F,I,B,UP,W`) and was caught only at fleet rollout.

`tests/test_downstream_lint.py` pins the two rules that fired, with the standard
library (no linter needed): no line over 100 columns, and no `raise` inside an
`except` clause without `from`. Before tagging, also run the strictest linter any
downstream project you maintain applies, over exactly those files:

```bash
python3 - <<'PY' | xargs ruff check --line-length 100 --select E,F,I,B,UP,W --target-version py39
import importlib.util
spec = importlib.util.spec_from_file_location("e", "scripts/enrich-project.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("\n".join(p for p in m.ALWAYS_INSTALLED_FILE_PATHS if p.endswith(".py")))
PY
```

`--target-version py39` matters: phasekit's Python floor is older than most
consumers', so an "upgrade the syntax" autofix aimed at a newer target (e.g.
`datetime.UTC`, 3.11+) must be answered with a floor-compatible rewrite, not
taken.

## Pre-tag: the suite leaves its TMPDIR empty (v0.18.5)

A full suite run used to leave ~1,226 empty `tmp.*` files in `$TMPDIR`
(the loop's bare `mktemp` sites, the tests' own temporaries); release
sessions run it repeatedly on one host, and on 2026-10-01 that was 122k
files in /tmp. Each test now runs under its own TMPDIR inside one directory
the harness removes (`tests/_suite_tmp.py`, imported by every test module),
and the loop keeps its temporaries in one directory its EXIT trap removes.
Run the full suite once under a fresh TMPDIR and check it is empty:

```bash
t=$(mktemp -d) && TMPDIR=$t python3 -m unittest discover -s tests -p 'test_*.py' > /tmp/suite.log 2>&1; \
  echo "suite rc=$?"; find "$t" -mindepth 1 | head; rmdir "$t" && echo TMPDIR-EMPTY
```

A new test module needs the one line `import _suite_tmp  # noqa: F401`;
`tests/test_suite_tmpdir.py` refuses a module without it.

## Pre-tag: the loop behaves the same with the engine outside the tree (v0.19.0)

Every loop-behaviour test builds its fixture project through `tests/_layout.py`,
so the same case runs in both layouts: `PHASEKIT_TEST_LAYOUT=vendored` (the
default — the engine's files in the fixture's own tree) and
`PHASEKIT_TEST_LAYOUT=pinned` (the fixture holds no engine file; the engine is a
separate, read-only directory and the loop is pointed at the project with
`PHASEKIT_PROJECT_DIR`). The full suite runs vendored; run the loop-behaviour
modules (`LAYOUT_MODULES`) again pinned:

```bash
bash tests/run-layouts.sh pinned          # or: both
```

A case green vendored and red pinned is a release blocker: it means the engine
reads or writes the project through its own location (or the reverse).

## Pre-tag: the release note names every loop surface it moved (v0.16.0)

A downstream project may pin a loop internal in its own tests — xmeo-v3 pinned
the argument list of the loop's jq capture filter, and v0.14.10 added three
bindings to it without saying so; the project found out by going red. Before
tagging, run:

```bash
python3 scripts/release-surfaces.py <previous-tag>
```

and paste its output into the release commit message under a `Loop surfaces:`
heading. It lists contract entries added, removed or changed, `PHASEKIT_*`
variables the loop reads, and, per loop function, the names its jq calls bind.
`(none)` is a valid answer and is pasted too, so a reader can tell "nothing
moved" from "nobody looked".

## Cutting a release

1. Land all changes on `master` and push.
2. Pick the next version (semver, for *scaffold* changes):
   - **patch** (`v0.1.0 → v0.1.1`): doc/script fixes that preserve interfaces.
   - **minor** (`v0.1.0 → v0.2.0`): additive — new files, profiles, flags, agents.
   - **major** (`v0.1.0 → v1.0.0`): breaking — manifest schema bump, a removed
     downstream-shipped file, or changed script interface.
3. Tag annotated and push the tag:
   ```bash
   git tag -a v0.2.0 -m "phasekit v0.2.0"
   git push origin v0.2.0
   ```

Pushing the tag is the release action: new installs (`install.sh`) and `phasekit self-update` track the highest `v*` tag, and the loop nudge / `--check-version` (vendored) compare against it. Until a tag is pushed, nothing downstream sees the change.

4. On every host that runs pinned projects, put the tag into the engine store:
   `phasekit self-update` does it when it lands on the tag (so does a fresh
   `install.sh`); `phasekit engines install vX.Y.Z` does it explicitly. A pinned
   project whose engine is missing fetches it on first use unless the host runs
   with `PHASEKIT_NO_AUTO_FETCH=1` — then this step is required.
5. If the release changes `.devcontainer/`, rebuild the shared image once per
   host (`phasekit container build`): a pinned run never rebuilds or retags the
   image per dispatch.

There is no `CHANGELOG.md` yet; the annotated tag message and `git log` are the
record.

## Rolling a release out

- **Pinned projects: a pin bump.** In each project, `phasekit upgrade` (or
  `phasekit upgrade --to vX.Y.Z`) installs the engine, writes the pin, runs the
  project's own gate under the new engine, and commits one line
  (`chore(phasekit): pin vA -> vB`); a red gate changes nothing (exit 4) and the
  project stays on its old release. It can run at any time: a running iteration
  finishes on the engine it started with, and a pin bump on the integration
  branch reaches an open iteration's work branch only at its merge-back (the pin
  travels with the branch). There is no rest window to wait for and no skip to
  write down. `phasekit check` (exit 0 / 3 / 6) confirms the result.
- **Vendored projects (legacy, until migrated):** upgraded the old way — the
  vendored 3-way `phasekit upgrade` at the project's resting boundary
  (`docs/INSTALL_LIFECYCLE.md`, the legacy section). `phasekit migrate` converts
  one to a pin.
- **A supervisor that vendors phasekit's contract** (`contracts/interface.json`)
  re-vendors it after each release while it is itself vendored; once it is
  pinned, it reads the contract from its pinned engine and the pin bump replaces
  the re-vendor.

## How downstream discovers updates

- **Pinned projects:** `phasekit self-update` learns new release tags;
  `phasekit upgrade` then bumps to the newest release known on the machine, or
  says there is nothing to do. The loop's update nudge reads the vendored
  manifest, so a pinned project gets no nudge.
- **Explicit (vendored):** from a scaffold clone, `phasekit --check-version /path/to/project`
  reports the project's recorded version vs the running scaffold, using git
  ancestry for a precise "behind by N commits" verdict when resolvable.
- **Automatic (vendored):** `scripts/run-until-done.sh` prints a one-line, non-fatal nudge
  at loop start when a newer `v*` tag exists upstream (read via `git ls-remote`
  against the manifest's `origin_url`, falling back to the canonical remote).
  Opt out with `PHASEKIT_NO_UPDATE_CHECK=1`.

Neither auto-upgrades; both point the operator at the vendored upgrade (`phasekit upgrade`).
