# Compatibility and Versioning

## Scaffold versioning

The scaffold uses a single integer version in the manifest (`version: 1` in `capabilities/project-capabilities.yaml`). This version is the compatibility contract between the scaffold and downstream projects that adopt it.

### Current version: 1

Version 1 has been stable since the scaffold's initial release. No breaking changes have occurred.

### What triggers a version bump

The version increments only when the manifest schema changes in a backward-incompatible way:
- Removing or renaming a required key
- Changing the semantics of an existing key
- Restructuring a section so that existing entries no longer parse correctly

The following changes do **not** require a version bump:
- Adding new optional keys to existing sections
- Adding new profiles, agents, skills, docs, hooks, or scripts
- Adding new top-level sections
- Updating documentation or templates
- Changing script behavior while preserving interfaces

## Compatibility promise

Downstream repos that adopted the scaffold on version 1 will continue to work with future scaffold updates that remain on version 1. Specifically:
- `enrich-project.py` will continue to resolve version-1 manifests
- `bootstrap-new-project.sh` and `adopt-existing-repo.sh` will continue to function
- Existing profiles and their inheritance chains will be preserved

## Upgrading a downstream project

When adopting a newer scaffold release:

1. Check the scaffold's current manifest version against your downstream project's adopted version
2. Review the scaffold changelog or git log for changes since your adoption
3. Run `python3 scripts/enrich-project.py /path/to/project --dry-run` to see what would change without writing files
4. Re-run the enrichment script to pick up new or updated files
5. Review diffs before committing — the enrichment script skips existing files by default (pass `--force` to overwrite)

## Breaking change protocol

If a version 2 is ever needed:
1. The decision will be documented with rationale and reviewed before implementation
2. The changelog will list every breaking change
3. Migration steps will be documented in this file
4. Downstream repos will have a documented path from version 1 to version 2

## Non-breaking evolution examples

The scaffold has evolved through multiple meta-phases without breaking downstream compatibility:
- **M5 → M5.1 (containerization):** Moved from `container/Dockerfile` to `.devcontainer/` setup. No manifest schema change. See `docs/CONTAINERIZATION.md` for migration details.
- **M6 (planner/red-team integration):** Added planning gate requirements. No changes to downstream-facing scripts or manifest schema.
- **M7 (self-application):** Added a doc entry and refined agent definitions. Fully additive.

## What downstream repos should track

- The `version` field in `capabilities/project-capabilities.yaml` — this is the compatibility signal
- The scaffold's git tags or releases (if published) for change summaries
- New agents, docs, or hooks added to profiles they use — these are available but not forced on existing repos
