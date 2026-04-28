#!/usr/bin/env python3
"""Enrich a downstream project with scaffold capabilities, or audit the
scaffold's own ownership taxonomy.

Usage:
    # Enrich a downstream project (default)
    python3 scripts/enrich-project.py TARGET_DIR [--profile PROFILE] [--force] [--dry-run]

    # Audit scaffold-side ownership taxonomy (M9 §8)
    python3 scripts/enrich-project.py --self-check

    # Compare downstream project against its .scaffold/manifest.json (M9 §5)
    # (Slice B writes the manifest; --check is harmless without one.)
    python3 scripts/enrich-project.py --check TARGET_DIR [--strict]

Resolves the named profile from capabilities/project-capabilities.yaml, then copies
agents, docs, hooks, and scripts to TARGET_DIR. Generates .claude/CLAUDE.md from template.
Skills are not copied directly — use generate-skill.py and package-skill.py for those.

If --profile is omitted, uses 'default'.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "capabilities" / "project-capabilities.yaml"

# Files that describe THIS scaffold repo and must never be installed into a
# downstream project. Any code path attempting to copy one of these will fail.
# Phase M9 will move this list into capabilities/project-capabilities.yaml as
# part of the formal ownership-class taxonomy. Until then, this set is the
# authoritative explicit deny-list.
SCAFFOLD_INTERNAL_FILES = frozenset({
    "LICENSE",
    "CONTRIBUTING.md",
    "README.md",
    "AGENTS.md",
})


def assert_not_scaffold_internal(rel_path):
    """Refuse to install scaffold-internal files into downstream projects."""
    if str(rel_path) in SCAFFOLD_INTERNAL_FILES:
        raise RuntimeError(
            f"Refusing to install scaffold-internal file '{rel_path}' into a "
            "downstream project. This file describes the scaffold repo itself."
        )


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
    try:
        rel_src = src.relative_to(REPO_ROOT)
        assert_not_scaffold_internal(rel_src)
    except ValueError:
        pass
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


# ============================================================================
# M9 — install lifecycle and provenance helpers
# ============================================================================

# Default normalization recipe used by `--check` and the manifest writer.
# Stored in `.scaffold/manifest.json` so it can evolve under schema_version.
NORMALIZATION_RECIPE = "lf-trim-trailing-ws-single-final-newline"
NORMALIZATION_VERSION = 1

# Valid ownership classes (M9 §2).
OWNERSHIP_CLASSES = frozenset({
    "scaffold",
    "bootstrap-frozen",
    "bootstrap-with-template-tracking",
    "scaffold-template",
    "scaffold-internal",
})

# Path prefixes that the constrained `ignore:` policy forbids any glob from
# matching (M9 §8). If `ignore: ["docs/**"]` is added and a path under
# `git ls-files docs/` matches, --self-check fails.
SELF_CHECK_PROTECTED_PREFIXES = (
    "docs/",
    ".claude/",
    "scripts/",
    "templates/",
    ".devcontainer/",
    "capabilities/",
)


def normalize_text(content_bytes):
    """Apply the normalization recipe to text content for hashing.

    Recipe (NORMALIZATION_RECIPE v1): UTF-8, LF endings, strip trailing
    whitespace per line, single trailing newline. Idempotent.
    """
    text = content_bytes.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")


def sha256_normalized(file_path):
    """Compute normalized sha256 of a file (text recipe)."""
    return hashlib.sha256(normalize_text(file_path.read_bytes())).hexdigest()


def sha256_strict(file_path):
    """Compute byte-exact sha256 of a file."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def compile_glob(glob_pattern):
    """Convert a gitignore-style glob to a compiled regex.

    Supports `**` (cross-slash), `*` (within-slash), and `?` (single char).
    """
    parts = []
    i = 0
    while i < len(glob_pattern):
        if glob_pattern[i:i + 2] == "**":
            parts.append(".*")
            i += 2
        elif glob_pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob_pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        elif glob_pattern[i] in ".()[]{}+^$|\\":
            parts.append(re.escape(glob_pattern[i]))
            i += 1
        else:
            parts.append(glob_pattern[i])
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def glob_matches(glob_pattern, path):
    """Check if a path matches a glob pattern."""
    return compile_glob(glob_pattern).match(path) is not None


def collect_classified_paths(manifest):
    """Walk the manifest and collect every enumerated path with its class.

    Returns dict {path: (ownership, source_section)}.
    Raises RuntimeError on duplicate paths or invalid ownership classes.
    """
    paths = {}

    typed_sections = {
        "agents": "source",
        "docs": "path",
        "hooks": "path",
        "scripts": "path",
    }
    for section, path_field in typed_sections.items():
        for key, entry in manifest.get(section, {}).items():
            p = entry.get(path_field)
            ownership = entry.get("ownership")
            if not p:
                continue
            if p in paths:
                raise RuntimeError(
                    f"Duplicate path in manifest: '{p}' (in {section!r} and {paths[p][1]!r})"
                )
            if ownership not in OWNERSHIP_CLASSES:
                raise RuntimeError(
                    f"Invalid ownership '{ownership}' for {p!r} in {section!r}; "
                    f"expected one of {sorted(OWNERSHIP_CLASSES)}"
                )
            paths[p] = (ownership, section)

    for p, entry in manifest.get("files", {}).items():
        ownership = entry.get("ownership")
        if p in paths:
            raise RuntimeError(
                f"Duplicate path in manifest: '{p}' (in 'files' and {paths[p][1]!r})"
            )
        if ownership not in OWNERSHIP_CLASSES:
            raise RuntimeError(
                f"Invalid ownership '{ownership}' for {p!r} in 'files'; "
                f"expected one of {sorted(OWNERSHIP_CLASSES)}"
            )
        paths[p] = (ownership, "files")

    return paths


def cmd_self_check():
    """Walk `git ls-files` of the scaffold repo and verify every path is
    classified by the manifest exactly once (or matches an `ignore:` glob,
    subject to the protected-prefix constraint).

    Implements M9 acceptance criterion #1 and the audit half of #2.
    Returns 0 on pass, 1 on failure.
    """
    try:
        manifest = load_manifest()
    except Exception as e:
        print(f"--self-check FAIL: cannot load manifest: {e}", file=sys.stderr)
        return 1

    try:
        classified = collect_classified_paths(manifest)
    except RuntimeError as e:
        print(f"--self-check FAIL: manifest invalid: {e}", file=sys.stderr)
        return 1

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"--self-check FAIL: `git ls-files` failed: {e}", file=sys.stderr)
        return 1
    tracked = sorted(p for p in result.stdout.strip().split("\n") if p)

    ignore_globs = manifest.get("ignore", []) or []

    # Constraint: no `ignore:` glob may match a path under a protected prefix.
    constraint_violations = []
    for glob_pattern in ignore_globs:
        compiled = compile_glob(glob_pattern)
        for tracked_path in tracked:
            if not any(tracked_path.startswith(pfx) for pfx in SELF_CHECK_PROTECTED_PREFIXES):
                continue
            if compiled.match(tracked_path):
                constraint_violations.append((glob_pattern, tracked_path))

    # Classify each tracked file.
    unclassified = []
    double_classified = []
    classified_count = 0
    ignored_count = 0
    classified_summary = {}  # ownership -> count

    for path in tracked:
        in_manifest = path in classified
        matching_globs = [g for g in ignore_globs if glob_matches(g, path)]
        ignored = bool(matching_globs)

        if in_manifest and ignored:
            double_classified.append((path, classified[path][1], matching_globs))
        elif in_manifest:
            classified_count += 1
            ownership = classified[path][0]
            classified_summary[ownership] = classified_summary.get(ownership, 0) + 1
        elif ignored:
            ignored_count += 1
        else:
            unclassified.append(path)

    failed = bool(constraint_violations or unclassified or double_classified)

    print(f"--self-check: {len(tracked)} tracked files")
    print(f"  classified: {classified_count}")
    for ownership in sorted(classified_summary):
        print(f"    {ownership}: {classified_summary[ownership]}")
    print(f"  ignored:    {ignored_count}")
    print(f"  unclassified: {len(unclassified)}")
    print(f"  double-classified: {len(double_classified)}")
    print(f"  ignore: glob constraint violations: {len(constraint_violations)}")

    if constraint_violations:
        print("\nFAIL: `ignore:` globs matching protected paths:", file=sys.stderr)
        for glob_pattern, path in constraint_violations:
            print(f"  {glob_pattern!r} matches {path!r}", file=sys.stderr)
        print(
            "  (paths under "
            + ", ".join(p.rstrip("/") for p in SELF_CHECK_PROTECTED_PREFIXES)
            + " are protected)",
            file=sys.stderr,
        )

    if unclassified:
        print("\nFAIL: tracked files not classified:", file=sys.stderr)
        for path in unclassified:
            print(f"  {path}", file=sys.stderr)

    if double_classified:
        print("\nFAIL: tracked files matching both manifest and `ignore:`:", file=sys.stderr)
        for path, section, globs in double_classified:
            print(f"  {path}  (in {section!r}; matches {globs})", file=sys.stderr)

    return 1 if failed else 0


def load_downstream_manifest(target_dir):
    """Read .scaffold/manifest.json from a downstream project, or None."""
    manifest_path = Path(target_dir) / ".scaffold" / "manifest.json"
    if not manifest_path.exists():
        return None
    with open(manifest_path) as f:
        return json.load(f)


def cmd_check(target_dir, strict=False):
    """Compare on-disk files against the downstream manifest's recorded shas.

    Returns 0 if clean, 3 if drift detected, 1 on error.
    Slice B writes the downstream manifest; this command is harmless before then.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        return 1

    manifest = load_downstream_manifest(target)
    if manifest is None:
        print(f"No .scaffold/manifest.json in {target}.", file=sys.stderr)
        print("Run `enrich-project.py --reconcile` first (Slice B feature).", file=sys.stderr)
        return 1

    drift = []
    missing = []
    skipped = []
    clean = 0

    for entry in manifest.get("files", []):
        path = target / entry["path"]
        ownership = entry.get("ownership")

        # `bootstrap-frozen` files are never re-checked under --strict
        # (consistent with their never-re-checked semantics in M9 §2).
        if strict and ownership == "bootstrap-frozen":
            skipped.append(entry["path"])
            continue

        if not path.exists():
            missing.append(entry["path"])
            continue

        is_text = entry.get("text", True)
        if is_text:
            current_sha = sha256_strict(path) if strict else sha256_normalized(path)
            recorded_sha = entry.get("sha256_strict" if strict else "sha256")
        else:
            # Binary: same hash both modes.
            current_sha = sha256_strict(path)
            recorded_sha = entry.get("sha256") or entry.get("sha256_strict")

        if recorded_sha is None:
            print(f"  WARN: no recorded sha for {entry['path']} in mode "
                  f"{'strict' if strict else 'normalized'}", file=sys.stderr)
            continue

        if current_sha != recorded_sha:
            drift.append((entry["path"], ownership))
        else:
            clean += 1

    label = "--check (--strict)" if strict else "--check"
    print(f"{label}: scaffold {manifest.get('scaffold_version', '?')}")
    print(f"  clean: {clean}")
    print(f"  drifted: {len(drift)}")
    print(f"  missing: {len(missing)}")
    if skipped:
        print(f"  skipped (bootstrap-frozen, --strict): {len(skipped)}")

    for path, ownership in drift:
        print(f"  DRIFT: {path}  ({ownership})")
    for path in missing:
        print(f"  MISSING: {path}")

    if drift or missing:
        return 3
    return 0


# ============================================================================
# Default command: enrich
# ============================================================================


def cmd_enrich(args):
    """Enrich a downstream project from a manifest profile (existing behavior)."""
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

    # Project-shared settings.json — declares conservative permissions and
    # wires up the deny-dangerous-commands hook. Without this, the hook
    # script is copied but never actually runs.
    print("\nProject settings:")
    src = REPO_ROOT / ".claude" / "settings.json"
    dest = target / ".claude" / "settings.json"
    if src.exists():
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

    # Container files for autonomous unattended execution (opt-in to use,
    # but included by default since CONTAINERIZATION.md is also included
    # and users without Docker simply do not run them).
    print("\nContainer files:")
    container_files = [
        "scripts/container-setup.sh",
        "scripts/verify-container.sh",
        ".devcontainer/devcontainer.json",
        ".devcontainer/Dockerfile",
        ".devcontainer/entrypoint.sh",
        ".devcontainer/init-firewall.sh",
    ]
    for filename in container_files:
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


def main():
    parser = argparse.ArgumentParser(description="Enrich a downstream project from scaffold manifest, or audit scaffold ownership.")
    parser.add_argument("target_dir", nargs="?", help="Path to the downstream project directory (required for enrich and --check)")
    parser.add_argument("--profile", default="default", help="Manifest profile to use (default: 'default')")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without doing it")
    parser.add_argument("--self-check", dest="self_check", action="store_true",
                        help="Audit scaffold-side ownership taxonomy (M9 §8); ignores target_dir")
    parser.add_argument("--check", action="store_true",
                        help="Compare downstream project against its .scaffold/manifest.json")
    parser.add_argument("--strict", action="store_true",
                        help="Use byte-exact hashing instead of normalized (skips bootstrap-frozen)")
    args = parser.parse_args()

    if args.self_check:
        sys.exit(cmd_self_check())

    if args.check:
        if not args.target_dir:
            parser.error("--check requires target_dir")
        sys.exit(cmd_check(args.target_dir, strict=args.strict))

    if not args.target_dir:
        parser.error("target_dir is required for enrichment (or use --self-check)")
    cmd_enrich(args)


if __name__ == "__main__":
    main()
