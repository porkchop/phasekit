#!/usr/bin/env python3
"""Cross-project contract dependencies — the consumer half (phasekit v0.7.0).

WHY THIS EXISTS
---------------
Three times in one week a component was built against an interface it had
*guessed*: phasekit consumed an env var nothing emitted; the orchestrator
exported an env var nothing consumed; a dashboard read `rec.id` from an
endpoint that sends `iteration`. Each was caught by a human's eye, late.

This module gives a consumer repo a machine-checkable copy of its
dependency's real interface, so drift is a red gate instead of a discovery.

THE TWO QUESTIONS, DELIBERATELY SEPARATED
-----------------------------------------
  Conformance   Does my code match the contract?
                -> the project's ordinary test suite
                -> needs only the COMMITTED (vendored) copy in this repo
  Authenticity  Is that copy really the producer's current contract?
                -> the phasekit verify gate
                -> needs the provider MOUNT

The consumer vendors the contract exactly as this fleet commits `uv.lock`:
`git clone && <test command>` passes anywhere, hermetically, with no mount
and no provider. The gate asserts vendored == mounted and fails loudly on
any difference — whether the producer moved on, or a consumer edited its
copy to go green. A forged copy simply does not survive the next build.

STANDALONE IS A HARD CONSTRAINT
-------------------------------
phasekit is a public tool with no orchestrator required (docs/META_SPEC.md).
So the trigger for refusing is a REPO-OWNED DECLARATION, never the mount's
absence:

  * no `contracts.yaml`            -> nothing declared, nothing checked, silent
  * `contracts.yaml` declares deps -> a missing/unreadable contract REFUSES

Because the declaration lives in the repo it travels: a standalone user who
wants contract validation declares dependencies and mounts them by hand, and
phasekit behaves identically. Foundry is one possible *provider* of a
documented interface, not a prerequisite.

Usage:
  python3 scripts/phasekit-contracts.py status   [--repo DIR] [--json]
  python3 scripts/phasekit-contracts.py provider [--json]

See docs/CONTRACTS.md for the file format and the full lifecycle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by humans, not CI
    print(
        "Error: pyyaml is required to read contracts.yaml. "
        "Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


# --- Conventions (single source of truth; docs/CONTRACTS.md quotes these) ---

# The consumer's declaration. Repo root, PROJECT-OWNED: phasekit never writes,
# renders or upgrades this file, so `phasekit upgrade` can never erase a
# project's declarations.
CONTRACTS_FILENAME = "contracts.yaml"

# Where the consumer vendors its copies. One directory per dependency slug.
# Deliberately NOT `contracts/` — that name belongs to a repo's own PRODUCED
# contracts, and a repo can be both producer and consumer.
DEFAULT_VENDOR_ROOT = "vendor/contracts"

# Where a provider (e.g. the Foundry orchestrator) mounts the authoritative
# copies, read-only. Overridable via PHASEKIT_CONTRACTS_DIR so a standalone
# user — or a test — can point somewhere else.
DEFAULT_MOUNT_DIR = "/contracts"
MOUNT_DIR_ENV = "PHASEKIT_CONTRACTS_DIR"

# The provider's manifest inside the mount. "No dependencies" is a manifest
# with zero entries, NEVER an empty directory: an empty dir is
# indistinguishable from a broken bind mount, whereas a manifest is the
# provider *asserting* that it checked.
INDEX_FILENAME = "index.json"

SUPPORTED_VERSION = 1

# Slugs become path components, so they are constrained hard: lowercase,
# no separators, no dots-only traversal.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_TOP_LEVEL_KEYS = {"version", "depends_on"}
_ENTRY_KEYS = {"slug", "vendor", "description"}


class ContractsError(Exception):
    """A malformed declaration. Always a hard error — never fail-open.

    A typo'd key that silently does nothing is precisely the bug class this
    release exists to kill, so unknown keys are errors too.
    """


@dataclass(frozen=True)
class Dependency:
    """One declared contract dependency."""

    slug: str
    # Repo-relative POSIX path holding the vendored copy.
    vendor_path: str
    description: str = ""

    def vendor_dir(self, repo_root: Path) -> Path:
        return repo_root / self.vendor_path


@dataclass(frozen=True)
class Declaration:
    """The parsed `contracts.yaml`, or the inert absence of one."""

    # True only when contracts.yaml was found on disk. The gate keys off this:
    # absent == a v0.6.6 repo, which must behave exactly as it always did.
    present: bool = False
    path: Path | None = None
    version: int = SUPPORTED_VERSION
    dependencies: tuple[Dependency, ...] = field(default_factory=tuple)

    @property
    def declares_dependencies(self) -> bool:
        return bool(self.dependencies)

    def slugs(self) -> tuple[str, ...]:
        return tuple(d.slug for d in self.dependencies)


def contracts_file(repo_root: Path | str) -> Path:
    return Path(repo_root) / CONTRACTS_FILENAME


def _require_relative_path(raw: object, slug: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractsError(
            f"dependency '{slug}': `vendor` must be a non-empty string"
        )
    value = raw.strip()
    pure = Path(value)
    if pure.is_absolute() or value.startswith("/") or value.startswith("~"):
        raise ContractsError(
            f"dependency '{slug}': `vendor` must be a repo-relative path, got {value!r}"
        )
    parts = pure.parts
    if any(part == ".." for part in parts):
        raise ContractsError(
            f"dependency '{slug}': `vendor` must not escape the repo with '..', got {value!r}"
        )
    # Normalize away "./" and trailing slashes so two spellings of the same
    # path can never produce two different vendored locations.
    return Path(*[p for p in parts if p != "."]).as_posix()


def _parse_entry(raw: object, index: int) -> Dependency:
    # Shorthand: a bare string is the slug, with every default applied.
    if isinstance(raw, str):
        raw = {"slug": raw}
    if not isinstance(raw, dict):
        raise ContractsError(
            f"depends_on[{index}]: expected a slug string or a mapping, got {type(raw).__name__}"
        )

    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ContractsError(
            f"depends_on[{index}]: unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ENTRY_KEYS)}"
        )

    slug = raw.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ContractsError(
            f"depends_on[{index}]: `slug` must match {SLUG_RE.pattern} "
            f"(lowercase, no path separators), got {slug!r}"
        )

    vendor_raw = raw.get("vendor")
    if vendor_raw is None:
        vendor_path = f"{DEFAULT_VENDOR_ROOT}/{slug}"
    else:
        vendor_path = _require_relative_path(vendor_raw, slug)

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ContractsError(
            f"dependency '{slug}': `description` must be a string"
        )

    return Dependency(slug=slug, vendor_path=vendor_path, description=description)


def load_declaration(repo_root: Path | str) -> Declaration:
    """Read `<repo_root>/contracts.yaml`.

    Returns an inert `Declaration(present=False)` when the file does not
    exist — that is the v0.6.6 behavior every existing repo must keep.
    Raises ContractsError on any malformed declaration.
    """
    path = contracts_file(repo_root)
    if not path.exists():
        return Declaration(present=False, path=None)

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ContractsError(f"{CONTRACTS_FILENAME}: not valid YAML: {exc}") from exc

    if data is None:
        raise ContractsError(
            f"{CONTRACTS_FILENAME} is empty. Declare `version: {SUPPORTED_VERSION}` "
            f"and a `depends_on:` list (use `depends_on: []` for none), "
            f"or delete the file."
        )
    if not isinstance(data, dict):
        raise ContractsError(
            f"{CONTRACTS_FILENAME}: top level must be a mapping, "
            f"got {type(data).__name__}"
        )

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ContractsError(
            f"{CONTRACTS_FILENAME}: unknown top-level key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_TOP_LEVEL_KEYS)}"
        )

    version = data.get("version")
    if version != SUPPORTED_VERSION:
        raise ContractsError(
            f"{CONTRACTS_FILENAME}: `version` must be {SUPPORTED_VERSION}, got {version!r}"
        )

    raw_deps = data.get("depends_on", [])
    if raw_deps is None:
        raw_deps = []
    if not isinstance(raw_deps, list):
        raise ContractsError(
            f"{CONTRACTS_FILENAME}: `depends_on` must be a list "
            f"(use `depends_on: []` to declare none), got {type(raw_deps).__name__}"
        )

    deps: list[Dependency] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_deps):
        dep = _parse_entry(raw, i)
        if dep.slug in seen:
            raise ContractsError(
                f"{CONTRACTS_FILENAME}: duplicate dependency slug {dep.slug!r}"
            )
        seen.add(dep.slug)
        deps.append(dep)

    return Declaration(
        present=True,
        path=path,
        version=SUPPORTED_VERSION,
        dependencies=tuple(deps),
    )


def mount_dir() -> Path:
    """The provider mount point. Overridable for standalone/test use."""
    return Path(os.environ.get(MOUNT_DIR_ENV) or DEFAULT_MOUNT_DIR)


# --- The provider side of the mount ----------------------------------------


@dataclass(frozen=True)
class ProviderEntry:
    """One contract the provider says it has mounted."""

    slug: str
    # Path relative to the mount root. Defaults to the slug.
    rel_path: str

    def source_dir(self, mount: Path) -> Path:
        return mount / self.rel_path


@dataclass(frozen=True)
class ProviderIndex:
    """The mount's `index.json`, or the positive absence of a provider.

    `present` distinguishes "a provider is here and asserts what it offers"
    from "no provider at all". A provider with nothing to offer still ships an
    index with zero entries — that is the provider stating it checked, and it
    is deliberately NOT the same observable as a missing or broken mount.
    """

    present: bool = False
    path: Path | None = None
    mount: Path | None = None
    entries: tuple[ProviderEntry, ...] = field(default_factory=tuple)

    def get(self, slug: str) -> ProviderEntry | None:
        for entry in self.entries:
            if entry.slug == slug:
                return entry
        return None

    def slugs(self) -> tuple[str, ...]:
        return tuple(e.slug for e in self.entries)


def load_provider_index(mount: Path | None = None) -> ProviderIndex:
    """Read `<mount>/index.json`.

    Returns `ProviderIndex(present=False)` when no provider is mounted — the
    ordinary standalone case, which is never an error here. Raises
    ContractsError only when a provider IS present and its manifest is broken,
    because a mounted-but-unreadable manifest is a misconfiguration that must
    never be mistaken for "no dependencies".

    Note the deliberate asymmetry with `load_declaration`: unknown keys in
    index.json are TOLERATED. contracts.yaml is phasekit's own format, so
    strictness there catches typos; index.json crosses a repo boundary and is
    versioned by a separate producer, so tolerating additive fields is what
    lets the two sides ship independently.
    """
    root = Path(mount) if mount is not None else mount_dir()
    index_path = root / INDEX_FILENAME

    if not index_path.is_file():
        return ProviderIndex(present=False, mount=root)

    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractsError(f"{index_path}: unreadable provider manifest: {exc}") from exc

    if not isinstance(raw, dict):
        raise ContractsError(
            f"{index_path}: provider manifest must be a JSON object, "
            f"got {type(raw).__name__}"
        )

    raw_entries = raw.get("entries", [])
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise ContractsError(
            f"{index_path}: `entries` must be a list (use [] to assert none), "
            f"got {type(raw_entries).__name__}"
        )

    entries: list[ProviderEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_entries):
        if isinstance(item, str):
            item = {"slug": item}
        if not isinstance(item, dict):
            raise ContractsError(
                f"{index_path}: entries[{i}] must be a slug string or an object"
            )
        slug = item.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ContractsError(
                f"{index_path}: entries[{i}] has an invalid `slug`: {slug!r}"
            )
        if slug in seen:
            raise ContractsError(f"{index_path}: duplicate entry slug {slug!r}")
        seen.add(slug)

        rel = item.get("path", slug)
        if not isinstance(rel, str) or not rel.strip():
            raise ContractsError(
                f"{index_path}: entries[{i}] `path` must be a non-empty string"
            )
        rel = rel.strip()
        rel_pure = Path(rel)
        if rel_pure.is_absolute() or ".." in rel_pure.parts:
            raise ContractsError(
                f"{index_path}: entries[{i}] `path` must stay inside the mount, got {rel!r}"
            )
        entries.append(
            ProviderEntry(
                slug=slug,
                rel_path=Path(*[p for p in rel_pure.parts if p != "."]).as_posix(),
            )
        )

    return ProviderIndex(
        present=True, path=index_path, mount=root, entries=tuple(entries)
    )


# --- CLI -------------------------------------------------------------------


def cmd_status(repo_root: Path, as_json: bool) -> int:
    decl = load_declaration(repo_root)

    if as_json:
        payload = {
            "declaration_present": decl.present,
            "version": decl.version if decl.present else None,
            "dependencies": [
                {
                    "slug": d.slug,
                    "vendor": d.vendor_path,
                    "vendored": (repo_root / d.vendor_path).is_dir(),
                    "description": d.description,
                }
                for d in decl.dependencies
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not decl.present:
        print(
            f"contracts: no {CONTRACTS_FILENAME} — this repo declares no contract "
            f"dependencies (nothing is checked)."
        )
        return 0
    if not decl.declares_dependencies:
        print(f"contracts: {CONTRACTS_FILENAME} present, 0 dependencies declared.")
        return 0

    print(f"contracts: {len(decl.dependencies)} dependency(ies) declared:")
    for dep in decl.dependencies:
        state = "vendored" if (repo_root / dep.vendor_path).is_dir() else "MISSING"
        suffix = f" — {dep.description}" if dep.description else ""
        print(f"  - {dep.slug}: {dep.vendor_path} [{state}]{suffix}")
    return 0


def cmd_provider(as_json: bool) -> int:
    index = load_provider_index()
    mount = index.mount or mount_dir()

    if as_json:
        print(json.dumps(
            {
                "mount": str(mount),
                "provider_present": index.present,
                "entries": [
                    {
                        "slug": e.slug,
                        "path": e.rel_path,
                        "readable": e.source_dir(mount).is_dir(),
                    }
                    for e in index.entries
                ],
            },
            indent=2, sort_keys=True,
        ))
        return 0

    if not index.present:
        print(
            f"contracts: no provider mounted at {mount} "
            f"(set {MOUNT_DIR_ENV}, or run under a provider that supplies one)."
        )
        return 0
    if not index.entries:
        print(f"contracts: provider mounted at {mount}, asserting 0 available contracts.")
        return 0

    print(f"contracts: provider mounted at {mount}, {len(index.entries)} available:")
    for entry in index.entries:
        state = "readable" if entry.source_dir(mount).is_dir() else "MISSING"
        print(f"  - {entry.slug}: {entry.rel_path} [{state}]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phasekit-contracts",
        description="Cross-project contract dependencies (consumer side).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repository root (default: the parent of this script's directory).",
    )
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser(
        "status", help="Show what this repo declares and whether it is vendored."
    )
    p_status.add_argument("--json", action="store_true", help="Machine-readable output.")

    p_provider = sub.add_parser(
        "provider",
        help=f"Show what the mounted provider offers (mount: ${MOUNT_DIR_ENV} "
             f"or {DEFAULT_MOUNT_DIR}).",
    )
    p_provider.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def resolve_repo_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    repo_root = resolve_repo_root(args.repo)
    try:
        if args.command == "status":
            return cmd_status(repo_root, as_json=args.json)
        if args.command == "provider":
            return cmd_provider(as_json=args.json)
    except ContractsError as exc:
        print(f"phasekit-contracts: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
