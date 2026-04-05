# Capability Manifest

## Overview

`capabilities/project-capabilities.yaml` is the single source of truth for which capabilities a downstream project should receive. Every agent, skill, hook, doc, and script that the scaffold manages is declared here. Settings fragments (e.g. `.claude/settings.json` entries) are managed directly and may be added to the manifest schema in a future phase.

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
| `profiles` | yes | Named capability bundles for different project types |
| `agents` | yes | Agent definitions |
| `skills` | yes | Skill definitions |
| `docs` | yes | Document definitions |
| `hooks` | yes | Hook definitions |
| `scripts` | yes | Script definitions |
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
```

### `docs`

```yaml
docs:
  <doc-key>:
    path: <path>           # relative path to the document
    required: <bool>       # whether the document must exist for the scaffold to be valid
```

### `hooks`

```yaml
hooks:
  <hook-key>:
    path: <path>
    required: <bool>
```

### `scripts`

```yaml
scripts:
  <script-key>:
    path: <path>
    required: <bool>
```

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
