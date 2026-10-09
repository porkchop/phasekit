#!/usr/bin/env python3
"""phasekit's declared surface for downstream tests (v0.18.3, queue row 1194).

Two verbs, stdlib only, read-only:

  facts [--json | --path]
      The facts phasekit DECLARES for downstream tests: the `facts` section of
      this project's installed contracts/interface.json (a vendored provider
      copy under vendor/contracts/phasekit/ is the same file). A test that
      needs to know how the loop behaves reads this — never the loop's text.
      phasekit's own suite proves every fact against the loop's behaviour
      (tests/test_declared_surface.py), so a reshape that keeps the facts true
      cannot break a consumer, and one that changes a fact changes this file.
      `--path` (v0.19.2) prints the contract file's absolute path instead: the
      file $PHASEKIT_CONTRACT names inside a loop or a gate, in both layouts.

  scaffold-reads [--json] [ROOT]
      The `scaffold-reads` advisory: project test files that READ a
      scaffold-owned file (ownership `scaffold` in .scaffold/manifest.json —
      the vendored loop and scripts, the hooks, the scaffold docs) instead of
      the declared surface. Warn-only, never a refusal: it prints, and its
      JSON form is what the loop records as boundary-state.json
      `scaffold_reads`. Exit 0 always (2 only on a usage error). v0.19.2: it
      also prints the `migration-readiness` hint — the project's own code
      files (tests, scripts, source) that read an engine file by its in-tree
      path, the declared contract included, with file:line — which is what
      `phasekit migrate` refuses before its gate (JSON: `migration_reads`,
      `migration_line`; never recorded in boundary-state.json).

The rule it advises on (docs/QUALITY_GATES.md "Tests read the declared
surface"): a test reads the project's own tree and phasekit's declared surface
($PHASEKIT_CONTRACT, `phasekit facts`), never scaffold-owned files; a
fact a test needs that the surface lacks is a request to phasekit, not a
parse.
"""

import bisect
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

ADVISORY_ID = "scaffold-reads"
RECORD_FIELD = "scaffold_reads"

# The declared surface itself: reading these is exactly what a test should do.
SURFACE_PATHS = ("contracts/interface.json",)
SURFACE_PREFIXES = ("vendor/contracts/",)

# Which tracked files are tests (the hermetic-tests advisory's selection).
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|\.(test|spec)\.[A-Za-z]+$|_test\.[A-Za-z]+$"
    r"|(^|/)test_[^/]*\.py$")
SKIP_RE = re.compile(r"(^|/)(node_modules|fixtures|__fixtures__|testdata)/")
# test CODE, not the data a test reads (a captured log naming a path is not a read)
CODE_RE = re.compile(r"\.(py|ts|tsx|mts|cts|js|jsx|mjs|cjs|sh|bash|bats)$")

# A read, in a test's own words. v0.18.5: decided on the file's CODE, never on
# a line of text. The file is lexed first (code, string literals, comments,
# inert text: a JS regex literal, a shell heredoc body), so a read spelled
# INSIDE a string (a planted code sample, the fixture of what a guard must
# refuse) or a comment is never a read, and a path in an expected value or a
# list of forbidden names is data. Then a read is:
#   a call:    open(…), readFileSync(…), read_text(…), or a test's own helper
#              named for reading (`_read(p)`, `extractBlock(p, …)`,
#              `loadText(p)`, `parseLoop(p)`), written in code, with the path
#              among ITS OWN arguments, nested calls included
#              (`open(os.path.join(…))`); never a later call on the same line
#              (`forbiddenReads(t)).toEqual(['x'])`), never an element of a
#              list or map literal inside the call;
#   a member:  (…path…).read_text(), LOOP.read_text(), p.open();
#   the shell: cat / grep / sed / awk / head / tail / source / `.` … path, at a
#              command position in code (a shell test file; a `$( … )` in
#              double quotes or an unquoted heredoc is code), or as a command
#              string handed to a call that runs commands
#              (`subprocess.run(["grep", …, path])`, `execSync('cat path')`).
# The path is one whole string literal (under any directory prefix: `ROOT/`,
# `${root}/`, `../../`) or adjacent literal pieces joined by `,` `/` or `+`
# (`ROOT / "scripts" / "x.sh"`, `path.join(root, 'scripts', 'x.sh')`),
# directly or through a name bound to that path (`LOOP = ROOT / "scripts" /
# "x.sh"` … `LOOP.read_text()`). A path merely named (a docstring, a comment,
# a list of names, an expected value, a code sample in a string, argv of a
# script the test RUNS) is not a read.
# Residual, by design: an advisory errs toward silence, because every false
# positive files a fix row downstream. Not seen: a path built at run time
# from non-literal pieces, a read inside `bash -c "…"` in a shell test, a
# command list assembled before the call that runs it, a read through a
# helper whose name does not say it reads, a path passed through a second
# name, a path joined at run time or looked up in a map, JSX text, and (JS) a
# regex literal the lexer takes for a division (right after `)`). A predicate
# whose name says it reads (`isScaffoldRead('x')`) is still counted.
READ_CALL = re.compile(
    r"(?<![A-Za-z0-9_$])(?!loads\b|dumps\b)(?:open|read_text|read_bytes|readlines|"
    r"readFileSync|readFile|createReadStream|[A-Za-z_]*(?:[Rr]ead|[Ll]oad|[Pp]arse|"
    r"[Ee]xtract|[Ss]lurp)[A-Za-z_]*)\s*\(")
READ_NAMES = {"open", "read_text", "read_bytes", "readlines", "readFileSync", "readFile",
              "createReadStream"}
# the word starts the name or a `_`/camelCase part of it
READ_WORD = re.compile(r"(?:^|_)(?:read|load|parse|extract|slurp)"
                       r"|(?:^|[a-z0-9])(?:Read|Load|Parse|Extract|Slurp)")
WRITE_MODE = re.compile(r"[wax][bt+]*")
MEMBER_READ = re.compile(r"\.\s*(?:read_text|read_bytes|readlines|open)\s*\(")
SHELL_WORDS = r"(?:cat|grep|egrep|fgrep|sed|awk|head|tail|source)"
# a shell test file: the command word (or `.`, only where a command starts)
SHELL_CMD = re.compile(r"(?:(?<![^\s;&|(`])" + SHELL_WORDS
                       + r"|(?:^|(?<=[;&|(`]))[ \t]*\.)(?=[ \t])", re.M)
# any other file: a string literal that IS the command (argv[0] or a command line)
QUOTED_CMD = re.compile(r"^\s*" + SHELL_WORDS + r"(?:\s|$)")
# the call it is handed to runs commands: subprocess.run / check_output /
# Popen, os.system, execSync / execFileSync, spawnSync, …
RUNNER_RE = re.compile(r"(?i)run|exec|spawn|popen|call|system|output|shell|^sh$|^bash$")
SHELL_FILE_RE = re.compile(r"\.(sh|bash|bats)$")
ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+|readonly\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=(?!=)")
# between two literal pieces of ONE path: `,` (join args), `/` (pathlib), `+`, or nothing
JOIN_SEP = re.compile(r"\s*[,/+]?\s*")
IDENT = r"[A-Za-z0-9_$]"

CODE, STRING, COMMENT, INERT = 0, 1, 2, 3
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
                "throw", "yield", "await"}
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _tracked(root):
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def scaffold_owned(root):
    """The scaffold-owned paths the manifest records, minus the declared surface."""
    try:
        text = (Path(root) / ".scaffold" / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(text)
    except (OSError, ValueError):
        return []
    files = manifest.get("files") if isinstance(manifest, dict) else None
    out = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or f.get("ownership") != "scaffold":
            continue
        p = f.get("path")
        if isinstance(p, str) and p not in SURFACE_PATHS and not p.startswith(SURFACE_PREFIXES):
            out.append(p)
    return sorted(set(out))


class _Lexed:
    """One test file, lexed for its language ("py", "sh", else JS/TS).

    `kind[i]` says what character i is: CODE, STRING (a literal, its quotes
    included), COMMENT, or INERT (a JS regex literal, a shell heredoc body).
    `strings` are the literals written in code, in order, as (start, end,
    content); `close[o]` is where the bracket opened at code position o
    closes (the end of the text when it never does), `parent[o]` the bracket
    around it (-1 at top level); `inner[s]` is the bracket around literal s."""

    def __init__(self, text, lang):
        self.text, self.lang = text, lang
        self.kind = bytearray(len(text))
        self.strings, self.inner = [], {}
        self.close, self.parent = {}, {}
        self.commas = set()  # brackets with a comma directly inside: a tuple, a list, args
        self._open = []
        {"py": self._lex_py, "sh": self._lex_sh}.get(lang, self._lex_js)()
        for o in self._open:
            self.close[o] = len(text)
        self.strings.sort()
        self.opens = sorted(self.close)
        self.starts = [s[0] for s in self.strings]
        self.ends = {s[1]: s for s in self.strings}

    # -- marking ----------------------------------------------------------
    def _mark(self, a, b, k):
        self.kind[a:b] = bytes([k]) * (b - a)

    def _bracket(self, i):
        c = self.text[i]
        if c in "([{":
            self.parent[i] = self._open[-1] if self._open else -1
            self._open.append(i)
        elif c in ")]}" and self._open:
            self.close[self._open.pop()] = i
        elif c == "," and self._open:
            self.commas.add(self._open[-1])

    def _to_eol(self, i):
        j = self.text.find("\n", i)
        return len(self.text) if j < 0 else j

    def _literal(self, i, j, content):
        self.strings.append((i, j, content))
        self.inner[(i, j, content)] = self._open[-1] if self._open else -1

    def _string(self, i, q, multiline, escapes=True):
        """The literal opened by quote `q` at i; returns its end."""
        t, n = self.text, len(self.text)
        j, closed = i + len(q), False
        while j < n:
            if escapes and t[j] == "\\":
                j += 2
                continue
            if t.startswith(q, j):
                j += len(q)
                closed = True
                break
            if t[j] == "\n" and not multiline:
                break
            j += 1
        j = min(j, n)
        self._mark(i, j, STRING)
        self._literal(i, j, t[i + len(q):j - len(q) if closed else j])
        return j

    # -- Python -----------------------------------------------------------
    def _lex_py(self):
        t, n, i = self.text, len(self.text), 0
        while i < n:
            c = t[i]
            if c == "#":
                j = self._to_eol(i)
                self._mark(i, j, COMMENT)
                i = j
            elif c in "'\"":
                q = t[i:i + 3] if t[i:i + 3] in ("'''", '"""') else c
                i = self._string(i, q, len(q) == 3)
            else:
                self._bracket(i)
                i += 1

    # -- JS / TS ----------------------------------------------------------
    def _lex_js(self):
        self._js_code(0, False)

    def _regex_end(self, i):
        """The end of a regex literal opened at i, or 0 when it is none."""
        t, n = self.text, len(self.text)
        j, in_class = i + 1, False
        while j < n and t[j] != "\n":
            c = t[j]
            if c == "\\":
                j += 2
                continue
            if in_class:
                in_class = c != "]"
            elif c == "[":
                in_class = True
            elif c == "/":
                j += 1
                while j < n and t[j].isalpha():
                    j += 1
                return j
            j += 1
        return 0

    def _js_code(self, i, in_template):
        """Code from i; inside a template's `${…}`, up to and past its `}`."""
        t, n = self.text, len(self.text)
        depth, prev, word, gap = 0, "", "", False
        while i < n:
            c, nx = t[i], t[i + 1:i + 2]
            if c == "/" and nx == "/":
                j = self._to_eol(i)
                self._mark(i, j, COMMENT)
                i = j
                continue
            if c == "/" and nx == "*":
                j = t.find("*/", i + 2)
                j = n if j < 0 else j + 2
                self._mark(i, j, COMMENT)
                i = j
                continue
            if c == "/" and (prev == "" or prev in _REGEX_PREV or word in _REGEX_WORDS):
                j = self._regex_end(i)
                if j:
                    self._mark(i, j, INERT)
                    i, prev, word = j, "x", ""
                    continue
            if c in "'\"`":
                i = self._string(i, c, False) if c != "`" else self._js_template(i)
                prev, word = "x", ""
                continue
            if in_template and c == "}" and depth == 0:
                self._mark(i, i + 1, STRING)
                return i + 1
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            self._bracket(i)
            if c.isspace():
                gap = True
            else:
                ident = c.isalnum() or c in "_$"
                word = (word + c if word and not gap else c) if ident else ""
                prev, gap = c, False
            i += 1
        return n

    def _js_template(self, i):
        t, n = self.text, len(self.text)
        j, seg = i + 1, i
        while j < n:
            c = t[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                j += 1
                break
            if c == "$" and t[j + 1:j + 2] == "{":
                self._mark(seg, j + 2, STRING)
                j = seg = self._js_code(j + 2, True)
                continue
            j += 1
        j = min(j, n)
        self._mark(seg, j, STRING)
        self._literal(i, j, t[i + 1:j - 1 if t[j - 1:j] == "`" and j - 1 > i else j])
        return j

    # -- shell ------------------------------------------------------------
    def _lex_sh(self):
        self._sh_code(0, False)

    def _sh_code(self, i, in_subst):
        """Shell code from i; inside a `$( … )`, up to and past its `)`. A
        substitution is code wherever it stands — in double quotes, in an
        unquoted heredoc body — because the shell runs it."""
        t, n = self.text, len(self.text)
        pending = []  # heredocs opened on the current line: (strip tabs, quoted, delimiter)
        depth = 0
        while i < n:
            c = t[i]
            if c == "\n" and pending:
                i = self._heredoc_bodies(i + 1, pending)
                pending = []
            elif c == "#" and (i == 0 or t[i - 1] in " \t\n;&|()"):
                j = self._to_eol(i)
                self._mark(i, j, COMMENT)
                i = j
            elif c == "\\":
                i += 2
            elif c == "$" and t[i + 1:i + 2] == "(":
                i = self._sh_code(i + 2, True)
            elif c == '"':
                i = self._sh_dquote(i)
            elif c == "'":
                # $'…' (ANSI-C quoting) takes backslash escapes; '…' does not
                i = self._string(i, c, True, escapes=i > 0 and t[i - 1] == "$")
            elif t.startswith("<<", i) and not t.startswith("<<<", i) and _HEREDOC.match(t, i):
                m = _HEREDOC.match(t, i)
                pending.append((m.group(1) == "-", bool(m.group(2)), m.group(3)))
                i = m.end()
            else:
                if in_subst and c == ")" and depth == 0:
                    return i + 1
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                self._bracket(i)
                i += 1
        return n

    def _sh_dquote(self, i):
        """A double-quoted word from i: string, except its `$( … )`."""
        t, n = self.text, len(self.text)
        j, seg = i + 1, i
        while j < n:
            c = t[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                j += 1
                break
            if c == "$" and t[j + 1:j + 2] == "(":
                self._mark(seg, j + 2, STRING)
                j = seg = self._sh_code(j + 2, True)
                continue
            j += 1
        j = min(j, n)
        self._mark(seg, j, STRING)
        self._literal(i, j, t[i + 1:j - 1 if t[j - 1:j] == '"' and j - 1 > i else j])
        return j

    def _heredoc_bodies(self, i, pending):
        t, n = self.text, len(self.text)
        for strip, quoted, delim in pending:
            start = i
            while i < n:
                j = self._to_eol(i)
                line = t[i:j]
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                i = j + 1
            end = min(i, n)
            # the body is text, except an unquoted body's substitutions, which run
            seg, k = start, start
            while not quoted and k < end:
                k = t.find("$(", k, end)
                if k < 0:
                    break
                self._mark(seg, k, INERT)
                seg = k = min(self._sh_code(k + 2, True), end)
            self._mark(seg, end, INERT)
            if i < n:
                i = self._to_eol(i) + 1
        return min(i, n)

    # -- queries ----------------------------------------------------------
    def code_at(self, i):
        return 0 <= i < len(self.kind) and self.kind[i] == CODE

    def transparent(self, e):
        """Whether a literal inside the bracket at e is still an argument: a
        call's parentheses or a grouping `( … )`. A list, a map, a tuple
        (a parenthesis with a comma that no call precedes) is data."""
        if self.text[e] != "(":
            return False
        return e not in self.commas or (e > 0 and re.match(r"[A-Za-z0-9_$)\]]", self.text[e - 1]))

    def args_only(self, o, e):
        """From the bracket e up to the bracket o, only calls and groupings."""
        while e not in (o, -1):
            if not self.transparent(e):
                return False
            e = self.parent.get(e, -1)
        return e == o

    def literals(self, a, b):
        """The literals that start in [a, b)."""
        return self.strings[bisect.bisect_left(self.starts, a):bisect.bisect_left(self.starts, b)]

    def callee(self, o):
        """The name called by the parenthesis at o (`subprocess.run(` → run)."""
        k = o - 1
        while k >= 0 and self.text[k] in " \t":
            k -= 1
        j = k
        while j >= 0 and re.match(IDENT, self.text[j]):
            j -= 1
        return self.text[j + 1:k + 1]

    def innermost(self, pos):
        """The innermost bracket open around code position pos, or -1: the
        last bracket opened before pos, or the first of its ancestors still
        open at pos (any bracket around pos also encloses that one)."""
        k = bisect.bisect_left(self.opens, pos) - 1
        e = self.opens[k] if k >= 0 else -1
        while e != -1 and self.close[e] <= pos:
            e = self.parent.get(e, -1)
        return e

    def idents(self, name, a, b):
        """Code positions in [a, b) where the identifier `name` stands."""
        pat = re.compile(r"(?<!" + IDENT + r")" + re.escape(name) + r"(?!" + IDENT + r")")
        return [m.start() for m in pat.finditer(self.text, a, b) if self.code_at(m.start())]


def _q(s):
    return r"""["'`]""" + re.escape(s) + r"""["'`]"""


def _refs(path):
    """A shell test's spelling of `path`: the literal under any prefix (`$ROOT/`,
    `../`), or its pieces quoted and joined."""
    parts = path.split("/")
    alts = [r"(?<![A-Za-z0-9_.-])" + re.escape(path) + r"(?![A-Za-z0-9_.-])"]
    if len(parts) > 1:
        alts.append(r"\s*(?:/|,)\s*".join(_q(p) for p in parts))
        alts.append(_q("/".join(parts[:-1])) + r"\s*(?:/|,)\s*" + _q(parts[-1]))
    return "(?:" + "|".join(alts) + ")"


def _names_path(s, path):
    """Whether a spelling IS `path`: the whole value, under a directory
    prefix at most — never a fragment of longer text."""
    return s == path or s.endswith("/" + path)


MAX_PIECES = 8  # a path joined from more literal pieces than this is not looked for


def _spellings(lx, toks, last=None):
    """Every path `toks` spell: one literal, or adjacent literals with only a
    joiner between them (when `last` is given, only spellings ending there).
    Text with whitespace in it is never a path."""
    out, run = set(), []
    for tk in toks:
        if run and not JOIN_SEP.fullmatch(lx.text, run[-1][1], tk[0]):
            run = []
        run.append(tk)
        if last is not None and tk != last:
            continue
        joined = ""
        for piece in reversed(run[-MAX_PIECES:]):
            joined = piece[2] + ("/" + joined if joined else "")
            s = re.sub(r"/+", "/", joined)
            if not re.search(r"\s", s):
                out.add(s)
    return out


def _sites(lx):
    """Every place in this file that reads something, independent of the
    path: (kind, spellings of literal paths it reads, (a, b, args_from) where
    a bound name would be read, or a member's receiver name, the offset where
    the read is written)."""
    t, sites = lx.text, []
    for m in READ_CALL.finditer(t):
        o = m.end() - 1
        # a capitalised callee constructs a value (`ScaffoldRead(t, p)`); it reads
        # nothing, and nor does a name that only contains the letters
        # (`already_installed`, `download`)
        name = m.group(0).rstrip("( \t")
        if not (lx.code_at(m.start()) and lx.code_at(o) and not name[0].isupper()
                and (name in READ_NAMES or READ_WORD.search(name))):
            continue
        c = lx.close.get(o, len(t))
        toks = [tk for tk in lx.literals(o + 1, c) if lx.args_only(o, lx.inner[tk])]
        # open(…, "w") writes
        if not (name == "open" and any(WRITE_MODE.fullmatch(tk[2]) for tk in toks)):
            sites.append(("call", _spellings(lx, toks), (o, c), m.start()))
    for m in MEMBER_READ.finditer(t):
        if not lx.code_at(m.start()):
            continue
        k = m.start() - 1
        while k >= 0 and (t[k] in " \t\n" or (t[k] == ")" and lx.code_at(k))):
            k -= 1
        if k >= 0 and k + 1 in lx.ends and lx.kind[k] == STRING:
            tk = lx.ends[k + 1]
            before = lx.literals(0, k + 1)[-MAX_PIECES:]
            sites.append(("member", _spellings(lx, before, last=tk), None, m.start()))
        elif k >= 0 and re.match(IDENT, t[k]) and lx.code_at(k):
            j = k
            while j >= 0 and re.match(IDENT, t[j]):
                j -= 1
            sites.append(("member", set(), t[j + 1:k + 1], m.start()))
    if lx.lang == "sh":
        for m in SHELL_CMD.finditer(t):
            if not lx.code_at(m.end() - 1):
                continue
            b = m.end()
            while b < len(t) and not (t[b] in "|;&\n" and lx.code_at(b)):
                b += 1
            sites.append(("shell", set(), (m.end(), b), m.start()))
        return sites
    for tk in lx.strings:
        if not QUOTED_CMD.match(tk[2]):
            continue
        e = lx.inner[tk]
        while e != -1 and lx.text[e] != "(":
            e = lx.parent.get(e, -1)
        if e == -1 or not RUNNER_RE.search(lx.callee(e)):
            continue
        if not re.fullmatch(r"\s*" + SHELL_WORDS + r"\s*", tk[2]):
            sites.append(("command-line", set(), tk[2], tk[0]))
        else:
            # argv: the rest of the bracket the command word sits in
            end = lx.close.get(lx.inner[tk], len(t))
            sites.append(("argv", _spellings(lx, lx.literals(tk[1], end)), (tk[1], end), tk[0]))
    return sites


def _site_reads(lx, site, path=None, name=None):
    """Whether one read site reads `path` (a literal spelling) or `name` (a bound name)."""
    kind, spelled, where = site[:3]
    if path is not None and any(_names_path(s, path) for s in spelled):
        return True
    t = lx.text
    if kind == "call" and name is not None:
        o, c = where
        return any(lx.args_only(o, lx.innermost(p)) for p in lx.idents(name, o + 1, c))
    if kind == "member":
        return name is not None and where == name
    if kind == "shell":
        a, b = where
        if path is not None:
            return any(lx.kind[m.start()] in (CODE, STRING)
                       for m in re.compile(_refs(path)).finditer(t, a, b))
        return re.search(r"\$\{?" + re.escape(name) + r"(?!" + IDENT + r")", t[a:b]) is not None
    if kind == "command-line":
        return path is not None and re.search(_refs(path), where) is not None
    if kind == "argv" and name is not None:
        return bool(lx.idents(name, *where))
    return False


def _bindings(lx, paths):
    """{name: path} for each name bound, in code, to a path (never to a list or map)."""
    names, off = {}, 0
    for line in lx.text.split("\n"):
        m = ASSIGN_RE.match(line)
        rhs = line[m.end():].lstrip() if m else ""
        if m and lx.code_at(off + m.start(1)) and not rhs.startswith(("[", "{")):
            a = off + m.end()
            toks = [tk for tk in lx.literals(a, off + len(line))
                    if _inside_from(lx, a, lx.inner[tk])]
            spelled = _spellings(lx, toks)
            rhs_text = line[m.end():]
            for p in paths:
                # a shell binding may be an unquoted word: LOOP=$ROOT/scripts/x.sh
                if any(_names_path(s, p) for s in spelled) or (
                        lx.lang == "sh" and re.search(_refs(p), rhs_text)):
                    names[m.group(1)] = p
                    break
        off += len(line) + 1
    return names


def _inside_from(lx, a, e):
    """Whether every bracket from e outwards that opened at or after a is a call or a grouping."""
    while e != -1 and e >= a:
        if not lx.transparent(e):
            return False
        e = lx.parent.get(e, -1)
    return True


def _lang(rel):
    if SHELL_FILE_RE.search(rel):
        return "sh"
    return "py" if rel.endswith(".py") else "js"


def _reads_at(text, paths, lang="js", names=None):
    """{path: [line, …]} for each of `paths` this code reads — directly, or
    through a name bound to the path (`LOOP = ROOT / "scripts" / "x.sh"` …
    `LOOP.read_text()`); `names` adds bindings made elsewhere ({name: path},
    a name this file imports from another). Lines are where the read is
    written (1-based), sorted."""
    lx = _Lexed(text, lang)
    sites = _sites(lx)
    found = {}

    def hit(p, site):
        found.setdefault(p, set()).add(text.count("\n", 0, site[3]) + 1)

    for p in paths:
        for site in sites:
            if _site_reads(lx, site, path=p):
                hit(p, site)
    bound = _bindings(lx, list(paths))
    for name, p in {**(names or {}), **bound}.items():
        for site in sites:
            if _site_reads(lx, site, name=name):
                hit(p, site)
    return {p: sorted(lines) for p, lines in found.items()}


def _reads_in(text, paths, lang="js"):
    """Which of `paths` this test text reads (see _reads_at)."""
    return sorted(_reads_at(text, paths, lang))


def scaffold_reads(root):
    """[{"test": <test file>, "paths": [<scaffold-owned paths it reads>]}], sorted."""
    root = Path(root)
    paths = scaffold_owned(root)
    if not paths:
        return []
    out = []
    for rel in _tracked(root):
        if rel in paths or SKIP_RE.search(rel):
            continue
        if not (TEST_FILE_RE.search(rel) and CODE_RE.search(rel)):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # cheap prefilter: only the paths whose file name the text mentions
        cand = [p for p in paths if p.rsplit("/", 1)[-1] in text]
        hit = _reads_in(text, cand, _lang(rel)) if cand else []
        if hit:
            out.append({"test": rel, "paths": hit})
    return sorted(out, key=lambda e: e["test"])


# --- migration readiness (v0.19.2) ---------------------------------------------
# The same reads, asked the other way round: which of the PROJECT's own code
# files (tests, scripts, source) read an engine file by its in-tree path, the
# declared contract included. In the vendored layout that works; in the pinned
# layout those paths are not in the project, so `phasekit migrate` refuses
# them before its gate (the pre-flight) and the scaffold-reads advisory names
# them as a hint. The remedy for the contract is the exported path,
# $PHASEKIT_CONTRACT (`phasekit facts --path` by hand); for any other engine
# file, the fact it carries (docs/QUALITY_GATES.md "Tests read the declared
# surface"). The lexer above decides read vs literal, so a path in prose, a
# comment, a planted sample or a fixture is never named.
MIGRATION_ID = "migration-readiness"
MIGRATION_FIELD = "migration_reads"
MANIFEST_PATH = ".scaffold/manifest.json"  # read for the engine paths; never itself one
# A binding another file can import: `export const NAME =` (JS/TS), a
# module-level `NAME =` (Python).
EXPORT_JS = re.compile(r"^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.M)
EXPORT_PY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?=(?!=)", re.M)
IMPORT_JS = re.compile(r"""\bimport\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""")
IMPORT_PY = re.compile(r"^[ \t]*from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n]+)", re.M)
JS_EXT = re.compile(r"\.(?:[cm]?[jt]sx?)$")


def _module_of(rel):
    """A code file as a module key: its path without the extension."""
    return rel[:-3] if rel.endswith(".py") else JS_EXT.sub("", rel)


def _names_module(rel, lang, spec, exporter):
    """Whether an import of `spec` written in file `rel` names the module of
    file `exporter`: a relative JS specifier resolved against the importing
    file (or its /index); a Python module, relative (leading dots) or absolute
    (a file whose path ends with it). A bare JS package name never does."""
    key = _module_of(exporter)
    here = posixpath.dirname(rel)
    if lang == "py":
        tail = spec.lstrip(".").replace(".", "/")
        dots = len(spec) - len(spec.lstrip("."))
        if dots:
            base = here
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            want = posixpath.normpath(posixpath.join(base, tail)) if tail else base
            return key in (want, want + "/__init__")
        return any(key == t or key.endswith("/" + t) for t in (tail, tail + "/__init__"))
    if not spec.startswith("."):
        return False
    want = _module_of(posixpath.normpath(posixpath.join(here, spec)))
    return key in (want, want + "/index")


def engine_owned(root):
    """Every engine path a vendored project carries (manifest `scaffold`
    class, the declared contract included); [] for a project with no manifest.
    The manifest itself is left out on purpose: a supervisor's tests read
    OTHER projects' manifests in fixture repos under the same name (the
    foundry-orchestrator scan, 2026-10-08: 31 such reads, none of its own),
    and a project reading its own is caught by the migration's gate."""
    try:
        manifest = json.loads((Path(root) / MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = manifest.get("files") if isinstance(manifest, dict) else None
    out = set()
    for f in files if isinstance(files, list) else []:
        if (isinstance(f, dict) and f.get("ownership") == "scaffold"
                and isinstance(f.get("path"), str)):
            out.add(f["path"])
    return sorted(out)


def _exported(text, lang, paths):
    """{name: path} for each binding of an engine path another file can import."""
    lx = _Lexed(text, lang)
    bound = _bindings(lx, paths)
    if not bound:
        return {}
    rx = EXPORT_PY if lang == "py" else EXPORT_JS if lang == "js" else None
    if rx is None:
        return {}
    names = {m.group(1) for m in rx.finditer(text) if lx.code_at(m.start(1))}
    return {n: p for n, p in bound.items() if n in names}


def _imported(text, lang, rel, exported):
    """{local name: path} for each engine-path binding this file imports by
    name FROM the module that exports it (`exported`: {(file, name): path})."""
    rx = IMPORT_PY if lang == "py" else IMPORT_JS if lang == "js" else None
    if rx is None:
        return {}
    matches = list(rx.finditer(text))
    if not matches:
        return {}
    lx = _Lexed(text, lang)
    out = {}
    for m in matches:
        if not lx.code_at(m.start()):
            continue
        spec, names = (m.group(1), m.group(2)) if lang == "py" else (m.group(2), m.group(1))
        for piece in names.strip("()").split(","):
            words = piece.split()
            if words[:1] == ["type"]:
                words = words[1:]
            if not words:
                continue
            orig, local = words[0], (words[2] if len(words) >= 3 and words[1] == "as" else words[0])
            for (exporter, name), p in exported.items():
                if name == orig and _names_module(rel, lang, spec, exporter):
                    out[local] = p
    return out


def migration_reads(root, paths=None):
    """[{"file": <tracked code file>, "reads": [{"path": <engine path>, "line": n}]}],
    sorted: the project's own code that reads an engine file by its in-tree path."""
    root = Path(root)
    paths = engine_owned(root) if paths is None else sorted(set(paths))
    if not paths:
        return []
    owned = set(paths)
    files = []
    for rel in _tracked(root):
        if (rel in owned or rel.startswith(".scaffold/") or SKIP_RE.search(rel)
                or not CODE_RE.search(rel)):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append((rel, text, _lang(rel), [p for p in paths if p.rsplit("/", 1)[-1] in text]))
    exported = {}  # {(exporting file, name): path}
    for rel, text, lang, cand in files:
        if cand:
            for name, p in _exported(text, lang, cand).items():
                exported[(rel, name)] = p
    out = []
    for rel, text, lang, cand in files:
        names = _imported(text, lang, rel, exported) if exported else {}
        if not cand and not names:
            continue
        at = _reads_at(text, cand, lang, names)
        if at:
            reads = [{"path": p, "line": n} for p, lines in at.items() for n in lines]
            out.append({"file": rel, "reads": sorted(reads, key=lambda r: (r["line"], r["path"]))})
    return sorted(out, key=lambda e: e["file"])


def migration_lines(reads):
    """`file:line: path`, one per read."""
    return [f"{e['file']}:{r['line']}: {r['path']}" for e in reads for r in e["reads"]]


REMEDY = ("read the declared contract through $PHASEKIT_CONTRACT (its absolute path, exported "
          "by the loop, its gate, `phasekit verify` and the upgrade gate in both layouts; "
          "`phasekit facts --path` when a test is run by hand) and any other engine fact "
          "through that contract, never an engine file — docs/QUALITY_GATES.md \"Tests read "
          "the declared surface\"")


def migration_line(reads):
    """The migration-readiness hint (empty when there is nothing to say)."""
    if not reads:
        return ""
    names = [e["file"] for e in reads]
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    return (f"ADVISORY {MIGRATION_ID}: {len(names)} file(s) read engine files by their in-tree "
            f"path ({shown}) — a pinned project carries no engine file, so `phasekit migrate` "
            f"refuses them: {REMEDY}. Advisory only: the gate is unchanged.")


def advisory_line(reads):
    """The one-line named advisory (empty when there is nothing to say)."""
    if not reads:
        return ""
    names = [e["test"] for e in reads]
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    return (f"ADVISORY {ADVISORY_ID}: {len(names)} test file(s) read scaffold-owned files "
            f"({shown}) — a test reads the project's own tree and phasekit's declared surface "
            "(contracts/interface.json `facts`, `phasekit facts --json`), never the vendored "
            "loop, scripts, hooks or scaffold docs; a fact the surface lacks is a request to "
            "phasekit. See docs/QUALITY_GATES.md \"Tests read the declared surface\". Advisory "
            "only: the gate is unchanged.")


def _contract(root):
    """(path, contract) of THIS project's phasekit contract, or (None, None)."""
    candidates = [Path(root) / rel for rel in
                  ("contracts/interface.json", "vendor/contracts/phasekit/interface.json")]
    if (Path(root) / ".phasekit-version").is_file():
        # v0.19.0: a pinned project carries no contract of its own; the facts
        # are this engine's (scripts/phasekit.sh runs the PINNED engine's copy)
        candidates = [Path(__file__).resolve().parent.parent / "contracts" / "interface.json"]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("interface") != "phasekit":
            continue
        if isinstance(data.get("facts"), dict):
            return p.resolve(), data
    return None, None


def _top(root):
    """The project's top level: the nearest directory at or above `root` that
    holds `.git` (a directory, or a worktree's file); `root` itself when none.
    Found on disk, not by asking git: a downstream tree may pin that it has
    exactly one "which repository is this" rule (foundry-orchestrator does)."""
    start = Path(root).resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    as_path = "--path" in rest
    rest = [a for a in rest if a not in ("--json", "--path")]
    root = Path(rest[0]) if rest else Path.cwd()
    if verb == "facts":
        # the facts of THIS project's phasekit (its installed contract, found
        # from any subdirectory) — never a newer install's, which may not
        # describe the loop this project vendors
        path, data = _contract(_top(root))
        if data is None:
            print("phasekit facts: no contracts/interface.json with a `facts` section here "
                  "(phasekit v0.18.3 or later installs it)", file=sys.stderr)
            return 1
        facts = data["facts"]
        if as_path:
            # v0.19.2: the file itself — what $PHASEKIT_CONTRACT names inside a
            # loop or a gate — for a test run by hand
            print(path)
        elif as_json:
            print(json.dumps(facts, indent=2, sort_keys=True))
        else:
            for name in sorted(k for k in facts if not k.startswith("_")):
                fact = facts[name]
                print(f"{name}: {fact.get('summary', '') if isinstance(fact, dict) else fact}")
        return 0
    if verb == "scaffold-reads":
        # the project's top level, from any subdirectory (never a silent all-clear)
        top = _top(root)
        reads = scaffold_reads(top)
        try:
            # the hint is isolated: it never costs the scaffold_reads record
            mreads = migration_reads(top)
        except Exception:  # noqa: BLE001 - an advisory never fails
            mreads = []
        if as_json:
            record = {"advisory": ADVISORY_ID, RECORD_FIELD: reads, "line": advisory_line(reads),
                      MIGRATION_FIELD: mreads, "migration_line": migration_line(mreads)}
            print(json.dumps(record, sort_keys=True))
        else:
            line = advisory_line(reads)
            if line:
                print(line)
                for e in reads:
                    print(f"  {e['test']}: {', '.join(e['paths'])}")
            line = migration_line(mreads)
            if line:
                print(line)
                for entry in migration_lines(mreads):
                    print(f"  {entry}")
        return 0
    print(f"phasekit-surface: unknown verb {verb!r} (facts | scaffold-reads)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
