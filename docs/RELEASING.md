# Releasing phasekit

This describes how phasekit cuts a release and how downstream projects discover
that a newer one exists. It is about the **scaffold release version**, which is
a different axis from the **manifest schema version** (`version: 1` in
`capabilities/project-capabilities.yaml`) — see `docs/COMPATIBILITY.md` for the
schema-compatibility contract.

## The two version axes

| Axis | Where | Changes when | Consumed by |
| --- | --- | --- | --- |
| Manifest schema version | `version: 1` / `SCHEMA_VERSION_CURRENT` | backward-incompatible manifest change | `enrich-project.py` migrations |
| Scaffold release version | git tag `vX.Y.Z` → `scaffold_version` | every release | `--check-version`, loop update nudge |

## Scaffold release version

`scaffold_version` is computed by `get_scaffold_version()` in
`scripts/enrich-project.py` as `git describe --tags --always --dirty`:

- On a tagged commit: `v0.1.0`
- Past a tag: `v0.1.0-3-g0d9ee74` (3 commits ahead, at `0d9ee74`)
- Dirty tree: `…-dirty`
- No tags at all (or git unavailable): falls back to the short commit, or
  `0.0.0+git.unknown`

It is recorded in every enriched project's `.scaffold/manifest.json` alongside
`scaffold_commit` and `origin_url`, so a project always knows what it was built
from and where upstream lives.

## Pre-tag: the suite under the runtime's jq (v0.14.12)

The unit suite runs on the developer host; the fleet runs the loop inside
`scaffold-runner`. Those two once disagreed on `jq` (host 1.8, image 1.6 — jq
1.6 rejects `$label` as a variable name), and every loop filter that bound it
failed silently in-container from v0.14.5 to v0.14.10 with the host suite
green. So before tagging a release that touches `scripts/run-until-done.sh`,
the hooks, or the image, run the boundary-state suite **inside the image**:

```bash
bash scripts/container-setup.sh build     # if the image is stale
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

Pushing the tag is the release action: new installs (`install.sh`) and `phasekit self-update` track the highest `v*` tag, and the loop nudge / `--check-version` compare against it. Until a tag is pushed, nothing downstream sees the change.

There is no `CHANGELOG.md` yet; the annotated tag message and `git log` are the
record. (A changelog + `--upgrade --to vX.Y.Z` is tracked as future work in
`docs/META_PHASES.md`.)

## How downstream discovers updates

- **Explicit:** from a scaffold clone, `phasekit --check-version /path/to/project`
  reports the project's recorded version vs the running scaffold, using git
  ancestry for a precise "behind by N commits" verdict when resolvable.
- **Automatic:** `scripts/run-until-done.sh` prints a one-line, non-fatal nudge
  at loop start when a newer `v*` tag exists upstream (read via `git ls-remote`
  against the manifest's `origin_url`, falling back to the canonical remote).
  Opt out with `PHASEKIT_NO_UPDATE_CHECK=1`.

Neither auto-upgrades; both point the operator at `phasekit --upgrade`.
