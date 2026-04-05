#!/usr/bin/env python3
"""Validate skill folder structure against required conventions.

Usage:
    python3 scripts/validate-skill.py [--skill SKILL_KEY] [--source SOURCE_DIR]

If --skill is given, looks up the source_dir from the manifest.
If --source is given, validates that directory directly.
If neither is given, validates all skills declared in the manifest.

Exit code 0 = all valid, 1 = at least one failure.
"""

import argparse
import json
import re
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


def validate_skill(skill_key, skill_dir):
    """Validate a single skill folder. Returns list of error strings."""
    errors = []

    if not skill_dir.is_dir():
        return [f"{skill_key}: directory does not exist: {skill_dir}"]

    # SKILL.md must exist
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        errors.append(f"{skill_key}: missing SKILL.md")
    else:
        content = skill_md.read_text()

        # Must have YAML frontmatter with name and description
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            errors.append(f"{skill_key}: SKILL.md missing YAML frontmatter (---)")
        else:
            try:
                fm = yaml.safe_load(fm_match.group(1))
                if not isinstance(fm, dict):
                    errors.append(f"{skill_key}: SKILL.md frontmatter is not a mapping")
                else:
                    if not fm.get("name"):
                        errors.append(f"{skill_key}: SKILL.md frontmatter missing 'name'")
                    if not fm.get("description"):
                        errors.append(f"{skill_key}: SKILL.md frontmatter missing 'description'")
            except yaml.YAMLError as e:
                errors.append(f"{skill_key}: SKILL.md frontmatter is invalid YAML: {e}")

        # Must have at least one markdown heading
        if not re.search(r"^#\s+", content, re.MULTILINE):
            errors.append(f"{skill_key}: SKILL.md has no markdown headings")

    # agents/openai.yaml must exist (for OpenAI/ChatGPT interface)
    openai_yaml = skill_dir / "agents" / "openai.yaml"
    if not openai_yaml.is_file():
        errors.append(f"{skill_key}: missing agents/openai.yaml")
    else:
        try:
            oa = yaml.safe_load(openai_yaml.read_text())
            if not isinstance(oa, dict):
                errors.append(f"{skill_key}: agents/openai.yaml is not a mapping")
            elif "interface" not in oa:
                errors.append(f"{skill_key}: agents/openai.yaml missing 'interface' key")
        except yaml.YAMLError as e:
            errors.append(f"{skill_key}: agents/openai.yaml is invalid YAML: {e}")

    return errors


def resolve_skill_dir(skill_key, skill_config):
    """Resolve skill directory from manifest config, checking both source_dir and generated."""
    source_dir = REPO_ROOT / skill_config.get("source_dir", "")
    if source_dir.is_dir():
        return source_dir

    # Also check generated output
    manifest = load_manifest()
    gen_root = REPO_ROOT / manifest.get("generation", {}).get("output_root", "generated")
    gen_dir = gen_root / "skills" / skill_key
    if gen_dir.is_dir():
        return gen_dir

    return source_dir  # Return source_dir even if missing, so error message is clear


def main():
    parser = argparse.ArgumentParser(description="Validate skill folder structure")
    parser.add_argument("--skill", help="Validate a specific skill by manifest key")
    parser.add_argument("--source", help="Validate a skill directory directly (path)")
    args = parser.parse_args()

    all_errors = []

    if args.source:
        source = Path(args.source)
        key = source.name
        errors = validate_skill(key, source)
        all_errors.extend(errors)
        if not errors:
            print(f"  OK: {key} ({source})")
    elif args.skill:
        manifest = load_manifest()
        skills = manifest.get("skills", {})
        if args.skill not in skills:
            print(f"Error: skill '{args.skill}' not found in manifest", file=sys.stderr)
            sys.exit(1)
        skill_dir = resolve_skill_dir(args.skill, skills[args.skill])
        errors = validate_skill(args.skill, skill_dir)
        all_errors.extend(errors)
        if not errors:
            print(f"  OK: {args.skill} ({skill_dir})")
    else:
        manifest = load_manifest()
        skills = manifest.get("skills", {})
        print(f"Validating {len(skills)} skill(s)...")
        for key, config in skills.items():
            if not config.get("validate", True):
                print(f"  Skipped {key}: validate=false")
                continue
            skill_dir = resolve_skill_dir(key, config)
            errors = validate_skill(key, skill_dir)
            all_errors.extend(errors)
            if not errors:
                print(f"  OK: {key} ({skill_dir})")

    if all_errors:
        print(f"\nValidation FAILED ({len(all_errors)} error(s)):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)
    else:
        print("\nAll skills valid.")


if __name__ == "__main__":
    main()
