#!/usr/bin/env python3
"""Enrich a downstream project with scaffold capabilities based on a manifest profile.

Usage:
    python3 scripts/enrich-project.py TARGET_DIR [--profile PROFILE] [--force] [--dry-run]

Resolves the named profile from capabilities/project-capabilities.yaml, then copies
agents, docs, hooks, and scripts to TARGET_DIR. Generates .claude/CLAUDE.md from template.
Skills are not copied directly — use generate-skill.py and package-skill.py for those.

If --profile is omitted, uses 'default'.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "capabilities" / "project-capabilities.yaml"


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def resolve_profile(profiles, profile_name, _seen=None):
    """Resolve a profile, merging parent includes via 'extends'."""
    if _seen is None:
        _seen = set()
    if profile_name in _seen:
        print(f"Error: circular profile inheritance detected: {profile_name}", file=sys.stderr)
        sys.exit(1)
    _seen.add(profile_name)

    if profile_name not in profiles:
        print(f"Error: profile '{profile_name}' not found in manifest", file=sys.stderr)
        print(f"Available profiles: {', '.join(profiles.keys())}", file=sys.stderr)
        sys.exit(1)

    profile = profiles[profile_name]
    result = {
        "include_agents": [],
        "include_skills": [],
        "include_docs": [],
        "include_hooks": [],
        "include_scripts": [],
    }

    if "extends" in profile:
        parent = resolve_profile(profiles, profile["extends"], _seen)
        for key in result:
            result[key] = list(parent.get(key, []))

    for key in result:
        if key in profile:
            for item in profile[key]:
                if item not in result[key]:
                    result[key].append(item)

    return result


def copy_file(src, dest, force=False, dry_run=False):
    """Copy a file, creating parent dirs as needed. Returns True if copied."""
    if dest.exists() and not force:
        print(f"  Skip (exists): {dest}")
        return False
    if dry_run:
        print(f"  Would copy: {src} -> {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"  Copied: {dest}")
    return True


def render_claude_md(target_dir, project_name, force=False, dry_run=False):
    """Generate .claude/CLAUDE.md from template if it doesn't exist."""
    template_path = REPO_ROOT / "templates" / "CLAUDE.template.md"
    dest = target_dir / ".claude" / "CLAUDE.md"

    if dest.exists() and not force:
        print(f"  Skip (exists): {dest}")
        return False
    if not template_path.exists():
        print(f"  Warning: template not found at {template_path}", file=sys.stderr)
        return False
    if dry_run:
        print(f"  Would render: {dest}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    template_text = template_path.read_text()
    rendered = re.sub(r"\{\{PROJECT_NAME\}\}", project_name, template_text)
    rendered = re.sub(r"\{\{OPTIONAL_REFERENCES\}\}", "", rendered)
    dest.write_text(rendered)
    print(f"  Rendered: {dest}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Enrich a downstream project from scaffold manifest")
    parser.add_argument("target_dir", help="Path to the downstream project directory")
    parser.add_argument("--profile", default="default", help="Manifest profile to use (default: 'default')")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without doing it")
    args = parser.parse_args()

    target = Path(args.target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest()
    profiles = manifest.get("profiles", {})
    resolved = resolve_profile(profiles, args.profile)

    project_name = target.name
    copied = 0
    skipped = 0

    # Agents
    agents = manifest.get("agents", {})
    if resolved["include_agents"]:
        print(f"\nAgents ({len(resolved['include_agents'])}):")
        for key in resolved["include_agents"]:
            if key not in agents:
                print(f"  Warning: agent '{key}' not found in manifest", file=sys.stderr)
                continue
            src = REPO_ROOT / agents[key]["source"]
            dest = target / agents[key]["source"]
            if copy_file(src, dest, force=args.force, dry_run=args.dry_run):
                copied += 1
            else:
                skipped += 1

    # Docs (copy templates, not the scaffold's own docs)
    docs = manifest.get("docs", {})
    template_map = {
        "SPEC": "spec.template.md",
        "ARCHITECTURE": "architecture.template.md",
        "PROD_REQUIREMENTS": "prod-requirements.template.md",
    }
    # Scaffold-internal docs that should not be copied downstream
    scaffold_only_docs = {"META_SPEC", "META_PHASES", "CAPABILITY_MANIFEST"}
    downstream_docs = [k for k in resolved["include_docs"] if k not in scaffold_only_docs]
    if downstream_docs:
        print(f"\nDocs ({len(downstream_docs)}):")
        for key in downstream_docs:
            if key not in docs:
                print(f"  Warning: doc '{key}' not found in manifest", file=sys.stderr)
                continue
            doc_path = docs[key]["path"]
            dest = target / doc_path

            # Use template if available, otherwise copy the scaffold doc
            if key in template_map:
                template_src = REPO_ROOT / "templates" / template_map[key]
                if template_src.exists():
                    if copy_file(template_src, dest, force=args.force, dry_run=args.dry_run):
                        copied += 1
                    else:
                        skipped += 1
                    continue

            # For docs without templates (PHASES, QUALITY_GATES, USAGE_PATTERNS),
            # copy from scaffold directly
            src = REPO_ROOT / doc_path
            if src.exists():
                if copy_file(src, dest, force=args.force, dry_run=args.dry_run):
                    copied += 1
                else:
                    skipped += 1

    # Hooks
    hooks = manifest.get("hooks", {})
    if resolved["include_hooks"]:
        print(f"\nHooks ({len(resolved['include_hooks'])}):")
        for key in resolved["include_hooks"]:
            if key not in hooks:
                print(f"  Warning: hook '{key}' not found in manifest", file=sys.stderr)
                continue
            src = REPO_ROOT / hooks[key]["path"]
            dest = target / hooks[key]["path"]
            if copy_file(src, dest, force=args.force, dry_run=args.dry_run):
                copied += 1
            else:
                skipped += 1

    # Scripts (only workflow scripts, not scaffold-internal ones)
    scripts = manifest.get("scripts", {})
    workflow_scripts = ["run-phase", "run-until-done"]
    included_scripts = [s for s in resolved.get("include_scripts", []) if s in workflow_scripts]
    if included_scripts:
        print(f"\nScripts ({len(included_scripts)}):")
        for key in included_scripts:
            if key not in scripts:
                print(f"  Warning: script '{key}' not found in manifest", file=sys.stderr)
                continue
            src = REPO_ROOT / scripts[key]["path"]
            dest = target / scripts[key]["path"]
            if copy_file(src, dest, force=args.force, dry_run=args.dry_run):
                copied += 1
            else:
                skipped += 1

    # .claude/CLAUDE.md
    print("\nClaude startup file:")
    if render_claude_md(target, project_name, force=args.force, dry_run=args.dry_run):
        copied += 1
    else:
        skipped += 1

    # Workflow root files required by run-until-done.sh
    print("\nWorkflow root files:")
    for filename in ["CONTINUE_PROMPT.txt"]:
        src = REPO_ROOT / filename
        dest = target / filename
        if src.exists():
            if copy_file(src, dest, force=args.force, dry_run=args.dry_run):
                copied += 1
            else:
                skipped += 1

    # Artifacts and ADR directories
    for d in ["artifacts", "docs/adr"]:
        dir_path = target / d
        if not dir_path.exists():
            if args.dry_run:
                print(f"  Would create dir: {dir_path}")
            else:
                dir_path.mkdir(parents=True, exist_ok=True)
                print(f"  Created dir: {dir_path}")

    print(f"\nDone. {copied} file(s) copied, {skipped} skipped (already exist).")
    if args.dry_run:
        print("(dry-run mode — no files were actually written)")


if __name__ == "__main__":
    main()
