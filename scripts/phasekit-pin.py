#!/usr/bin/env python3
"""phasekit's engine outside the repository (v0.19.0): pins, the engine store,
and the verbs that work on a PINNED project.

A pinned project tracks `.phasekit-version` (one release tag) and none of
phasekit's engine files: the loop, the hooks, the agents, the scaffold docs and
the container setup live in a read-only engine checkout outside the tree,
`<store>/<tag>/`, filled by `git archive <tag>` from the canonical clone. A
vendored project (scripts/run-until-done.sh in its own tree) keeps working
exactly as before; nothing here touches one except `migrate`, which converts it.

Verbs (run by scripts/phasekit.sh; all operate on the project around the cwd):

  resolve                 print the engine directory for this project's pin
                          (fetching it first unless offline — see below)
  engines install TAG     fill <store>/TAG (idempotent; verified, read-only)
  engines list            the installed engines
  engines path TAG        print <store>/TAG (exit 6 when not installed)
  init [PROFILE]          a NEW pinned project: the pin, the project's own docs,
                          its gate, settings (permissions only); one commit
  migrate [--discard-local] [--dry-run]
                          a VENDORED project -> pinned: delete exactly the
                          manifest's scaffold entries and .scaffold/, write the
                          pin, strip the hook wiring from .claude/settings.json,
                          run the gate under the engine, one commit. Idempotent.
  check                   the pinned project's health (exit 0 / 3 / 6)
  upgrade [--to TAG]      a pin bump through the project's gate, one commit
  plugin install|status   the Claude Code plugin for interactive sessions
  docs                    print the engine's docs directory

Auto-fetch: a pin the store lacks is installed on first use from the canonical
clone's own tags (git objects, so the bytes are the tag's tree); a tag the
clone lacks is fetched from the clone's `origin` first. PHASEKIT_NO_AUTO_FETCH=1
(offline use) turns both off: the verb then fails with exit 6 naming
`phasekit engines install TAG`. Never a network fetch of anything but git tags.

Exit codes: 0 ok; 1 error; 2 usage / not a project of the right kind; 3 check
found a leftover (a tracked engine path, stale hook wiring); 4 the gate was
red and nothing changed; 6 the pinned engine is not installed (and was not
fetched).
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
PIN_FILE = ".phasekit-version"
VERSION_FILE = ".engine-version"
COMMIT_FILE = ".engine-commit"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# The first engine that can run a project from outside its tree.
MIN_ENGINE = (0, 19, 0)
NO_AUTO_FETCH_ENV = "PHASEKIT_NO_AUTO_FETCH"
STORE_ENV = "PHASEKIT_ENGINE_STORE"
ENGINE_LOCK_ENV = "PHASEKIT_ENGINE_DIR"
PLUGIN_NAME = "phasekit"
MARKETPLACE_NAME = "phasekit"
MIGRATE_SUBJECT = "chore(phasekit): migrate to the engine outside the repo"
PIN_SUBJECT = "chore(phasekit): pin"
INIT_SUBJECT = "chore(phasekit): init"
# The engine's container-side mount and its docs, for prompts and messages.
ENGINE_CONTAINER_DIR = "/opt/phasekit"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LEFTOVER = 3
EXIT_GATE_RED = 4
EXIT_NOT_INSTALLED = 6


class PinError(Exception):
    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code


def say(msg):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


# --- the enrich engine (profiles, templates, the gate runner) ----------------

_ENRICH = None


def enrich():
    """scripts/enrich-project.py as a module (its name has a hyphen)."""
    global _ENRICH
    if _ENRICH is None:
        spec = importlib.util.spec_from_file_location(
            "phasekit_enrich", ENGINE / "scripts" / "enrich-project.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ENRICH = module
    return _ENRICH


# --- versions, the store, the canonical clone --------------------------------

def parse_tag(tag):
    m = TAG_RE.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def engine_version(engine=ENGINE):
    """The release tag an engine checkout IS, or its describe string, or None.

    A store engine records its tag at install (.engine-version); the canonical
    clone answers with `git describe` (a tag only when it sits exactly on one).
    """
    engine = Path(engine)
    f = engine / VERSION_FILE
    if f.is_file():
        v = f.read_text(encoding="utf-8").strip()
        return v or None
    if (engine / ".git").exists():
        r = subprocess.run(["git", "-C", str(engine), "describe", "--tags", "--always"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip() or None
    return None


def store_dir():
    env = (os.environ.get(STORE_ENV) or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if (ENGINE / ".git").exists():
        return ENGINE / "engines"
    if ENGINE.parent.name == "engines":
        return ENGINE.parent
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "phasekit" / "engines"


def canonical_clone():
    """The phasekit git clone that holds the release tags."""
    env = (os.environ.get("PHASEKIT_HOME") or "").strip()
    candidates = [Path(env).expanduser()] if env else []
    candidates += [ENGINE, store_dir().parent]
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    candidates.append(Path(base) / "phasekit")
    for c in candidates:
        if (c / ".git").exists() and (c / "scripts" / "run-until-done.sh").is_file():
            return c.resolve()
    return None


def auto_fetch_allowed():
    return os.environ.get(NO_AUTO_FETCH_ENV) != "1"


def _git(cwd, *args, check=False):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PinError(f"git {' '.join(args)}: {(r.stderr or '').strip()}")
    return r


def tag_commit(clone, tag):
    r = _git(clone, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def validate_tag(tag):
    v = parse_tag(tag)
    if v is None:
        raise PinError(f"'{tag}' is not a release tag (vMAJOR.MINOR.PATCH)", EXIT_USAGE)
    if v < MIN_ENGINE:
        raise PinError(
            f"{tag} predates the engine outside the repository (v0.19.0): it cannot run a "
            "pinned project. Pin v0.19.0 or later.", EXIT_USAGE)
    return v


def engine_ok(path, tag):
    path = Path(path)
    return (path / "scripts" / "run-until-done.sh").is_file() and \
        (path / VERSION_FILE).is_file() and \
        (path / VERSION_FILE).read_text(encoding="utf-8").strip() == tag


def _make_read_only(root):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                continue
            mode = os.lstat(p).st_mode
            os.chmod(p, stat.S_IMODE(mode) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        mode = os.lstat(dirpath).st_mode
        os.chmod(dirpath, stat.S_IMODE(mode) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_writable(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, stat.S_IMODE(os.lstat(dirpath).st_mode) | stat.S_IWUSR)
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IWUSR)


def _safe_members(tar, dest):
    dest = os.path.realpath(dest)
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise PinError(f"refusing archive member {name!r}")
        if not (m.isfile() or m.isdir() or m.issym()):
            raise PinError(f"refusing archive member {name!r} (type {m.type!r})")
        if m.issym():
            link = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
            if m.linkname.startswith("/") or link.startswith(".."):
                raise PinError(f"refusing symlink {name!r} -> {m.linkname!r} out of the engine")
        target = os.path.realpath(os.path.join(dest, name))
        if not (target == dest or target.startswith(dest + os.sep)):
            raise PinError(f"refusing archive member {name!r} outside the engine")
        yield m


def _sweep_stale_installs(store):
    """A killed install leaves its .install-* directory; one an hour old is no
    install in progress."""
    import time
    for d in Path(store).glob(".install-*"):
        try:
            if time.time() - d.stat().st_mtime > 3600:
                _make_writable(d)
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def install_engine(tag, quiet=False, allow_fetch=None):
    """Install TAG into the store; returns its path. Idempotent.

    The bytes come from `git archive` of the tag's commit in the canonical
    clone: git's object store is content-addressed, so what lands is exactly
    the tree the tag names. A tag the clone lacks is fetched from the clone's
    own `origin` first (auto-fetch, unless PHASEKIT_NO_AUTO_FETCH=1). The
    engine is extracted beside its final path, checked, made read-only and
    renamed into place in one step, so a reader never sees half an engine.
    """
    validate_tag(tag)
    store = store_dir()
    final = store / tag
    if engine_ok(final, tag):
        if not quiet:
            warn(f"phasekit engines: {tag} already installed at {final}")
        return final
    if final.exists():
        raise PinError(f"{final} exists but is not a complete {tag} engine; move it aside "
                       "and run `phasekit engines install " + tag + "` again")
    clone = canonical_clone()
    if clone is None:
        raise PinError("no phasekit clone holds the release tags here (install phasekit: "
                       "install.sh, or set PHASEKIT_HOME to a phasekit clone)",
                       EXIT_NOT_INSTALLED)
    commit = tag_commit(clone, tag)
    if commit is None:
        fetch = auto_fetch_allowed() if allow_fetch is None else allow_fetch
        if not fetch:
            raise PinError(f"tag {tag} is not in {clone} and fetching is off "
                           f"({NO_AUTO_FETCH_ENV}=1); run `git -C {clone} fetch --tags` "
                           f"and `phasekit engines install {tag}`", EXIT_NOT_INSTALLED)
        if not quiet:
            warn(f"phasekit engines: fetching tags into {clone} for {tag}")
        r = _git(clone, "fetch", "--tags", "--quiet", "origin")
        commit = tag_commit(clone, tag)
        if commit is None:
            detail = (r.stderr or "").strip().splitlines()
            raise PinError(f"tag {tag} does not exist in {clone} or its origin"
                           + (f" ({detail[-1]})" if detail else ""), EXIT_NOT_INSTALLED)
    store.mkdir(parents=True, exist_ok=True)
    _sweep_stale_installs(store)
    tmp = Path(tempfile.mkdtemp(prefix=f".install-{tag}-", dir=str(store)))
    os.chmod(tmp, 0o755)  # mkdtemp's 0700 would hide the engine from a container user
    try:
        proc = subprocess.run(["git", "-C", str(clone), "archive", "--format=tar", commit],
                              capture_output=True)
        if proc.returncode != 0:
            raise PinError(f"git archive {tag}: {proc.stderr.decode(errors='replace').strip()}")
        import io
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
            members = list(_safe_members(tar, tmp))
            for m in members:
                tar.extract(m, path=str(tmp), set_attrs=True)
        if not (tmp / "scripts" / "run-until-done.sh").is_file():
            raise PinError(f"{tag} has no scripts/run-until-done.sh — not a phasekit engine")
        (tmp / VERSION_FILE).write_text(tag + "\n", encoding="utf-8")
        (tmp / COMMIT_FILE).write_text(commit + "\n", encoding="utf-8")
        _make_read_only(tmp)
        try:
            os.rename(tmp, final)
        except OSError:
            if engine_ok(final, tag):  # another install won the race
                _make_writable(tmp)
                shutil.rmtree(tmp, ignore_errors=True)
                return final
            raise
    except BaseException:
        if tmp.exists():
            _make_writable(tmp)
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    if not quiet:
        warn(f"phasekit engines: installed {tag} ({commit[:12]}) at {final} (read-only)")
    return final


# --- projects ----------------------------------------------------------------

def project_root(start=None):
    start = Path(start or os.getcwd())
    r = _git(start, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip()).resolve()


def layout(root):
    """pinned | vendored | none — the pin wins (see scripts/phasekit.sh pk_layout)."""
    root = Path(root)
    if (root / PIN_FILE).is_file():
        return "pinned"
    if (root / "scripts" / "run-until-done.sh").is_file():
        return "vendored"
    return "none"


def read_pin(root):
    p = Path(root) / PIN_FILE
    try:
        pin = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PinError(f"cannot read {p}: {exc}") from exc
    if parse_tag(pin) is None:
        raise PinError(f"{p} holds {pin!r}, not a release tag (vMAJOR.MINOR.PATCH)", EXIT_USAGE)
    return pin


def resolve_engine(root, fetch=True):
    """The engine directory that runs this pinned project."""
    lock = (os.environ.get(ENGINE_LOCK_ENV) or "").strip()
    if lock and (Path(lock) / "scripts" / "run-until-done.sh").is_file():
        return Path(lock).resolve()  # a running loop's own engine, whatever the pin says now
    pin = read_pin(root)
    validate_tag(pin)
    if is_store_engine(ENGINE, pin):
        return ENGINE  # this CLI IS that engine (in the container: /opt/phasekit)
    candidate = store_dir() / pin
    if engine_ok(candidate, pin):
        return candidate
    if fetch and auto_fetch_allowed():
        warn(f"phasekit: {root.name} pins {pin}, which is not installed — installing it")
        return install_engine(pin, quiet=False)
    raise PinError(f"{root.name} pins {pin}, which is not installed here: run "
                   f"`phasekit engines install {pin}`", EXIT_NOT_INSTALLED)


def require_project(kind):
    root = project_root()
    if root is None:
        raise PinError("not inside a git repository", EXIT_USAGE)
    lay = layout(root)
    if kind and lay != kind:
        raise PinError(f"{root} is not a {kind} phasekit project (it is {lay})", EXIT_USAGE)
    return root


# --- settings, templates -----------------------------------------------------

# The hooks phasekit itself wires in a vendored project (the plugin provides
# them in a pinned one). A project's OWN hooks under .claude/hooks/ are its
# business: never stripped, never a leftover.
ENGINE_HOOKS = ("deny-dangerous-commands", "require-verdict", "wrapup-nudge", "compact-reanchor")
_ENGINE_HOOK_RE = re.compile(r"(^|[\s/\"'])\.claude/hooks/(" + "|".join(ENGINE_HOOKS) + r")\.sh\b")


def _is_engine_hook(command):
    return isinstance(command, str) and _ENGINE_HOOK_RE.search(command) is not None


def strip_hook_wiring(settings):
    """settings.json minus every hook entry that runs a .claude/hooks/ script
    (phasekit's vendored wiring); other hooks, and every other key, untouched."""
    out = dict(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    new_hooks = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"]
                    if not (isinstance(h, dict) and _is_engine_hook(h.get("command")))]
            if kept:
                g = dict(group)
                g["hooks"] = kept
                kept_groups.append(g)
        if kept_groups:
            new_hooks[event] = kept_groups
    if new_hooks:
        out["hooks"] = new_hooks
    else:
        out.pop("hooks", None)
    return out


def stale_hook_paths(settings):
    found = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        for group in groups if isinstance(groups, list) else []:
            for h in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(h, dict) and _is_engine_hook(h.get("command")):
                    found.append(h["command"])
    return found


def dump_settings(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --- the gate ------------------------------------------------------------------

def run_gate(root, engine):
    """The project's gate under ENGINE (the v0.16 upgrade gate: footprint
    restored and red, host or runner image per PHASEKIT_UPGRADE_VERIFY)."""
    e = enrich()
    try:
        mode = e.upgrade_verify_mode()
    except ValueError as exc:
        raise PinError(str(exc), EXIT_USAGE) from exc
    if mode == "off":
        return {"status": "passed", "where": "off", "label": None, "detail": "gate off", "tail": ""}
    label, _cmd = e.resolve_upgrade_gate(root)
    if label is None:
        return {"status": "passed", "where": mode, "label": None,
                "detail": f"no gate to run ({_cmd})", "tail": ""}
    return e.run_upgrade_gate(root, mode, engine=engine)


def report_gate(result, what):
    status = result.get("status")
    where = result.get("where")
    if status == "passed":
        say(f"  gate: {result.get('label') or 'none'} — {result.get('detail')} ({where})")
        return True
    warn(f"phasekit {what}: the gate was not green ({status}, {where}): {result.get('detail')}")
    tail = result.get("tail") or ""
    if tail:
        warn("  --- gate output (tail) ---")
        for line in tail.splitlines():
            warn("  " + line)
    return False


# --- commits -------------------------------------------------------------------

def _changed_paths(root):
    r = _git(root, "diff-index", "--cached", "--no-renames", "--name-only",
             "--ignore-submodules=none", "HEAD")
    return [p for p in r.stdout.splitlines() if p] if r.returncode == 0 else None


def _has_head(root):
    return _git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0


def commit_exactly(root, paths, subject, body=None):
    """Stage exactly PATHS (deletions included) and commit only them. The
    index must hold nothing else (callers require a clean tree first)."""
    for p in paths:
        r = _git(root, "add", "--all", "--", p)
        if r.returncode != 0 and _git(root, "ls-files", "--", p).stdout.strip():
            raise PinError(f"could not stage {p}: {(r.stderr or '').strip()}")
    if _has_head(root):
        changed = _changed_paths(root)
        if changed is None:
            raise PinError("could not read the index")
        stray = sorted(set(changed) - set(_expand(root, paths)))
        if stray:
            raise PinError("the index holds changes this command did not make: " + ", ".join(stray[:8]))
        if not changed:
            return None
    args = ["commit", "-q", "-m", subject]
    if body:
        args += ["-m", body]
    r = _git(root, *args)
    if r.returncode != 0:
        raise PinError("commit failed: " + ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1])
    return _git(root, "rev-parse", "--short=12", "HEAD").stdout.strip()


def _expand(root, paths):
    """Every file path under PATHS that git knows (tracked or now staged)."""
    out = set()
    for p in paths:
        out.add(p.rstrip("/"))
        r = _git(root, "ls-files", "--cached", "--", p)
        out.update(x for x in r.stdout.splitlines() if x)
        r = _git(root, "diff-index", "--cached", "--no-renames", "--name-only", "HEAD", "--", p) \
            if _has_head(root) else None
        if r is not None:
            out.update(x for x in r.stdout.splitlines() if x)
    return out


def tree_dirty(root):
    r = _git(root, "status", "--porcelain", "--untracked-files=all")
    if r.returncode != 0:
        raise PinError("git status failed: " + (r.stderr or "").strip())
    return [line for line in r.stdout.splitlines() if line]


# --- verbs -------------------------------------------------------------------

def cmd_resolve(_args):
    root = require_project("pinned")
    say(str(resolve_engine(root)))
    return EXIT_OK


def cmd_engines(args):
    if args.action == "install":
        if not args.tag:
            raise PinError("usage: phasekit engines install TAG", EXIT_USAGE)
        install_engine(args.tag, allow_fetch=True)
        return EXIT_OK
    if args.action == "path":
        if not args.tag:
            raise PinError("usage: phasekit engines path TAG", EXIT_USAGE)
        validate_tag(args.tag)
        p = store_dir() / args.tag
        if not engine_ok(p, args.tag):
            raise PinError(f"{args.tag} is not installed: run `phasekit engines install {args.tag}`",
                           EXIT_NOT_INSTALLED)
        say(str(p))
        return EXIT_OK
    store = store_dir()
    rows = []
    if store.is_dir():
        for d in sorted(store.iterdir(), key=lambda p: parse_tag(p.name) or (0, 0, 0)):
            if parse_tag(d.name) and engine_ok(d, d.name):
                commit = (d / COMMIT_FILE).read_text().strip()[:12] if (d / COMMIT_FILE).is_file() else "?"
                rows.append(f"{d.name}\t{commit}\t{d}")
    say(f"engine store: {store}")
    for row in rows:
        say(row)
    if not rows:
        say("(no engines installed)")
    return EXIT_OK


def own_release_tag(explicit=None):
    if explicit:
        validate_tag(explicit)
        return explicit
    v = engine_version(ENGINE)
    if v and parse_tag(v):
        validate_tag(v)
        return v
    raise PinError(f"this phasekit ({v or 'unknown version'}) is not a release; pass --pin vX.Y.Z "
                   "(an installed or fetchable release tag)", EXIT_USAGE)


def render_pinned_files(root, profile, tag):
    """Write the project-owned files a pinned project starts with (never
    overwriting one that exists). Returns the written paths."""
    e = enrich()
    manifest = e.load_manifest()
    resolved = e.resolve_profile(manifest.get("profiles", {}), profile)
    specs = [s for s in e.enumerate_install_targets(manifest, resolved)
             if s.get("ownership") != "scaffold"]
    written = []
    for spec in specs:
        rel = spec["path"]
        dest = root / rel
        if dest.exists():
            say(f"  keep (exists): {rel}")
            continue
        src = e.REPO_ROOT / (spec.get("rendered_from") or rel)
        if not src.exists():
            warn(f"  warning: template missing: {src}")
            continue
        if rel == ".claude/settings.json":
            data = json.loads(e.render_template_text(src, root.name, layout="pinned"))
            text = dump_settings(strip_hook_wiring(data))
        elif spec.get("rendered_from"):
            text = e.render_template_text(src, root.name, layout="pinned")
        else:
            text = src.read_text(encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        if rel.endswith(".sh"):
            os.chmod(dest, 0o755)
        written.append(rel)
        say(f"  wrote: {rel}")
    (root / PIN_FILE).write_text(tag + "\n", encoding="utf-8")
    written.append(PIN_FILE)
    say(f"  wrote: {PIN_FILE} ({tag})")
    return written


def cmd_init(args):
    cwd = Path(os.getcwd()).resolve()
    root = project_root(cwd)
    if root is None:
        r = subprocess.run(["git", "init", "-q", str(cwd)], capture_output=True, text=True)
        if r.returncode != 0:
            raise PinError("git init failed: " + r.stderr.strip())
        root = cwd
        say(f"phasekit init: initialised a git repository at {root}")
    lay = layout(root)
    if lay == "vendored" or (root / ".scaffold" / "manifest.json").exists():
        raise PinError(f"{root} is a vendored phasekit project: run `phasekit migrate` instead",
                       EXIT_USAGE)
    if lay == "pinned":
        say(f"phasekit init: {root.name} is already pinned to {read_pin(root)} — nothing to do")
        if not args.no_plugin:
            plugin_install(quiet=True)
        return EXIT_OK
    tag = own_release_tag(args.pin)
    engine = resolve_engine_for_tag(tag)
    if _has_head(root):
        staged = _changed_paths(root)
    else:
        staged = [p for p in _git(root, "diff", "--cached", "--name-only").stdout.splitlines() if p]
    if staged:
        raise PinError("the index holds staged changes; commit or unstage them first", EXIT_USAGE)
    say(f"phasekit init: {root.name} on phasekit {tag} (profile {args.profile})")
    written = render_pinned_files(root, args.profile, tag)
    (root / "artifacts").mkdir(exist_ok=True)
    sha = None
    if not args.no_commit:
        sha = commit_exactly(root, written, f"{INIT_SUBJECT} {tag} ({args.profile})")
    say(f"phasekit init: done{(' — commit ' + sha) if sha else ''}. The engine is read-only at "
        f"{engine}; this repository carries only its pin.")
    if not args.no_plugin:
        plugin_install(quiet=False)
    say("Next: fill docs/SPEC.md and docs/PHASES.md, then `phasekit loop` (host) or "
        "`phasekit run` (container). Upgrade later with `phasekit upgrade`.")
    return EXIT_OK


def is_store_engine(path, tag):
    """An immutable engine for TAG (a store install, or one mounted from it).
    The canonical clone is never one: it is writable and moves with
    self-update, so a project never runs from it — even on its own tag."""
    return engine_ok(path, tag)


def resolve_engine_for_tag(tag):
    if is_store_engine(ENGINE, tag):
        return ENGINE
    p = store_dir() / tag
    if engine_ok(p, tag):
        return p
    return install_engine(tag)


def load_manifest_entries(root):
    path = root / ".scaffold" / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PinError(f"cannot read {path}: {exc}") from exc
    files = data.get("files") or []
    if not isinstance(files, list):
        raise PinError(f"{path}: unexpected manifest shape")
    return data, files


def migrate_plan(root):
    """(scaffold paths to delete, locally changed ones, settings before/after)."""
    e = enrich()
    _data, files = load_manifest_entries(root)
    delete, changed = [], []
    for entry in files:
        if entry.get("ownership") != "scaffold":
            continue
        rel = entry["path"]
        delete.append(rel)
        p = root / rel
        if not p.exists():
            continue
        if entry.get("local") == "kept":
            changed.append((rel, "kept locally"))
            continue
        current = e.sha256_normalized(p) if entry.get("text", True) else e.sha256_strict(p)
        if entry.get("sha256") and current != entry["sha256"]:
            changed.append((rel, "edited since phasekit wrote it"))
    settings_path = root / ".claude" / "settings.json"
    before = after = None
    if settings_path.is_file():
        before = json.loads(settings_path.read_text(encoding="utf-8"))
        after = strip_hook_wiring(before)
    return sorted(set(delete)), changed, before, after


def cmd_migrate(args):
    refuse_under_loop("migrate")
    root = require_project(None)
    lay = layout(root)
    has_manifest = (root / ".scaffold" / "manifest.json").is_file()
    if lay == "pinned" and not has_manifest:
        say(f"phasekit migrate: {root.name} is already pinned to {read_pin(root)} — nothing to do")
        return EXIT_OK
    if not has_manifest:
        raise PinError(f"{root} has no .scaffold/manifest.json: not a vendored phasekit project "
                       "(a new project: `phasekit init`)", EXIT_USAGE)
    tag = own_release_tag(args.pin)
    if not _has_head(root):
        raise PinError("the repository has no commits yet", EXIT_USAGE)
    dirty = tree_dirty(root)
    if dirty:
        raise PinError("the working tree is not clean (commit or stash first): "
                       + "; ".join(dirty[:6]), EXIT_USAGE)
    delete, changed, before, after = migrate_plan(root)
    if changed and not args.discard_local:
        lines = "\n".join(f"  {p}: {why}" for p, why in changed)
        raise PinError("these engine files carry local changes the migration would discard "
                       "(the engine's copy replaces them):\n" + lines
                       + "\nMove what you need into project-owned files (docs/project/<NAME>.md), "
                       "then re-run, or pass --discard-local.", EXIT_USAGE)
    tracked = set(_git(root, "ls-files").stdout.splitlines())
    to_remove = [p for p in delete if p in tracked or (root / p).exists()]
    say(f"phasekit migrate: {root.name} -> pinned {tag}")
    say(f"  delete {len(to_remove)} engine file(s) the manifest names (scaffold class) and .scaffold/")
    settings_changed = before is not None and after != before
    if settings_changed:
        say("  strip the engine hook wiring from .claude/settings.json (permissions unchanged)")
    say(f"  write {PIN_FILE} = {tag}")
    if args.dry_run:
        for p in to_remove:
            say(f"    - {p}")
        return EXIT_OK
    engine = resolve_engine_for_tag(tag)

    # Snapshot for the red path: every byte this command touches.
    snap = {}
    for rel in to_remove + [".claude/settings.json"]:
        p = root / rel
        if p.is_file():
            snap[rel] = (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    scaffold_dir = root / ".scaffold"
    scaffold_snap = {}
    if scaffold_dir.is_dir():
        for p in scaffold_dir.rglob("*"):
            if p.is_file():
                scaffold_snap[str(p.relative_to(root))] = (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))

    def restore():
        _git(root, "reset", "-q", "--", ".")
        for rel, (data, mode) in {**snap, **scaffold_snap}.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.chmod(p, mode)
        try:
            (root / PIN_FILE).unlink()
        except FileNotFoundError:
            pass

    try:
        for rel in to_remove:
            p = root / rel
            if p.is_file() or p.is_symlink():
                p.unlink()
                e_prune_empty_parents(root, p.parent)
        if scaffold_dir.exists():
            shutil.rmtree(scaffold_dir)
        if settings_changed:
            (root / ".claude" / "settings.json").write_text(dump_settings(after), encoding="utf-8")
        (root / PIN_FILE).write_text(tag + "\n", encoding="utf-8")
        result = run_gate(root, engine)
        if not report_gate(result, "migrate"):
            restore()
            warn("phasekit migrate: nothing changed (the tree is as it was).")
            return EXIT_GATE_RED
        paths = sorted(set(to_remove) | {".scaffold", PIN_FILE}
                       | ({".claude/settings.json"} if settings_changed else set()))
        # .scaffold/ may hold tracked files the manifest does not list (the
        # manifest itself, a once-tracked lock): stage the directory whole.
        sha = commit_exactly(root, paths, f"{MIGRATE_SUBJECT} ({tag})",
                             body=f"Removed {len(to_remove)} vendored engine file(s) and .scaffold/; "
                                  f"phasekit {tag} runs this project from outside the tree "
                                  f"({PIN_FILE}).")
    except BaseException:
        restore()
        raise
    say(f"phasekit migrate: done — commit {sha}. Push when ready (nothing was pushed).")
    return EXIT_OK


def e_prune_empty_parents(root, d):
    root = Path(root).resolve()
    d = Path(d)
    while d.resolve() != root and d.is_dir():
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def plugin_status():
    """(installed?, detail). None when the claude CLI is absent."""
    claude = shutil.which("claude")
    if claude is None:
        return None, "the claude CLI is not on PATH"
    try:
        r = subprocess.run([claude, "plugin", "list", "--json"], capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"`claude plugin list` failed ({type(exc).__name__})"
    if r.returncode != 0:
        return False, "`claude plugin list` failed: " + (r.stderr or "").strip()[:200]
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return False, "`claude plugin list --json` printed no JSON"
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("id", "")).split("@")[0] == PLUGIN_NAME:
            if row.get("enabled") is False:
                return False, f"{row.get('id')} is installed but disabled (`claude plugin enable {row.get('id')}`)"
            return True, str(row.get("id"))
    return False, "not installed"


def marketplace_root():
    """The directory that holds .claude-plugin/marketplace.json: the canonical
    clone when there is one (it moves with self-update), else this engine."""
    clone = canonical_clone()
    for c in (clone, ENGINE):
        if c is not None and (c / ".claude-plugin" / "marketplace.json").is_file():
            return c
    return None


def plugin_install(quiet=False):
    """Install the plugin for interactive Claude Code, once per machine
    (user scope). Idempotent: an installed plugin is left as it is."""
    if os.environ.get("PHASEKIT_NO_PLUGIN") == "1":
        return EXIT_OK
    installed, detail = plugin_status()
    if installed is None:
        if not quiet:
            say(f"phasekit plugin: skipped — {detail}; install Claude Code, then "
                "`phasekit plugin install`")
        return EXIT_OK
    if installed:
        if not quiet:
            say(f"phasekit plugin: installed ({detail})")
        return EXIT_OK
    src = marketplace_root()
    if src is None:
        warn("phasekit plugin: no .claude-plugin/marketplace.json found to install from")
        return EXIT_ERROR
    claude = shutil.which("claude")
    steps = [[claude, "plugin", "marketplace", "add", str(src)],
             [claude, "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"]]
    for argv in steps:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            warn(f"phasekit plugin: `{' '.join(argv[1:])}` failed ({type(exc).__name__})")
            return EXIT_ERROR
        if r.returncode != 0:
            warn(f"phasekit plugin: `{' '.join(argv[1:])}` failed: "
                 + ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1])
            return EXIT_ERROR
    installed, detail = plugin_status()
    if installed:
        say(f"phasekit plugin: installed {detail} (from {src}) — interactive Claude Code "
            "sessions in a pinned project now get phasekit's guard and agents")
        return EXIT_OK
    warn(f"phasekit plugin: install reported success but the plugin is {detail}")
    return EXIT_ERROR


def cmd_plugin(args):
    if args.action == "install":
        return plugin_install(quiet=False)
    installed, detail = plugin_status()
    say(f"phasekit plugin: {'installed' if installed else 'NOT installed'} — {detail}")
    return EXIT_OK if installed else EXIT_LEFTOVER


def cmd_check(args):
    """A pinned project's health. 0 clean; 3 a leftover (a tracked engine
    path, `.scaffold/`, stale hook wiring); 6 the pinned engine is not
    installed (never fetched here: check reports, it does not change). The
    plugin's absence is a loud warning, not an exit code (a CI box runs no
    interactive sessions)."""
    root = require_project("pinned")
    pin = read_pin(root)
    validate_tag(pin)
    rc = EXIT_OK
    lock = (os.environ.get(ENGINE_LOCK_ENV) or "").strip()
    if is_store_engine(ENGINE, pin):
        engine = ENGINE
    elif lock and is_store_engine(lock, pin):
        engine = Path(lock)
    else:
        engine = store_dir() / pin
        if not engine_ok(engine, pin):
            warn(f"phasekit check: {root.name} pins {pin}, which is NOT installed here — run "
                 f"`phasekit engines install {pin}` (a pinned verb also fetches it on first use)")
            return EXIT_NOT_INSTALLED
    say(f"phasekit check: {root.name} pins {pin}; engine {engine}")
    # Leftovers: anything the engine's manifest calls scaffold, tracked here.
    e = enrich()
    try:
        manifest = e.load_manifest()
        scaffold_paths = {s["path"] for s in e.enumerate_install_targets(
            manifest, e.resolve_profile(manifest.get("profiles", {}), "default"))
            if s.get("ownership") == "scaffold"}
        for sec in ("agents", "hooks", "scripts", "docs"):
            for entry in (manifest.get(sec) or {}).values():
                p = entry.get("source") or entry.get("path")
                if p and entry.get("ownership") == "scaffold":
                    scaffold_paths.add(p)
    except Exception as exc:  # the check must report, not crash
        warn(f"phasekit check: could not read the engine manifest ({exc})")
        scaffold_paths = set()
    tracked = set(_git(root, "ls-files").stdout.splitlines())
    leftovers = sorted(p for p in tracked if p in scaffold_paths or p.startswith(".scaffold/"))
    if leftovers:
        warn(f"LEFTOVER: {len(leftovers)} engine path(s) are tracked in this pinned project "
             "(the engine provides them; delete them): " + ", ".join(leftovers[:10])
             + (" …" if len(leftovers) > 10 else ""))
        rc = EXIT_LEFTOVER
    settings = root / ".claude" / "settings.json"
    if settings.is_file():
        try:
            stale = stale_hook_paths(json.loads(settings.read_text(encoding="utf-8")))
        except ValueError:
            stale = []
            warn("phasekit check: .claude/settings.json is not valid JSON")
        if stale:
            warn("LEFTOVER: .claude/settings.json still wires vendored hook paths (the plugin "
                 "provides the hooks): " + ", ".join(stale))
            rc = EXIT_LEFTOVER
    installed, detail = plugin_status()
    if installed is False:
        warn("WARNING: the phasekit plugin is not installed for interactive Claude Code "
             f"({detail}) — an interactive session in this project runs WITHOUT phasekit's "
             "command guard. Run `phasekit plugin install` (the loop is unaffected: it passes "
             "the engine's plugin itself).")
    elif installed is None:
        warn(f"note: {detail}; the phasekit plugin cannot be checked")
    if rc == EXIT_OK:
        say("phasekit check: clean")
    return rc


def refuse_under_loop(verb):
    """A pin moves only by an operator: never from inside a loop session (the
    engine that started an iteration finishes it; the loop never commits a
    session's pin change, and `phasekit upgrade` would commit one itself)."""
    if os.environ.get("PHASEKIT_ITER_MARKER"):
        raise PinError(f"`phasekit {verb}` is an operator's command; it refuses to run inside a "
                       "phasekit loop session", EXIT_USAGE)


def cmd_upgrade(args):
    """A pin bump: install the target engine, write the pin, run the project's
    gate under the NEW engine, commit one line. Red: the pin is put back and
    nothing changes (exit 4)."""
    refuse_under_loop("upgrade")
    root = require_project("pinned")
    old = read_pin(root)
    if args.to:
        new = args.to
        validate_tag(new)
    else:
        new = latest_tag()
    if parse_tag(new) <= parse_tag(old) and not args.to:
        say(f"phasekit upgrade: {root.name} is on {old}; the newest release here is {new} — nothing to do")
        return EXIT_OK
    if new == old:
        say(f"phasekit upgrade: {root.name} already pins {new} — nothing to do")
        return EXIT_OK
    staged = _changed_paths(root)
    if staged:
        raise PinError("the index holds staged changes; commit or unstage them first", EXIT_USAGE)
    pin_status = _git(root, "status", "--porcelain", "--", PIN_FILE).stdout.strip()
    if pin_status:
        raise PinError(f"{PIN_FILE} has uncommitted changes", EXIT_USAGE)
    engine = resolve_engine_for_tag(new)
    say(f"phasekit upgrade: {root.name} {old} -> {new} (engine {engine})")
    old_bytes = (root / PIN_FILE).read_bytes()
    (root / PIN_FILE).write_text(new + "\n", encoding="utf-8")
    try:
        result = run_gate(root, engine)
        if not report_gate(result, "upgrade"):
            (root / PIN_FILE).write_bytes(old_bytes)
            warn(f"phasekit upgrade: the pin stays {old}; nothing changed.")
            return EXIT_GATE_RED
        sha = commit_exactly(root, [PIN_FILE], f"{PIN_SUBJECT} {old} -> {new}")
    except BaseException:
        (root / PIN_FILE).write_bytes(old_bytes)
        raise
    say(f"phasekit upgrade: done — commit {sha}" + ("" if not args.push else ""))
    if args.push:
        r = _git(root, "push")
        say("  push: ok" if r.returncode == 0 else "  note: push failed; the commit is local")
    return EXIT_OK


def latest_tag():
    clone = canonical_clone()
    tags = []
    if clone is not None:
        tags = [t for t in _git(clone, "tag", "-l", "v*").stdout.split() if parse_tag(t)]
    store = store_dir()
    if store.is_dir():
        tags += [d.name for d in store.iterdir() if parse_tag(d.name) and engine_ok(d, d.name)]
    own = engine_version(ENGINE)
    if own and parse_tag(own):
        tags.append(own)
    tags = [t for t in set(tags) if parse_tag(t) >= MIN_ENGINE]
    if not tags:
        raise PinError("no release v0.19.0 or later is known here (phasekit self-update)", EXIT_ERROR)
    return max(tags, key=parse_tag)


def cmd_docs(_args):
    root = project_root()
    if root is not None and layout(root) == "pinned":
        say(str(resolve_engine(root) / "docs"))
    else:
        say(str(ENGINE / "docs"))
    return EXIT_OK


def main(argv=None):
    p = argparse.ArgumentParser(prog="phasekit", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("resolve")
    pe = sub.add_parser("engines")
    pe.add_argument("action", nargs="?", default="list", choices=["install", "list", "path"])
    pe.add_argument("tag", nargs="?")
    pi = sub.add_parser("init")
    pi.add_argument("profile", nargs="?", default="default")
    pi.add_argument("--pin", help="the release tag to pin (default: this phasekit's own)")
    pi.add_argument("--no-commit", action="store_true")
    pi.add_argument("--no-plugin", action="store_true")
    pm = sub.add_parser("migrate")
    pm.add_argument("--pin")
    pm.add_argument("--dry-run", action="store_true")
    pm.add_argument("--discard-local", action="store_true")
    sub.add_parser("check")
    pu = sub.add_parser("upgrade")
    pu.add_argument("--to")
    pu.add_argument("--push", action="store_true")
    pu.add_argument("--yes", action="store_true", help="accepted for symmetry with the vendored upgrade")
    pp = sub.add_parser("plugin")
    pp.add_argument("action", nargs="?", default="status", choices=["install", "status"])
    sub.add_parser("docs")
    args = p.parse_args(argv)
    handlers = {"resolve": cmd_resolve, "engines": cmd_engines, "init": cmd_init,
                "migrate": cmd_migrate, "check": cmd_check, "upgrade": cmd_upgrade,
                "plugin": cmd_plugin, "docs": cmd_docs}
    try:
        return handlers[args.verb](args)
    except PinError as exc:
        warn(f"phasekit {args.verb}: {exc}")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
