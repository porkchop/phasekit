#!/usr/bin/env python3
"""Package validated skill folders into skill.zip archives.

Usage:
    python3 scripts/package-skill.py [--skill SKILL_KEY] [--force]

If --skill is omitted, packages all skills with package=true in the manifest.
Runs validation before packaging. Fails if validation fails.

Exit code 0 = all packaged, 1 = at least one failure.
"""

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "capabilities" / "project-capabilities.yaml"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate-skill.py"


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def resolve_skill_dir(skill_key, skill_config, manifest):
    """Resolve skill source directory, checking source_dir then generated output."""
    source_dir = REPO_ROOT / skill_config.get("source_dir", "")
    if source_dir.is_dir():
        return source_dir
    gen_root = REPO_ROOT / manifest.get("generation", {}).get("output_root", "generated")
    gen_dir = gen_root / "skills" / skill_key
    if gen_dir.is_dir():
        return gen_dir
    return source_dir


def validate_skill(skill_key, skill_dir):
    """Run validate-skill.py on a skill directory. Returns True if valid."""
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), "--source", str(skill_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Validation failed for {skill_key}:", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return False
    return True


def package_skill(skill_key, skill_config, manifest, force=False):
    """Validate and package a single skill into a zip archive."""
    package_output = skill_config.get("package_output")
    if not package_output:
        print(f"  Skipped {skill_key}: no package_output defined")
        return True

    skill_dir = resolve_skill_dir(skill_key, skill_config, manifest)
    dest = REPO_ROOT / package_output

    if dest.exists() and not force:
        print(f"  Skipped {skill_key}: {dest} already exists (use --force to overwrite)")
        return True

    # Validate first
    if not validate_skill(skill_key, skill_dir):
        return False

    # Create output directory
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Build zip archive
    resolved_root = skill_dir.resolve()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(skill_dir.rglob("*")):
            if file_path.is_dir():
                continue
            # Reject symlinks to prevent arbitrary file inclusion
            if file_path.is_symlink():
                print(f"  Warning: skipping symlink {file_path}", file=sys.stderr)
                continue
            # Verify resolved path stays within skill directory
            if not file_path.resolve().is_relative_to(resolved_root):
                print(f"  Warning: skipping out-of-tree path {file_path}", file=sys.stderr)
                continue
            # Skip dotfiles/dirs and __pycache__ anywhere in path
            rel_parts = file_path.relative_to(skill_dir).parts
            if any(p.startswith(".") for p in rel_parts) or "__pycache__" in rel_parts:
                continue
            arcname = file_path.relative_to(skill_dir)
            zf.write(file_path, arcname)

    # Report contents
    with zipfile.ZipFile(dest, "r") as zf:
        names = zf.namelist()

    print(f"  Packaged {skill_key} -> {dest} ({len(names)} file(s), {dest.stat().st_size} bytes)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Package skill folders into skill.zip")
    parser.add_argument("--skill", help="Package a specific skill by manifest key")
    parser.add_argument("--force", action="store_true", help="Overwrite existing archives")
    args = parser.parse_args()

    manifest = load_manifest()
    skills = manifest.get("skills", {})

    if args.skill:
        if args.skill not in skills:
            print(f"Error: skill '{args.skill}' not found in manifest", file=sys.stderr)
            sys.exit(1)
        targets = {args.skill: skills[args.skill]}
    else:
        targets = {k: v for k, v in skills.items() if v.get("package", False)}

    if not targets:
        print("No skills to package.")
        return

    print(f"Packaging {len(targets)} skill(s)...")
    failures = 0
    for key, config in targets.items():
        if not package_skill(key, config, manifest, force=args.force):
            failures += 1

    if failures:
        print(f"\nPackaging FAILED: {failures} skill(s) had errors.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\nAll {len(targets)} skill(s) packaged successfully.")


if __name__ == "__main__":
    main()
