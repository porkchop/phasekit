# Decision Memo: Meta-M8 Generalization and Compatibility

**Date:** 2026-04-06  
**Phase:** meta-M8  
**Planning gate triggered by:** new documentation deliverables affecting downstream adoption and extension surface

---

## Decision Summary

Deliver M8 as two new docs (COMPATIBILITY.md, EXTENSION_PATTERNS.md) and a small update to CAPABILITY_MANIFEST.md for versioning policy. No new scripts, no new profiles, no schema changes.

---

## Options Considered

### Option A -- Docs-heavy: new guide per gap, plus new profiles

Add four new documents (compatibility guide, extension patterns, profile selection matrix, adoption checklist) and two additional example profiles (e.g., `cli-tool`, `library`). Update the manifest schema with a formal semver versioning mechanism (migration scripts, version-checking logic in enrich-project.py).

**Pros:** Comprehensive coverage. More profiles demonstrate breadth.  
**Cons:** High scope for a scaffold that already works. New profiles without real downstream validation are aspirational. Migration scripts imply a versioning mechanism that does not yet have a second version to migrate from. The adoption checklist largely duplicates USAGE_PATTERNS.md patterns 1-2.

**Verdict:** Over-engineered. Violates "practical, not aspirational" constraint.

### Option B -- Minimal docs: two new documents, one doc update, no new code (recommended)

Add two focused documents:
1. `docs/COMPATIBILITY.md` -- versioning policy, compatibility promises, and upgrade guidance for downstream repos
2. `docs/EXTENSION_PATTERNS.md` -- how to add custom profiles, agents, hooks, and skills; profile selection guidance included as a section rather than a standalone doc

Update one existing document:
3. `docs/CAPABILITY_MANIFEST.md` -- add a "Versioning policy" section clarifying what `version: 1` means and when it would increment

No new profiles, no new scripts, no schema changes.

**Pros:** Directly satisfies all three acceptance criteria. Keeps scope narrow. Documents what already exists rather than inventing new mechanisms. Profile selection guidance fits naturally in the extension doc rather than requiring its own file.  
**Cons:** Does not add new example profiles. Downstream repos get guidance but not automation for upgrades.

**Verdict:** Right-sized. Covers the gaps without speculative engineering.

### Option C -- Update-only: fold everything into existing docs

Add compatibility and extension content as new sections in CAPABILITY_MANIFEST.md and USAGE_PATTERNS.md. No new files.

**Pros:** Fewest new files.  
**Cons:** CAPABILITY_MANIFEST.md is already long and focused on schema reference. Adding compatibility policy and extension how-tos would dilute its purpose. USAGE_PATTERNS.md is a workflow catalog, not a reference guide. Reviewers would flag the scope creep in those files.

**Verdict:** Too cramped. The topics deserve their own documents.

---

## Recommended Approach: Option B

### Deliverables

**1. `docs/COMPATIBILITY.md` (new)**

Contents:
- Scaffold versioning policy: manifest `version` field is the compatibility contract; it increments only for breaking changes to schema shape or script interfaces
- Current version: `1`. No breaking changes have occurred since initial release
- Compatibility promise: downstream repos on version 1 will continue to work with future scaffold updates that stay on version 1
- Upgrade guidance: when adopting a newer scaffold release, re-run `enrich-project.py --dry-run` to see what would change; review diffs before applying
- Breaking change protocol: if version 2 is ever needed, the changelog will list every breaking change and the migration steps
- Reference to M5.1 migration (in CONTAINERIZATION.md) as an example of a non-breaking evolution

**2. `docs/EXTENSION_PATTERNS.md` (new)**

Contents:
- How to add a custom profile (add entry to `profiles:` in manifest, reference existing agent/doc/hook/script keys, optionally use `extends`)
- How to add a custom agent (create `.md` file, add to `agents:` section, include in relevant profiles)
- How to add a custom hook (create script, add to `hooks:` section, include in profiles)
- How to add a custom skill (create directory, add to `skills:` section, update generation/packaging)
- How to add a custom doc (create file, add to `docs:` section)
- Profile selection guide: decision matrix mapping project characteristics to recommended profiles (table format)
- Downstream adoption checklist: 5-step list referencing bootstrap/adopt scripts and pointing to USAGE_PATTERNS.md for detailed workflows

**3. `docs/CAPABILITY_MANIFEST.md` update (existing)**

Add a "Versioning policy" section after the existing "Extending the manifest" section:
- `version: 1` is the current and only schema version
- The version increments only when the schema shape changes in a backward-incompatible way (e.g., removing a required key, changing key semantics)
- Additive changes (new optional keys, new profiles, new capability entries) do not require a version bump
- Cross-reference to `docs/COMPATIBILITY.md` for full policy

**4. Manifest updates**

Add the two new docs to `capabilities/project-capabilities.yaml` under the `docs:` section and include them in the `default` profile's `include_docs` list.

### What is explicitly out of scope

- No new profiles (game-project and saas-project already demonstrate the extension pattern; adding more without real downstream use is speculative)
- No new scripts or migration tooling (no second version exists to migrate from)
- No schema changes to the manifest format
- No changes to enrich-project.py or other scripts
- No new example project directories

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Compatibility doc makes promises the scaffold cannot keep | Policy is minimal: "version 1 is stable; we will document breaking changes if they happen." No automation promises. |
| Extension patterns doc becomes stale as scaffold evolves | Each pattern references specific manifest keys and file paths that are validated by existing scripts. Staleness would surface during enrichment runs. |
| Profile selection guidance is too generic | Use a concrete table with project characteristics mapped to profiles. Keep it short. |
| Downstream adoption checklist duplicates USAGE_PATTERNS.md | Checklist is a 5-item summary that cross-references patterns 1 and 2 rather than repeating them. |

---

## Rollback Path

All changes are additive documentation. Rollback is `git revert HEAD` after the M8 commit. No scripts, schemas, or behavior change.

---

## Acceptance Criteria

1. `docs/COMPATIBILITY.md` exists and covers versioning policy, upgrade guidance, and compatibility promise.
2. `docs/EXTENSION_PATTERNS.md` exists and covers adding custom profiles, agents, hooks, skills, and docs; includes profile selection matrix and adoption checklist.
3. `docs/CAPABILITY_MANIFEST.md` includes a "Versioning policy" section cross-referencing COMPATIBILITY.md.
4. `capabilities/project-capabilities.yaml` includes entries for both new docs.
5. `code-reviewer` approves all changes.
6. `architecture-red-team` confirms no over-engineering or speculative scope.
7. `artifacts/phase-approval.json` written with `approved: true` for meta-M8.
