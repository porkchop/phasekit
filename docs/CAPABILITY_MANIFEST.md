# Capability Manifest

## Overview

`capabilities/project-capabilities.yaml` is the single source of truth for which capabilities a downstream project should receive. Every agent, skill, hook, doc, and script that the scaffold manages is declared here. As of Phase M9, every tracked file in the scaffold repo is also classified by an **ownership class** so that downstream projects can be upgraded, audited, and uninstalled cleanly.

## Ownership classes (M9)

Every file enumerated by the manifest carries one of these classes:

| Class | Where it lives | Behavior on downstream upgrade |
|---|---|---|
| `scaffold` | scaffold repo + downstream | re-enrichment overwrites after acknowledged drift; default upgrade target |
| `bootstrap-frozen` | downstream only, write-once | never re-rendered, never re-checked under `--check --strict` |
| `bootstrap-with-template-tracking` | downstream only, write-once | never auto-overwritten; the manifest stores a `template_sha`; an advisory drift surfaces when the source template changes |
| `scaffold-template` | scaffold only (`templates/`) | rendered into downstream files; never copied verbatim |
| `scaffold-internal` | scaffold only | never installable; `--self-check` enforces |
| `scaffold-orphan` | downstream only (after `--upgrade`) | left in place when the new scaffold no longer declares the path; `--uninstall` will remove. Never appears in scaffold-side classification. |

Notes:

- Classes describe *downstream* behavior. In the scaffold repo itself, `bootstrap-*` and `scaffold-template` files are also scaffold-internal in the sense that they are never re-installed *into* the scaffold; the class describes their relationship to downstream projects.
- The downstream provenance record (`.scaffold/manifest.json`) MUST be committed by the downstream project. The engine warns when it is gitignored.
- An explicit `ignore:` glob list (see below) covers generated/example/test paths that don't fit any class. Globs are constrained: no `ignore:` glob may match a path under `docs/`, `.claude/`, `scripts/`, `templates/`, `.devcontainer/`, or `capabilities/`. `--self-check` enforces this constraint at runtime.

## File location

```
capabilities/project-capabilities.yaml
```

## Schema

### Top-level keys

| Key | Required | Description |
|---|---|---|
| `version` | yes | Integer schema version (currently `1`) |
| `project` | yes | Project identity and mode declaration |
| `text_default` | yes (M9) | Boolean default for `text:` per-entry overrides; controls hashing recipe |
| `ignore` | yes (M9) | Constrained glob list for paths not classifiable by ownership |
| `profiles` | yes | Named capability bundles for different project types |
| `agents` | yes | Agent definitions |
| `skills` | yes | Skill definitions |
| `docs` | yes | Document definitions |
| `hooks` | yes | Hook definitions |
| `scripts` | yes | Script definitions |
| `files` | yes (M9) | Per-file ownership for paths not covered by typed sections |
| `generation` | yes | Output path conventions for generated/packaged assets |

### `project`

```yaml
project:
  name: <string>          # repository/project name
  mode:                    # list of active product modes
    - scaffold
    - capability-packager
    - self-improvement
```

### `profiles`

Profiles define named bundles of capabilities for different downstream project types.

```yaml
profiles:
  <profile-name>:
    extends: <other-profile>     # optional, inherit from another profile
    include_agents: [<agent-key>, ...]
    include_skills: [<skill-key>, ...]
    include_docs: [<doc-key>, ...]
    include_hooks: [<hook-key>, ...]
    include_scripts: [<script-key>, ...]
```

- `extends` merges the parent profile's includes before applying the current profile's includes.
- Each `include_*` list references keys defined in the corresponding top-level section.

### `agents`

```yaml
agents:
  <agent-key>:
    enabled: <bool>        # whether this agent is active
    source: <path>         # relative path to the agent definition file
    purpose: <string>      # one-line description of what the agent does
    ownership: <class>     # M9 — one of the five ownership classes above
```

### `skills`

```yaml
skills:
  <skill-key>:
    enabled: <bool>
    source_dir: <path>            # relative path to the skill source directory
    package_output: <path>        # relative path where the packaged skill.zip is written
    validate: <bool>              # whether skill validation is required
    package: <bool>               # whether skill packaging is required
    purpose: <string>
    ownership: <class>            # M9 — typically `scaffold` for the package as a whole
```

### `docs`

```yaml
docs:
  <doc-key>:
    path: <path>           # relative path to the document
    required: <bool>       # whether the document must exist for the scaffold to be valid
    ownership: <class>     # M9 — `scaffold`, `bootstrap-frozen`, or `scaffold-internal`
```

### `hooks`

```yaml
hooks:
  <hook-key>:
    path: <path>
    required: <bool>
    ownership: <class>     # M9
```

### `scripts`

```yaml
scripts:
  <script-key>:
    path: <path>
    required: <bool>
    ownership: <class>     # M9
```

### `files` (M9)

Top-level map enumerating every tracked file not covered by a typed section
(root files, container files, templates, individual skill files, etc.).
Together with the typed sections this seeds `--self-check`: every path in
`git ls-files` must classify into exactly one ownership class or match an
`ignore:` glob.

```yaml
files:
  <relative/path>:
    ownership: <class>            # required, one of the five M9 classes
    text: <bool>                  # optional; defaults to top-level text_default
    rendered_from: <path>         # required for bootstrap-with-template-tracking
```

### `ignore` (M9)

```yaml
ignore:
  - "<glob>"                       # gitignore-style glob; ** crosses /, * doesn't
```

Constrained: `--self-check` fails if any glob matches a path under
`docs/`, `.claude/`, `scripts/`, `templates/`, `.devcontainer/`, or `capabilities/`.

### `text_default` (M9)

```yaml
text_default: true                  # default for files without an explicit text:
```

Controls hashing recipe: `text: true` files use the normalized recipe
(LF, trim trailing whitespace, single trailing newline); `text: false`
files use byte-exact hashing only.

### `generation`

```yaml
generation:
  output_root: <path>                  # root directory for generated assets
  skills_output_root: <path>           # root directory for packaged skill archives
  preserve_existing_files: <bool>      # if true, generation will not overwrite existing files
  require_review_for_overwrite: <bool> # if true, overwriting requires explicit reviewer approval
```

## Manifest-to-output mapping

Each manifest entry type maps to a specific file or directory in the repo:

| Manifest section | Entry key | Output location | Notes |
|---|---|---|---|
| `agents` | `<agent-key>` | Value of `source` (e.g. `.claude/agents/project-lead.md`) | One file per agent |
| `skills` | `<skill-key>` | Value of `source_dir` (e.g. `.claude/skills/autonomous-product-builder/`) | Directory per skill |
| `skills` (packaged) | `<skill-key>` | Value of `package_output` (e.g. `dist/skills/autonomous-product-builder/skill.zip`) | Generated archive |
| `docs` | `<doc-key>` | Value of `path` (e.g. `docs/SPEC.md`) | One file per doc |
| `hooks` | `<hook-key>` | Value of `path` (e.g. `.claude/hooks/deny-dangerous-commands.sh`) | One file per hook |
| `scripts` | `<script-key>` | Value of `path` (e.g. `scripts/run-phase.sh`) | One file per script |

### Traceability rule

Every file managed by the scaffold should have a corresponding entry in the manifest. To trace a generated or managed file back to its manifest entry:

1. Identify the file type (agent, skill, doc, hook, script)
2. Look up the corresponding section in `project-capabilities.yaml`
3. Find the entry whose `source`, `source_dir`, `path`, or `package_output` matches the file

### Profile resolution

To determine which capabilities apply to a given project type:

1. Look up the profile by name in `profiles`
2. If the profile has `extends`, recursively resolve the parent profile first
3. Merge `include_*` lists (parent first, then child additions)
4. Each included key must exist in the corresponding top-level section

## Extending the manifest

To add a new capability type:

1. Add a new top-level section with per-entry schema following the existing pattern (`path`/`source`, `required`/`enabled`, `purpose`)
2. Add `include_<type>` support in profiles
3. Document the new section in this file
4. Update `ARCHITECTURE.md` if the new type changes layer boundaries

For step-by-step guides on adding profiles, agents, hooks, skills, and docs, see `docs/EXTENSION_PATTERNS.md`.

## Versioning policy

The `version` field at the top of the manifest is the schema compatibility contract.

- **Current version:** `1`
- The version increments only when the schema shape changes in a backward-incompatible way (removing a required key, changing key semantics, restructuring sections)
- Additive changes — new optional keys, new profiles, new capability entries, new top-level sections — do **not** require a version bump
- Downstream repos can check this field to confirm they are compatible with the current scaffold

For the full versioning policy, upgrade guidance, and breaking-change protocol, see `docs/COMPATIBILITY.md`.
