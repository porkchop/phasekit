#!/usr/bin/env python3
"""The one composer of a `docker run` of the runner image (v0.22.0, O5).

Every Claude container in a phasekit fleet is one of four ROLES, and this file
is where a role's shared parts are written once: the runner user and the
environment that goes with it, the Claude login (one file of the credentials
volume, never the whole volume: v0.22.1), the image, mount
syntax, secrets crossed by name only, and the bounds (`--memory`, a
`--memory-swap` equal to it, `--pids-limit`) on EVERY role, judged by one
validator. A caller supplies only what genuinely varies (its mounts, its
variables, its command) and executes the argv itself, so its own timeouts,
stream capture and `docker kill` by name are untouched.

    phasekit launch-argv --role ROLE --name NAME [options] [--json] -- CMD...
    phasekit-launch.py bounds --role ROLE      # "MEMORY PIDS" on one line

Roles (defaults overridable by PHASEKIT_<ROLE>_MEMORY / _PIDS_LIMIT):

    session  the build loop (container-setup.sh; bounds only) 10g  8192
    runner   gate, landing, upgrade, an unblock predicate    8g  4096
    oneshot  one `claude -p` turn, or a deploy-QA script     2g   512
    repair   a session that edits a project tree             4g   512

A bad bound value is the role's default with a stderr line, never unbounded:
docker truncates a size under one byte to 0 (no bound) and refuses one under
6 MiB, and reads `--pids-limit 0` as unlimited.

Stdlib only, and importable: scripts/enrich-project.py loads it for the
runner's bounds, so there is one validator.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

IMAGE_ENV = "PHASEKIT_RUNNER_IMAGE"
IMAGE_DEFAULT = "scaffold-runner"
CREDENTIALS_VOLUME_ENV = "CLAUDE_VOLUME"
CREDENTIALS_VOLUME_DEFAULT = "scaffold-claude-config"
HOME = "/home/node"
CLAUDE_CONFIG_DIR = f"{HOME}/.claude"
# The users that own the image's /home/node: root, and the image's `node` user.
HOME_USERS = ("0", "root", "1000", "node")
THROWAWAY_HOME = "/tmp/phasekit-upgrade-home"
# The only files a container takes from the shared credentials volume
# (v0.22.1): the login, and the fleet's settings (its model and environment).
# Everything else Claude keeps there (transcripts under projects/, history,
# sessions, .claude.json) would be shared by every container that mounts the
# whole volume, so none does: CLAUDE_CONFIG_DIR is a private tmpfs per
# container and these two files are mounted into it one by one.
# The login file is writable by default on every role: refresh tokens rotate
# (measured 2026-10-07), so a container that refreshes into a read-only login
# strands the new token and logs the whole fleet out (container-setup.sh says
# the same of its sessions). Writing ONE file shares no transcripts.
CREDENTIALS_FILE = ".credentials.json"
SETTINGS_FILE = "settings.json"
# The session's argv is composed by container-setup.sh (its per-project config
# root, resume by id, the login-outlives-session guard); here it has bounds only.
BOUNDS_ONLY_ROLES = ("session",)
KILLED_RC = 137  # 128 + SIGKILL
DOCKER_FAILED_RCS = {125: "docker run itself failed (no such image, daemon, bad flag)",
                     126: "the container's command could not be invoked",
                     127: "the container's command was not found, or a file a mount names is "
                          "missing (docker reports a missing volume-subpath as 127)"}

# role: (memory default, pids default, credentials default, memory env, pids env)
ROLES = {
    "session": ("10g", 8192, "rw", "PHASEKIT_SESSION_MEMORY", "PHASEKIT_SESSION_PIDS_LIMIT"),
    "runner": ("8g", 4096, "none", "PHASEKIT_RUNNER_MEMORY", "PHASEKIT_RUNNER_PIDS_LIMIT"),
    "oneshot": ("2g", 512, "rw", "PHASEKIT_ONESHOT_MEMORY", "PHASEKIT_ONESHOT_PIDS_LIMIT"),
    "repair": ("4g", 512, "rw", "PHASEKIT_REPAIR_MEMORY", "PHASEKIT_REPAIR_PIDS_LIMIT"),
}

_MEMORY_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([bkmgtp]?)b?", re.IGNORECASE | re.ASCII)
_MEMORY_UNITS = {"": 1, "b": 1, "k": 2**10, "m": 2**20, "g": 2**30, "t": 2**40, "p": 2**50}
MEMORY_MIN_BYTES = 6 * 2**20  # docker's own minimum
MEMORY_MAX_BYTES = 2**62
_PIDS_RE = re.compile(r"0*([1-9][0-9]{0,8})", re.ASCII)  # 1..999999999, as a string
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# Names this module sets for the container; a caller can never override them.
OWNED_NAMES = ("HOME", "PATH", "CLAUDE_CONFIG_DIR", "IS_SANDBOX")


class LaunchError(ValueError):
    """A request this module refuses; the message names why."""


def memory_env(role):
    return _role(role)[3]


def pids_env(role):
    return _role(role)[4]


def memory_ok(value):
    """True when docker reads VALUE as a bound of at least 6 MiB (below 2^62)."""
    m = _MEMORY_RE.fullmatch(value)
    if not m:
        return False
    try:
        size = int(float(m.group(1)) * _MEMORY_UNITS[m.group(2).lower()])
    except OverflowError:
        return False
    return MEMORY_MIN_BYTES <= size < MEMORY_MAX_BYTES


def pids_value(value):
    """VALUE as a decimal pid limit 1..999999999, or None."""
    m = _PIDS_RE.fullmatch(value)
    return m.group(1) if m else None


def bounds(role, *, quiet=False, environ=None):
    """(memory, pids) for ROLE from its environment, else its defaults."""
    env = os.environ if environ is None else environ
    mem_default, pids_default = _role(role)[:2]
    mem = (env.get(memory_env(role)) or "").strip()
    pids = (env.get(pids_env(role)) or "").strip()
    if not mem:
        mem = mem_default
    elif not memory_ok(mem):
        if not quiet:
            print(f"phasekit: {memory_env(role)}='{mem}' is not a docker size from 6m "
                  f"(e.g. {mem_default}); using {mem_default}", file=sys.stderr)
        mem = mem_default
    if not pids:
        pids = str(pids_default)
    else:
        good = pids_value(pids)
        if good is None and not quiet:
            print(f"phasekit: {pids_env(role)}='{pids}' is not an integer from 1 to "
                  f"999999999 (0 would mean unlimited to docker); using {pids_default}",
                  file=sys.stderr)
        pids = good or str(pids_default)
    return mem, int(pids)


def _role(role):
    try:
        return ROLES[role]
    except KeyError:
        raise LaunchError(f"unknown role '{role}' (one of: {', '.join(ROLES)})") from None


def runner_user(environ=None):
    """The container user: PHASEKIT_CONTAINER_USER when set; else 0:0 under
    rootless docker (container root IS the host user); else the host uid."""
    env = os.environ if environ is None else environ
    explicit = (env.get("PHASEKIT_CONTAINER_USER") or "").strip()
    if explicit:
        return "0:0" if explicit == "root" else explicit
    rootless = env.get("PHASEKIT_ROOTLESS_DOCKER") == "1"
    if not rootless:
        try:
            r = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"],
                               capture_output=True, text=True, timeout=60)
            rootless = "rootless" in (r.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            rootless = False
    return "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"


def is_root(user):
    """Whether USER (a docker --user spec) is container root."""
    return user.split(":", 1)[0] in ("0", "root")


def home_for(user):
    """HOME for USER: the image's /home/node for the users that own it, else a
    throwaway (the image's /home/node is not theirs)."""
    return HOME if user.split(":", 1)[0] in HOME_USERS else THROWAWAY_HOME


def mount_arg(src, dst, *, readonly=False, kind="bind", subpath=None):
    """A --mount value, CSV-quoted so a path with ':' or ',' is still one field.
    SUBPATH (a volume only) mounts that one entry of the volume, not all of it."""
    if not re.fullmatch(r"/[A-Za-z0-9_./@+-]*", dst, re.ASCII):
        raise LaunchError(f"mount destination '{dst}' is not a plain absolute container path "
                          "(no ',', quotes, spaces or control characters)")
    for part in (str(src), dst):
        if "\n" in part or "\0" in part:
            raise LaunchError("a mount path may not contain a newline or NUL")
    quoted = str(src).replace('"', '""')
    sub = ""
    if subpath is not None:
        if kind != "volume" or not re.fullmatch(r"[A-Za-z0-9_.-]+", subpath, re.ASCII) \
                or subpath in (".", ".."):
            raise LaunchError(f"volume subpath '{subpath}' is not one plain entry name")
        sub = f",volume-subpath={subpath}"
    return f'type={kind},"src={quoted}",dst={dst}{sub}' + (",readonly" if readonly else "")


def volume_entries(volume):
    """The names at the top of VOLUME, or None when they cannot be read (no
    docker, no such volume, a mountpoint this user cannot list)."""
    try:
        r = subprocess.run(["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume],
                           capture_output=True, text=True, timeout=60)
        return set(os.listdir(r.stdout.strip())) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def credentials_mounts(volume, credentials, entries=None):
    """The --mount values that give a container the login without the shared
    volume: a private tmpfs as CLAUDE_CONFIG_DIR (mode 1777, so any --user can
    write it), then the volume's login file (read-only only when the call asks
    `ro`) and its settings (always read-only). ENTRIES is what the volume
    holds: a file it does not hold is not mounted (an API-key login has no
    login file; a fresh volume may have no settings), and when ENTRIES is None
    (unreadable) both are mounted, so a missing one fails the run (docker's
    127) rather than widening it. Needs docker 26+ (volume-subpath)."""
    mounts = [f"type=tmpfs,dst={CLAUDE_CONFIG_DIR},tmpfs-mode=1777"]
    for name, readonly in ((CREDENTIALS_FILE, credentials == "ro"), (SETTINGS_FILE, True)):
        if entries is None or name in entries:
            mounts.append(mount_arg(volume, f"{CLAUDE_CONFIG_DIR}/{name}", kind="volume",
                                    subpath=name, readonly=readonly))
    return mounts


def _parse_mount(spec):
    """SRC:DST or SRC:DST:ro, split at the LAST colons so SRC may hold one."""
    readonly = spec.endswith(":ro")
    body = spec[:-3] if readonly else spec
    src, sep, dst = body.rpartition(":")
    if not sep or not src or not dst:
        raise LaunchError(f"--mount '{spec}' is not SRC:DST or SRC:DST:ro")
    return src, dst, readonly


def compose(role, name, command, *, mounts=(), volumes=(), credentials=None, secrets=(),
            sets=(), add_hosts=(), workdir=None, entrypoint=None, user=None, image=None,
            interactive=False, environ=None):
    """The whole `docker run` argv for ROLE. Pure but for the environment
    read (bounds, image, secrets present, the rootless probe when USER is None)."""
    env = os.environ if environ is None else environ
    cred_default = _role(role)[2]
    if role in BOUNDS_ONLY_ROLES:
        raise LaunchError(f"the {role} container is composed by container-setup.sh; "
                          f"this module gives it bounds only (`bounds --role {role}`)")
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name, re.ASCII):
        raise LaunchError(f"container name '{name}' is not a docker name")
    if not command:
        raise LaunchError("no command after --")
    credentials = cred_default if credentials is None else credentials
    if credentials not in ("none", "ro", "rw"):
        raise LaunchError(f"--credentials '{credentials}' is not none, ro or rw")
    mem, pids = bounds(role, environ=env)
    user = user or runner_user(env)
    home = home_for(user)
    argv = ["docker", "run", "--rm", *(["-i"] if interactive else []), "--name", name,
            "--user", user,
            "--memory", mem, "--memory-swap", mem, "--pids-limit", str(pids)]
    for host in add_hosts:
        argv += ["--add-host", host]
    argv += ["-e", f"HOME={home}", "-e", f"CLAUDE_CONFIG_DIR={CLAUDE_CONFIG_DIR}"]
    if is_root(user):
        # Claude Code refuses bypassPermissions as uid 0 unless the environment
        # declares itself sandboxed; under rootless docker it is.
        argv += ["-e", "IS_SANDBOX=1"]
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep or not _NAME_RE.fullmatch(key):
            raise LaunchError(f"--set '{key}' is not NAME=VALUE")
        if key in OWNED_NAMES:
            raise LaunchError(f"--set {key}: the launcher owns that name")
        argv += ["-e", f"{key}={value}"]
    for secret in secrets:
        if not _NAME_RE.fullmatch(secret):
            raise LaunchError(f"--env '{secret[:40]}' is not a variable name (names only)")
        if secret in OWNED_NAMES:
            raise LaunchError(f"--env {secret}: the launcher owns that name")
        if env.get(secret):
            argv += ["-e", secret]  # by name: docker reads the value, argv never holds it
    volume = (env.get(CREDENTIALS_VOLUME_ENV) or "").strip() or CREDENTIALS_VOLUME_DEFAULT
    if credentials != "none":
        for value in credentials_mounts(volume, credentials, volume_entries(volume)):
            argv += ["--mount", value]
    for spec in mounts:
        src, dst, readonly = _parse_mount(spec)
        argv += ["--mount", mount_arg(src, dst, readonly=readonly)]
    for spec in volumes:
        src, dst, readonly = _parse_mount(spec)
        if src == volume:
            raise LaunchError(f"--volume {src}: the credentials volume is never mounted whole "
                              "(it holds every container's transcripts); use --credentials")
        argv += ["--mount", mount_arg(src, dst, readonly=readonly, kind="volume")]
    if workdir:
        argv += ["-w", workdir]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    image = image or (env.get(IMAGE_ENV) or "").strip() or IMAGE_DEFAULT
    return [*argv, image, *command]


def exit_reason(role, rc, environ=None):
    """A sentence naming what a container's exit RC means, or None for an
    ordinary exit. The pid bound never SIGKILLs (a fork past it fails with
    EAGAIN), so a 137 never names it. In a `runner` PID 1 is the gate itself,
    which a cgroup OOM does kill, so its memory bound is named first; in every
    other role PID 1 is a shell the OOM killer spares in favour of the largest
    task, so an outside `docker kill` (a supervisor's timeout) is named first."""
    if rc == KILLED_RC:
        mem, _ = bounds(role, quiet=True, environ=environ)
        if role == "runner":
            return (f"SIGKILLed (exit {KILLED_RC}); inside the runner that is most often its "
                    f"memory bound ({mem}, {memory_env(role)}), else the host's OOM killer or an "
                    "outside docker kill")
        return (f"the {role} container was SIGKILLed (exit {KILLED_RC}): an outside docker kill "
                f"(e.g. a supervisor's timeout), else its memory bound ({mem}, "
                f"{memory_env(role)}) or the host's OOM killer")
    if rc in DOCKER_FAILED_RCS:
        return f"exit {rc}: {DOCKER_FAILED_RCS[rc]}"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="phasekit launch-argv",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    b = sub.add_parser("bounds", help="print MEMORY PIDS for a role")
    b.add_argument("--role", required=True)
    r = sub.add_parser("reason", help="name what a container exit code means")
    r.add_argument("--role", required=True)
    r.add_argument("rc", type=int)
    a = sub.add_parser("argv", help="print the docker run argv for a role")
    a.add_argument("--role", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--mount", action="append", default=[], metavar="SRC:DST[:ro]")
    a.add_argument("--volume", action="append", default=[], metavar="NAME:DST[:ro]")
    a.add_argument("--credentials", choices=("none", "ro", "rw"),
                   help="the login file read-only, writable, or none (default: the role's)")
    a.add_argument("--env", action="append", default=[], metavar="NAME",
                   help="a secret crossed by name, only when set")
    a.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    a.add_argument("--add-host", action="append", default=[], metavar="HOST:IP")
    a.add_argument("--workdir")
    a.add_argument("--entrypoint")
    a.add_argument("--user", help="default: the runner user (0:0 under rootless docker)")
    a.add_argument("--image", help=f"default: ${IMAGE_ENV}, else {IMAGE_DEFAULT}")
    a.add_argument("--interactive", action="store_true", help="keep stdin open (-i)")
    a.add_argument("--json", action="store_true", help="one JSON array (default: NUL-separated)")
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` is the container's command, split here
    # rather than by argparse.REMAINDER, whose `--` handling varies by Python.
    command = []
    if "--" in argv:
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    try:
        if args.verb == "bounds":
            mem, pids = bounds(args.role)
            print(f"{mem} {pids}")
            return 0
        if args.verb == "reason":
            _role(args.role)
            reason = exit_reason(args.role, args.rc)
            if reason:
                print(reason)
            return 0
        out = compose(args.role, args.name, command, mounts=args.mount, volumes=args.volume,
                      credentials=args.credentials, secrets=args.env, sets=args.set,
                      add_hosts=args.add_host, workdir=args.workdir,
                      entrypoint=args.entrypoint, user=args.user, image=args.image,
                      interactive=args.interactive)
    except LaunchError as exc:
        print(f"phasekit launch-argv: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out))
    else:
        sys.stdout.write("\0".join(out) + "\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
