#!/usr/bin/env bash
#
# PreToolUse (Bash) command guard.
#
# Two rules, one parse (scope: v0.18.3, queue row 1194's release; Aaron,
# 2026-09-30):
#
#   (1) EVERY SESSION (the loop's and an interactive one): the truly
#       destructive commands — `git reset --hard`; `git clean` with -f and -d
#       (-fd, -fdx, …); a FORCE push of any ref (`--force`, `-f`,
#       `--force-with-lease`, a `+ref` refspec) and every other push that
#       deletes or overwrites remote refs (`--delete`/`-d`, a `:ref` refspec,
#       `--mirror`, `--prune`); deleting or overwriting a tag (`git tag -d` /
#       `-f`, `git update-ref` on refs/tags/); `sudo`; `shred`; a recursive
#       `rm` of this repository's root (or anything above it) or of its
#       `.git`. Ordinary writes — a plain `git push`, `git tag v1`, a commit —
#       are NOT on this list: an operator session pushing an
#       `operator-<topic>` branch is the operator lane working.
#
#   (2) UNDER THE LOOP ONLY (v0.18.2, queue row 1233; every session prompt
#       states it as "The loop owns every commit"): git commands that write
#       THIS repository's history, refs or
#       index — commit, commit-tree, add, rm, mv, reset, restore, checkout
#       (with arguments), switch, stash (except list/show), merge, rebase,
#       cherry-pick, revert, am, pull, fetch, tag, branch (creating one, -f,
#       -D, -m, …), update-ref, update-index, read-tree, symbolic-ref
#       (writing), worktree (except list), the write forms of notes, replace,
#       reflog, submodule, remote, bisect and sparse-checkout, apply
#       --index/--cached, prune, filter-branch — and ANY `git push` (from any
#       repository: the loop is the only thing that publishes). Read-only git
#       (status, diff, log, show, grep, blame, ls-files, rev-parse, …) is
#       allowed. Why: phasekit always meant the wrapper to commit, but nothing
#       enforced it; a model that committed the completion record itself made
#       the landing walk read "recorded" as done over work that commit did
#       not carry. The loop's OWN git calls are unaffected — they run outside
#       the model's tool calls. An interactive session is a human's: rule (2)
#       is inert there, exactly like require-verdict.sh and wrapup-nudge.sh.
#
# "Under the loop" = the loop's two exported variables, PHASEKIT_ARTIFACTS_DIR
# and PHASEKIT_ITER_MARKER (run-until-done.sh exports both before its first
# model turn; nothing else sets them), are both NON-EMPTY. Unlike the Stop
# hooks this guard does not also require the two PATHS to exist: the model's
# own tool calls can delete a /tmp marker or move artifacts/, and that must
# never turn the loop's rule off. The variables themselves are fixed by the
# harness when the session starts; no tool call can change them.
#
# What the harness gives a PreToolUse hook: the JSON payload on STDIN
# ({tool_input: {command}, cwd, …}) — probed in scaffold-runner, claude
# 2.1.285, 2026-09-30, where exit 2 refused the call under
# bypassPermissions. Until v0.18.1 this hook read only CLAUDE_TOOL_INPUT,
# which the harness never sets: it matched nothing, anywhere. The variable
# is still read as a fallback (an older harness, a hand test).
#
# The parse follows the command the way a shell would run it: chains (`;`
# `&&` `||` `|` `&`, newlines), subshells, `$(…)` and backticks, `bash -c` /
# `sh -c` strings, `eval`, heredocs fed to a shell, env prefixes
# (`GIT_DIR=… git …`), wrappers (env, command, exec, nohup, nice, time,
# timeout, xargs, find -exec, flock, setsid, stdbuf), `git -C <dir>` /
# `--git-dir` / `--work-tree`, git aliases, and `cd`/`pushd` within the
# chain (the payload's cwd is where the chain starts). A write aimed at
# ANOTHER repository (a scratch repo under /tmp, one a `git init` in the same
# command created) is not rule (2)'s business. A script FILE is not followed
# (`bash x.sh`, `./x.sh`): a static read flags branches a run never takes —
# xmeo-v3's own verify script holds git writes a model's verify run never
# reaches. What a parse does not see (a script, a program that runs git
# itself) is the loop's whole-tree check at every completion
# (run-until-done.sh, step 3).
#
# Refusal: exit 2 and ONE plain line on stderr (the PreToolUse contract: the
# model reads it, the call does not run). Fail-open everywhere else — a
# broken hook must never wedge a session: without python3, or when the parse
# fails, a narrow regex over the command text stands in.

set -u

_payload="$(cat 2>/dev/null)" || _payload=""
[[ -n "$_payload" ]] || _payload="${CLAUDE_TOOL_INPUT:-}"
[[ -n "$_payload" ]] || exit 0

_loop=0
if [[ -n "${PHASEKIT_ARTIFACTS_DIR:-}" && -n "${PHASEKIT_ITER_MARKER:-}" ]]; then
  _loop=1
fi

# The regexes that stand in when the parse cannot run (no python3) or fails
# (python's fallback below reads these same two, so they cannot drift). GNU
# ERE, case-sensitive (a shell's command names are).
PK_GUARD_DESTRUCTIVE_RE='(^|[^A-Za-z0-9_./-])(git\s+((-c|-C)\s+\S+\s+)*(reset\s+--har?d?|clean\s+-\w*(f\w*d|d\w*f)|push(\s[^;&|#]*)?\s(--for|--mir|--del|--pru|-\w*[fd]\b|\+|:\S)|tag\s+(-\w*[df]|--del|--for))|sudo\s|shred\s|rm\s+(\S+\s+)*-\w*[rR]\w*\s+(\S+\s+)*(\S*/)?\.git(\s|$|/(objects|refs)))'
PK_GUARD_LOOP_RE='(^|[^a-z0-9_./-])git(\s+(-c|-C|--git-dir|--work-tree)\s+\S+|\s+--[a-z-]+(=\S+)?)*\s+(commit|commit-tree|add|rm|mv|reset|restore|switch|stash|merge|rebase|cherry-pick|revert|am|pull|fetch|push|send-pack|subtree|update-ref|update-index|read-tree|worktree|checkout|tag|filter-branch)(\s|$|[;&|)<>])'
export PK_GUARD_DESTRUCTIVE_RE PK_GUARD_LOOP_RE

_deny() {
  # $1 = destructive | loop, $2 = what matched
  if [[ "$1" == loop ]]; then
    echo "phasekit: the loop owns every commit — \`$2\` writes this repository's history, refs or index, so it is refused; write your verdict instead (artifacts/phase-update.json, phase-approval.json or project-complete.json) and the loop commits it, verify-gated." >&2
  else
    echo "Blocked dangerous command pattern: $2 — destructive, so refused in every session (an ordinary write such as a plain git push is not)." >&2
  fi
  exit 2
}

_fallback() {
  # No python3, or the parse failed: a narrow regex over the command text.
  local cmd="$_payload" lc pat
  if command -v jq >/dev/null 2>&1; then
    cmd="$(jq -r '.tool_input.command // empty' <<<"$_payload" 2>/dev/null)" || cmd=""
    [[ -n "$cmd" ]] || cmd="$_payload"
  fi
  lc="$cmd"
  pat="$(printf '%s' "$lc" | grep -oE -- "$PK_GUARD_DESTRUCTIVE_RE" 2>/dev/null | head -n1)" || pat=""
  if [[ -n "$pat" ]]; then _deny destructive "${pat#"${pat%%[A-Za-z]*}"}"; fi
  if [[ "$_loop" == 1 ]] && printf '%s' "$lc" | grep -Eq -- "$PK_GUARD_LOOP_RE"; then
    _deny loop "git (write)"
  fi
  exit 0
}

command -v python3 >/dev/null 2>&1 || _fallback

read -r -d '' _GUARD_PY <<'GUARD_PY'
import fnmatch, json, os, re, shlex, subprocess, sys

raw = sys.stdin.read()
LOOP = os.environ.get("PK_GUARD_LOOP") == "1"
cmd, start = None, None
try:
    data = json.loads(raw)
    if isinstance(data, dict):
        ti = data.get("tool_input") or {}
        cmd = ti.get("command") if isinstance(ti, dict) else None
        start = data.get("cwd")
except ValueError:
    pass
if cmd is None:
    cmd = raw
if not isinstance(cmd, str) or not cmd.strip():
    print("OK"); sys.exit(0)
if not isinstance(start, str) or not start:
    start = os.getcwd()

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "mksh"}
REMOTE = {"ssh", "docker", "kubectl", "podman"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "esac", "time", "coproc"}

class Deny(Exception):
    def __init__(self, kind, what):
        Exception.__init__(self, what)
        self.kind, self.what = kind, what

def git_out(args, cwd):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=5,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None

def common_dir(d):
    """The repository a directory belongs to: its common git dir (a linked
    worktree resolves to the repository it writes into). None outside any."""
    if not d:
        return None
    probe = d
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    out = git_out(["rev-parse", "--git-common-dir"], probe)
    return os.path.realpath(os.path.join(probe, out)) if out else None

_PROJECT = []
def project():
    if not _PROJECT:
        found = None
        art = os.environ.get("PHASEKIT_ARTIFACTS_DIR")
        for d in ([os.path.dirname(art.rstrip("/"))] if art else []) + [os.environ.get("CLAUDE_PROJECT_DIR"), start]:
            found = common_dir(d) if d else None
            if found:
                break
        _PROJECT.append(found)
    return _PROJECT[0]

def expand(word, cwd):
    w = os.path.expanduser(os.path.expandvars(word))
    if "$" in w or "`" in w:
        return None
    if cwd is None and not os.path.isabs(w):
        return None
    return os.path.normpath(os.path.join(cwd or "/", w))

def strip_comment(line):
    """Drop a `#` comment (a `#` that starts a word, outside quotes)."""
    q, prev = None, " "
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"':
                pass
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (prev.isspace() or prev in ";&|()"):
            return line[:i]
        prev = ch
    return line

def quoted_or_arith(line, pos):
    """Is position pos inside a quoted string or a $(( … )) of line?"""
    q, i, arith = None, 0, 0
    while i < pos:
        ch = line[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"':
                i += 1
        elif ch in "'\"":
            q = ch
        elif line.startswith("$((", i):
            arith += 1; i += 2
        elif line.startswith("))", i) and arith:
            arith -= 1; i += 1
        i += 1
    return q is not None or arith > 0

def split_heredocs(text):
    """(text without heredoc bodies, bodies fed to a local shell)."""
    lines = text.split("\n")
    out, bodies, i = [], [], 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        i += 1
        for m in re.finditer(r"(?<!<)<<(?!<)(-?)\s*(?:(['\"])([^'\"\s]+)\2|\\?([A-Za-z_][A-Za-z0-9_.-]*))", ln):
            if quoted_or_arith(ln, m.start()):
                continue    # `echo "x <<EOF"`, `$((1<<x))` are not heredocs
            dash, delim, body = m.group(1), m.group(3) or m.group(4), []
            while i < len(lines):
                cand = lines[i].lstrip("\t") if dash else lines[i]
                i += 1
                if cand == delim:
                    break
                body.append(lines[i - 1])
            try:
                words = shlex.split(ln, posix=True)
            except ValueError:
                words = ln.split()
            names = {os.path.basename(w) for w in words}
            if names & SHELLS and not names & REMOTE:
                bodies.append("\n".join(body))
            elif not m.group(2) and "\\" not in m.group(0):
                # an UNQUOTED delimiter: bash still expands $(…) and `…` in
                # the body, whatever reads it (review round 5)
                bodies.extend(substitutions("\n".join(body), quotes=False))
    return "\n".join(out), bodies

def ansi_c(text):
    """Decode $'…' (bash ANSI-C quoting) into an ordinary single-quoted word."""
    def dec(m):
        try:
            v = m.group(1).encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            v = m.group(1)
        return shlex.quote(v)
    return re.sub(r"\$'((?:[^'\\]|\\.)*)'", dec, text)

# `\(` `'('` `")"` are WORDS (find's grouping), never a subshell
QUOTED_PARENS = re.compile(r'''(?<!\S)(?:\\([()])|'([()])'|"([()])")(?!\S)''')    # a word of its own only (round 6)

def tokens(text):
    text = ansi_c(text)
    text = text.replace("\\\n", " ")
    text = "\n".join(strip_comment(ln) for ln in text.split("\n"))
    # command substitutions inside double quotes run too: their bodies are
    # checked on their own (a literal in single quotes is a rare false
    # positive, never a silent pass)
    subs = substitutions(text)
    text = QUOTED_PARENS.sub(lambda m: " __LP__ " if (m.group(1) or m.group(2) or m.group(3)) == "(" else " __RP__ ", text)
    lex = shlex.shlex(text.replace("\n", " ; ").replace("`", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    for t in lex:
        out.extend(split_ops(t) if t and set(t) <= PUNCT else [t])
    return out, subs

def substitutions(text, quotes=True):
    """The bodies of $(…) and `…` that a shell would run: outside single
    quotes (inside double quotes they run too). quotes=False: a heredoc body,
    where quote characters are literal and only a backslash escapes."""
    out, i, q, n = [], 0, None, len(text)
    while i < n:
        ch = text[i]
        if q == "'":
            if ch == "'":
                q = None
            i += 1; continue
        if ch == "\\":
            i += 2; continue
        if quotes and ch == "'" and q is None:
            q = "'"; i += 1; continue
        if quotes and ch == '"':
            q = None if q == '"' else '"'
            i += 1; continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            out.append(text[i + 1:j]); i = j + 1; continue
        if text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            out.append(text[i + 2:j - 1]); i = j; continue
        i += 1
    return out

PUNCT = set(";&|()<>")
OPS = ("<<<", ";;&", "&>>", "&&", "||", ";;", ";&", "|&", ">>", "<<", ">&", "<&", "&>", ">|", "<>",
       ";", "&", "|", "(", ")", "<", ">")

def split_ops(t):
    """shlex returns a run of operator characters as one token (`);`): split it."""
    out, i = [], 0
    while i < len(t):
        for op in OPS:
            if t.startswith(op, i):
                out.append(op); i += len(op); break
        else:
            out.append(t[i]); i += 1
    return out

def is_redirect(t):
    return bool(t) and set(t) <= set("<>&|") and ("<" in t or ">" in t)

def segments(toks):
    """Simple commands as (words, sep): sep is the operator that ended the
    command (`;` `&&` `|` `&` …, None at the end); ([], '(' / ')') marks a
    subshell."""
    cur, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t and set(t) <= set(";&|"):
            if cur:
                yield cur, "sep:" + t
            cur = []; i += 1; continue
        if t in ("(", ")"):
            if t == "(" and cur and cur[-1].endswith("$"):
                # `$(`: a bare `$` goes; a glued word (`../$(…)`, `./$(…)`) keeps
                # its `$`, so it stays UNRESOLVABLE — never the prefix alone
                # (review round 5: `cd ../$(…)` read as `cd ..`)
                if cur[-1] == "$":
                    cur.pop()
            if cur:
                yield cur, None
            cur = []
            yield [], t
            i += 1; continue
        if t in ("<", ">") and i + 1 < len(toks) and toks[i + 1] == "(":
            # process substitution: its command runs, in a subshell
            if cur:
                yield cur, None
            cur = []
            yield [], "("
            i += 2; continue
        if is_redirect(t):
            if cur and cur[-1].isdigit():
                cur.pop()
            if t == "<<<" and i + 1 < len(toks):
                cur.append(HERESTR + toks[i + 1])     # a shell reading it runs it
            i += 2; continue
        cur.append(t); i += 1
    if cur:
        yield cur, None

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HERESTR = "\x00<<<"

def skip_opts(words, j, with_arg=()):
    while j < len(words) and words[j].startswith("-") and words[j] != "-":
        j += 2 if words[j] in with_arg else 1
    return j

def unwrap(words, env):
    """Strip assignments, keywords and wrappers: the words of the real command."""
    while words:
        w0 = words[0]
        b = os.path.basename(w0)
        if ASSIGN.match(w0):
            k, v = w0.split("=", 1); env[k] = v; words = words[1:]; continue
        if w0 == "function" and len(words) > 1:
            # `function f { …; }`: the body's words follow the name (round 8)
            words = words[2:]; continue
        if w0 == "time" and words[1:2] in (["-p"], ["--"]):
            words = words[2:]; continue
        if w0 == "time" and len(words) > 1 and words[1].startswith("-"):
            # `\time -f %e …` is GNU time (the keyword takes only -p)
            words = words[skip_opts(words, 1, ("-f", "-o", "--format", "--output")):]; continue
        if w0 == "--" and len(words) > 1:
            words = words[1:]; continue     # `time -p -- cmd`
        if w0 in KEYWORDS:
            words = words[1:]; continue
        if b == "sudo":
            raise Deny("destructive", "sudo")
        if b == "env":
            j = 1
            while j < len(words):
                a = words[j]
                split_arg = None
                if a in ("-S", "--split-string") and j + 1 < len(words):
                    split_arg, drop = words[j + 1], 2
                elif a.startswith("--split-string="):
                    split_arg, drop = a.split("=", 1)[1], 1
                elif a.startswith("-S") and len(a) > 2:
                    split_arg, drop = a[2:], 1
                if split_arg is not None:
                    try:
                        split = shlex.split(split_arg)
                    except ValueError:
                        split = split_arg.split()
                    words = words[:j] + split + words[j + drop:]
                    continue
                if a in ("-C", "--chdir") and j + 1 < len(words):
                    env["__CWD__"] = words[j + 1]; j += 2; continue
                if a.startswith("--chdir=") or (a.startswith("-C") and len(a) > 2):
                    env["__CWD__"] = a.split("=", 1)[1] if a.startswith("--chdir=") else a[2:]; j += 1; continue
                if a in ("-u", "--unset"):
                    j += 2
                elif ASSIGN.match(a):
                    k, v = a.split("=", 1); env[k] = v; j += 1
                elif a.startswith("-"):
                    j += 1
                else:
                    break
            words = words[j:]; continue
        if b in ("command", "builtin", "exec", "nohup", "setsid", "chronic", "unbuffer"):
            words = words[skip_opts(words, 1):]; continue
        if b in ("nice", "ionice", "stdbuf", "time"):
            words = words[skip_opts(words, 1, ("-n", "-c", "-p", "-i", "-o", "-e", "-f")):]; continue
        if b == "timeout":
            words = words[skip_opts(words, 1, ("-s", "-k", "--signal", "--kill-after")) + 1:]; continue
        if b == "flock":
            words = words[skip_opts(words, 1, ("-w", "-E", "--timeout", "--conflict-exit-code")) + 1:]; continue
        if b == "xargs":
            words = words[skip_opts(words, 1, ("-I", "-n", "-P", "-L", "-d", "-s", "-a", "-E", "--max-args", "--max-procs", "--delimiter", "--arg-file")):]; continue
        break
    return words

GIT_OPT_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env", "--list-cmds"}
ALWAYS_WRITES = {"commit", "commit-tree", "add", "stage", "rm", "mv", "reset", "restore", "switch", "merge", "rebase",
                 "cherry-pick", "revert", "am", "pull", "fetch", "update-ref", "update-index", "read-tree",
                 "checkout-index", "filter-branch", "filter-repo", "fast-import", "prune", "mergetool"}

# git's own commands (an alias never shadows one): no alias lookup for these
KNOWN_SUBS = {"status", "diff", "log", "show", "grep", "blame", "ls-files", "ls-tree", "rev-parse", "rev-list",
              "cat-file", "describe", "shortlog", "reflog", "merge-base", "name-rev", "for-each-ref", "show-ref",
              "whatchanged", "range-diff", "diff-tree", "diff-index", "diff-files", "check-ignore", "check-attr",
              "var", "version", "help", "count-objects", "fsck", "cherry", "format-patch", "archive", "bundle",
              "config", "remote", "branch", "tag", "stash", "worktree", "notes", "submodule", "bisect", "apply",
              "clean", "init", "clone", "push", "gc", "maintenance", "hash-object", "write-tree", "mktree",
              "verify-commit", "verify-tag", "ls-remote", "symbolic-ref", "sparse-checkout", "replace",
              "checkout", "annotate", "show-branch", "request-pull", "mailinfo", "interpret-trailers", "repack",
              "pack-refs", "get-tar-commit-id", "column", "credential", "difftool", "rerere", "range-diff"}

# git options whose value is the next word (so it is never read as a name)
OPT_ARG = {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message", "--author",
           "--date", "-t", "--template", "--format", "--sort", "--contains", "--no-contains", "--merged",
           "--no-merged", "--points-at", "-u", "--local-user", "--set-upstream-to", "--track"}

def positionals(rest):
    out, skip = [], False
    for a in rest:
        if skip:
            skip = False; continue
        if a.startswith("-"):
            skip = a in OPT_ARG
            continue
        out.append(a)
    return out

def git_writes(sub, rest):
    """What `git <sub> <rest>` writes of history/refs/index, named; None = read-only."""
    if any(a in ("-h", "--help") for a in rest):
        return None
    opts = [a for a in rest if a.startswith("-")]
    pos = positionals(rest)
    if sub == "commit" and "--dry-run" in [a for i, a in enumerate(rest)
                                          if i == 0 or rest[i - 1] not in OPT_ARG]:
        return None
    if sub in ALWAYS_WRITES:
        return sub
    if sub == "checkout":
        return "checkout" if rest else None
    if sub == "stash":
        return None if pos and pos[0] in ("list", "show") else "stash"
    if sub == "branch":
        opts, skip = [], False
        for a in rest:                      # an option's value is not an option
            if skip:
                skip = False; continue
            if a.startswith("-"):
                opts.append(a)
                skip = a in OPT_ARG
        for o in opts:
            name = o.split("=", 1)[0]
            if name in ("--delete", "--force", "--move", "--copy", "--set-upstream-to", "--unset-upstream",
                        "--edit-description", "--track", "--no-track", "--create-reflog", "--set-upstream"):
                return "branch " + name
            if not o.startswith("--") and set(o[1:]) & set("dDfmMcCut"):
                return "branch " + o
        listing = ("-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--show-current")
        if pos and not any(o.split("=", 1)[0] in listing for o in opts):
            return "branch <name>"
        return None
    if sub == "tag":
        names = [o.split("=", 1)[0] for o in opts]
        if any(n in ("-d", "--delete", "-a", "--annotate", "-s", "--sign", "-f", "--force", "-m", "--message",
                     "-F", "--file", "-u", "--local-user", "-e", "--edit") for n in names):
            return "tag"
        # only these put `git tag` in list mode; --sort/--format/--column/-i
        # with a name still CREATE the tag
        listing = any(n in ("-l", "--list", "-v", "--verify", "--contains", "--no-contains", "--points-at",
                            "--merged", "--no-merged") or re.fullmatch(r"-n\d*", n)
                      for n in names)
        return "tag" if pos and not listing else None
    first = pos[0] if pos else ""
    if sub == "worktree":
        return None if first in ("", "list") else "worktree " + first
    if sub == "notes" and first in ("add", "append", "copy", "edit", "merge", "prune", "remove"):
        return "notes " + first
    if sub == "replace":
        return None if not rest or any(o in ("-l", "--list") for o in opts) else "replace"
    if sub == "reflog" and first in ("expire", "delete", "drop"):
        return "reflog " + first
    if sub == "submodule" and first in ("add", "update", "init", "deinit", "sync", "set-branch", "set-url", "absorbgitdirs", "foreach"):
        return "submodule " + first
    if sub == "remote" and first in ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update"):
        return "remote " + first
    if sub == "symbolic-ref" and ("-d" in opts or "--delete" in opts or len(pos) >= 2):
        return "symbolic-ref"
    if sub == "bisect" and first and first not in ("log", "view", "visualize", "help"):
        return "bisect " + first
    if sub == "sparse-checkout" and first and first != "list":
        return "sparse-checkout " + first
    if sub == "apply" and any(o in ("--index", "--cached", "-3", "--3way") for o in opts):
        return "apply --index"
    return None

CONFIGS = []    # the `-c` values of the git command being judged

PUSH_OPT_ARG = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}

def long_is(opt, *names):
    """git accepts any unambiguous PREFIX of a long option (`--del` is
    `--delete`); an ambiguous one git refuses itself, so a prefix of a
    destructive name is treated as that name."""
    o = opt.split("=", 1)[0]
    return len(o) > 2 and any(n.startswith(o) for n in names)

MIRROR_CONFIG = re.compile(r"^remote\.[^=]+\.(mirror($|=(true|yes|on|-?[1-9][0-9]*)$)|push=[+:])", re.I)

def destructive_push(rest):
    """A push that deletes or overwrites remote refs, named; None = an ordinary push."""
    pos, skip = [], False
    for a in rest:
        if skip:
            skip = False; continue
        if a.startswith("-") and a != "-":
            name = a.split("=", 1)[0]
            if name.startswith("--") and long_is(name, "--force", "--force-with-lease", "--mirror",
                                                 "--delete", "--prune"):
                return "git push " + name
            if not a.startswith("--"):
                for ch in a[1:]:
                    if ch == "o":
                        break           # -o<option>: the rest is its value
                    if ch in "fd":
                        return "git push -" + ch
            skip = a in PUSH_OPT_ARG
            continue
        pos.append(a)
    for ref in pos[1:]:                 # pos[0] is the remote
        if ref.startswith("+") and ref != "+":     # a lone `+` is find -exec's terminator
            return "git push +<ref> (force)"
        if ref.startswith(":") and ref != ":":        # a lone `:` is the matching push
            return "git push :<ref> (delete)"
    return None

def destructive_git(sub, rest):
    """Rule (1): what `git <sub> <rest>` destroys, named; None = not destructive."""
    opts = [a for a in rest if a.startswith("-")]
    if sub in ("push", "send-pack"):
        return destructive_push(rest)
    if sub == "tag":
        skip = False
        for o in rest:
            if skip or not o.startswith("-") or o == "-":
                skip = False; continue
            skip = o in OPT_ARG                 # `--sort -v:refname`: a value, not flags
            if o.startswith("--") and long_is(o, "--delete", "--force"):
                return "git tag " + o.split("=", 1)[0]
            if not o.startswith("--"):
                for ch in o[1:]:
                    if ch in "muFn":
                        break           # -m<msg>, -u<key>, -F<file>, -n<num>: the rest is a value
                    if ch in "df":
                        return "git tag -" + ch
        return None
    if sub == "update-ref" and any(r.startswith("refs/tags/") for r in positionals(rest)):
        return "git update-ref refs/tags/…"
    if sub == "reset" and any(o.startswith("--") and long_is(o, "--hard") for o in opts):
        return "git reset --hard"
    if sub == "clean":
        flags = ""
        for o in opts:
            if o.startswith("--"):
                continue
            for ch in o[1:]:
                if ch == "e":
                    break           # -e<pattern>: the rest is its value
                flags += ch
        force = "f" in flags or any(o.startswith("--") and long_is(o, "--force") for o in opts) \
            or any(re.match(r"(?i)clean\.requireforce=(false|no|off|0)$", c) for c in CONFIGS)
        dry = "n" in flags or any(o.startswith("--") and long_is(o, "--dry-run") for o in opts)
        dirs = "d" in flags
        if force and dirs and not dry:
            return "git clean -fd"
    return None

class State:
    def __init__(self, cwd, new_repos=None, aliases=None):
        self.cwd = cwd
        self.oldpwd = None
        self.stack = []        # subshell cwd stack
        self.dirs = []         # pushd stack
        self.new_repos = set(new_repos or ())
        self.aliases = dict(aliases or {})

    def child(self, cwd=None):
        return State(self.cwd if cwd is None else cwd, self.new_repos, self.aliases)

def check_git(words, st, env, depth):
    args = words[1:]
    gdir, cdir, configs, i = env.get("GIT_DIR"), st.cwd, [], 0
    if env.get("__CWD__"):
        cdir = expand(env["__CWD__"], cdir)     # env -C <dir> git …
    while i < len(args):
        a = args[i]
        if a in GIT_OPT_WITH_ARG and i + 1 < len(args):
            v = args[i + 1]
            if a == "-C":
                cdir = expand(v, cdir)
            elif a == "-c":
                configs.append(v)
            elif a == "--git-dir":
                gdir = v
            i += 2; continue
        if a.startswith("--git-dir="):
            gdir = a.split("=", 1)[1]; i += 1; continue
        if a in ("--version", "--help", "-h", "-v"):
            return
        if a.startswith("-"):
            i += 1; continue
        break
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if depth < 4:
        alias = None
        for c in configs:
            if c.startswith("alias." + sub + "="):
                alias = c.split("=", 1)[1]
        if alias is None:
            alias = st.aliases.get(sub)
        if alias is None and cdir is not None and os.path.isdir(cdir) and sub not in ALWAYS_WRITES \
                and sub not in KNOWN_SUBS:
            alias = git_out(["config", "--get", "alias." + sub], cdir)
        if alias:
            if alias.startswith("!"):
                check_text(alias[1:] + " " + " ".join(shlex.quote(r) for r in rest), st.child(cdir), depth + 1)
                return
            try:
                exp = shlex.split(alias)
            except ValueError:
                exp = alias.split()
            check_git(["git"] + args[:i] + exp + rest, st, env, depth + 1)
            return
    CONFIGS[:] = configs
    bad = destructive_git(sub, rest)
    if not bad and sub == "push" and any(MIRROR_CONFIG.match(c) for c in configs):
        bad = "git -c remote.<name>.mirror/push=+… push"
    if bad:
        raise Deny("destructive", bad)
    if sub == "init":
        tgt = next((r for r in rest if not r.startswith("-")), ".")
        st.new_repos.add(expand(tgt, cdir))
        return
    if sub == "config":
        # an alias defined in this very command is what a later `git <alias>` runs
        vals = [r for r in rest if not r.startswith("-")]
        if len(vals) >= 2 and vals[0].startswith("alias."):
            st.aliases[vals[0][len("alias."):]] = " ".join(vals[1:])
    if not LOOP:
        return
    if sub in ("push", "send-pack") or (sub == "subtree" and "push" in rest):
        raise Deny("loop", "git " + sub)    # from any repository: only the loop publishes
    what = git_writes(sub, rest)
    if not what and sub == "subtree" and set(rest) & {"add", "merge", "pull", "split"}:
        what = "subtree"
    if not what:
        return
    what = "git " + what
    proj = project()
    if gdir:
        target = expand(gdir, cdir if cdir is not None else "/")
        tc = common_dir(target) if target else None
        if target and os.path.isdir(target) and tc is None:
            tc = os.path.realpath(target)
        if proj and tc and tc != proj:
            return
        raise Deny("loop", what)
    if cdir is None:
        if None in st.new_repos:
            return          # the same unresolved directory a `git init` just made a repository
        raise Deny("loop", what)
    # a repository a `git init` in this command made (it may not exist yet
    # when the hook runs): its own, unless it IS the project's top level (a
    # re-init of this repository)
    top = os.path.dirname(proj) if proj and os.path.basename(proj) == ".git" else None
    for r in st.new_repos:
        if not r or not (cdir == r or cdir.startswith(r.rstrip("/") + "/")):
            continue
        rr = os.path.realpath(r)
        # this repository's own top level at or below the new one is nearer
        # to cdir (`git init ..` from inside it changes nothing here)
        if top and (top == rr or top.startswith(rr.rstrip("/") + "/")) and \
                (os.path.realpath(cdir) == top or os.path.realpath(cdir).startswith(top.rstrip("/") + "/")):
            continue
        return
    c = common_dir(cdir)
    if proj is not None and c != proj:
        return              # another repository, or none: not this one
    raise Deny("loop", what)

_PROTECTED = []
def protected():
    """(this repository's top level, its git dirs) — what a recursive rm must never reach."""
    if not _PROJECT:
        project()
    if not _PROTECTED:
        top, gits = None, set()
        art = os.environ.get("PHASEKIT_ARTIFACTS_DIR")
        for d in ([os.path.dirname(art.rstrip("/"))] if art else []) + [os.environ.get("CLAUDE_PROJECT_DIR"), start]:
            if d and os.path.isdir(d):
                top = git_out(["rev-parse", "--show-toplevel"], d)
                if top:
                    g = git_out(["rev-parse", "--absolute-git-dir"], d)
                    gits = {os.path.realpath(x) for x in (g, _PROJECT[0], os.path.join(top, ".git")) if x}
                    top = os.path.realpath(top)
                    break
        _PROTECTED.append((top, gits))
    return _PROTECTED[0]

def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")

UNSET_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# set by bash itself, never exported to the hook (review round 10)
SHELL_SET = {"RANDOM", "HOSTNAME", "UID", "EUID", "PPID", "BASHPID", "SECONDS", "EPOCHSECONDS",
             "EPOCHREALTIME", "OSTYPE", "MACHTYPE", "HOSTTYPE", "BASH", "BASH_VERSION", "LINENO",
             "SRANDOM", "GROUPS", "HISTFILE", "IFS", "PS1", "PS2", "PS4", "SHELLOPTS", "BASHOPTS",
             "OPTIND", "OPTARG", "REPLY", "PIPESTATUS", "FUNCNAME", "DIRSTACK", "COLUMNS", "LINES"}
MAY_BIND = re.compile(r"(^|[\s;&|(])(source|\.|eval|select|getopts|read|mapfile|readarray)(?=[\s;&|)]|$)|printf\s+-v")

def empty_unset(word, st):
    """A variable the shell cannot have (not in the environment, never bound
    in this command) is EMPTY there: `rm -rf "$OUT"/*` with OUT unset is
    `rm -rf /*` (review round 8). Applied only when that makes the target an
    ABSOLUTE path — the case that destroys; anything else stays unresolved
    (review round 9: `rm -rf "$tmp"` with tmp unset removes nothing). $PWD /
    $OLDPWD are the tracked cwd."""
    text = CMD_TEXT[0]
    def bound(name):
        if name in os.environ:
            return True
        # quote removal glues `"$d"_old` into `$d_old`: any bound PREFIX counts
        for k in range(1, len(name) + 1):
            n = name[:k]
            if n in os.environ or re.search(r"(^|[\s;&|(])" + re.escape(n) + r"=", text) \
                    or re.search(r"\b(for|local|declare|export|readonly|typeset)\b[^;&|]*\b" + re.escape(n) + r"\b", text):
                return True
        return False
    def sub(m):
        name = m.group(1) or m.group(2)
        if name in ("PWD", "OLDPWD"):
            v = st.cwd if name == "PWD" else st.oldpwd
            return v if v else "$__unknown"     # the tracked dir, never the hook's own (round 10)
        if name in SHELL_SET or MAY_BIND.search(text) or bound(name):
            return m.group(0)
        return ""
    out = UNSET_VAR.sub(sub, word)
    if out != word and "$" not in out and not out.startswith("/"):
        return word         # emptied to a relative path (or nothing): not judged
    return out

CMD_TEXT = [""]

def rm_destroys_repo(words, st):
    """A recursive rm whose target is this repository's root, a directory
    above it, or its .git (or .git/objects|refs), named; None otherwise. A
    variable the command cannot have set is judged EMPTY when that makes the
    target absolute (`"$OUT"/*` → `/*`, empty_unset); any other target the
    parse cannot resolve (`$(…)`, a variable that may be set) is not judged."""
    flags, targets, ended = set(), [], False
    for a in words[1:]:
        if not ended and a == "--":
            ended = True; continue
        if not ended and a.startswith("--"):
            flags |= {"r"} if len(a) > 2 and "--recursive".startswith(a) else set()
            continue
        if not ended and a.startswith("-") and a != "-":
            flags |= set(a[1:].lower()); continue
        targets.append(a)
    if "r" not in flags:
        return None
    top, gits = protected()
    if not top:
        return None
    for t in targets:
        raw = t
        t = empty_unset(t, st)
        t = t.rstrip("/") or t              # `../*/` is `../*` (the slash only selects directories)
        glob = any(c in t for c in "*?[")
        base = os.path.basename(t.rstrip("/")) if glob else None
        path = expand(os.path.dirname(t) or "." if glob else t, st.cwd)
        if path is None:
            continue
        if glob:
            path = os.path.realpath(path)
            # `rm -rf *` / `.*` / `.g*` in the top level reaches the tree or
            # .git; a glob in a directory ABOVE it (`../*`, `/*`) reaches it all
            if path == top and (base in ("*", "./*") or (base.startswith(".") and fnmatch.fnmatchcase(".git", base))):
                return "rm -r " + t + " (this repository)"
            if path != top and _under(top, path):
                comp = os.path.relpath(top, path).split(os.sep)[0]
                if fnmatch.fnmatchcase(comp, base) and (base.startswith(".") or not comp.startswith(".")):
                    return "rm -r " + t + " (a directory above this repository)"
            continue
        if os.path.islink(path) and not raw.endswith("/"):
            continue            # rm removes the link, never what it points at (`link/` follows it)
        path = os.path.realpath(path)
        if _under(top, path):
            return "rm -r " + t + " (this repository's root)"
        for g in gits:
            # .git itself, or its object store / refs; a lock file or a
            # stale rebase-merge/ inside it is ordinary recovery
            if path == g or _under(path, os.path.join(g, "objects")) or _under(path, os.path.join(g, "refs")):
                return "rm -r " + t + " (this repository's .git)"
    return None

def check_words(words, st, depth):
    env = {}
    herestrings = [w[len(HERESTR):] for w in words if w.startswith(HERESTR)]
    words = unwrap([w for w in words if not w.startswith(HERESTR)], env)
    if not words:
        return
    if (os.path.basename(words[0]) in SHELLS and not any(w == "-c" or (w.startswith("-") and not w.startswith("--") and "c" in w[1:]) for w in words[1:])) \
            or (words[0] in (".", "source") and words[1:2] in (["/dev/stdin"], ["-"], [])):
        for h in herestrings:
            check_text(h, st.child(), depth + 1)
    w0 = words[0]
    b = os.path.basename(w0)
    if b == "shred":
        raise Deny("destructive", "shred")
    if b == "rm":
        hit = rm_destroys_repo(words, st)
        if hit:
            raise Deny("destructive", hit)
    if b in ("cd", "pushd", "popd"):
        prev = st.cwd
        tgt = next((w for w in words[1:] if not w.startswith("-") and not w.startswith("+")), None)
        if b == "popd":
            st.cwd = st.dirs.pop() if st.dirs else None
        elif "-" in words[1:]:
            st.cwd = st.oldpwd
        elif tgt is None:
            st.cwd = os.path.expanduser("~") if b == "cd" else None
        else:
            st.cwd = expand(tgt, st.cwd)
        if b == "pushd":
            st.dirs.append(prev)
        st.oldpwd = prev
        return
    if b == "git":
        check_git(words, st, env, depth)
        return
    k = 1
    while k < len(words) and words[k].startswith("-"):
        k += 2 if words[k] in GIT_OPT_WITH_ARG else 1
    if LOOP and ("$" in w0 or "`" in w0) and k < len(words) and (words[k] in KNOWN_SUBS or words[k] in ALWAYS_WRITES):
        # `$G push`, `${GIT:-git} commit`, `"$(which git)" push`: a command
        # word the parse cannot resolve, followed by a git subcommand, is
        # judged as git under the loop (a false refusal of `$UV add` beats a
        # silent push; interactive sessions are never judged this way)
        check_git(["git"] + words[1:], st, env, depth)
        return
    if b.startswith("git-") and len(b) > 4:
        # git's own programs (/usr/lib/git-core/git-push) are `git <sub>`
        check_git(["git", b[4:]] + words[1:], st, env, depth)
        return
    if b in SHELLS:
        j = 1
        while j < len(words) and words[j][:1] in ("-", "+"):
            flag = words[j]
            if flag in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                j += 2; continue
            if not flag.startswith("--") and "c" in flag[1:]:
                if j + 1 < len(words):
                    check_text(words[j + 1], st.child(), depth + 1)
                return
            j += 1
        # A script FILE the shell runs is not followed: a static read of a
        # whole script flags branches the run never takes (a project's own
        # verify script, phasekit.sh itself) — the completion's whole-tree
        # check is the net for what a script commits.
        return
    if b == "eval":
        check_text(" ".join(words[1:]), st, depth + 1)
        return
    if b == "find":
        # `find <dir> -delete` with no filtering predicate (only depth/mount
        # options) removes everything under <dir>; `find . -name x -delete`
        # is ordinary cleanup
        j = 1
        while j < len(words) and (words[j] in ("-L", "-P", "-H") or re.fullmatch(r"-O\d*", words[j])
                                  or words[j] == "-D"):
            j += 2 if words[j] == "-D" else 1
        targets = []
        while j < len(words) and not words[j].startswith("-") and words[j] not in ("(", "!", "__LP__"):
            targets.append(words[j]); j += 1
        # a filter counts only BEFORE -delete (find evaluates in order); an
        # -o at the top level may take an unfiltered branch; a group filters
        # when each of its -o alternatives does
        NOFILTER = ("-depth", "-xdev", "-mount", "-ignore_readdir_race", "-print", "-print0", "-true",
                    "-ls", "-a", "-and")
        def filters(seq):
            alts, cur, depth, k = [], [], 0, 0
            while k < len(seq):
                w = seq[k]
                if w in ("(", "__LP__"):
                    depth += 1; cur.append(w)
                elif w in (")", "__RP__"):
                    depth -= 1; cur.append(w)
                elif w in ("-o", "-or", ",") and depth == 0:
                    alts.append(cur); cur = []
                elif w in ("-mindepth", "-maxdepth") and depth == 0:
                    k += 1
                else:
                    cur.append(w)
                k += 1
            alts.append(cur)
            def alt_filters(a):
                if not a:
                    return False
                if a[0] in ("(", "__LP__") and a[-1] in (")", "__RP__"):
                    return filters(a[1:-1])
                return any(w not in NOFILTER and w not in ("(", ")", "__LP__", "__RP__") for w in a)
            return all(alt_filters(a) for a in alts)
        k = j
        while k < len(words) and words[k] != "-delete":
            k += 1
        preds = ["filtered"] if filters(words[j:k]) else []
        if "-delete" in words and not preds:
            for w in targets or ["."]:
                hit = rm_destroys_repo(["rm", "-r", w], st)
                if hit:
                    raise Deny("destructive", "find " + w + " -delete" + hit[hit.index(" (", 5):])
        for j, w in enumerate(words):
            if w in ("-exec", "-execdir", "-ok", "-okdir"):
                check_words(words[j + 1:], st.child(), depth + 1)
                return
        return

def echoed_text(words):
    """What `echo …` / `printf fmt args…` writes (enough to read commands in it)."""
    b = os.path.basename(words[0])
    args = words[1:]
    if b == "echo":
        return " ".join(a for a in args if a not in ("-e", "-n", "-E")).replace("\\n", "\n")
    if not args:
        return ""
    fmt, vals = args[0], list(args[1:])
    out = re.sub(r"%[sbdq]", lambda m: vals.pop(0) if vals else "", fmt)
    return out.replace("\\n", "\n").replace("\\t", "\t")

def piped_script(words):
    """Does this command run a script it reads from stdin (`bash`, `sh -s`)?"""
    env = {}
    try:
        w = unwrap(list(words), env)
    except Deny:
        return False
    if not w or os.path.basename(w[0]) not in SHELLS:
        return False
    rest = w[1:]
    j = 0
    while j < len(rest) and rest[j][:1] in ("-", "+") and rest[j] != "-":
        if rest[j] in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            j += 2; continue
        if not rest[j].startswith("--") and "c" in rest[j][1:]:
            return j + 1 >= len(rest)       # `xargs … sh -c`: the string arrives on stdin
        j += 1
    return j >= len(rest) or rest[j] in ("-", "/dev/stdin", "/dev/fd/0")

class TooDeep(Exception):
    pass

def check_text(text, st, depth=0):
    if depth > 5:
        raise TooDeep()     # never a silent pass: the regex decides
    text, bodies = split_heredocs(text)
    toks, subs = tokens(text)
    for s in subs:
        check_text(s, st.child(), depth + 1)
    prev_sep, pipe_src = None, None
    for words, ev in segments(toks):
        if ev == "(":
            st.stack.append(st.cwd); continue
        if ev == ")":
            if st.stack:
                st.cwd = st.stack.pop()
            continue
        sep = ev[4:] if ev else None
        # a pipeline member or a backgrounded command runs in a subshell:
        # its cd does not reach the next command
        sub_shell = sep in ("|", "|&", "&") or prev_sep in ("|", "|&")
        saved = (st.cwd, st.oldpwd, list(st.dirs))
        b = os.path.basename(words[0]) if words else ""
        in_pipe = prev_sep in ("|", "|&")
        if in_pipe and pipe_src and piped_script(words):
            # `echo "git commit -am x" | bash`: the shell runs what was echoed
            check_text(echoed_text(pipe_src), st.child(), depth + 1)
        check_words(words, st, depth)
        if sub_shell:
            st.cwd, st.oldpwd, st.dirs = saved
        # the text a pipeline carries: an echo/printf at its head, passed on
        # through tee/cat members
        if not in_pipe:
            pipe_src = words if b in ("echo", "printf") else None
        elif b not in ("tee", "cat"):
            pipe_src = None
        prev_sep = sep
    for body in bodies:
        check_text(body, st.child(), depth + 1)

# the same two regexes the shell fallback uses (exported above): one source
FALLBACK_RE = re.compile(os.environ.get("PK_GUARD_LOOP_RE", "$^"))
DESTRUCTIVE_RE = re.compile(os.environ.get("PK_GUARD_DESTRUCTIVE_RE", "$^"))

CMD_TEXT[0] = cmd
try:
    check_text(cmd, State(os.path.realpath(start) if os.path.isdir(start) else start))
    print("OK")
except Deny as d:
    print("DENY\t%s\t%s" % (d.kind, d.what))
except Exception:
    # The parse failed: a narrow regex over the text — heredoc bodies no
    # shell runs (a document being written) excluded.
    try:
        text, bodies = split_heredocs(cmd)
        text = "\n".join([text] + bodies)
    except Exception:
        text = cmd
    m = DESTRUCTIVE_RE.search(text)
    if m:
        print("DENY\tdestructive\t%s" % re.sub(r"\s+", " ", m.group(2)).strip())
    elif LOOP and FALLBACK_RE.search(text):
        print("DENY\tloop\tgit (write)")
    else:
        print("OK")
GUARD_PY

_verdict="$(printf '%s' "$_payload" | PK_GUARD_LOOP="$_loop" python3 -c "$_GUARD_PY" 2>/dev/null | tail -n1)" || _verdict="FALLBACK"
case "$_verdict" in
  OK) exit 0 ;;
  DENY*)
    IFS=$'\t' read -r _ _kind _what <<<"$_verdict"
    _deny "${_kind:-loop}" "${_what:-git (write)}" ;;
  *) _fallback ;;
esac
