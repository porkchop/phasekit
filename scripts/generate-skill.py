#!/usr/bin/env python3
"""Generate skill folders from templates and the capability manifest.

Usage:
    python3 scripts/generate-skill.py [--skill SKILL_KEY] [--output-root DIR] [--force]

If --skill is omitted, generates all skills declared in the manifest.
Output defaults to the generation.output_root from the manifest (generated/).
"""

import argparse
import os
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
TEMPLATE_DIR = REPO_ROOT / "templates" / "skill"


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def render_template(template_text, variables):
    """Replace {{VAR_NAME}} placeholders with values from variables dict."""
    def replacer(match):
        key = match.group(1)
        return variables.get(key, match.group(0))
    return re.sub(r"\{\{(\w+)\}\}", replacer, template_text)


def title_case_from_key(key):
    """Convert 'my-skill-name' to 'My Skill Name'."""
    return key.replace("-", " ").replace("_", " ").title()


def generate_skill(skill_key, skill_config, output_root, force=False):
    """Generate a single skill folder from templates and manifest data."""
    skill_dir = output_root / "skills" / skill_key
    if skill_dir.exists() and not force:
        print(f"  Skipped {skill_key}: already exists (use --force to overwrite)")
        return False

    variables = {
        "SKILL_NAME": skill_key,
        "SKILL_DISPLAY_NAME": title_case_from_key(skill_key),
        "SKILL_DESCRIPTION": skill_config.get("purpose", ""),
        "SKILL_PURPOSE": skill_config.get("purpose", ""),
    }

    # Walk template directory and render each file
    for template_file in TEMPLATE_DIR.rglob("*"):
        if template_file.is_dir():
            continue
        rel_path = template_file.relative_to(TEMPLATE_DIR)
        # Strip .template from output filenames (e.g. SKILL.template.md -> SKILL.md)
        dest_name = str(rel_path).replace(".template", "")
        dest_path = skill_dir / dest_name

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        template_text = template_file.read_text()
        rendered = render_template(template_text, variables)
        dest_path.write_text(rendered)

    print(f"  Generated {skill_key} -> {skill_dir}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate skill folders from manifest")
    parser.add_argument("--skill", help="Generate a specific skill by key (default: all)")
    parser.add_argument("--output-root", help="Output root directory (default: from manifest)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing skill folders")
    args = parser.parse_args()

    manifest = load_manifest()
    skills = manifest.get("skills", {})
    generation = manifest.get("generation", {})

    output_root = Path(args.output_root) if args.output_root else REPO_ROOT / generation.get("output_root", "generated")

    if args.skill:
        if args.skill not in skills:
            print(f"Error: skill '{args.skill}' not found in manifest", file=sys.stderr)
            print(f"Available skills: {', '.join(skills.keys())}", file=sys.stderr)
            sys.exit(1)
        targets = {args.skill: skills[args.skill]}
    else:
        targets = skills

    print(f"Generating {len(targets)} skill(s) to {output_root}/skills/")
    generated = 0
    for key, config in targets.items():
        if generate_skill(key, config, output_root, force=args.force):
            generated += 1

    print(f"Done. {generated} skill(s) generated.")


if __name__ == "__main__":
    main()
