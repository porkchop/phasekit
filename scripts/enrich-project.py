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
import signal
import stat
import subprocess
import sys
import tempfile
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

# Suffix used for atomic per-file write: copy goes to <dest>.scaffold-tmp,
# then os.replace promotes it. Orphan .scaffold-tmp files are swept at
# engine startup (M9 §5, F8).
TMP_SUFFIX = ".scaffold-tmp"


def get_scaffold_internal_paths():
    """Return the set of paths classified as `scaffold-internal` in the manifest.

    Replaces the M9-pre SCAFFOLD_INTERNAL_FILES constant. The manifest is
    now the single source of truth.
    """
    manifest = load_manifest()
    classified = collect_classified_paths(manifest)
    return frozenset(p for p, (cls, _) in classified.items() if cls == "scaffold-internal")


def assert_not_scaffold_internal(rel_path):
    """Refuse to install scaffold-internal files into downstream projects.

    M9: deny-list is derived from `capabilities/project-capabilities.yaml`.
    """
    internal = get_scaffold_internal_paths()
    if str(rel_path) in internal:
        raise RuntimeError(
            f"Refusing to install scaffold-internal file '{rel_path}' into a "
            "downstream project. This file is classified `scaffold-internal` "
            "in capabilities/project-capabilities.yaml."
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
        # v0.5.0 stack contract: name of the stack whose verify template +
        # conventions doc this profile seeds (None = no stack contract; the
        # stub verify template is used and no CONVENTIONS.md is installed).
        "stack": None,
    }

    if "extends" in profile:
        parent = resolve_profile(profiles, profile["extends"], _seen)
        for key in result:
            if key == "stack":
                result[key] = parent.get(key)
            else:
                result[key] = list(parent.get(key, []))

    for key in result:
        if key == "stack":
            if profile.get("stack") is not None:
                result[key] = profile["stack"]
            continue
        if key in profile:
            for item in profile[key]:
                if item not in result[key]:
                    result[key].append(item)

    return result


def atomic_copy(src, dest):
    """Atomic per-file copy: write to <dest>.scaffold-tmp, then os.replace.

    SIGKILL/disk-full mid-write leaves only the tmp file (which the next
    engine startup sweeps via `sweep_orphan_tmpfiles`). Never leaves a
    partial `dest`. We deliberately do NOT clean up tmp on Python
    exceptions either — orphan sweep is the single recovery path, so
    a transient error and a hard kill recover identically.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + TMP_SUFFIX)
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


# === Pre-install safety: secrets scan + symlink refusal (M9 §9, F11) =======

# Secret patterns refused at install time (M9 §9 row 8, F11b). Short
# placeholder forms (e.g. literal "sk-ant-..." with periods) do not match
# the live-key regexes because `.` is outside the allowed key char class.
_SECRET_PATTERNS = (
    ("AWS access key ID",       re.compile(r"AKIA[0-9A-Z]{16}")),
    ("PEM private key block",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Slack token",             re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Anthropic API key",       re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}")),
    ("GitHub PAT",              re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("GitHub OAuth token",      re.compile(r"gho_[A-Za-z0-9]{30,}")),
)


def scan_for_secrets(file_path):
    """Return a list of (label, snippet) tuples for any matches in the file.

    Empty list if the file is clean. Binary files (non-decodable as UTF-8)
    are scanned as bytes converted to str with errors='replace'.
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, IsADirectoryError):
        return []
    findings = []
    for label, pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            findings.append((label, m.group(0)[:48]))
    return findings


def assert_no_symlink_escape(target_root, dest_path):
    """Refuse to install a file when its destination, or any parent dir up
    to target_root, is a symlink whose realpath escapes target_root.

    Mitigates F11a: a malicious or misconfigured symlink could otherwise
    cause atomic_copy to write into another repo or system directory.
    """
    target_real = target_root.resolve(strict=False)
    p = dest_path
    while True:
        if p.is_symlink():
            real = p.resolve(strict=False)
            try:
                real.relative_to(target_real)
            except ValueError:
                raise RuntimeError(
                    f"Refusing to install via symlink that escapes target: "
                    f"{p} -> {real}"
                )
        if p == target_real or p.parent == p:
            return
        p = p.parent


def safe_install(src, dest, target_root):
    """Pre-install checks (secrets, symlinks) followed by atomic_copy."""
    findings = scan_for_secrets(src)
    if findings:
        labels = ", ".join(f"{label} ({snippet!r})" for label, snippet in findings)
        raise RuntimeError(
            f"Refusing to install {src.name}: secret-shaped strings found: {labels}"
        )
    assert_no_symlink_escape(target_root, dest)
    atomic_copy(src, dest)


def sweep_orphan_tmpfiles(target_dir):
    """Walk target_dir, log and remove any orphan `*.scaffold-tmp` files.

    Called at the start of mutating commands (M9 §5, F8). A SIGKILL during
    a previous run may have left these behind.
    """
    target_dir = Path(target_dir)
    if not target_dir.exists():
        return 0
    swept = 0
    for path in target_dir.rglob("*" + TMP_SUFFIX):
        # Skip the manifest's own tmp; it's owned by the lock holder
        # (which, if absent, means the previous run died — same recovery).
        try:
            path.unlink()
            print(f"  Swept orphan tmp file: {path}", file=sys.stderr)
            swept += 1
        except FileNotFoundError:
            pass
    return swept


def copy_file(src, dest, force=False, dry_run=False, target_root=None):
    """Copy a file, creating parent dirs as needed. Returns True if copied.

    If `target_root` is provided, runs pre-install safety checks (secrets
    scan, symlink-escape refusal) before the atomic copy. Callers in
    cmd_enrich and apply_upgrade_plan pass target_root; legacy callers
    fall back to a plain atomic_copy.
    """
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
    if target_root is not None:
        safe_install(src, dest, Path(target_root))
    else:
        atomic_copy(src, dest)
    print(f"  Copied: {dest}")
    return True


def render_template_text(template_path, project_name):
    """Render a scaffold template's text by substituting placeholders.

    Currently supports `{{PROJECT_NAME}}` and `{{OPTIONAL_REFERENCES}}`.
    Idempotent (substitutions on a fully-rendered file are no-ops).
    """
    text = Path(template_path).read_text()
    text = re.sub(r"\{\{PROJECT_NAME\}\}", project_name, text)
    text = re.sub(r"\{\{OPTIONAL_REFERENCES\}\}", "", text)
    return text


def install_from_spec(spec, target, project_name, force=False, dry_run=False):
    """Install one file (rendered or direct-copy) per its install spec.

    Spec keys:
      path           — destination path relative to `target`
      rendered_from  — optional template path (relative to scaffold REPO_ROOT)
      ownership      — informational; not used for routing here
      text           — informational; not used for routing here

    Returns True if a file was installed (or would be under --dry-run);
    False if skipped (existed and not force).
    """
    rel_path = spec["path"]
    rendered_from = spec.get("rendered_from")
    dest = Path(target) / rel_path

    src = REPO_ROOT / (rendered_from or rel_path)
    if not src.exists():
        print(f"  Warning: scaffold source missing: {src}", file=sys.stderr)
        return False

    if dest.exists() and not force:
        print(f"  Skip (exists): {dest}")
        return False
    if dry_run:
        print(f"  Would install: {dest}")
        return True

    if rendered_from:
        rendered = render_template_text(src, project_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / (dest.name + TMP_SUFFIX)
        tmp.write_text(rendered)
        findings = scan_for_secrets(tmp)
        if findings:
            tmp.unlink()
            labels = ", ".join(f"{label} ({snippet!r})" for label, snippet in findings)
            raise RuntimeError(
                f"Refusing to render {dest.name}: secret-shaped strings in output: {labels}"
            )
        assert_no_symlink_escape(Path(target), dest)
        os.replace(tmp, dest)
        print(f"  Rendered: {dest}")
    else:
        # Direct copy goes through the scaffold-internal deny-list and the
        # secrets scan + symlink check.
        try:
            rel_src = src.relative_to(REPO_ROOT)
            assert_not_scaffold_internal(rel_src)
        except ValueError:
            pass
        safe_install(src, dest, Path(target))
        print(f"  Copied: {dest}")
    return True


# Retained as a thin wrapper for any callers that haven't migrated to
# install_from_spec yet. Subsumed by install_from_spec and slated for
# removal once external callers (if any) update.
def render_claude_md(target_dir, project_name, force=False, dry_run=False):
    """Generate .claude/CLAUDE.md from template. Subsumed by install_from_spec."""
    spec = {
        "path": ".claude/CLAUDE.md",
        "rendered_from": "templates/CLAUDE.template.md",
        "ownership": "bootstrap-with-template-tracking",
        "text": True,
    }
    return install_from_spec(spec, target_dir, project_name, force=force, dry_run=dry_run)


# ============================================================================
# M9 — install lifecycle and provenance helpers
# ============================================================================

# Default normalization recipe used by `--check` and the manifest writer.
# Stored in `.scaffold/manifest.json` so it can evolve under schema_version.
NORMALIZATION_RECIPE = "lf-trim-trailing-ws-single-final-newline"
NORMALIZATION_VERSION = 1

# Valid ownership classes (M9 §2).
#
# `scaffold` and `scaffold-internal` describe scaffold-side classification
# (used by `--self-check`). `bootstrap-frozen` and
# `bootstrap-with-template-tracking` describe downstream classification.
# `scaffold-template` is scaffold-only.
#
# `scaffold-orphan` (added in Slice C.5) appears only in downstream manifests
# after `--upgrade` finds a previously-tracked file the new scaffold no
# longer declares. It's never produced scaffold-side; `--self-check` will
# never see it.
OWNERSHIP_CLASSES_SCAFFOLD_SIDE = frozenset({
    "scaffold",
    "bootstrap-frozen",
    "bootstrap-with-template-tracking",
    "scaffold-template",
    "scaffold-internal",
})
OWNERSHIP_CLASSES_DOWNSTREAM_ONLY = frozenset({
    "scaffold-orphan",
})
OWNERSHIP_CLASSES = OWNERSHIP_CLASSES_SCAFFOLD_SIDE  # backward-compat alias
OWNERSHIP_CLASS_ORPHAN = "scaffold-orphan"

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


# === --upgrade (M9 §6) =====================================================
# Three-way reconciliation: manifest sha (recorded) vs. scaffold-new sha
# (current canonical) vs. on-disk sha. Plan-then-confirm; never silent
# overwrite. F10 acceptance #4 and #6.

# Per-file action codes used in the plan output.
ACTION_NOOP = "no-op"            # clean and no scaffold update
ACTION_TAKE_NEW = "take-new"     # copy scaffold-new -> dest; update manifest sha
ACTION_KEEP_LOCAL = "keep-local" # leave on-disk; update manifest sha to current
ACTION_INSTALL = "install"       # not on disk yet; copy scaffold-new
ACTION_ORPHAN = "orphan"         # scaffold-new doesn't have it; leave + flag
ACTION_DELETE = "delete"         # explicit --accept-removal
ACTION_ADOPT = "adopt"           # collision-novel: record current sha as canonical
ACTION_RENAME_LOCAL = "rename-local"  # collision-novel: move out of the way
ACTION_REFUSE = "refuse"         # ambiguous: needs an explicit per-file flag

# v0.16.0: the manifest value recording a STANDING keep-local decision.
LOCAL_KEPT = "kept"

# v0.17.0 (row 1140): the classes a project OWNS after seeding. phasekit never
# overwrites them on upgrade (the verify gate's stub re-seed is the one
# exception), so a standing keep-local on one of them decides nothing — except
# on the verify gate, where it also stops that re-seed.
PROJECT_OWNED_CLASSES = frozenset({"bootstrap-frozen", "bootstrap-with-template-tracking"})


def _keep_is_moot(path, ownership):
    """True when a standing keep-local on this path can never change an outcome."""
    return ownership in PROJECT_OWNED_CLASSES and path != VERIFY_DEST_PATH


def _local_after(path, ownership, action, asked_keep):
    """What the manifest entry's `local` records after this upgrade: a keep the
    project ASKED for (flag, standing, or an interactive answer) stands; the
    ownership default records nothing; take-new/reinstall/removal releases it;
    and on a project-owned file it is moot, so it is never recorded (v0.17.0)."""
    if action not in (ACTION_KEEP_LOCAL, ACTION_NOOP) or not asked_keep:
        return None
    if _keep_is_moot(path, ownership):
        return None
    return LOCAL_KEPT


def _scaffold_source_for_spec(spec):
    """Return the scaffold-side path that supplies content for this install spec.

    For rendered files (`rendered_from` in spec), the source is the template.
    Otherwise it's the same path inside the scaffold repo.
    """
    rendered_from = spec.get("rendered_from")
    if rendered_from:
        return REPO_ROOT / rendered_from
    return REPO_ROOT / spec["path"]


def compute_upgrade_plan(
    target_dir, scaffold_manifest, existing_manifest, resolved_profile,
    keep_local=(), take_new=(), adopt=(), rename_local=(), accept_removal=(),
):
    """Compute the per-file upgrade plan.

    Returns a list of plan entries (dicts), each with keys:
      path, state, action, ownership, text, rendered_from?,
      manifest_sha, current_sha, scaffold_new_sha, note?
    """
    target = Path(target_dir).resolve()
    keep_local = set(keep_local)
    take_new = set(take_new)
    adopt = set(adopt)
    rename_local_map = dict(p.split("=", 1) for p in rename_local if "=" in p)
    accept_removal = set(accept_removal)

    existing_by_path = {f["path"]: f for f in existing_manifest.get("files", [])}
    new_specs = enumerate_install_targets(scaffold_manifest, resolved_profile)
    new_by_path = {s["path"]: s for s in new_specs}

    plans = []

    # Files declared by scaffold-new (with or without existing manifest entries).
    for path, spec in new_by_path.items():
        on_disk = target / path
        is_text = spec.get("text", True)
        ownership = spec["ownership"]
        rendered_from = spec.get("rendered_from")

        if on_disk.exists():
            cur_norm, cur_strict = compute_file_shas(on_disk, is_text)
            current_sha = cur_norm if is_text else cur_strict
        else:
            current_sha = cur_strict = None

        # Compute scaffold-new sha (what the engine would install today).
        src = _scaffold_source_for_spec(spec)
        if src.exists():
            new_norm, new_strict = compute_file_shas(src, is_text)
            scaffold_new_sha = new_norm if is_text else new_strict
        else:
            scaffold_new_sha = None

        existing = existing_by_path.get(path)
        manifest_sha = existing.get("sha256") if existing else None
        # v0.17.0 (row 1140): a file the scaffold owned until now and the
        # project owns from this release (docs/CONVENTIONS.md). This one
        # upgrade still judges it as the scaffold file it was — an unedited
        # copy takes the release's text, an edited or kept one is kept, never
        # refused — and from then on phasekit never writes it again.
        adopting = (
            existing is not None
            and existing.get("ownership") == "scaffold"
            and ownership in PROJECT_OWNED_CLASSES
        )
        # A re-profile moved a project-owned file to another stack's template
        # (docs/CONVENTIONS.md, static-web -> game-canvas). A copy still
        # byte-identical to the template it was seeded from takes the new
        # stack's text; an edited one is the project's and is kept. The
        # verify gate never: a configured gate is never overwritten.
        template_switched = (
            existing is not None
            and ownership == "bootstrap-with-template-tracking"
            and path != VERIFY_DEST_PATH
            and bool(rendered_from)
            and bool(existing.get("rendered_from"))
            and existing.get("rendered_from") != rendered_from
        )
        # v0.16.0: a `--keep-local` is a STANDING decision, recorded on the
        # manifest entry as `"local": "kept"`, and honoured by every later
        # upgrade until `--take-new PATH` releases it. Before this, the flag
        # re-baselined the file to the project's bytes and was then forgotten:
        # the next upgrade saw local == manifest with a newer scaffold version,
        # called it `update-available`, and TOOK NEW — silently deleting the
        # project's amendment. xmeo-v3's docs/CONVENTIONS.md lost its amended
        # block that way three times (row 813).
        standing = bool(existing) and existing.get("local") == LOCAL_KEPT
        keep = path in keep_local or (standing and path not in take_new)

        # State + default action
        if existing is None:
            if current_sha is not None:
                # collision-novel: scaffold-new declares a path the project already has
                state = "collision-novel"
                if path in adopt:
                    action = ACTION_ADOPT
                elif path in rename_local_map:
                    action = ACTION_RENAME_LOCAL
                elif ownership in PROJECT_OWNED_CLASSES:
                    # v0.17.0: the project owns this path after seeding
                    # anyway, so its own file IS the seed — adopt, never
                    # refuse (a project-owned companion that pre-dates the
                    # release that declares it).
                    action = ACTION_ADOPT
                else:
                    action = ACTION_REFUSE
            else:
                state = "new-install"
                action = ACTION_INSTALL
        else:
            # Tracked
            #
            # v0.5.0 stub-reseed: when a stack profile supplies a real verify
            # gate and the on-disk gate is still the configure-me stub
            # (PHASEKIT_VERIFY_CONFIGURED=0), take the stack template even
            # though bootstrap-* files are normally never overwritten. A
            # configured gate (sentinel flipped or script rewritten) never
            # matches and is always kept — seeding fills stubs only.
            stub_reseed = (
                path == VERIFY_DEST_PATH
                and rendered_from in STACK_VERIFY_TEMPLATES.values()
                and current_sha is not None
                and verify_gate_is_stub(on_disk)
            )
            if current_sha is None:
                state = "missing"
                action = ACTION_INSTALL
            elif stub_reseed:
                state = "stub-reseed"
                action = ACTION_KEEP_LOCAL if keep else ACTION_TAKE_NEW
            elif (template_switched and not keep
                  and (path in take_new or cur_strict == existing.get("template_sha"))):
                state = "template-switched"
                action = ACTION_TAKE_NEW
            elif current_sha == manifest_sha:
                # local == manifest. For `scaffold` class, also compare
                # scaffold-new sha to surface an "update available". For
                # bootstrap-* classes, content-tracking is the manifest sha
                # only; template-source drift surfaces via
                # `--check --include-templates`, not via the upgrade plan
                # (M9 review F5 fix).
                if (
                    (ownership == "scaffold" or adopting)
                    and scaffold_new_sha is not None
                    and scaffold_new_sha != manifest_sha
                ):
                    state = "update-available"
                    # Default action is to take the scaffold-new version, but
                    # `--keep-local` overrides — the user intent ("preserve
                    # my version even though scaffold has a newer canonical")
                    # applies symmetrically to drifted and update-available.
                    if keep:
                        action = ACTION_KEEP_LOCAL
                    else:
                        action = ACTION_TAKE_NEW
                else:
                    state = "clean"
                    # An explicit --take-new re-renders a clean file too: it
                    # is how a project adopts a later template (v0.17.0).
                    action = ACTION_TAKE_NEW if path in take_new else ACTION_NOOP
            else:
                # drifted: current != manifest
                state = "drifted"
                if keep:
                    action = ACTION_KEEP_LOCAL
                elif path in take_new:
                    action = ACTION_TAKE_NEW
                else:
                    # bootstrap-* default keep-local; scaffold default refuse
                    if ownership in ("bootstrap-frozen", "bootstrap-with-template-tracking"):
                        action = ACTION_KEEP_LOCAL
                    else:
                        action = ACTION_REFUSE

        # What the entry records afterwards: a keep-local the project ASKED for
        # (flagged now, or standing from before) stands; the ownership default
        # (a drifted bootstrap-* file is kept without asking) is not a decision
        # and records nothing; take-new, reinstall or removal releases it. On a
        # project-owned file it is moot and cleared, with a note (v0.17.0).
        local_after = _local_after(path, ownership, action, path in keep_local or standing)
        cleared_standing = (
            standing and action in (ACTION_KEEP_LOCAL, ACTION_NOOP)
            and _keep_is_moot(path, ownership)
        )

        # v0.17.0: the template base a project-owned file records, so that
        # `check --include-templates` keeps reporting a template change until
        # the project acts on it (before, every upgrade re-stamped the current
        # template's sha and the advisory vanished at the next upgrade). A
        # file this run writes is based on today's template (None = current);
        # `--keep-local PATH` acknowledges today's template; a file adopted
        # from the scaffold class or from a collision is based on its own
        # bytes; anything else carries the recorded base forward.
        template_sha_after = None
        if (ownership == "bootstrap-with-template-tracking"
                and action in (ACTION_KEEP_LOCAL, ACTION_NOOP, ACTION_ADOPT)):
            if path in keep_local:
                template_sha_after = None
            elif (adopting or template_switched or action == ACTION_ADOPT
                  or not existing.get("template_sha")):
                # Based on its own bytes: adopted from the scaffold class or a
                # collision, kept across a template switch, or no base recorded
                # (a scaffold-orphan coming back, a pre-M9 entry).
                template_sha_after = cur_strict
            else:
                template_sha_after = existing.get("template_sha")
        plans.append({
            "path": path,
            "state": state,
            "action": action,
            "ownership": ownership,
            "text": is_text,
            "rendered_from": rendered_from,
            "manifest_sha": manifest_sha,
            "current_sha": current_sha,
            "scaffold_new_sha": scaffold_new_sha,
            "rename_target": rename_local_map.get(path),
            "standing": (standing and action == ACTION_KEEP_LOCAL and path not in keep_local
                         and not cleared_standing),
            "local_after": local_after,
            "cleared_standing": cleared_standing,
            "adopting": adopting,
            "template_sha_after": template_sha_after,
        })

    # Removed: in existing manifest but not in scaffold-new install set.
    for path, existing_entry in existing_by_path.items():
        if path in new_by_path:
            continue
        on_disk = target / path
        if on_disk.exists():
            if path in accept_removal:
                action = ACTION_DELETE
            else:
                action = ACTION_ORPHAN
            plans.append({
                "path": path,
                "state": "removed",
                "action": action,
                "ownership": existing_entry.get("ownership", "scaffold"),
                "text": existing_entry.get("text", True),
                "rendered_from": existing_entry.get("rendered_from"),
                "manifest_sha": existing_entry.get("sha256"),
                "current_sha": None,
                "scaffold_new_sha": None,
            })

    return plans


def print_upgrade_plan(plans):
    """Pretty-print an upgrade plan grouped by action."""
    by_action = {}
    for p in plans:
        by_action.setdefault(p["action"], []).append(p)

    summary = ", ".join(f"{action}: {len(rows)}" for action, rows in sorted(by_action.items()))
    print(f"--upgrade plan: {summary}")
    print()
    for action in (ACTION_TAKE_NEW, ACTION_INSTALL, ACTION_KEEP_LOCAL,
                   ACTION_ADOPT, ACTION_RENAME_LOCAL, ACTION_DELETE,
                   ACTION_ORPHAN, ACTION_REFUSE, ACTION_NOOP):
        rows = by_action.get(action, [])
        if not rows:
            continue
        print(f"  [{action}] ({len(rows)})")
        for p in rows:
            note = ""
            if p["state"] == "drifted":
                note = "  (local edits differ from manifest)"
            elif p["state"] == "update-available":
                note = "  (scaffold has a newer canonical version)"
            elif p["state"] == "update-available-advisory":
                note = "  (scaffold updated but bootstrap-* never auto-overwritten)"
            elif p["state"] == "template-switched":
                note = "  (profile changed its template; the copy was unedited, taking the new one)"
            elif p["state"] == "stub-reseed":
                note = "  (verify gate still in stub mode; seeding the stack profile's real gate)"
            elif p["state"] == "collision-novel":
                note = "  (scaffold v2 declares an existing project path)"
            elif p["state"] == "removed":
                note = "  (no longer declared by the scaffold)"
            elif p["state"] == "new-install":
                note = "  (not yet installed)"
            if p.get("standing"):
                note += "  (standing keep-local; release with --take-new PATH)"
            if p.get("adopting"):
                note += f"  (now project-owned: {p['ownership']}; never overwritten after this)"
            elif p["state"] == "collision-novel" and p["action"] == ACTION_ADOPT:
                note = "  (the project already has it; adopted as its own)"
            if p.get("cleared_standing"):
                note += "  (standing keep-local cleared: moot on a project-owned file)"
            elif p["state"] == "missing":
                note = "  (tracked but file missing)"
            print(f"    {p['path']}{note}")
        print()


def apply_upgrade_plan(target_dir, scaffold_manifest, plans, profile):
    """Execute the plan and rewrite the manifest. Returns 0 on success."""
    target = Path(target_dir).resolve()

    # Refuse if any action is REFUSE — caller should have caught this.
    refusals = [p for p in plans if p["action"] == ACTION_REFUSE]
    if refusals:
        print("Refusing to apply: ambiguous plan (use --keep-local PATH / --take-new PATH / --adopt PATH / --rename-local PATH=NEWPATH):", file=sys.stderr)
        for p in refusals:
            print(f"  {p['path']}  ({p['state']})", file=sys.stderr)
        return 3

    file_specs_for_manifest = []  # what to record in the new manifest

    for p in plans:
        path = p["path"]
        action = p["action"]
        dest = target / path

        if p.get("cleared_standing"):
            print(f"  note: {path}: standing keep-local cleared — the file is "
                  f"project-owned ({p['ownership']}), so phasekit never overwrites it; a "
                  "template change shows in `phasekit check --include-templates`")
        if action == ACTION_NOOP:
            # Tracked clean files stay in the manifest
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
                "installed": False, "local": p.get("local_after"),
                "template_sha": p.get("template_sha_after"),
            })
        elif action == ACTION_TAKE_NEW or action == ACTION_INSTALL:
            spec = {"path": path, "ownership": p["ownership"], "text": p["text"],
                    "rendered_from": p["rendered_from"]}
            try:
                install_from_spec(spec, target, target.name, force=True)
            except RuntimeError as e:
                print(f"  REFUSE: {e}", file=sys.stderr)
                return 1
            print(f"  {action}: {path}")
            file_specs_for_manifest.append({**spec, "installed": True, "local": None})
        elif action == ACTION_KEEP_LOCAL:
            # Leave on-disk file as-is. Manifest sha will be updated to current,
            # and the decision is recorded so the next upgrade honours it too.
            print(f"  keep-local: {path}" + ("  (standing)" if p.get("standing") else ""))
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
                "installed": False, "local": p.get("local_after"),
                "template_sha": p.get("template_sha_after"),
            })
        elif action == ACTION_ADOPT:
            # collision-novel: trust on-disk content; record under scaffold-new path
            print(f"  adopt: {path}")
            # Adoption is a decision made now: the stamp records it.
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
                "installed": True, "template_sha": p.get("template_sha_after"),
            })
        elif action == ACTION_RENAME_LOCAL:
            # Move on-disk file aside; install scaffold-new on the original path
            new_path = p["rename_target"]
            if not new_path:
                print(f"  ERROR: --rename-local for {path} missing target", file=sys.stderr)
                return 1
            new_dest = target / new_path
            new_dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, new_dest)
            spec = {"path": path, "ownership": p["ownership"], "text": p["text"],
                    "rendered_from": p["rendered_from"]}
            try:
                install_from_spec(spec, target, target.name, force=True)
            except RuntimeError as e:
                print(f"  REFUSE: {e}", file=sys.stderr)
                return 1
            print(f"  rename-local: {path} -> {new_path}; installed scaffold-new at {path}")
            file_specs_for_manifest.append({**spec, "installed": True})
        elif action == ACTION_DELETE:
            try:
                dest.unlink()
                print(f"  delete: {path}")
            except FileNotFoundError:
                pass
            # Do NOT add to file_specs_for_manifest
        elif action == ACTION_ORPHAN:
            print(f"  orphan: {path}  (left in place; scaffold no longer declares it)")
            # Re-record under a downgraded class so subsequent --check stays
            # sane — except a project-owned file, which stays the project's:
            # a plain --uninstall removes orphans, and must never remove
            # project content (v0.17.0; CONVENTIONS.md after a re-profile).
            orphan_class = (p["ownership"] if p["ownership"] in PROJECT_OWNED_CLASSES
                            else OWNERSHIP_CLASS_ORPHAN)
            file_specs_for_manifest.append({
                "path": path, "ownership": orphan_class,
                "text": p["text"], "rendered_from": p["rendered_from"],
                "installed": False,
            })

    sync_hook_registrations(target)

    # Rewrite the manifest with the post-apply state.
    write_downstream_manifest(target, scaffold_manifest, profile,
                              file_specs_for_manifest)
    return 0


def sync_hook_registrations(target, quiet=False):
    """Add any MISSING scaffold hook registrations to .claude/settings.json.

    Why this exists. Hook files are `scaffold`-class and install on every
    upgrade, but `.claude/settings.json` is `bootstrap-with-template-tracking`
    — write-once, never overwritten, because it also carries project-owned
    permissions. So shipping a new hook used to deliver the FILE to every
    existing project and the REGISTRATION to none of them: the hook lands and
    never fires. A shipped no-op that everyone believes is running is worse
    than not shipping it — it is the producer-built/consumer-missing failure
    this scaffold has now spent two releases eliminating.

    Additive only, and deliberately narrow:
      - the desired wiring is read from templates/settings.template.json, which
        is already the single source of truth for it; nothing is invented here
      - an (event, command) pair that is already registered is left completely
        alone, whatever its matcher or ordering
      - existing entries are never modified, reordered or removed, and no other
        key in settings.json is touched
      - idempotent: a second run reports nothing

    Returns the list of (event, command) pairs added.
    """
    settings_path = target / ".claude" / "settings.json"
    template_path = REPO_ROOT / "templates" / "settings.template.json"
    if not settings_path.is_file() or not template_path.is_file():
        return []

    try:
        with settings_path.open() as f:
            settings = json.load(f)
        with template_path.open() as f:
            template = json.load(f)
    except (OSError, ValueError) as e:
        # Never let this fail an upgrade — the files are all installed by now.
        if not quiet:
            print(f"  note: could not sync hook registrations ({e})", file=sys.stderr)
        return []

    def commands(entry):
        return [h.get("command") for h in (entry.get("hooks") or [])
                if isinstance(h, dict)]

    added = []
    existing_hooks = settings.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}
    for event, entries in (template.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        current = existing_hooks.get(event)
        if not isinstance(current, list):
            current = []
        registered = {c for e in current if isinstance(e, dict) for c in commands(e)}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            wanted = [c for c in commands(entry) if c and c not in registered]
            if not wanted:
                continue
            current.append(json.loads(json.dumps(entry)))
            registered.update(wanted)
            added.extend((event, c) for c in wanted)
        if current:
            existing_hooks[event] = current

    if not added:
        return []

    settings["hooks"] = existing_hooks
    tmp = settings_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)
    if not quiet:
        for event, command in added:
            print(f"  hook-register: {event} -> {command}")
    return added


def _interactive_resolve(plans, target):
    """Walk every drifted/refuse entry and prompt the user [k/t/d/s].

    [k]eep-local | [t]ake-new | [d]iff (show, then re-prompt) | [s]top.
    Returns the modified plans list, or None if user stopped.
    """
    print("\nInteractive resolution: per-file [k=keep-local / t=take-new / d=diff / s=stop]")
    for p in plans:
        if p["state"] not in ("drifted",):
            continue
        if p["action"] != ACTION_REFUSE and p["action"] not in (ACTION_KEEP_LOCAL, ACTION_TAKE_NEW):
            continue
        path = p["path"]
        while True:
            try:
                ans = input(f"  {path}: [k/t/d/s] ").strip().lower()
            except EOFError:
                ans = "s"
            if ans in ("k", "keep", "keep-local"):
                p["action"] = ACTION_KEEP_LOCAL
                # Keeping it after seeing the diff acknowledges the template,
                # as `--keep-local PATH` does (v0.17.0) — even when keep was
                # already the default for this project-owned file.
                if p["ownership"] == "bootstrap-with-template-tracking":
                    p["template_sha_after"] = None
                break
            elif ans in ("t", "take", "take-new"):
                p["action"] = ACTION_TAKE_NEW
                break
            elif ans in ("d", "diff"):
                local_path = target / path
                if not local_path.exists():
                    print("    (no local file to diff)")
                    continue
                spec = {"path": path, "rendered_from": p["rendered_from"]}
                src = _scaffold_source_for_spec(spec)
                if not src.exists():
                    print("    (no scaffold source to diff)")
                    continue
                try:
                    out = subprocess.run(
                        ["diff", "-u", str(src), str(local_path)],
                        capture_output=True, text=True,
                    )
                    print(out.stdout if out.stdout else "    (no textual diff)")
                except FileNotFoundError:
                    print("    (diff command not found)")
            elif ans in ("s", "stop"):
                return None
            else:
                print("    (unknown — try k, t, d, or s)")
    return plans


def cmd_upgrade(target_dir, profile=None, dry_run=False, yes=False, no_lock=False,
                interactive=False,
                keep_local=(), take_new=(), adopt=(), rename_local=(),
                accept_removal=(), commit=True, no_verify=False):
    """Upgrade a downstream project: re-evaluate scaffold-owned files and
    apply changes after a plan-then-confirm cycle.

    Exit codes (pinned in contracts/interface.json): 0 success; 1 error or bad
    input; 2 another process holds the lock, or --interactive with --yes; 3
    unresolved refusals; 4 no green verdict — the project's gate failed, could
    not run, or wrote into the tree; every file restored, nothing committed; 5
    applied and verified but NOT committed (staging or the commit failed) — the
    next upgrade commits it. A missing git identity is not a failure: exit 0,
    files installed, a note, the tree left dirty (v0.8 behavior; v0.16.3).
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target does not exist: {target}", file=sys.stderr)
        return 1
    try:
        verify_mode = upgrade_verify_mode(no_verify=no_verify, commit=commit)
    except ValueError as e:
        print(f"--upgrade: {e}", file=sys.stderr)
        return 1

    with target_lock(target, no_lock=no_lock):
        if dry_run and _pending_upgrade(target):
            print("--upgrade: an interrupted upgrade is pending for this project; run "
                  "the upgrade (without --dry-run) to recover it first.", file=sys.stderr)
            return 1
        rc = recover_pending_upgrade(target)
        if rc is not None:
            return rc
        return _upgrade_locked(target, profile, dry_run, yes, interactive, keep_local,
                               take_new, adopt, rename_local, accept_removal, commit,
                               no_verify, verify_mode)


def _upgrade_locked(target, profile, dry_run, yes, interactive, keep_local, take_new,
                    adopt, rename_local, accept_removal, commit, no_verify, verify_mode):
    sweep_orphan_tmpfiles(target)

    existing = load_downstream_manifest(target)
    if existing is None:
        print(f"No .scaffold/manifest.json in {target}; run --reconcile first.",
              file=sys.stderr)
        return 1
    try:
        existing = migrate_manifest(existing)
    except RuntimeError as e:
        print(f"--upgrade: {e}", file=sys.stderr)
        return 1

    if profile is None:
        profile = existing.get("profile") or "default"

    scaffold_manifest = load_manifest()
    profiles = scaffold_manifest.get("profiles", {})
    resolved = resolve_profile(profiles, profile)

    plans = compute_upgrade_plan(
        target, scaffold_manifest, existing, resolved,
        keep_local=keep_local, take_new=take_new,
        adopt=adopt, rename_local=rename_local,
        accept_removal=accept_removal,
    )

    if interactive:
        if yes:
            print("Error: --interactive cannot be used with --yes", file=sys.stderr)
            return 2
        before = {p["path"]: p["action"] for p in plans}
        plans = _interactive_resolve(plans, target)
        if plans is None:
            print("Stopped.", file=sys.stderr)
            return 1
        # An answer given here is as much a decision as the flag (review r2).
        for p in plans:
            if p["action"] != before.get(p["path"]):
                p["local_after"] = _local_after(p["path"], p["ownership"], p["action"],
                                                p["action"] == ACTION_KEEP_LOCAL)

    print_upgrade_plan(plans)

    refusals = [p for p in plans if p["action"] == ACTION_REFUSE]
    if refusals:
        print("\nNot applied: ambiguous (see [refuse] above).", file=sys.stderr)
        print("Resolve with --keep-local PATH / --take-new PATH / --adopt PATH / --rename-local PATH=NEWPATH",
              file=sys.stderr)
        return 3

    if dry_run:
        print("\n(dry-run; no changes written)")
        return 0

    if not yes:
        try:
            answer = input("Apply this plan? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            return 1

    old_version = existing.get("scaffold_version") or "unknown"
    new_version, _ = get_scaffold_version()
    # Decided from the PRE-apply tree: a project that had no gate is never
    # blocked by the gate its own upgrade just seeded (review round 1, M3).
    pre_label, pre_command = resolve_upgrade_gate(target)
    paths = _upgrade_touched_paths(plans)

    pending = PendingUpgrade.begin(target, paths, old_version, new_version, no_verify)

    with _interruptible() as killers:
        try:
            rc = apply_upgrade_plan(target, scaffold_manifest, plans, profile)
            if rc != 0:
                pending.abandon("the upgrade stopped partway; every file it wrote is restored")
                return rc
            pending.mark_applied()
            if verify_mode != "off":
                changed = pending.changed_paths()
                if changed:
                    if pre_label is None:
                        print(f"  gate: skipped — {pre_command}")
                    else:
                        result = run_upgrade_gate(target, verify_mode, killers)
                        if result["status"] != "passed":
                            pending.abandon(None)
                            report_red_upgrade(result, changed)
                            return EXIT_UPGRADE_GATE_RED
                        print(f"  gate: {result['label']} passed ({result['where']}) on the "
                              "upgraded tree")
        except UpgradeInterrupted:
            pending.abandon("interrupted; every file the upgrade wrote is restored")
            print("--upgrade: interrupted — nothing was committed.", file=sys.stderr)
            return 130

        if not commit:
            pending.finish()
            return 0
        try:
            to_commit = pending.write_set(upgrade_commit_paths(plans))
            pending.mark_verified(to_commit)
            status = commit_upgrade(target, plans, old_version, unverified=no_verify,
                                    paths=to_commit)
        except UpgradeInterrupted:
            if (_pending_upgrade(target) or {}).get("phase") != "verified":
                pending.abandon("interrupted; every file the upgrade wrote is restored")
                print("--upgrade: interrupted — nothing was committed.", file=sys.stderr)
            else:
                print("--upgrade: interrupted after the gate passed; the next "
                      "`phasekit upgrade` commits it.", file=sys.stderr)
            return 130
    # no-identity is deliberately absent: it is the v0.8 non-fatal case (v0.16.3).
    if status in ("stage-failed", "commit-failed"):
        print("  The upgrade is applied and verified but NOT committed; the next "
              "`phasekit upgrade` commits it once the cause above is cleared.",
              file=sys.stderr)
        return EXIT_UPGRADE_UNCOMMITTED
    pending.finish()
    return 0


# === The upgrade gate (v0.16.0, row 813) ====================================
#
# WHY. Upgrade commits ran no gate, so a scaffold change that broke a
# project's own checks landed silently and surfaced only when the next real
# iteration spent a session and blocked (xmeo-v3, 2026-09-15: 9 red tests on
# master from one upgrade commit). Now the project's pre-commit gate runs on
# the upgraded tree BEFORE the commit; anything but a clean green restores the
# tree byte-for-byte and commits nothing.
#
# WHERE IT RUNS, and why that is a policy rather than a detail. A project's
# gate is written for the runner image (its toolchain, its jq), and on a
# supervisor host it must never run bare — one managed project's suite kills
# every live runner container when started with the docker socket. So:
#
#   auto (default)  the runner image when docker is reachable; the HOST only
#                   when there is no docker CLI at all (a solo user without
#                   docker). A daemon that fails or hangs, or a missing image,
#                   is a refusal with the one-line fix — never a fallback.
#   container       the runner image or nothing
#   host            the host, always
#   off             no gate (also: --no-verify; also: --no-commit)
#
# A project whose gate was not configured BEFORE the upgrade (no script, or
# still the stub) is skipped: there was nothing to verify. The gate runs under
# `bash -eo pipefail -c`, as the loop runs it; it is read-only over the tree
# (the v0.14.10 `verify-gate-read-only` convention), and a footprint makes it
# red and is restored.
#
# A SESSION'S ENVIRONMENT, not a bare container (v0.16.2, row 1137). The gate
# is refused on anything a session would not see, so the container mirrors
# what scripts/container-setup.sh gives a session that a suite can observe:
# HOME=/home/node (the image's baked .gitconfig and Playwright cache) for the
# users that can write it, a global git identity written the way
# .devcontainer/entrypoint.sh writes GIT_USER_NAME/GIT_USER_EMAIL, and a
# contracts provider (PHASEKIT_CONTRACTS_MOUNT, else PHASEKIT_CONTRACTS_DIR on
# the upgrading host) bind-mounted read-only at /contracts. The identity goes
# into the GLOBAL config, never GIT_AUTHOR_*/GIT_COMMITTER_* env: env outranks
# a test's own `git config user.name` in its scratch repo, which a session
# never does. The host branch adds the same identity only when the host has
# none. Not mirrored, deliberately: the firewall and dropped capabilities (the
# entrypoint is bypassed — it needs sudo and NET_ADMIN, and a gate that can
# reach more network is never refused for it), the Claude credential volume
# (a gate gets no credentials), and the ssh agent / tokens (a gate pushes
# nothing). Measured on foundry-orchestrator 2026-09-27: 15 tests red on its
# landed main for want of an identity and a provider, so the upgrade was
# refused on a green tree.
#
# SURVIVING A KILL. Before anything is written, the exact pre-upgrade bytes of
# every path the upgrade may touch are copied OUTSIDE the tree, beside a
# pending record naming the phase. SIGINT/SIGTERM restore in-process; after a
# SIGKILL the next upgrade restores (phase `applied`) or commits (phase
# `verified`) before it plans anything, and `--check` reports the pending
# record as non-zero so a rollout loop cannot mistake it for clean.

UPGRADE_VERIFY_ENV = "PHASEKIT_UPGRADE_VERIFY"
UPGRADE_VERIFY_MODES = ("auto", "container", "host", "off")
UPGRADE_VERIFY_TIMEOUT_ENV = "PHASEKIT_UPGRADE_VERIFY_TIMEOUT"
UPGRADE_VERIFY_TIMEOUT_DEFAULT = 1800
RUNNER_IMAGE_ENV = "PHASEKIT_RUNNER_IMAGE"
RUNNER_IMAGE_DEFAULT = "scaffold-runner"
UPGRADE_GATE_TAIL_LINES = 40
EXIT_UPGRADE_GATE_RED = 4
EXIT_UPGRADE_UNCOMMITTED = 5
UNVERIFIED_SUFFIX = " (unverified: --no-verify)"
# The session's mirror (v0.16.2). CONTRACTS_CONTAINER_DIR must match
# container-setup.sh's CONTRACTS_CONTAINER_DIR and phasekit-contracts.py's
# DEFAULT_MOUNT_DIR; RUNNER_HOME is where container-setup.sh pins HOME for a
# --user override.
CONTRACTS_CONTAINER_DIR = "/contracts"
RUNNER_HOME = "/home/node"
RUNNER_HOME_USERS = ("0", "root", "1000", "node")  # root, and the image's `node` user, own it
RUNNER_THROWAWAY_HOME = "/tmp/phasekit-upgrade-home"
GATE_GIT_NAME_DEFAULT = "phasekit upgrade"
GATE_GIT_EMAIL_DEFAULT = "phasekit-upgrade@localhost"
RUNNER_START_FAILED_RC = 125  # docker run's own "could not start" (not an exit of this script): infra
# Written before the gate, as .devcontainer/entrypoint.sh writes a session's
# identity; a failure here is the runner's, not the project's.
GATE_SETUP_FAILED_MARK = "phasekit upgrade: the gate's session setup failed"
GATE_IDENTITY_PREAMBLE = (
    'mkdir -p "$HOME" && git config --global user.name "$GIT_USER_NAME" '
    '&& git config --global user.email "$GIT_USER_EMAIL" '
    f'|| {{ echo "{GATE_SETUP_FAILED_MARK} (could not write the git identity under $HOME)" >&2; '
    f'exit {RUNNER_START_FAILED_RC}; }}')
PENDING_SCHEMA = 1


class UpgradeInterrupted(Exception):
    """SIGINT or SIGTERM arrived while the upgrade held the tree."""


@contextlib.contextmanager
def _interruptible():
    """Turn SIGINT/SIGTERM into UpgradeInterrupted for the duration, so the
    tree is restored instead of left half-upgraded. Yields a list the gate
    appends its kill function to."""
    killers = []
    fired = []

    def handler(signum, frame):
        if fired:  # a second signal while restoring must not abort the restore
            return
        fired.append(signum)
        for kill in list(killers):
            try:
                kill()
            except Exception:  # noqa: BLE001 — best effort while unwinding
                pass
        raise UpgradeInterrupted()

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread
            pass
    try:
        yield killers
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def upgrade_verify_mode(no_verify=False, commit=True):
    """The effective gate mode, or raise ValueError for an unusable env value."""
    if no_verify or not commit:
        return "off"
    raw = (os.environ.get(UPGRADE_VERIFY_ENV) or "auto").strip().lower()
    if raw not in UPGRADE_VERIFY_MODES:
        raise ValueError(f"{UPGRADE_VERIFY_ENV}={raw!r} is not one of "
                         f"{', '.join(UPGRADE_VERIFY_MODES)}")
    return raw


def resolve_upgrade_gate(target):
    """(label, shell command) for this project's gate, or (None, reason)."""
    env_cmd = (os.environ.get("PHASEKIT_VERIFY_CMD") or "").strip()
    if env_cmd:
        return "PHASEKIT_VERIFY_CMD", env_cmd
    script = Path(target) / VERIFY_DEST_PATH
    if not script.is_file():
        return None, "no verify gate was configured (scripts/phasekit-verify.sh absent)"
    if verify_gate_is_stub(script):
        return None, "the verify gate was still the unconfigured stub"
    return VERIFY_DEST_PATH, f"bash {VERIFY_DEST_PATH}"


def _docker_state(image):
    """'absent' (no docker CLI at all) | 'unreachable' | 'no-image' | 'ready'.

    Only a missing CLI licenses the host: a CLI whose daemon fails or hangs is
    the supervisor host on a bad day (a wedged daemon; a non-login shell
    without the rootless DOCKER_HOST), exactly where a bare run is dangerous."""
    if shutil.which("docker") is None:
        return "absent"
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, timeout=30)
        if info.returncode != 0:
            return "unreachable"
        img = subprocess.run(["docker", "image", "inspect", image],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "unreachable"
    return "ready" if img.returncode == 0 else "no-image"


def _runner_user():
    """The user the gate runs as in the runner image — the pairing
    scripts/container-setup.sh documents: PHASEKIT_CONTAINER_USER when set;
    else 0:0 under rootless docker (container root IS the host user); else the
    host uid."""
    explicit = (os.environ.get("PHASEKIT_CONTAINER_USER") or "").strip()
    if explicit:
        return "0:0" if explicit == "root" else explicit
    rootless = os.environ.get("PHASEKIT_ROOTLESS_DOCKER") == "1"
    if not rootless:
        try:
            r = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"],
                               capture_output=True, text=True, timeout=60)
            rootless = "rootless" in (r.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            rootless = False
    return "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"


def _forward_env_args():
    """`-e NAME` for each PHASEKIT_FORWARD_ENV key that is set — the same keys
    scripts/container-setup.sh forwards to a session, so a suite that needs
    them is not red only at upgrade time. By name: docker reads the value from
    this environment, so it never appears in argv."""
    args = []
    raw = (os.environ.get("PHASEKIT_FORWARD_ENV") or "").replace("\n", ",")
    for name in (n.strip() for n in raw.split(",")):
        if (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or "")
                and name not in ("HOME", "PATH", "CLAUDE_CONFIG_DIR", "IS_SANDBOX",
                                 "PHASEKIT_CONTRACTS_DIR", "GIT_USER_NAME", "GIT_USER_EMAIL")
                and os.environ.get(name)):
            args += ["-e", name]
    return args


def _mount_arg(target, dst="/workspace", readonly=False):
    """--mount value; CSV-quoted so a path with ':' or ',' is still one field."""
    src = str(target).replace('"', '""')
    return f'type=bind,"src={src}",dst={dst}' + (",readonly" if readonly else "")


def _git_config_get(key, cwd, env=None):
    """The effective value git reports for `key` from `cwd`, or None."""
    try:
        r = subprocess.run(["git", "config", "--get", key], cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (r.stdout or "").strip()
    return value if r.returncode == 0 and value else None


def _gate_git_identity(target):
    """(name, email) the gate's global git config carries: GIT_USER_NAME /
    GIT_USER_EMAIL when set (container-setup.sh's knobs), else what the target
    repo resolves (its own config, then the upgrading user's), else a fixed
    phasekit-upgrade identity."""
    out = []
    for env_name, key, default in (("GIT_USER_NAME", "user.name", GATE_GIT_NAME_DEFAULT),
                                   ("GIT_USER_EMAIL", "user.email", GATE_GIT_EMAIL_DEFAULT)):
        value = (os.environ.get(env_name) or "").strip()
        out.append(value or _git_config_get(key, target) or default)
    return tuple(out)


def _contracts_source():
    """(env var, host path) of the contracts provider to mirror, or (None, None).
    PHASEKIT_CONTRACTS_MOUNT is container-setup.sh's name for it; a host that
    exported PHASEKIT_CONTRACTS_DIR (the checker's own name, as a provider or a
    standalone user sets it) means the same tree."""
    for var in ("PHASEKIT_CONTRACTS_MOUNT", "PHASEKIT_CONTRACTS_DIR"):
        value = (os.environ.get(var) or "").strip()
        if value:  # absolute once: the check, the host gate (cwd=target) and the mount agree
            return var, os.path.abspath(os.path.expanduser(value))
    return None, None


def _contracts_unusable(var, path):
    """container-setup.sh's refusal for a provider it cannot mount, or None."""
    if not os.path.isdir(path):
        return f"{var} is set to '{path}' but that is not a directory"
    if not os.access(os.path.join(path, "index.json"), os.R_OK):
        return (f"{var} '{path}' has no readable index.json (a provider with no "
                "dependencies still ships one with zero entries)")
    return None


def _runner_home(user):
    """container-setup.sh pins HOME=/home/node for a --user override; the gate
    does the same for the users that can write it, and a throwaway HOME for
    any other uid (the image's /home/node is not theirs)."""
    return RUNNER_HOME if user.split(":", 1)[0] in RUNNER_HOME_USERS else RUNNER_THROWAWAY_HOME


def _host_gate_env(target, scratch):
    """The host branch's environment: the upgrader's own, plus a global-scope
    identity only where the host has none (a temp GIT_CONFIG_GLOBAL that
    includes the real global files first), and PHASEKIT_CONTRACTS_DIR from
    PHASEKIT_CONTRACTS_MOUNT when only the mount name was given."""
    env = dict(os.environ)
    var, path = _contracts_source()
    if var == "PHASEKIT_CONTRACTS_MOUNT":  # the same tree the container would mount
        env["PHASEKIT_CONTRACTS_DIR"] = path
    # What a repo with no identity of its own would see: system + global.
    # The ceiling keeps a TMPDIR inside some work tree from lending its identity.
    probe_env = {**env, "GIT_CEILING_DIRECTORIES": str(Path(scratch).parent)}
    missing = [key for key in ("user.name", "user.email")
               if _git_config_get(key, scratch, probe_env) is None]
    if not missing:
        return env
    name, email = _gate_git_identity(target)
    cfg = Path(scratch) / "gitconfig"
    cfg.touch()
    if (env.get("GIT_CONFIG_GLOBAL") or "").strip():
        # absolute: an include resolves relative to the including file (review r1)
        originals = [os.path.abspath(os.path.expanduser(env["GIT_CONFIG_GLOBAL"]))]
    else:
        xdg = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        originals = [os.path.join(xdg, "git", "config"), os.path.expanduser("~/.gitconfig")]
    try:
        for original in originals:
            subprocess.run(["git", "config", "--file", str(cfg), "--add", "include.path",
                            original], check=True, capture_output=True, timeout=30)
        for key in missing:
            subprocess.run(["git", "config", "--file", str(cfg), key,
                            name if key == "user.name" else email],
                           check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return env  # the gate then runs as the host is, which is no worse than before
    env["GIT_CONFIG_GLOBAL"] = str(cfg)
    return env


def _tail(text, lines=UPGRADE_GATE_TAIL_LINES):
    return "\n".join((text or "").rstrip().splitlines()[-lines:])


def run_upgrade_gate(target, mode, killers=None):
    """Run the project's gate on the upgraded tree; never raises except
    UpgradeInterrupted. Returns {status: passed|failed|infra, where, label,
    detail, tail}. The gate's footprint (anything it changed outside ignored
    paths) is restored and turns the verdict red."""
    try:
        scratch = _gate_scratch(target)
    except OSError as exc:
        return {"status": "infra", "where": mode, "label": None,
                "detail": f"no usable temporary directory for the gate ({type(exc).__name__})",
                "tail": ""}
    try:
        return _run_upgrade_gate(target, mode, killers, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _gate_scratch(target):
    """A scratch dir OUTSIDE the target: phasekit's own files there would read
    as the gate's footprint (a TMPDIR inside the tree, review r2). Falls back
    to the upgrading user's state dir."""
    target = Path(target).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if tmp == target or target in tmp.parents:
        base = _pending_dir(target).parent.parent / "gate-scratch"
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="phasekit-upgrade-gate-", dir=str(base))
    return tempfile.mkdtemp(prefix="phasekit-upgrade-gate-")


def _run_upgrade_gate(target, mode, killers, scratch):
    target = Path(target).resolve()
    killers = killers if killers is not None else []
    label, command = resolve_upgrade_gate(target)
    image = (os.environ.get(RUNNER_IMAGE_ENV) or RUNNER_IMAGE_DEFAULT).strip()
    where = mode
    if mode in ("auto", "container"):
        state = _docker_state(image)
        if state == "ready":
            where = "container"
        elif mode == "auto" and state == "absent":
            where = "host"
        else:
            why = {"absent": "docker is not installed",
                   "unreachable": "the docker daemon did not answer"}.get(
                       state, f"the runner image '{image}' is not present")
            return {"status": "infra", "where": "container", "label": label,
                    "detail": f"{why}; refusing to run the gate on the host. Build the "
                              "image (bash scripts/container-setup.sh build), or set "
                              f"{UPGRADE_VERIFY_ENV}=host where running it bare is safe",
                    "tail": ""}
    contracts_var, contracts_path = _contracts_source()
    if contracts_var:  # host or container: a session would refuse it either way
        unusable = _contracts_unusable(contracts_var, contracts_path)
        if unusable:
            return {"status": "infra", "where": where, "label": label,
                    "detail": f"{unusable}; a session would refuse the same mount "
                              "(scripts/container-setup.sh). Pass a readable contracts tree, "
                              "or leave it unset",
                    "tail": ""}
    try:
        timeout = int(os.environ.get(UPGRADE_VERIFY_TIMEOUT_ENV)
                      or UPGRADE_VERIFY_TIMEOUT_DEFAULT)
    except ValueError:
        timeout = UPGRADE_VERIFY_TIMEOUT_DEFAULT

    try:
        before = _dirty_state(target)
    except RuntimeError as exc:
        return {"status": "infra", "where": where, "label": label,
                "detail": f"cannot observe the tree, so a footprint would go unseen ({exc})",
                "tail": ""}
    before_snap = _snapshot(target, sorted(before)) if before else {}
    index_before = _index_state(target)

    name = None
    if where == "container":
        name = (f"phasekit-upgrade-gate-{os.getpid()}-"
                f"{int(datetime.now(timezone.utc).timestamp() * 1000)}")
        user = _runner_user()
        git_name, git_email = _gate_git_identity(target)
        session = ["-e", f"HOME={_runner_home(user)}",
                   "-e", f"CLAUDE_CONFIG_DIR={RUNNER_HOME}/.claude",
                   "-e", f"GIT_USER_NAME={git_name}",
                   "-e", f"GIT_USER_EMAIL={git_email}"]
        if user == "0:0":
            session += ["-e", "IS_SANDBOX=1"]
        if contracts_var:
            session += ["--mount", _mount_arg(Path(contracts_path).resolve(),
                                              CONTRACTS_CONTAINER_DIR, readonly=True),
                        "-e", f"PHASEKIT_CONTRACTS_DIR={CONTRACTS_CONTAINER_DIR}"]
        # Forwarded project keys first: docker's last -e wins, and the names
        # above belong to this script (the forward list also refuses them).
        argv = ["docker", "run", "--rm", "--name", name, "--entrypoint", "bash",
                "--mount", _mount_arg(target), "-w", "/workspace",
                "--user", user,
                *_forward_env_args(),
                "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=*",
                *session,
                image, "-eo", "pipefail", "-c",
                GATE_IDENTITY_PREAMBLE + "\n" + command]
        cwd = None
        env = None
    else:
        argv = ["bash", "-eo", "pipefail", "-c", command]
        cwd = str(target)
        env = _host_gate_env(target, scratch)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
    except OSError as exc:
        return {"status": "infra", "where": where, "label": label,
                "detail": f"could not start the gate ({type(exc).__name__})", "tail": ""}

    def kill():
        # The whole process group on the host; the named container under docker
        # (killing the docker CLIENT does not stop the container).
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    killers.append(kill)
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill()
            timed_out = True
            try:
                out, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:  # something escaped the group holds the pipe
                out = ""
        # A green gate may still have left background children in its group:
        # they must not write after the footprint check (review r2). A child
        # that setsid()s out of the group is beyond reach — declined, recorded.
        if not name:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    finally:
        killers.remove(kill)

    # The index first: the worktree restore below checks tracked paths out
    # FROM the index, so a gate that edited-and-staged a file would otherwise
    # have its own content checked back out (review r3).
    index_touched = []
    index_after = _index_state(target)
    if index_before is not None and (index_after is None or index_after[0] != index_before[0]):
        Path(index_before[1]).write_bytes(index_before[2])
        index_touched = ["(the git index)"]
    try:
        footprint = _restore_footprint(target, before, before_snap) + index_touched
    except RuntimeError as exc:
        return {"status": "infra", "where": where, "label": label,
                "detail": f"cannot observe the tree after the gate ({exc})", "tail": _tail(out)}
    if timed_out:
        verdict = {"status": "failed", "detail": f"timed out after {timeout}s"}
    elif name and proc.returncode == RUNNER_START_FAILED_RC and GATE_SETUP_FAILED_MARK in (out or ""):
        verdict = {"status": "infra",
                   "detail": "the gate's session setup failed in the runner (its git identity "
                             "could not be written; see the tail)"}
    elif name and proc.returncode == RUNNER_START_FAILED_RC:
        verdict = {"status": "infra",
                   "detail": f"the runner could not start (docker exit {RUNNER_START_FAILED_RC})"}
    elif proc.returncode != 0:
        verdict = {"status": "failed", "detail": f"exit {proc.returncode}"}
    elif footprint:
        verdict = {"status": "failed",
                   "detail": "the gate wrote into the tree (restored): " + ", ".join(footprint[:8])
                   + (" …" if len(footprint) > 8 else "")
                   + ". A verify gate is read-only over the tree; its output belongs "
                   "under an ignored path"}
    else:
        verdict = {"status": "passed", "detail": "exit 0"}
    return {**verdict, "where": where, "label": label, "tail": _tail(out)}


def _dirty_state(target):
    """{path: digest} for every path git reports as not clean, ignored paths
    excluded; {} for a clean tree or a directory that is not a git work tree.
    Raises RuntimeError when git cannot answer for a work tree — a silent {}
    would make footprint detection vacuous."""
    if not (Path(target) / ".git").exists():
        return {}
    try:
        r = subprocess.run(["git", "-C", str(target), "status", "--porcelain=v1", "-z",
                            "--untracked-files=all"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git status: {type(exc).__name__}") from exc
    if r.returncode != 0:
        err = os.fsdecode(r.stderr).strip().splitlines()
        raise RuntimeError(f"git status: {err[0] if err else 'exit ' + str(r.returncode)}")
    out = {}
    fields = r.stdout.split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[:1] in (b"R", b"C"):
            i += 1  # the rename source follows as its own field
        rel = os.fsdecode(entry[3:])
        out[rel] = _digest(Path(target) / rel)
    return out


def _index_state(target):
    """(staged-entries digest, index path, index bytes), or None outside git.
    A gate that runs `git add` changes no worktree digest; this catches it."""
    if not (Path(target) / ".git").exists():
        return None
    ls = subprocess.run(["git", "-C", str(target), "ls-files", "-s", "-z"],
                        capture_output=True)
    where = subprocess.run(["git", "-C", str(target), "rev-parse", "--git-path", "index"],
                           capture_output=True, text=True)
    if ls.returncode != 0 or where.returncode != 0:
        return None
    index = Path(where.stdout.strip())
    if not index.is_absolute():
        index = Path(target) / index
    try:
        data = index.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(ls.stdout).hexdigest(), str(index), data


def _state_digest(target, rel):
    return _digest(Path(target) / rel)


def _digest(f):
    if f.is_symlink():
        return "link:" + os.readlink(f)
    if f.is_file():
        return (hashlib.sha256(f.read_bytes()).hexdigest()
                + f":{stat.S_IMODE(f.stat().st_mode):o}")
    return "absent"


def _restore_footprint(target, before, before_snap):
    """Undo everything the gate changed; return the paths it touched."""
    after = _dirty_state(target)
    touched = sorted(p for p in set(before) | set(after)
                     if before.get(p) != after.get(p))
    for rel in touched:
        if rel in before_snap:
            _restore(target, {rel: before_snap[rel]})
            continue
        # clean before the gate: back to the index (tracked) or gone (untracked)
        tracked = subprocess.run(["git", "-C", str(target), "ls-files", "--error-unmatch",
                                  "--", rel], capture_output=True).returncode == 0
        if tracked:
            subprocess.run(["git", "-C", str(target), "checkout", "--", rel],
                           capture_output=True)
        else:
            f = Path(target) / rel
            if f.is_symlink() or f.is_file():
                f.unlink()
            _prune_empty_parents(target, f.parent)
    return touched


def _prune_empty_parents(target, d):
    target = Path(target).resolve()
    d = Path(d)
    while d != target and target in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _upgrade_touched_paths(plans):
    paths = {".scaffold/manifest.json", ".claude/settings.json"}
    for p in plans:
        paths.add(p["path"])
        if p.get("rename_target"):
            paths.add(p["rename_target"])
    return sorted(paths)


def _snapshot(target, paths):
    """The exact current state of each path: file bytes + mode, link, or absent."""
    snap = {}
    for rel in paths:
        f = Path(target) / rel
        if f.is_symlink():
            snap[rel] = ("link", os.readlink(f))
        elif f.is_file():
            snap[rel] = ("file", f.read_bytes(), stat.S_IMODE(f.stat().st_mode))
        else:
            snap[rel] = ("absent",)
    return snap


def _restore(target, snap):
    for rel, rec in snap.items():
        f = Path(target) / rel
        if rec[0] == "absent":
            if f.is_symlink() or f.exists():
                f.unlink()
                _prune_empty_parents(target, f.parent)
        elif rec[0] == "link":
            if f.is_symlink() or f.exists():
                f.unlink()
            f.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(rec[1], f)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f.name + ".pk-restore")
            tmp.write_bytes(rec[1])
            os.chmod(tmp, rec[2])
            os.replace(tmp, f)


def _pending_dir(target):
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    key = hashlib.sha256(str(Path(target).resolve()).encode()).hexdigest()[:16]
    return Path(base) / "phasekit" / "upgrade-pending" / key


def _pending_upgrade(target):
    """The pending record for this project, or None."""
    f = _pending_dir(target) / "pending.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


class PendingUpgrade:
    """The out-of-tree record that makes an upgrade survive a kill: the exact
    pre-upgrade bytes of every path it may touch, the digest of each path as
    the upgrade LEFT it (`post`), and the phase it reached. Recovery only ever
    touches a path still exactly as the upgrade left it: anything newer is the
    project's work and wins (review r2)."""

    def __init__(self, target, record, snap):
        self.target, self.record, self.snap = target, record, snap
        self.dir = _pending_dir(target)

    @classmethod
    def begin(cls, target, paths, from_version, to_version, unverified):
        snap = _snapshot(target, paths)
        d = _pending_dir(target)
        if d.exists():
            shutil.rmtree(d)
        (d / "blobs").mkdir(parents=True)
        index = {}
        for n, (rel, rec) in enumerate(sorted(snap.items())):
            if rec[0] == "file":
                (d / "blobs" / f"{n:05d}").write_bytes(rec[1])
                index[rel] = {"kind": "file", "blob": f"{n:05d}", "mode": rec[2]}
            elif rec[0] == "link":
                index[rel] = {"kind": "link", "target": rec[1]}
            else:
                index[rel] = {"kind": "absent"}
        record = {"schema": PENDING_SCHEMA, "target": str(Path(target).resolve()),
                  "pid": os.getpid(), "started_at": utc_now_iso(), "phase": "applying",
                  "from_version": from_version, "to_version": to_version,
                  "unverified": bool(unverified), "paths": sorted(snap), "snapshot": index,
                  "pre": {rel: _state_digest(target, rel) for rel in snap}}
        self = cls(target, record, snap)
        self._write()
        return self

    @classmethod
    def load(cls, target):
        """The pending upgrade, None when there is none; raises RuntimeError
        for a record that exists but cannot be used (never discarded silently)."""
        d = _pending_dir(target)
        if not d.exists():
            return None
        record = _pending_upgrade(target)
        if not isinstance(record, dict) or record.get("schema") != PENDING_SCHEMA:
            if not (d / "pending.json").exists():  # killed inside begin(): nothing written yet
                shutil.rmtree(d, ignore_errors=True)
                return None
            raise RuntimeError(f"the pending-upgrade record in {d} is unreadable")
        snap = {}
        try:
            for rel, e in (record.get("snapshot") or {}).items():
                if e.get("kind") == "file":
                    snap[rel] = ("file", (d / "blobs" / e["blob"]).read_bytes(), e["mode"])
                elif e.get("kind") == "link":
                    snap[rel] = ("link", e["target"])
                else:
                    snap[rel] = ("absent",)
        except (OSError, KeyError) as exc:
            raise RuntimeError(f"the pending-upgrade record in {d} is incomplete "
                               f"({type(exc).__name__})") from exc
        return cls(target, record, snap)

    def _write(self):
        tmp = self.dir / "pending.json.tmp"
        tmp.write_text(json.dumps(self.record, indent=2))
        os.replace(tmp, self.dir / "pending.json")

    def changed_paths(self):
        now = _snapshot(self.target, [r for r in self.snap if r != ".scaffold/manifest.json"])
        return sorted(r for r, rec in now.items() if rec != self.snap[r])

    def mark_applied(self):
        self.record["phase"] = "applied"
        self.record["post"] = {rel: _state_digest(self.target, rel) for rel in self.snap}
        self._write()

    def write_set(self, candidates):
        """The candidates this upgrade actually changed. `.claude/settings.json`
        is always a candidate, so without this a project's in-flight edit to it
        rode along in a commit the upgrade never needed (review r3)."""
        pre, post = self.record.get("pre") or {}, self.record.get("post") or {}
        rel = ".claude/settings.json"
        # Only this always-listed path is filtered: every other candidate the
        # upgrade wrote, and staging an unchanged one is how an untracked
        # manifest gets re-tracked.
        return sorted(p for p in candidates
                      if p != rel or post.get(p) != pre.get(p))

    def mark_verified(self, commit_paths):
        self.record["phase"] = "verified"
        self.record["commit_paths"] = sorted(commit_paths)
        self._write()

    def ours(self):
        """(paths still exactly as the upgrade left them, paths changed since)."""
        post = self.record.get("post")
        pre = self.record.get("pre") or {}
        mine, theirs = [], []
        for rel in self.snap:
            now = _state_digest(self.target, rel)
            if post is None:
                # Killed while applying (a window of file writes, no gate yet):
                # there is no post-image to compare, so every path off its
                # pre-image is taken as the upgrade's. Recorded, narrow.
                if now != pre.get(rel):
                    mine.append(rel)
            elif now == post.get(rel):
                if now != pre.get(rel):
                    mine.append(rel)
            else:
                theirs.append(rel)
        return sorted(mine), sorted(theirs)

    def abandon(self, message, only=None):
        for rel in self.snap:  # a restore killed mid-write leaves its temp file
            tmp = Path(self.target) / (rel + ".pk-restore")
            if tmp.is_file():
                tmp.unlink()
        _restore(self.target, self.snap if only is None
                 else {r: self.snap[r] for r in only})
        self.finish()
        if message:
            print(f"--upgrade: {message}.", file=sys.stderr)

    def finish(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def recover_pending_upgrade(target):
    """Settle an upgrade a previous run could not finish. Returns None to
    continue with the new upgrade, or an exit code to stop with. Only paths
    still exactly as the upgrade left them are restored or committed; a path
    changed since belongs to whoever changed it and is named, not touched."""
    try:
        pending = PendingUpgrade.load(target)
    except RuntimeError as exc:
        print(f"--upgrade: {exc}. Inspect it — it holds the pre-upgrade bytes of every "
              f"file the interrupted upgrade may have written — then delete "
              f"{_pending_dir(target)} to proceed.", file=sys.stderr)
        return 1
    if pending is None:
        return None
    r = pending.record
    what = f"({r.get('from_version')} -> {r.get('to_version')}, started {r.get('started_at')})"
    mine, theirs = pending.ours()
    if theirs:
        print(f"--upgrade: changed since the interrupted upgrade {what}, so left as they "
              "are: " + ", ".join(theirs), file=sys.stderr)
    if r.get("phase") == "verified":
        print(f"--upgrade: completing an earlier upgrade {what} that was applied and "
              "verified but not committed.")
        message = f"{UPGRADE_COMMIT_PREFIX} {r.get('from_version')} -> {r.get('to_version')}"
        if r.get("unverified"):
            message += UNVERIFIED_SUFFIX
        paths = [p for p in (r.get("commit_paths") or []) if p not in theirs]
        status = _commit_paths(pending.target, paths, message)
        # no-identity settles the record like the normal path does (v0.16.3).
        if status in ("stage-failed", "commit-failed"):
            return EXIT_UPGRADE_UNCOMMITTED
        pending.finish()
        return None
    print(f"--upgrade: an earlier upgrade {what} was interrupted before its verdict; "
          "restoring the files it wrote before planning again.", file=sys.stderr)
    pending.abandon(None, only=mine)
    return None


def report_red_upgrade(result, changed):
    if result["status"] == "infra":
        print("\nUPGRADE NOT APPLIED: the project's gate could not run here,", file=sys.stderr)
    else:
        print("\nUPGRADE NOT APPLIED: the project's own gate did not pass on the upgraded "
              "tree,", file=sys.stderr)
    print("so every file this upgrade wrote has been restored byte-for-byte and "
          "nothing was committed.", file=sys.stderr)
    print(f"  gate:  {result['label']} ({result['where']}) — {result['detail']}",
          file=sys.stderr)
    print("  files the upgrade changed (restored):", file=sys.stderr)
    for rel in changed:
        print(f"    {rel}", file=sys.stderr)
    if result.get("tail"):
        print("  last lines of the gate's output:", file=sys.stderr)
        for line in result["tail"].splitlines():
            print(f"    | {line}", file=sys.stderr)
    print("  Next: keep a project-edited file with --keep-local PATH (it then stays "
          "kept), or fix the project forward and upgrade again. As a last resort, "
          "--no-verify commits without the gate and says so in the commit subject.",
          file=sys.stderr)


UPGRADE_COMMIT_PREFIX = "chore(scaffold): phasekit upgrade"


def commit_upgrade(target, plans, old_version, unverified=False, paths=None):
    """Commit (and try to push) the files this upgrade wrote. Returns one of
    committed | nothing | no-git | no-identity | commit-failed | stage-failed.

    Leaving the tree dirty caused two distinct failures in one day:

    1. IDLE PROJECTS SELF-DEADLOCK. The upgrade dirties the tree, and the
       orchestrator's on-ramp refuses a dirty tree — so a project that gets no
       sessions can never absorb its own upgrade: dirty tree -> no session ->
       still dirty. Hit three projects on the v0.7.1 rollout.
    2. ACTIVE PROJECTS FILE FALSE DRIFT SIGNALS. The projects whose sessions
       did absorb it committed scaffold-class files, tripping scope
       containment four times (operator tasks #130-133).

    Only the paths THIS upgrade touched are staged. Sweeping in whatever else
    happened to be dirty would hand a project's in-flight work a commit message
    about the scaffold, which is worse than the problem being fixed.

    A missing git identity, remote or upstream stays non-fatal: the files are
    installed (no-identity is its own status since v0.16.3, so callers never
    mistake it for a refused commit). A STAGING or COMMIT failure is reported
    and returned (v0.16.0: a stale .git/index.lock once let two upgrades return
    as if there were nothing to commit), so the caller can keep the pending
    record and exit 5.
    """
    new_version, _ = get_scaffold_version()
    message = f"{UPGRADE_COMMIT_PREFIX} {old_version} -> {new_version}"
    if unverified:
        message += UNVERIFIED_SUFFIX
    return _commit_paths(target, upgrade_commit_paths(plans) if paths is None else paths,
                         message)


def upgrade_commit_paths(plans):
    """The paths an upgrade commit may carry: the ones it WROTE. A kept or
    untouched path is the project's, whatever state it is in."""
    touched = {".scaffold/manifest.json", ".claude/settings.json"}
    for p in plans:
        if p["action"] in (ACTION_INSTALL, ACTION_TAKE_NEW, ACTION_DELETE,
                           ACTION_RENAME_LOCAL):
            touched.add(p["path"])
        if p["action"] == ACTION_RENAME_LOCAL and p.get("rename_target"):
            touched.add(p["rename_target"])
    return sorted(touched)


def _commit_paths(target, paths, message):
    target = Path(target)
    if not (target / ".git").exists():
        return "no-git"

    def git(*args):
        return subprocess.run(["git", "-C", str(target), *args],
                              capture_output=True, text=True)

    # Stage what we touched (`--all` on the pathspec so a deletion is recorded
    # too). A path that is neither on disk nor tracked has nothing to stage and
    # is skipped; any OTHER staging failure is reported, never swallowed.
    staging_failed = []
    for path in paths:
        if not (target / path).exists() and not git("ls-files", "--", path).stdout.strip():
            continue
        r = git("add", "--all", "--", path)
        if r.returncode != 0:
            # git's FIRST fatal/error line names the cause (e.g. the lock file);
            # its last line is generic advice.
            err = r.stderr.strip().splitlines()
            cause = next((line for line in err if line.startswith(("fatal:", "error:"))),
                         err[0] if err else "")
            staging_failed.append((path, cause))
    if staging_failed:
        path, detail = staging_failed[0]
        print(f"  note: could not stage the upgrade ({len(staging_failed)} path(s); first: "
              f"{path}: {detail}); the files are installed but the tree is left dirty.",
              file=sys.stderr)
        return "stage-failed"

    # Which of them actually differ from HEAD. An idempotent re-upgrade reaches
    # here with nothing to say and must not make an empty commit.
    changed = [p for p in git("diff", "--cached", "--name-only").stdout.split()
               if p in set(paths)]
    if not changed:
        return "nothing"

    # v0.16.3: a MISSING GIT IDENTITY is not a failed commit. Since v0.8 it has
    # been deliberately non-fatal (files installed, a note, exit 0 — a fresh
    # runner or CI box has no identity and nothing to fix); v0.16.0 folded it
    # into commit-failed -> exit 5 with the record kept pending, so every
    # identity-less upgrade "failed" and retried forever. It hid for three
    # releases because a workstation always has an identity (a global config, or
    # git's own guess from the passwd name + an FQDN hostname) and the GitHub
    # runner has neither (empty passwd name). Ask git before committing —
    # `git var` applies the same strict ident rules `git commit` does — so a
    # refusing hook still lands on commit-failed and only this cause is excused.
    # Only git's IDENT refusals count (review: a garbage GIT_*_DATE also fails
    # `git var`, and that one must stay a failed commit, exit 5).
    ident_refusal = ("ident name", "auto-detect", "tell me who you are",
                     "name consists only of disallowed")
    for ident in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        r = git("var", ident)
        if r.returncode != 0 and any(m in r.stderr.lower() for m in ident_refusal):
            err = r.stderr.strip().splitlines()
            detail = next((line for line in err if line.startswith(("fatal:", "error:"))),
                          err[-1] if err else "no git identity")
            print(f"  note: could not commit the upgrade (no git identity: {detail}); "
                  f"the files are installed but the tree is left dirty — set "
                  f"user.name/user.email and commit them.", file=sys.stderr)
            return "no-identity"

    # `--only <paths>` is load-bearing, not a flourish. A plain `git commit`
    # commits the WHOLE index, so anything the project had already staged when
    # the upgrade ran would be swept into a commit whose message says
    # "phasekit upgrade" — handing someone's in-flight work the wrong story,
    # which is worse than the dirty tree this feature exists to fix.
    # `--only` commits exactly these paths and leaves the rest of the index
    # staged and uncommitted.
    r = git("commit", "--only", "-m", message, "--", *changed)
    if r.returncode != 0:
        detail = (r.stderr.strip().splitlines() or [""])[-1]
        print(f"  note: could not commit the upgrade ({detail}); "
              f"the files are installed but the tree is left dirty.", file=sys.stderr)
        return "commit-failed"
    print(f"  commit: {message}")

    # Push only when there is somewhere to push to. A project with no remote or
    # no upstream is a normal standalone case, not a failure.
    if not git("remote").stdout.strip():
        return "committed"
    if git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").returncode != 0:
        print("  note: no upstream branch — commit is local.", file=sys.stderr)
        return "committed"
    r = git("push")
    if r.returncode == 0:
        print("  push: ok")
    else:
        print("  note: push failed; the upgrade commit is local.", file=sys.stderr)
    return "committed"


# === --uninstall (M9 §5) ===================================================

def cmd_uninstall(target_dir, include_once=False, yes=False, no_lock=False, dry_run=False):
    """Remove scaffold-owned files from the downstream project.

    Default: removes only `scaffold` class files (canonical scaffold-installed).
    With `--include-once`: also removes `bootstrap-frozen` and
    `bootstrap-with-template-tracking` (project-owned content; requires
    explicit acknowledgment).

    Writes `.scaffold/uninstall.log` before deletion (recovery aid).
    Files not tracked by the manifest are never touched.

    Returns 0 on success, 1 on error.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target does not exist: {target}", file=sys.stderr)
        return 1

    sweep_orphan_tmpfiles(target)

    existing = load_downstream_manifest(target)
    if existing is None:
        print(f"No .scaffold/manifest.json in {target}; nothing to uninstall.",
              file=sys.stderr)
        return 1
    try:
        existing = migrate_manifest(existing)
    except RuntimeError as e:
        print(f"--uninstall: {e}", file=sys.stderr)
        return 1

    classes_to_remove = {"scaffold", OWNERSHIP_CLASS_ORPHAN}
    if include_once:
        classes_to_remove.add("bootstrap-frozen")
        classes_to_remove.add("bootstrap-with-template-tracking")

    files = existing.get("files", [])
    to_remove = [f for f in files if f.get("ownership") in classes_to_remove]
    to_keep = [f for f in files if f.get("ownership") not in classes_to_remove]

    print(f"--uninstall: removing {len(to_remove)} files "
          f"({'scaffold + bootstrap-*' if include_once else 'scaffold class only'})")
    for entry in to_remove:
        print(f"  {entry['path']}  ({entry['ownership']})")
    if to_keep:
        print(f"\nWill keep {len(to_keep)} files (use --include-once for bootstrap-* removal):")
        for entry in to_keep:
            print(f"  {entry['path']}  ({entry['ownership']})")

    if dry_run:
        print("\n(dry-run; no changes written)")
        return 0

    if not yes:
        if include_once:
            print("\nWARNING: --include-once will remove project-owned bootstrap-* files")
            print("(SPEC.md, ARCHITECTURE.md, .claude/CLAUDE.md, etc.).")
        try:
            answer = input("\nProceed with uninstall? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            return 1

    with target_lock(target, no_lock=no_lock):
        # Write recovery log BEFORE deletion (M9 §5 partial-failure semantics).
        log_path = target / ".scaffold" / "uninstall.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_data = {
            "uninstalled_at": utc_now_iso(),
            "include_once": include_once,
            "scaffold_version": existing.get("scaffold_version"),
            "files": to_remove,
        }
        log_tmp = log_path.parent / (log_path.name + TMP_SUFFIX)
        log_tmp.write_text(json.dumps(log_data, indent=2) + "\n")
        os.replace(log_tmp, log_path)

        # Now perform deletions.
        deleted = 0
        for entry in to_remove:
            p = target / entry["path"]
            try:
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                pass

        # Update or remove the manifest.
        manifest_path = target / ".scaffold" / "manifest.json"
        if not to_keep:
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            print(f"\nRemoved manifest (no scaffold files remain).")
        else:
            existing["files"] = to_keep
            tmp = manifest_path.parent / (manifest_path.name + TMP_SUFFIX)
            tmp.write_text(json.dumps(existing, indent=2) + "\n")
            os.replace(tmp, manifest_path)

    print(f"Uninstalled {deleted} file(s). Recovery log: {log_path}")
    return 0


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


# Canonical upstream remote. Used only as a fallback when a downstream
# manifest predates the origin_url field; the live remote is preferred.
# Keep in sync with the same fallback in scripts/run-until-done.sh.
CANONICAL_ORIGIN_URL = "https://github.com/porkchop/phasekit.git"


def get_scaffold_version():
    """Compute scaffold version (semver tag-derived or fallback) and short commit.

    Returns (version_string, commit_string).

    With release tags present, `git describe` yields a meaningful version such
    as `v0.1.0`, `v0.1.0-3-g0d9ee74` (3 commits past the tag), or
    `v0.1.0-dirty`. Untagged checkouts fall back to the short commit via
    `--always`, preserving the pre-tag behavior (just without the synthetic
    `0.0.0+git.` prefix).
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0.0.0+git.unknown", "unknown"
    try:
        version = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        version = commit
    return version, commit


def get_scaffold_origin_url():
    """Return the scaffold repo's `origin` remote URL, or None if unavailable.

    Recorded in the downstream manifest so a project can later discover where
    its upstream lives (for `--check-version` and the loop update nudge)
    without hardcoding it.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        url = result.stdout.strip()
        return url or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


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
                          scaffold_manifest, is_text=True, rendered_from_override=None,
                          installed_at=None, local=None, template_sha=None):
    """Build one entry for the downstream `.scaffold/manifest.json`.

    `installed_at` defaults to now. `write_downstream_manifest` passes the
    PRIOR entry's stamp for a file this run did not install, so the stamp
    keeps meaning "when phasekit last wrote this file" rather than "when the
    manifest was last rewritten" (v0.14.1).

    `template_sha` (v0.17.0) is the template base to record for a
    `bootstrap-with-template-tracking` file; None means today's template.
    """
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
        "installed_at": installed_at or utc_now_iso(),
    }
    if local:
        entry["local"] = local

    if ownership == "bootstrap-with-template-tracking":
        rendered_from, current_template_sha = lookup_template_info(
            scaffold_manifest, downstream_path
        )
        if rendered_from_override:
            rendered_from = rendered_from_override
            tmpl = REPO_ROOT / rendered_from_override
            current_template_sha = sha256_strict(tmpl) if tmpl.exists() else None
        if rendered_from:
            entry["rendered_from"] = rendered_from
        template_sha = template_sha or current_template_sha
        if template_sha:
            entry["template_sha"] = template_sha

    return entry


def write_downstream_manifest(target_dir, scaffold_manifest, profile, file_specs):
    """Write `.scaffold/manifest.json` atomically via tmp + os.replace.

    file_specs: list of dicts with keys {path, ownership, text, rendered_from?,
    installed?}. `installed` (default True) says whether THIS run wrote the
    file. Returns the manifest path on success.

    Timestamps are not churn (v0.14.1). Two families of "when" live in this
    file, and both used to be re-stamped on every write: `enriched_at` at the
    top and `installed_at` on every entry. The `_timeless` guard below kept a
    byte-identical re-run from writing at all, but any real re-baseline — one
    project-owned doc edited since the last upgrade, whose sha the manifest
    must catch up with — rewrote every stamp in the file, so a weekly
    maintenance upgrade across an unchanged fleet produced a ~100-line
    manifest diff per project whose only content was one or two sha lines,
    and a commit and a deploy to carry it (foundry-orchestrator #495,
    2026-09-06). Now:

    * an entry's `installed_at` is CARRIED from the prior manifest unless this
      run installed the file (`installed: True`) or the path is new;
    * `enriched_at` is carried unless this run installed or removed a file,
      or the scaffold version/commit moved — a sha re-baseline alone is
      bookkeeping, not an enrichment.

    The diff a re-baseline writes is therefore exactly the sha lines that
    moved, and nothing else.
    """
    target = Path(target_dir).resolve()
    scaffold_dir = target / ".scaffold"
    scaffold_dir.mkdir(parents=True, exist_ok=True)

    version, commit = get_scaffold_version()

    manifest_path = scaffold_dir / "manifest.json"
    prior = None
    if manifest_path.is_file():
        try:
            prior = json.loads(manifest_path.read_text())
        except (ValueError, OSError):
            prior = None
    # Fail-open on a prior that parses but is malformed: `--reconcile` is
    # the documented recovery path for a broken manifest, so nothing read
    # here may raise. A non-dict prior, a `files` that is not a list, an
    # entry that is not a dict — all read as "no prior", which stamps fresh.
    prior_entries = {}
    if isinstance(prior, dict):
        prior_files = prior.get("files")
        if not isinstance(prior_files, list):
            prior_files = []
        prior_entries = {
            f.get("path"): f for f in prior_files if isinstance(f, dict)
        }

    entries = []
    installed_any = False
    for spec in file_specs:
        installed = bool(spec.get("installed", True))
        carried_stamp = None
        prior_entry = prior_entries.get(spec["path"])
        if not installed:
            if prior_entry is not None:
                carried_stamp = prior_entry.get("installed_at") or None
        # A standing keep-local survives any writer that does not decide it:
        # an upgrade states it explicitly per file; enrich and reconcile carry
        # whatever the prior entry recorded.
        if "local" in spec:
            local = spec["local"]
        else:
            local = prior_entry.get("local") if isinstance(prior_entry, dict) else None
        if local and _keep_is_moot(spec["path"], spec["ownership"]):
            local = None  # moot on a project-owned file (v0.17.0)
        # The template base (v0.17.0): an upgrade states it per file; any
        # other writer carries the prior entry's base for a file it did not
        # write, so a template change stays reported until the project acts.
        if "template_sha" in spec:
            template_sha = spec["template_sha"]
        elif not installed and isinstance(prior_entry, dict):
            template_sha = prior_entry.get("template_sha")
            on_disk = target / spec["path"]
            if (not template_sha and spec["ownership"] == "bootstrap-with-template-tracking"
                    and on_disk.is_file()):
                # No recorded base (e.g. a scaffold-class entry enrich now
                # re-records as project-owned): based on its own bytes.
                template_sha = sha256_strict(on_disk)
        else:
            template_sha = None
        entry = build_manifest_entry(
            spec["path"],
            spec["ownership"],
            target,
            scaffold_manifest,
            is_text=spec.get("text", True),
            rendered_from_override=spec.get("rendered_from"),
            installed_at=carried_stamp,
            local=local,
            template_sha=template_sha,
        )
        if entry is not None:
            entries.append(entry)
            if installed or carried_stamp is None:
                installed_any = True

    # Carried only when this run enriched nothing: no install, no removal,
    # same scaffold version/commit, same profile, and the same set of
    # (path, ownership) — a profile switch or an orphan reclassification IS
    # an enrichment even though no bytes moved (review finding, v0.14.1).
    enriched_at = utc_now_iso()
    prior_shape = {
        (path, f.get("ownership")) for path, f in prior_entries.items()
    }
    if (
        isinstance(prior, dict)
        and not installed_any
        and prior.get("enriched_at")
        and prior.get("scaffold_version") == version
        and prior.get("scaffold_commit") == commit
        and prior.get("profile") == profile
        and prior_shape == {(e["path"], e["ownership"]) for e in entries}
    ):
        enriched_at = prior["enriched_at"]

    manifest = {
        "schema_version": SCHEMA_VERSION_CURRENT,
        "scaffold_version": version,
        "scaffold_commit": commit,
        "origin_url": get_scaffold_origin_url(),
        "profile": profile,
        "enriched_at": enriched_at,
        "normalization": {
            "recipe": NORMALIZATION_RECIPE,
            "version": NORMALIZATION_VERSION,
        },
        "files": entries,
    }

    # A re-run that changes nothing must write nothing new. TWO timestamp
    # families differ between two identical enrichments — the top-level
    # `enriched_at` and every file entry's `installed_at` — and a
    # timestamps-only rewrite turns every no-op upgrade into a commit now that
    # the upgrade commits its own work (v0.8.0). CI caught this as a timing
    # flake — two upgrades inside one clock second passed, across a second
    # boundary failed (test_a_second_upgrade_makes_no_empty_commit) — but the
    # underlying behavior bit every real re-upgrade, which is always more than
    # a second after the first. When the manifests agree on everything except
    # timestamps, keep the prior file byte-for-byte: skip the write entirely.
    # (The first fix masked only `enriched_at` and still failed — the honest
    # comparison masks every field that exists to record "when".)
    def _timeless(m):
        return {
            **m,
            "enriched_at": None,
            "files": [{**f, "installed_at": None} for f in m.get("files", [])],
        }

    if manifest_path.is_file():
        try:
            prior = json.loads(manifest_path.read_text())
            if _timeless(prior) == _timeless(manifest):
                return manifest_path
        except (ValueError, KeyError, TypeError):
            pass
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
    "DESIGN": "templates/design.template.md",  # M10 — opt-in via with-design profile
    "LEARNINGS": "templates/learnings.template.md",  # v0.4.7 cross-session learnings
    # v0.17.0: project-owned companion of the scaffold's docs/QUALITY_GATES.md
    "PROJECT_QUALITY_GATES": "templates/project-quality-gates.template.md",
}

# Docs that exist in the scaffold but never install downstream (matches
# the existing cmd_enrich filter).
SCAFFOLD_ONLY_DOCS = {"META_SPEC", "META_PHASES", "CAPABILITY_MANIFEST"}

# Scripts in the manifest's `scripts:` section that are workflow-relevant
# downstream (not all scaffold scripts; matches cmd_enrich's filter).
# Scripts a profile may pull in by naming them in `include_scripts`.
# Everything else in the capabilities `scripts:` map either ships to every
# project via ALWAYS_INSTALLED_FILE_PATHS or is scaffold-internal.
# `mutation-run` is here precisely so it can be OPT-IN: no profile except
# `with-mutation` names it, so a default project never receives it.
WORKFLOW_SCRIPTS = ("run-phase", "run-until-done", "mutation-run")

# === v0.5.0 stack profiles ==================================================
# A profile carrying `stack: <name>` seeds a real verify gate and installs a
# fleet-consistent conventions doc. See capabilities yaml `profiles:` comments.

DEFAULT_VERIFY_TEMPLATE = "templates/phasekit-verify.template.sh"
VERIFY_DEST_PATH = "scripts/phasekit-verify.sh"
CONVENTIONS_DEST_PATH = "docs/CONVENTIONS.md"

STACK_VERIFY_TEMPLATES = {
    "python-uv": "templates/phasekit-verify.template.python-uv.sh",
    "static-web": "templates/phasekit-verify.template.static-web.sh",
    "game-canvas": "templates/phasekit-verify.template.game-canvas.sh",
    "docs-only": "templates/phasekit-verify.template.docs-only.sh",
}

STACK_CONVENTIONS_TEMPLATES = {
    "python-uv": "templates/conventions.python-uv.md",
    "static-web": "templates/conventions.static-web.md",
    "game-canvas": "templates/conventions.game-canvas.md",
    "docs-only": "templates/conventions.docs-only.md",
}

# Stub-mode sentinel as rendered by DEFAULT_VERIFY_TEMPLATE. While the on-disk
# verify script still carries this line, `--upgrade` under a stack profile
# re-seeds it from the stack template; once flipped to =1 (or rewritten), the
# gate is configured and is NEVER overwritten by the scaffold.
VERIFY_STUB_SENTINEL = re.compile(r"(?m)^PHASEKIT_VERIFY_CONFIGURED=0\b")


def verify_gate_is_stub(verify_path):
    """True if the on-disk verify script is still in stub mode."""
    try:
        return bool(VERIFY_STUB_SENTINEL.search(Path(verify_path).read_text(
            encoding="utf-8", errors="replace")))
    except OSError:
        return False

# Always-installed files that aren't enumerated by the typed sections of the
# scaffold manifest (scaffold root + container files).
ALWAYS_INSTALLED_FILE_PATHS = (
    "CONTINUE_PROMPT.txt",
    "contracts/interface.json",
    "scripts/container-setup.sh",
    "scripts/verify-container.sh",
    "scripts/phasekit.sh",
    "scripts/phasekit-channel.sh",
    "scripts/phasekit-log-fmt.sh",
    "scripts/phasekit-contracts.py",
    "scripts/phasekit-roadmap.py",
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

    # Downstream AGENTS.md (rendered from templates/AGENTS.template.md).
    # Same scaffold-vs-downstream class asymmetry as CLAUDE.md — the
    # scaffold's own AGENTS.md is scaffold-internal; downstream's is a
    # rendered, project-owned bootstrap-with-template-tracking file.
    specs.append({
        "path": "AGENTS.md",
        "ownership": "bootstrap-with-template-tracking",
        "text": True,
        "rendered_from": "templates/AGENTS.template.md",
    })

    # Downstream scripts/phasekit-verify.sh. Backs the pre-commit verification
    # gate in run-until-done.sh; project owns the file after install
    # (bootstrap-with-template-tracking). Stack profiles (v0.5.0) seed a real
    # per-stack gate; profiles without a stack render the configure-me stub.
    stack = resolved_profile.get("stack")
    specs.append({
        "path": VERIFY_DEST_PATH,
        "ownership": "bootstrap-with-template-tracking",
        "text": True,
        "rendered_from": STACK_VERIFY_TEMPLATES.get(stack, DEFAULT_VERIFY_TEMPLATE),
    })

    # Stack conventions doc (v0.5.0): docs/CONVENTIONS.md. PROJECT-OWNED since
    # v0.17.0 (row 1140; scaffold class before): seeded once from the stack's
    # template, then the project's to amend — a stack rule a project corrects
    # is a correction, and no upgrade may put the old rule back. A template
    # change is reported, never applied: `check --include-templates`. The
    # templates stay placeholder-free, so rendering is an identity copy: that
    # is what lets the one migrating upgrade tell an unedited scaffold-era copy
    # (which takes the release's text) from an amended one (kept).
    if stack:
        specs.append({
            "path": CONVENTIONS_DEST_PATH,
            "ownership": "bootstrap-with-template-tracking",
            "text": True,
            "rendered_from": STACK_CONVENTIONS_TEMPLATES[stack],
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

    sweep_orphan_tmpfiles(target)

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
    on_disk = [{**s, "installed": False} for s in targets if (target / s["path"]).exists()]
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


def cmd_check(target_dir, strict=False, include_templates=False):
    """Compare on-disk files against the downstream manifest's recorded shas.

    With `include_templates=True` (M9 F2), also compare the scaffold's current
    template sha against each `bootstrap-with-template-tracking` entry's
    recorded `template_sha`; mismatches are reported as advisory drift
    (never auto-overwritten — the file was rendered once and is project-owned).

    Returns 0 if clean, 3 if drift or template-source drift detected — or an
    interrupted upgrade is pending (v0.16.0) — 1 on error.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        return 1

    if (_pending_dir(target) / "pending.json").exists():
        pending = _pending_upgrade(target) or {}
        print(f"PENDING UPGRADE: an upgrade ({pending.get('from_version')} -> "
              f"{pending.get('to_version')}, phase {pending.get('phase', 'unreadable')}) was "
              "interrupted before it finished; run `phasekit upgrade` to settle it.",
              file=sys.stderr)
        return 3

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
    template_drift = []
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

        # Template-source drift advisory (M9 F2; --include-templates only)
        if include_templates and ownership == "bootstrap-with-template-tracking":
            rendered_from = entry.get("rendered_from")
            recorded_template_sha = entry.get("template_sha")
            if rendered_from and recorded_template_sha:
                template_path = REPO_ROOT / rendered_from
                if template_path.exists():
                    current_template_sha = sha256_strict(template_path)
                    if current_template_sha != recorded_template_sha:
                        template_drift.append({
                            "path": entry["path"],
                            "rendered_from": rendered_from,
                            "recorded": recorded_template_sha[:12],
                            "current": current_template_sha[:12],
                        })

    label = "--check"
    if strict:
        label += " --strict"
    if include_templates:
        label += " --include-templates"
    print(f"{label}: scaffold {manifest.get('scaffold_version', '?')}")
    print(f"  clean: {clean}")
    print(f"  drifted: {len(drift)}")
    print(f"  missing: {len(missing)}")
    if skipped:
        print(f"  skipped (bootstrap-frozen, --strict): {len(skipped)}")
    if include_templates:
        print(f"  template-source drift (advisory): {len(template_drift)}")

    for path, ownership in drift:
        print(f"  DRIFT: {path}  ({ownership})")
    for path in missing:
        print(f"  MISSING: {path}")
    for adv in template_drift:
        print(
            f"  TEMPLATE DRIFT (advisory; never auto-overwritten): "
            f"{adv['path']} ← {adv['rendered_from']} "
            f"(was {adv['recorded']}, now {adv['current']})"
        )

    if drift or missing or template_drift:
        return 3
    return 0


def cmd_check_version(target_dir):
    """Report whether a downstream project is behind the running scaffold.

    Complements `--check` (which detects *file* drift): this compares the
    `scaffold_version`/`scaffold_commit` the project was enriched from against
    the version of the scaffold clone this command runs in. When the recorded
    commit is resolvable in the local clone, ancestry gives a precise
    behind/ahead/diverged verdict; otherwise it falls back to a plain version
    string compare.

    "Behind" is informational (exit 0), matching `--check`'s tone. Only usage
    or I/O problems return non-zero.
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

    recorded_version = manifest.get("scaffold_version", "unknown")
    recorded_commit = manifest.get("scaffold_commit", "unknown")
    current_version, current_commit = get_scaffold_version()

    print(f"--check-version: {target}")
    print(f"  enriched from:  {recorded_version} ({recorded_commit})")
    print(f"  this scaffold:  {current_version} ({current_commit})")

    def _git(*args):
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    # Precise verdict when the recorded commit exists in this clone's history.
    resolvable = (
        recorded_commit not in (None, "", "unknown")
        and _git("cat-file", "-e", f"{recorded_commit}^{{commit}}") is not None
    )
    if resolvable:
        if recorded_commit == current_commit or _git("rev-parse", "HEAD") == \
                _git("rev-parse", recorded_commit):
            print("  status: up to date")
        elif _git("merge-base", "--is-ancestor", recorded_commit, "HEAD") is not None:
            behind = _git("rev-list", "--count", f"{recorded_commit}..HEAD") or "?"
            print(f"  status: BEHIND by {behind} commit(s) — run `phasekit --upgrade`")
        elif _git("merge-base", "--is-ancestor", "HEAD", recorded_commit) is not None:
            print("  status: ahead (enriched from a newer scaffold than this clone)")
        else:
            print("  status: diverged (no common ancestor on this branch)")
    else:
        # Fallback: the recorded commit isn't in this clone (shallow clone,
        # different remote, or a pre-tag synthetic version). Compare strings.
        if recorded_version == current_version:
            print("  status: up to date (by version string)")
        else:
            print("  status: differs — recorded commit not in this clone; "
                  "cannot determine direction. Run `phasekit --upgrade` if unsure.")
    return 0


def cmd_status(target_dir):
    """Print the project's current phase state as a *derived view* of the
    workflow artifacts — never a second source of truth. The authority remains
    `artifacts/phase-approval.json` (+ git log); this command only renders it.

    Reports, in order of precedence: project completion, the blocker that
    stopped the loop, a pending pre-commit verify failure, and otherwise the
    last approved phase with a pointer to the next one. Always exit 0 unless the
    target is unusable.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        return 1

    artifacts = target / "artifacts"

    def load(name):
        path = artifacts / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"  WARN: {path.name} is present but unreadable", file=sys.stderr)
            return None

    def trim(text, limit=200):
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    approval = load("phase-approval.json")
    blocked = load("phase-blocked.json")
    complete = load("project-complete.json")
    verify_failed = load("phase-verify-failed.json")

    print(f"phasekit status: {target}")

    if not any((approval, blocked, complete, verify_failed)):
        print("  no phase artifacts yet — project not started, or artifacts/ is empty.")
        return 0

    if approval is not None:
        phase = approval.get("phase", "?")
        suffix = "" if approval.get("approved") else "  (NOT marked approved)"
        print(f"  approved through: {phase}{suffix}")
        if approval.get("summary"):
            print(f"  summary: {trim(approval['summary'])}")

    # Git context: the commit that last recorded the approval (authority trail).
    try:
        last = subprocess.run(
            ["git", "-C", str(target), "log", "-1", "--format=%h %s",
             "--", "artifacts/phase-approval.json"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if last:
            print(f"  last approval commit: {last}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if complete is not None:
        print("  state: PROJECT COMPLETE")
        if complete.get("summary"):
            print(f"  summary: {trim(complete['summary'])}")
        return 0

    if blocked is not None:
        kind = blocked.get("blocker_kind", "")
        reason = blocked.get("reason") or blocked.get("summary") or "(no reason given)"
        print(f"  state: BLOCKED{(' [' + kind + ']') if kind else ''} — {trim(reason)}")
        if blocked.get("next_step"):
            print(f"  next step: {blocked['next_step']}")
        return 0

    if verify_failed is not None:
        attempts = verify_failed.get("attempts", "?")
        print(f"  state: VERIFY FAILED (attempt {attempts}) — "
              f"{trim(verify_failed.get('command', ''), 80)}; fix before the next commit")
        return 0

    print("  state: phase approved; next: start the next unapproved phase")
    return 0


# ============================================================================
# Default command: enrich
# ============================================================================


def cmd_enrich(args):
    """Enrich a downstream project from a manifest profile.

    Single source of truth: `enumerate_install_targets(manifest, resolved)`.
    Each spec is rendered (if `rendered_from`) or copied; secrets/symlink
    safety is enforced; `.scaffold/manifest.json` is written with shas
    matching everything that landed.
    """
    target = Path(args.target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target directory does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    sweep_orphan_tmpfiles(target)

    manifest = load_manifest()
    profiles = manifest.get("profiles", {})
    resolved = resolve_profile(profiles, args.profile)

    project_name = target.name
    install_specs = enumerate_install_targets(manifest, resolved)

    print(f"\nInstalling {len(install_specs)} target(s) from profile '{args.profile}':")
    copied = 0
    skipped = 0
    for spec in install_specs:
        try:
            installed = install_from_spec(
                spec, target, project_name,
                force=args.force, dry_run=args.dry_run,
            )
        except RuntimeError as e:
            print(f"  REFUSE: {e}", file=sys.stderr)
            sys.exit(1)
        spec["installed"] = bool(installed)
        if installed:
            copied += 1
        else:
            skipped += 1

    # Empty workflow directories (per existing convention).
    for d in ["artifacts", "docs/adr"]:
        dir_path = target / d
        if not dir_path.exists():
            if args.dry_run:
                print(f"  Would create dir: {dir_path}")
            else:
                dir_path.mkdir(parents=True, exist_ok=True)
                print(f"  Created dir: {dir_path}")

    # Provenance manifest (M9). Records sha for every file that ended up on
    # disk under one of our install specs.
    if not args.dry_run:
        on_disk = [s for s in install_specs if (target / s["path"]).exists()]
        with target_lock(target, no_lock=getattr(args, "no_lock", False)):
            mpath = write_downstream_manifest(target, manifest, args.profile, on_disk)
        print(f"\nManifest: {mpath}  ({len(on_disk)} entries)")

    print(f"\nDone. {copied} installed, {skipped} skipped (already exist).")
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
    parser.add_argument("--check-version", dest="check_version", action="store_true",
                        help="Report whether a downstream project is behind the running scaffold version")
    parser.add_argument("--status", action="store_true",
                        help="Print the project's current phase state (derived from workflow artifacts)")
    parser.add_argument("--reconcile", action="store_true",
                        help="Build a retroactive .scaffold/manifest.json for a project enriched before M9")
    parser.add_argument("--migrate-only", dest="migrate_only", action="store_true",
                        help="Migrate the manifest's schema_version forward without other side effects")
    parser.add_argument("--upgrade", action="store_true",
                        help="Plan-then-confirm upgrade of a downstream project against the current scaffold")
    parser.add_argument("--no-commit", action="store_true",
                        help="--upgrade: install the files but do not commit or push them "
                             "(default is to commit the upgrade's own work, so an idle "
                             "project is not left with a dirty tree it can never absorb)")
    parser.add_argument("--no-verify", dest="no_verify", action="store_true",
                        help="--upgrade: commit without running the project's own gate on "
                             "the upgraded tree (the commit subject says so). Default: the "
                             "gate runs first and a red gate restores the tree and commits "
                             "nothing. Where it runs: PHASEKIT_UPGRADE_VERIFY=auto|container|"
                             "host|off")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove scaffold-owned files (scaffold class). Use --include-once to also remove bootstrap-* files.")
    parser.add_argument("--include-once", dest="include_once", action="store_true",
                        help="With --uninstall: also remove bootstrap-frozen and bootstrap-with-template-tracking files")
    parser.add_argument("--strict", action="store_true",
                        help="Use byte-exact hashing instead of normalized (skips bootstrap-frozen)")
    parser.add_argument("--include-templates", dest="include_templates", action="store_true",
                        help="With --check: also surface advisory drift on bootstrap-with-template-tracking template_sha changes")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt for --upgrade (non-interactive)")
    parser.add_argument("--interactive", action="store_true",
                        help="With --upgrade: prompt per drifted file [k/t/d/s] (mutex with --yes)")
    parser.add_argument("--no-lock", dest="no_lock", action="store_true",
                        help="Skip per-target fcntl.flock (for CI with its own mutexes)")
    parser.add_argument("--keep-local", dest="keep_local", action="append", default=[],
                        metavar="PATH", help="Per-file: preserve on-disk version during --upgrade")
    parser.add_argument("--take-new", dest="take_new", action="append", default=[],
                        metavar="PATH", help="Per-file: take scaffold-new version during --upgrade")
    parser.add_argument("--adopt", action="append", default=[],
                        metavar="PATH", help="Resolve collision-novel: record current as canonical")
    parser.add_argument("--rename-local", dest="rename_local", action="append", default=[],
                        metavar="PATH=NEWPATH", help="Resolve collision-novel: move on-disk file aside")
    parser.add_argument("--accept-removal", dest="accept_removal", action="append", default=[],
                        metavar="PATH", help="Allow --upgrade to delete a removed scaffold file")
    args = parser.parse_args()

    if args.self_check:
        sys.exit(cmd_self_check())

    if args.check:
        if not args.target_dir:
            parser.error("--check requires target_dir")
        sys.exit(cmd_check(
            args.target_dir,
            strict=args.strict,
            include_templates=args.include_templates,
        ))

    if args.check_version:
        if not args.target_dir:
            parser.error("--check-version requires target_dir")
        sys.exit(cmd_check_version(args.target_dir))

    if args.status:
        if not args.target_dir:
            parser.error("--status requires target_dir")
        sys.exit(cmd_status(args.target_dir))

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

    if args.uninstall:
        if not args.target_dir:
            parser.error("--uninstall requires target_dir")
        sys.exit(cmd_uninstall(
            args.target_dir,
            include_once=args.include_once,
            yes=args.yes,
            no_lock=args.no_lock,
            dry_run=args.dry_run,
        ))

    if args.upgrade:
        if not args.target_dir:
            parser.error("--upgrade requires target_dir")
        sys.exit(cmd_upgrade(
            args.target_dir,
            profile=args.profile,
            dry_run=args.dry_run,
            yes=args.yes,
            no_lock=args.no_lock,
            interactive=args.interactive,
            keep_local=args.keep_local,
            take_new=args.take_new,
            adopt=args.adopt,
            rename_local=args.rename_local,
            accept_removal=args.accept_removal,
            commit=not args.no_commit,
            no_verify=args.no_verify,
        ))

    # Default: enrich
    if not args.target_dir:
        parser.error("target_dir is required for enrichment (or use --self-check)")
    if args.profile is None:
        args.profile = "default"
    cmd_enrich(args)


if __name__ == "__main__":
    main()
