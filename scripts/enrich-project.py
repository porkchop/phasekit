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
import contextlib
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
    _HAS_FLOCK = True
except ImportError:
    # fcntl is POSIX-only; on Windows we'll warn-and-proceed under --no-lock
    fcntl = None
    _HAS_FLOCK = False

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


# === Schema migrations (M9 §3, F4) =========================================
# Linear-chain only. Each migration is a pure function (no I/O) keyed by
# (from_version, to_version) and adds the schema deltas required to bring a
# manifest one version forward. Engine composes them in order.

def _migrate_v0_to_v1(manifest):
    """v0 was the unreleased pre-M9 in-memory shape (no normalization block,
    no per-entry overlays). v1 adds both. Pure function — no I/O.

    This migration exists primarily to test the migration mechanism (M9
    acceptance criterion #9). Real v0 manifests do not exist in the wild;
    pre-M9 projects had no manifest at all and use --reconcile instead.
    """
    manifest = json.loads(json.dumps(manifest))  # deep copy via JSON

    if "normalization" not in manifest:
        manifest["normalization"] = {
            "recipe": NORMALIZATION_RECIPE,
            "version": NORMALIZATION_VERSION,
        }

    for entry in manifest.get("files", []):
        if "overlays" not in entry:
            entry["overlays"] = []

    manifest["schema_version"] = 1
    return manifest


# Registry of migrations keyed by (from_version, to_version). Linear chain:
# adding (1, 2) is sufficient for a v2 release; engine composes (0,1) ∘ (1,2).
MIGRATIONS = {
    (0, 1): _migrate_v0_to_v1,
}


def migrate_manifest(manifest):
    """Apply linear-chain migrations from manifest's schema_version up to
    SCHEMA_VERSION_CURRENT. Returns the migrated manifest. Idempotent: a
    manifest already at SCHEMA_VERSION_CURRENT is returned unchanged.
    """
    current = manifest.get("schema_version", 0)
    while current < SCHEMA_VERSION_CURRENT:
        next_version = current + 1
        migrate_fn = MIGRATIONS.get((current, next_version))
        if migrate_fn is None:
            raise RuntimeError(
                f"No migration from schema v{current} to v{next_version} "
                f"(target schema v{SCHEMA_VERSION_CURRENT})"
            )
        manifest = migrate_fn(manifest)
        current = manifest.get("schema_version", next_version)
    return manifest


def cmd_migrate_only(target_dir, no_lock=False):
    """Rewrite the on-disk manifest forward to SCHEMA_VERSION_CURRENT.

    No-op (exit 0) if the manifest is already current.
    """
    target = Path(target_dir).resolve()
    manifest = load_downstream_manifest(target)
    if manifest is None:
        print(f"No .scaffold/manifest.json in {target}", file=sys.stderr)
        return 1

    current_version = manifest.get("schema_version", 0)
    if current_version == SCHEMA_VERSION_CURRENT:
        print(f"Manifest already at schema_version {SCHEMA_VERSION_CURRENT}.")
        return 0

    try:
        migrated = migrate_manifest(manifest)
    except RuntimeError as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1

    with target_lock(target, no_lock=no_lock):
        manifest_path = target / ".scaffold" / "manifest.json"
        tmp_path = target / ".scaffold" / "manifest.json.scaffold-tmp"
        tmp_path.write_text(json.dumps(migrated, indent=2) + "\n")
        os.replace(tmp_path, manifest_path)

    print(f"Migrated manifest from schema v{current_version} to v{migrated['schema_version']}.")
    return 0


# === Manifest writer (M9 §3, F8) ===========================================

# Latest schema version this engine writes. Older manifests are migrated
# in-memory before any operation; the on-disk manifest is rewritten only by
# mutating commands. Linear-chain migrations live in scripts/migrations/.
SCHEMA_VERSION_CURRENT = 1


def utc_now_iso():
    """ISO-8601 UTC timestamp with second precision and a trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_scaffold_version():
    """Compute scaffold version (semver-with-git or fallback) and short commit.

    Returns (version_string, commit_string).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        commit = result.stdout.strip()
        return f"0.0.0+git.{commit}", commit
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0.0.0+git.unknown", "unknown"


def compute_file_shas(file_path, is_text):
    """Compute (normalized, strict) sha256 pair for a file.

    For binary files (`is_text=False`), normalized == strict.
    """
    strict = sha256_strict(file_path)
    normalized = sha256_normalized(file_path) if is_text else strict
    return normalized, strict


@contextlib.contextmanager
def target_lock(target_dir, no_lock=False):
    """Per-target advisory lock via fcntl.flock on .scaffold/manifest.json.lock.

    On filesystems without flock support, or when `no_lock=True`, warn and proceed.
    Lock is released automatically on context exit (process exit also releases).
    """
    target_dir = Path(target_dir).resolve()
    if no_lock or not _HAS_FLOCK:
        if no_lock:
            print("  Warning: --no-lock requested; concurrent runs may corrupt manifest", file=sys.stderr)
        else:
            print("  Warning: flock unavailable on this platform; concurrent runs may corrupt manifest", file=sys.stderr)
        yield
        return

    lock_dir = target_dir / ".scaffold"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "manifest.json.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                print(
                    f"Error: another enrich-project.py process is operating on {target_dir}",
                    file=sys.stderr,
                )
                print("  (use --no-lock if you have your own mutex)", file=sys.stderr)
                sys.exit(2)
            raise
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lookup_template_info(scaffold_manifest, downstream_path):
    """For `bootstrap-with-template-tracking`, look up rendered_from + template_sha.

    Returns (rendered_from, template_sha) or (None, None).
    """
    files_section = scaffold_manifest.get("files", {}) or {}
    entry = files_section.get(downstream_path)
    if not entry:
        return None, None
    rendered_from = entry.get("rendered_from")
    if not rendered_from:
        return None, None
    template_path = REPO_ROOT / rendered_from
    if not template_path.exists():
        return rendered_from, None
    return rendered_from, sha256_strict(template_path)


def build_manifest_entry(downstream_path, ownership, target_dir,
                          scaffold_manifest, is_text=True, rendered_from_override=None):
    """Build one entry for the downstream `.scaffold/manifest.json`."""
    file_path = Path(target_dir) / downstream_path
    if not file_path.exists():
        return None  # Caller decides how to surface missing files

    sha_norm, sha_strict_val = compute_file_shas(file_path, is_text)

    entry = {
        "path": downstream_path,
        "ownership": ownership,
        "text": is_text,
        "sha256": sha_norm,
        "sha256_strict": sha_strict_val,
        "overlays": [],
        "installed_at": utc_now_iso(),
    }

    if ownership == "bootstrap-with-template-tracking":
        rendered_from, template_sha = lookup_template_info(
            scaffold_manifest, downstream_path
        )
        if rendered_from_override:
            rendered_from = rendered_from_override
            tmpl = REPO_ROOT / rendered_from_override
            template_sha = sha256_strict(tmpl) if tmpl.exists() else None
        if rendered_from:
            entry["rendered_from"] = rendered_from
        if template_sha:
            entry["template_sha"] = template_sha

    return entry


def write_downstream_manifest(target_dir, scaffold_manifest, profile, file_specs):
    """Write `.scaffold/manifest.json` atomically via tmp + os.replace.

    file_specs: list of dicts with keys {path, ownership, text, rendered_from?}.
    Returns the manifest path on success.
    """
    target = Path(target_dir).resolve()
    scaffold_dir = target / ".scaffold"
    scaffold_dir.mkdir(parents=True, exist_ok=True)

    version, commit = get_scaffold_version()

    entries = []
    for spec in file_specs:
        entry = build_manifest_entry(
            spec["path"],
            spec["ownership"],
            target,
            scaffold_manifest,
            is_text=spec.get("text", True),
            rendered_from_override=spec.get("rendered_from"),
        )
        if entry is not None:
            entries.append(entry)

    manifest = {
        "schema_version": SCHEMA_VERSION_CURRENT,
        "scaffold_version": version,
        "scaffold_commit": commit,
        "profile": profile,
        "enriched_at": utc_now_iso(),
        "normalization": {
            "recipe": NORMALIZATION_RECIPE,
            "version": NORMALIZATION_VERSION,
        },
        "files": entries,
    }

    manifest_path = scaffold_dir / "manifest.json"
    tmp_path = scaffold_dir / "manifest.json.scaffold-tmp"
    tmp_path.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp_path, manifest_path)
    return manifest_path


# === Install-target enumeration ============================================
# What the scaffold installs into a downstream project for a given profile.
# Used by both `cmd_enrich` (after copies) and `cmd_reconcile` (read-only).

# Docs whose downstream output is rendered from a template rather than copied
# from the scaffold's own doc. Keep this in sync with cmd_enrich's template_map.
DOC_TEMPLATE_MAP = {
    "SPEC": "templates/spec.template.md",
    "ARCHITECTURE": "templates/architecture.template.md",
    "PROD_REQUIREMENTS": "templates/prod-requirements.template.md",
}

# Docs that exist in the scaffold but never install downstream (matches
# the existing cmd_enrich filter).
SCAFFOLD_ONLY_DOCS = {"META_SPEC", "META_PHASES", "CAPABILITY_MANIFEST"}

# Scripts in the manifest's `scripts:` section that are workflow-relevant
# downstream (not all scaffold scripts; matches cmd_enrich's filter).
WORKFLOW_SCRIPTS = ("run-phase", "run-until-done")

# Always-installed files that aren't enumerated by the typed sections of the
# scaffold manifest (scaffold root + container files).
ALWAYS_INSTALLED_FILE_PATHS = (
    "CONTINUE_PROMPT.txt",
    "scripts/container-setup.sh",
    "scripts/verify-container.sh",
    ".devcontainer/devcontainer.json",
    ".devcontainer/Dockerfile",
    ".devcontainer/entrypoint.sh",
    ".devcontainer/init-firewall.sh",
)


def enumerate_install_targets(scaffold_manifest, resolved_profile):
    """Return list of {path, ownership, text, rendered_from?} specs the scaffold
    installs for this profile. Caller filters to paths actually on disk.

    Mirrors cmd_enrich's copy logic so the manifest reflects what was installed.
    """
    specs = []
    files_section = scaffold_manifest.get("files", {}) or {}

    # Agents
    agents = scaffold_manifest.get("agents", {})
    for key in resolved_profile.get("include_agents", []):
        entry = agents.get(key)
        if entry:
            specs.append({
                "path": entry["source"],
                "ownership": entry.get("ownership", "scaffold"),
                "text": True,
            })

    # Docs (filter scaffold-only; map template-rendered docs)
    docs = scaffold_manifest.get("docs", {})
    for key in resolved_profile.get("include_docs", []):
        if key in SCAFFOLD_ONLY_DOCS:
            continue
        entry = docs.get(key)
        if not entry:
            continue
        spec = {
            "path": entry["path"],
            "ownership": entry.get("ownership", "scaffold"),
            "text": True,
        }
        rendered_from = DOC_TEMPLATE_MAP.get(key)
        if rendered_from:
            spec["rendered_from"] = rendered_from
        specs.append(spec)

    # Hooks
    hooks = scaffold_manifest.get("hooks", {})
    for key in resolved_profile.get("include_hooks", []):
        entry = hooks.get(key)
        if entry:
            specs.append({
                "path": entry["path"],
                "ownership": entry.get("ownership", "scaffold"),
                "text": True,
            })

    # Workflow scripts (subset of scripts section)
    scripts = scaffold_manifest.get("scripts", {})
    included_scripts = set(resolved_profile.get("include_scripts", []))
    for key in WORKFLOW_SCRIPTS:
        if key in included_scripts:
            entry = scripts.get(key)
            if entry:
                specs.append({
                    "path": entry["path"],
                    "ownership": entry.get("ownership", "scaffold"),
                    "text": True,
                })

    # .claude/settings.json (always installed; class from manifest)
    settings_entry = files_section.get(".claude/settings.json", {})
    specs.append({
        "path": ".claude/settings.json",
        "ownership": settings_entry.get("ownership", "bootstrap-with-template-tracking"),
        "text": True,
        "rendered_from": settings_entry.get("rendered_from"),
    })

    # .claude/CLAUDE.md (rendered from template; downstream class is
    # bootstrap-with-template-tracking even though the scaffold's own copy
    # is scaffold-internal — see CAPABILITY_MANIFEST.md "Notes" on classes
    # describing downstream behavior).
    specs.append({
        "path": ".claude/CLAUDE.md",
        "ownership": "bootstrap-with-template-tracking",
        "text": True,
        "rendered_from": "templates/CLAUDE.template.md",
    })

    # Always-installed flat files
    for path in ALWAYS_INSTALLED_FILE_PATHS:
        f_entry = files_section.get(path, {})
        specs.append({
            "path": path,
            "ownership": f_entry.get("ownership", "scaffold"),
            "text": f_entry.get("text", True),
        })

    return specs


# === --reconcile (M9 §5) ===================================================

def cmd_reconcile(target_dir, profile=None, no_lock=False, force=False):
    """Build a `.scaffold/manifest.json` for a project enriched before M9.

    Walks the scaffold's profile-resolved install targets, hashes whatever is
    on disk in `target_dir`, and writes a retroactive manifest. If a manifest
    already exists and force=False, refuses (use --force to overwrite).

    Returns 0 on success, 1 on error.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        return 1

    existing = load_downstream_manifest(target)
    if existing is not None and not force:
        print(
            f"A .scaffold/manifest.json already exists in {target}.\n"
            "  Use --force to overwrite (rare; usually you want --check or --upgrade).",
            file=sys.stderr,
        )
        return 1

    scaffold_manifest = load_manifest()
    profiles = scaffold_manifest.get("profiles", {})

    # Pick a profile: explicit > existing manifest's > "default"
    if profile is None:
        if existing is not None and existing.get("profile"):
            profile = existing["profile"]
        else:
            profile = "default"

    resolved = resolve_profile(profiles, profile)
    targets = enumerate_install_targets(scaffold_manifest, resolved)
    on_disk = [s for s in targets if (target / s["path"]).exists()]
    missing = [s["path"] for s in targets if not (target / s["path"]).exists()]

    print(f"--reconcile: {len(on_disk)} files found on disk, {len(missing)} missing")
    for path in missing:
        print(f"  MISSING: {path}")

    with target_lock(target, no_lock=no_lock):
        manifest_path = write_downstream_manifest(
            target, scaffold_manifest, profile, on_disk
        )
    print(f"Manifest written: {manifest_path}")
    return 0


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
        print("Run `enrich-project.py --reconcile` first.", file=sys.stderr)
        return 1

    # Older manifests are upgraded in-memory before any read. The on-disk
    # manifest is only rewritten by mutating commands (use --migrate-only).
    try:
        manifest = migrate_manifest(manifest)
    except RuntimeError as e:
        print(f"--check: {e}", file=sys.stderr)
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

    # Provenance manifest (M9). Writes `.scaffold/manifest.json` with a sha
    # for every file just installed, so subsequent `--check` / `--upgrade`
    # can detect drift. Skipped under --dry-run.
    if not args.dry_run:
        targets = enumerate_install_targets(manifest, resolved)
        on_disk = [s for s in targets if (target / s["path"]).exists()]
        with target_lock(target, no_lock=getattr(args, "no_lock", False)):
            mpath = write_downstream_manifest(target, manifest, args.profile, on_disk)
        print(f"\nManifest: {mpath}  ({len(on_disk)} entries)")

    print(f"\nDone. {copied} file(s) copied, {skipped} skipped (already exist).")
    if args.dry_run:
        print("(dry-run mode — no files were actually written)")


def main():
    parser = argparse.ArgumentParser(description="Enrich a downstream project from scaffold manifest, or audit scaffold ownership.")
    parser.add_argument("target_dir", nargs="?", help="Path to the downstream project directory (required for enrich, --check, --reconcile)")
    parser.add_argument("--profile", default=None, help="Manifest profile to use (default: 'default'; for --reconcile, defaults to existing manifest's profile)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files (enrich) or existing manifest (--reconcile)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without doing it")
    parser.add_argument("--self-check", dest="self_check", action="store_true",
                        help="Audit scaffold-side ownership taxonomy (M9 §8); ignores target_dir")
    parser.add_argument("--check", action="store_true",
                        help="Compare downstream project against its .scaffold/manifest.json")
    parser.add_argument("--reconcile", action="store_true",
                        help="Build a retroactive .scaffold/manifest.json for a project enriched before M9")
    parser.add_argument("--migrate-only", dest="migrate_only", action="store_true",
                        help="Migrate the manifest's schema_version forward without other side effects")
    parser.add_argument("--strict", action="store_true",
                        help="Use byte-exact hashing instead of normalized (skips bootstrap-frozen)")
    parser.add_argument("--no-lock", dest="no_lock", action="store_true",
                        help="Skip per-target fcntl.flock (for CI with its own mutexes)")
    args = parser.parse_args()

    if args.self_check:
        sys.exit(cmd_self_check())

    if args.check:
        if not args.target_dir:
            parser.error("--check requires target_dir")
        sys.exit(cmd_check(args.target_dir, strict=args.strict))

    if args.reconcile:
        if not args.target_dir:
            parser.error("--reconcile requires target_dir")
        sys.exit(cmd_reconcile(
            args.target_dir,
            profile=args.profile,
            no_lock=args.no_lock,
            force=args.force,
        ))

    if args.migrate_only:
        if not args.target_dir:
            parser.error("--migrate-only requires target_dir")
        sys.exit(cmd_migrate_only(args.target_dir, no_lock=args.no_lock))

    # Default: enrich
    if not args.target_dir:
        parser.error("target_dir is required for enrichment (or use --self-check)")
    if args.profile is None:
        args.profile = "default"
    cmd_enrich(args)


if __name__ == "__main__":
    main()
