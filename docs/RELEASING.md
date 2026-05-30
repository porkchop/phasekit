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
