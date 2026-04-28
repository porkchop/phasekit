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
            current_sha = None

        # Compute scaffold-new sha (what the engine would install today).
        src = _scaffold_source_for_spec(spec)
        if src.exists():
            new_norm, new_strict = compute_file_shas(src, is_text)
            scaffold_new_sha = new_norm if is_text else new_strict
        else:
            scaffold_new_sha = None

        existing = existing_by_path.get(path)
        manifest_sha = existing.get("sha256") if existing else None

        # State + default action
        if existing is None:
            if current_sha is not None:
                # collision-novel: scaffold-new declares a path the project already has
                state = "collision-novel"
                if path in adopt:
                    action = ACTION_ADOPT
                elif path in rename_local_map:
                    action = ACTION_RENAME_LOCAL
                else:
                    action = ACTION_REFUSE
            else:
                state = "new-install"
                action = ACTION_INSTALL
        else:
            # Tracked
            if current_sha is None:
                state = "missing"
                action = ACTION_INSTALL
            elif current_sha == manifest_sha:
                # local == manifest. For `scaffold` class, also compare
                # scaffold-new sha to surface an "update available". For
                # bootstrap-* classes, content-tracking is the manifest sha
                # only; template-source drift surfaces via
                # `--check --include-templates`, not via the upgrade plan
                # (M9 review F5 fix).
                if (
                    ownership == "scaffold"
                    and scaffold_new_sha is not None
                    and scaffold_new_sha != manifest_sha
                ):
                    state = "update-available"
                    # Default action is to take the scaffold-new version, but
                    # `--keep-local` overrides — the user intent ("preserve
                    # my version even though scaffold has a newer canonical")
                    # applies symmetrically to drifted and update-available.
                    if path in keep_local:
                        action = ACTION_KEEP_LOCAL
                    else:
                        action = ACTION_TAKE_NEW
                else:
                    state = "clean"
                    action = ACTION_NOOP
            else:
                # drifted: current != manifest
                state = "drifted"
                if path in keep_local:
                    action = ACTION_KEEP_LOCAL
                elif path in take_new:
                    action = ACTION_TAKE_NEW
                else:
                    # bootstrap-* default keep-local; scaffold default refuse
                    if ownership in ("bootstrap-frozen", "bootstrap-with-template-tracking"):
                        action = ACTION_KEEP_LOCAL
                    else:
                        action = ACTION_REFUSE

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
            elif p["state"] == "collision-novel":
                note = "  (scaffold v2 declares an existing project path)"
            elif p["state"] == "removed":
                note = "  (no longer declared by the scaffold)"
            elif p["state"] == "new-install":
                note = "  (not yet installed)"
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

        if action == ACTION_NOOP:
            # Tracked clean files stay in the manifest
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
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
            file_specs_for_manifest.append(spec)
        elif action == ACTION_KEEP_LOCAL:
            # Leave on-disk file as-is. Manifest sha will be updated to current.
            print(f"  keep-local: {path}")
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
            })
        elif action == ACTION_ADOPT:
            # collision-novel: trust on-disk content; record under scaffold-new path
            print(f"  adopt: {path}")
            file_specs_for_manifest.append({
                "path": path, "ownership": p["ownership"],
                "text": p["text"], "rendered_from": p["rendered_from"],
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
            file_specs_for_manifest.append(spec)
        elif action == ACTION_DELETE:
            try:
                dest.unlink()
                print(f"  delete: {path}")
            except FileNotFoundError:
                pass
            # Do NOT add to file_specs_for_manifest
        elif action == ACTION_ORPHAN:
            print(f"  orphan: {path}  (left in place; scaffold no longer declares it)")
            # Re-record under a downgraded class so subsequent --check stays sane
            file_specs_for_manifest.append({
                "path": path, "ownership": OWNERSHIP_CLASS_ORPHAN,
                "text": p["text"], "rendered_from": p["rendered_from"],
            })

    # Rewrite the manifest with the post-apply state.
    write_downstream_manifest(target, scaffold_manifest, profile,
                              file_specs_for_manifest)
    return 0


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
                accept_removal=()):
    """Upgrade a downstream project: re-evaluate scaffold-owned files and
    apply changes after a plan-then-confirm cycle.

    Returns 0 on success, 1 on error, 3 on unresolved refusals.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        print(f"Error: target does not exist: {target}", file=sys.stderr)
        return 1

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
        plans = _interactive_resolve(plans, target)
        if plans is None:
            print("Stopped.", file=sys.stderr)
            return 1

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

    with target_lock(target, no_lock=no_lock):
        return apply_upgrade_plan(target, scaffold_manifest, plans, profile)


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


def cmd_check(target_dir, strict=False, include_templates=False):
    """Compare on-disk files against the downstream manifest's recorded shas.

    With `include_templates=True` (M9 F2), also compare the scaffold's current
    template sha against each `bootstrap-with-template-tracking` entry's
    recorded `template_sha`; mismatches are reported as advisory drift
    (never auto-overwritten — the file was rendered once and is project-owned).

    Returns 0 if clean, 3 if drift or template-source drift detected, 1 on error.
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
    parser.add_argument("--reconcile", action="store_true",
                        help="Build a retroactive .scaffold/manifest.json for a project enriched before M9")
    parser.add_argument("--migrate-only", dest="migrate_only", action="store_true",
                        help="Migrate the manifest's schema_version forward without other side effects")
    parser.add_argument("--upgrade", action="store_true",
                        help="Plan-then-confirm upgrade of a downstream project against the current scaffold")
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
        ))

    # Default: enrich
    if not args.target_dir:
        parser.error("target_dir is required for enrichment (or use --self-check)")
    if args.profile is None:
        args.profile = "default"
    cmd_enrich(args)


if __name__ == "__main__":
    main()
