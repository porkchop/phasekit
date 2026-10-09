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
      `migration_line`; never recorded in boundary-state.json). v0.19.3 (docs/
      QUALITY_GATES.md "A project's tests test the project"): `scaffold_reads`
      also names test files whose only subject is phasekit (they read the
      contract or `phasekit facts` and no project code), and the JSON carries
      the `process-reads` advisory (`process_reads`, `process_line`: test files
      that read a process document — SPEC, PHASES, LEARNINGS, the deferral
      ledger, records under artifacts/), which the loop records in
      boundary-state.json. `phasekit check` adds `criterion-suites`.

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
import time
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


class Family(str):
    """A family of paths, matched by a regex (v0.19.3): `docs/PHASES*.md`,
    everything under `artifacts/iterations/`. Its value is the label the
    advisory names; `rx` is the path family (no anchors), `hint` a word its
    spelling must contain (the cheap prefilter). Wherever the lexer compares
    a spelling with an exact path, a Family compares it with its regex, under
    a directory prefix at most — the same rule (`_names_path`)."""

    def __new__(cls, label, rx, hint):
        self = super().__new__(cls, label)
        self.rx, self.hint = rx, hint
        self.whole = re.compile(r"(?:.*/)?(?:" + rx + r")")
        return self


def _refs(path):
    """A shell test's spelling of `path`: the literal under any prefix (`$ROOT/`,
    `../`), or its pieces quoted and joined."""
    if isinstance(path, Family):
        return r"(?<![A-Za-z0-9_.-])(?:" + path.rx + r")(?![A-Za-z0-9_.-])"
    parts = path.split("/")
    alts = [r"(?<![A-Za-z0-9_.-])" + re.escape(path) + r"(?![A-Za-z0-9_.-])"]
    if len(parts) > 1:
        alts.append(r"\s*(?:/|,)\s*".join(_q(p) for p in parts))
        alts.append(_q("/".join(parts[:-1])) + r"\s*(?:/|,)\s*" + _q(parts[-1]))
    return "(?:" + "|".join(alts) + ")"


def _names_path(s, path):
    """Whether a spelling IS `path`: the whole value, under a directory
    prefix at most — never a fragment of longer text."""
    if isinstance(path, Family):
        return path.whole.fullmatch(s) is not None
    return s == path or s.endswith("/" + path)


MAX_PIECES = 8  # a path joined from more literal pieces than this is not looked for


def _spellings(lx, toks, last=None):
    """Every path `toks` spell: one literal, or adjacent literals with only a
    joiner between them (when `last` is given, only spellings ending there).
    Text with whitespace in it is never a path. {spelling: its first piece}."""
    out, run = {}, []
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
                out.setdefault(s, piece)
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
            sites.append(("member", {}, t[j + 1:k + 1], m.start()))
    if lx.lang == "sh":
        for m in SHELL_CMD.finditer(t):
            if not lx.code_at(m.end() - 1):
                continue
            b = m.end()
            while b < len(t) and not (t[b] in "|;&\n" and lx.code_at(b)):
                b += 1
            sites.append(("shell", {}, (m.end(), b), m.start()))
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
            sites.append(("command-line", {}, tk[2], tk[0]))
        else:
            # argv: the rest of the bracket the command word sits in
            end = lx.close.get(lx.inner[tk], len(t))
            sites.append(("argv", _spellings(lx, lx.literals(tk[1], end)), (tk[1], end), tk[0]))
    return sites


def _site_reads(lx, site, path=None, name=None, own=None):
    """Whether one read site reads `path` (a literal spelling) or `name` (a bound name).
    `own`, when given (v0.19.3), must also accept where the path is rooted:
    `own(lx, piece, spelling, path)` for a literal spelling, `own(lx, None, None,
    None, at=offset)` for a bound name used at `at` (THIS project, not a
    fixture tree), and `own(lx, None, text_before, path, shell=True, at=offset)`
    for a shell spelling."""
    kind, spelled, where = site[:3]
    if path is not None and any(_names_path(s, path) and (own is None or own(lx, piece, s, path))
                                for s, piece in spelled.items()):
        return True
    t = lx.text
    if kind == "call" and name is not None:
        o, c = where
        return any(lx.args_only(o, lx.innermost(p))
                   and (own is None or own(lx, None, None, None, at=p))
                   for p in lx.idents(name, o + 1, c))
    if kind == "member":
        if name is None or where != name:
            return False
        return own is None or own(lx, None, None, None, at=t.rfind(name, 0, site[3]))
    if kind == "shell":
        a, b = where
        if path is not None:
            return any(lx.kind[m.start()] in (CODE, STRING)
                       and (own is None or own(lx, None, t[max(0, m.start() - 200):m.start()], path,
                                               shell=True, at=m.start()))
                       for m in re.compile(_refs(path)).finditer(t, a, b))
        var = re.compile(r"\$\{?" + re.escape(name) + r"(?!" + IDENT + r")")
        return any(own is None or own(lx, None, t[max(0, m.start() - 200):m.start()], None,
                                      shell=True, at=m.start())
                   for m in var.finditer(t, a, b))
    if kind == "command-line":
        return path is not None and any(
            own is None or own(lx, None, where[:m.start()], path, shell=True, at=site[3])
            for m in re.compile(_refs(path)).finditer(where))
    if kind == "argv" and name is not None:
        return bool(lx.idents(name, *where))
    return False


def _bindings(lx, paths, own=None):
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
                if any(_names_path(s, p) and (own is None or own(lx, piece, s, p))
                       for s, piece in spelled.items()) or (
                        lx.lang == "sh" and any(
                            own is None or own(lx, None, rhs_text[:r.start()], p, shell=True, at=a)
                            for r in re.finditer(_refs(p), rhs_text))):
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


def _reads_at(text, paths, lang="js", names=None, own=None):
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
            if _site_reads(lx, site, path=p, own=own):
                hit(p, site)
    bound = _bindings(lx, list(paths), own)
    for name, p in {**(names or {}), **bound}.items():
        for site in sites:
            if _site_reads(lx, site, name=name, own=own):
                hit(p, site)
    return {p: sorted(lines) for p, lines in found.items()}


def _reads_in(text, paths, lang="js", own=None):
    """Which of `paths` this test text reads (see _reads_at)."""
    return sorted(_reads_at(text, paths, lang, own=own))


# v0.19.3: the process-reads and phasekit-only advisories name test files by
# their NAME (a helper module under tests/ is support code; "belongs upstream"
# is the wrong advice for it); scaffold-reads keeps the path selection.
TEST_NAME_RE = re.compile(r"\.(?:test|spec)\.[A-Za-z]+$|_test\.[A-Za-z]+$|(?:^|/)test_[^/]*\.py$"
                          r"|\.(?:sh|bash|bats)$")  # a shell file under a test path is a test


def _test_files(root, tracked, skip=(), named=False):
    """(rel, text) for each tracked test CODE file (fixtures and node_modules
    excluded), in tracked order; `named`: only files whose name says test."""
    for rel in tracked:
        if rel in skip or SKIP_RE.search(rel):
            continue
        if not (TEST_FILE_RE.search(rel) and CODE_RE.search(rel)):
            continue
        if named and not TEST_NAME_RE.search(rel):
            continue
        try:
            yield rel, (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue


def scaffold_reads(root):
    """[{"test": <test file>, "paths": [<what it reads>]}], sorted: test files
    that read a scaffold-owned file, and (v0.19.3) test files whose only
    subject is phasekit — they read its contract or its facts and touch none
    of the project's own code (`phasekit_only_reads`)."""
    root = Path(root)
    paths = scaffold_owned(root)
    tracked = _tracked(root)
    found = {}
    for rel, text in (_test_files(root, tracked, set(paths)) if paths else ()):
        # cheap prefilter: only the paths whose file name the text mentions
        cand = [p for p in paths if p.rsplit("/", 1)[-1] in text]
        hit = _reads_in(text, cand, _lang(rel)) if cand else []
        if hit:
            found[rel] = set(hit)
    try:
        for e in phasekit_only_reads(root, tracked, time.monotonic() + SCAN_BUDGET_S):
            found.setdefault(e["test"], set()).update(e["paths"])
    except Exception:  # noqa: BLE001 - the widening never costs the scaffold-owned reads
        pass
    return [{"test": rel, "paths": sorted(found[rel])} for rel in sorted(found)]


# --- phasekit as a test's only subject (v0.19.3) ---------------------------------
# docs/QUALITY_GATES.md "A project's tests test the project": phasekit's suite
# proves phasekit; a project test reads phasekit's declared surface only where
# the project's OWN code consumes it (a supervisor checking that a name its
# code reads is declared). A test file that reads the contract or the facts and
# touches none of the project's own code — it imports no project module and
# names no project file — has phasekit as its only subject: it belongs
# upstream. Named in `scaffold_reads` with what it reads:
ONLY_ENV_SPELLING = "$PHASEKIT_CONTRACT"    # the exported path (env, any spelling)
ONLY_CLI_SPELLING = "phasekit facts"        # the CLI, as argv or a command line
CONTRACT_PATH = "contracts/interface.json"  # the in-tree copy (a vendored project's)
# A supervisor's vendored provider copy (vendor/contracts/<provider>/) is the
# project's own file and is never one of these.
_ENV_NAME = "PHASEKIT_CONTRACT"
_FACTS_CMD = re.compile(r"(?:^|[\s/])phasekit(?:\.sh)?\s+facts(?![A-Za-z0-9_-])")
_FACTS_TOOL_WORD = re.compile(r"(?:^|/)phasekit(?:\.sh)?$")
_ENV_CALLEES = re.compile(r"^(?:get|getenv|environ|env|fetch)$")
JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+)['"]([^'"]+)['"]""", re.M)
PY_IMPORT = re.compile(
    r"^[ \t]*(?:from\s+([.\w]+)\s+import|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", re.M)
JS_EXTS = ("", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".jsx")


def _executables(root):
    """Tracked files with the executable bit (a `bin/tool` with no extension)."""
    try:
        out = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {e.split("\t", 1)[1] for e in out.stdout.decode("utf-8", "replace").split("\0")
            if e.startswith("100755 ") and "\t" in e}


def _source_index(root, tracked, owned):
    """What counts as the project's own code: its tracked code files that are
    not tests, not fixtures and not engine files; the Python names they
    provide; and the package names of its tracked package.json files."""
    exe = _executables(root)
    src = {rel for rel in tracked
           if (CODE_RE.search(rel) or rel in exe) and not TEST_FILE_RE.search(rel)
           and not SKIP_RE.search(rel) and rel not in owned and not rel.startswith(".scaffold/")}
    py = set()
    for rel in src:
        if rel.endswith(".py"):
            parts = rel[:-3].split("/")
            py.add(parts[-1] if parts[-1] != "__init__" else (parts[-2] if len(parts) > 1 else ""))
            py.update(parts[:-1])
    py.discard("")
    packages = set()
    for rel in tracked:
        if rel.endswith("package.json") and not SKIP_RE.search(rel):
            try:
                name = json.loads((Path(root) / rel).read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(name, str) and name:
                packages.add(name)
    return src, py, packages


def _touches_project(lx, rel, src, py, packages):
    """Whether this test file reaches the project's own code: it imports a
    project module (a relative import that resolves to a project file, a
    workspace package, a Python name a project file provides), or a literal
    in it names a project file (a script it runs, a module it loads)."""
    t, here = lx.text, posixpath.dirname(rel)
    if lx.lang == "py":
        for m in PY_IMPORT.finditer(t):
            if not lx.code_at(m.end(0) - 1):
                continue
            specs = [m.group(1)] if m.group(1) else [s.strip() for s in m.group(2).split(",")]
            for spec in specs:
                if spec.startswith("."):
                    return True  # a relative import is this tree's code (a package's test)
                if spec.split(".")[0] in py:
                    return True
    elif lx.lang == "js":
        for m in JS_IMPORT.finditer(t):
            spec = m.group(1)
            if spec.startswith("."):
                base = posixpath.normpath(posixpath.join(here, spec))
                stem = JS_EXT.sub("", base)
                for cand in [base + x for x in JS_EXTS] + [stem + x for x in JS_EXTS[1:]] + [
                        base + "/index" + x for x in JS_EXTS[1:]]:
                    if cand in src:
                        return True
            elif spec in packages or any(spec.startswith(p + "/") for p in packages):
                return True
    # a literal naming a project file — a script the test runs, a module it
    # loads — whole or joined from pieces (`ROOT / "scripts" / "check.py"`)
    for s in _spellings(lx, lx.strings):
        parts = s.strip().lstrip("./").split("/")
        if any("/".join(parts[i:]) in src for i in range(len(parts))):
            return True
    if lx.lang == "sh":
        for p in src:
            if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(p) + r"(?![A-Za-z0-9_.-])", t):
                return True
    return False


def _phasekit_surface_reads(lx, contract_is_phasekit, names=None):
    """What of phasekit's declared surface this test file reads: the exported
    path, the facts CLI, the in-tree contract (decided by the same lexer: a
    spelling in prose, a comment or a list of names is data)."""
    t, found = lx.text, set()
    if lx.lang == "sh":
        for m in re.finditer(r"\$\{?" + _ENV_NAME + r"(?![A-Za-z0-9_])", t):
            if lx.kind[m.start()] in (CODE, STRING):
                found.add(ONLY_ENV_SPELLING)
        for m in re.finditer(r"phasekit(?:\.sh)?[ \t]+facts(?![A-Za-z0-9_-])", t):
            if lx.kind[m.start()] in (CODE, STRING):
                found.add(ONLY_CLI_SPELLING)
    else:
        for m in re.finditer(r"(?<![A-Za-z0-9_$])" + _ENV_NAME + r"(?![A-Za-z0-9_$])", t):
            if lx.code_at(m.start()):
                found.add(ONLY_ENV_SPELLING)  # process.env.PHASEKIT_CONTRACT
        for tk in lx.strings:
            e = lx.inner[tk]
            if tk[2] == _ENV_NAME and e != -1:
                # os.environ.get("…"), os.getenv("…"), environ["…"], process.env['…']
                if ((lx.text[e] == "(" and _ENV_CALLEES.match(lx.callee(e)))
                        or (lx.text[e] == "[" and re.search(r"(?:environ|env)\s*$",
                                                            lx.text[max(0, e - 40):e]))):
                    found.add(ONLY_ENV_SPELLING)
            elif _FACTS_CMD.search(tk[2]):
                o = e
                while o != -1 and lx.text[o] != "(":
                    o = lx.parent.get(o, -1)
                if o != -1 and RUNNER_RE.search(lx.callee(o)):
                    found.add(ONLY_CLI_SPELLING)
            elif tk[2] == "facts":
                o = e
                while o != -1 and lx.text[o] != "(":
                    o = lx.parent.get(o, -1)
                if o != -1 and any(_FACTS_TOOL_WORD.search(x[2])
                                   for x in lx.literals(o + 1, lx.close.get(o, len(t)))):
                    found.add(ONLY_CLI_SPELLING)
    if (contract_is_phasekit and (names or CONTRACT_PATH.rsplit("/", 1)[-1] in t)
            and _reads_at(t, [CONTRACT_PATH], lx.lang, names)):
        found.add(CONTRACT_PATH)
    return found


def phasekit_only_reads(root, tracked=None, deadline=None):
    """[{"test": <test file>, "paths": [<what of phasekit's surface it reads>]}]:
    the test files whose only subject is phasekit (see above), sorted."""
    root = Path(root)
    tracked = _tracked(root) if tracked is None else tracked
    try:
        own = json.loads((root / CONTRACT_PATH).read_text(encoding="utf-8"))
        contract_is_phasekit = isinstance(own, dict) and own.get("interface") == "phasekit"
    except (OSError, ValueError):
        contract_is_phasekit = True  # none here: a read of it can only mean phasekit's
    index = None
    files = [(rel, text, _lang(rel)) for rel, text in _test_files(root, tracked)]
    named = {rel for rel, _ in _test_files(root, tracked, named=True)} if files else set()
    # a test helper's exported binding of the contract path, followed into the
    # tests that import it (xmeo-v3's lib/phasekit-facts.ts and its test)
    exported = {}
    for rel, text, lang in files:
        if contract_is_phasekit and "interface.json" in text:
            for name, p in _exported(text, lang, [CONTRACT_PATH]).items():
                exported[(rel, name)] = p
    out = []
    for rel, text, lang in files:
        _budget(deadline)
        if rel not in named:
            continue  # a helper: its exported binding is followed into the tests above
        names = _imported(text, lang, rel, exported) if exported else {}
        if (not names and _ENV_NAME not in text and "facts" not in text
                and "interface.json" not in text):
            continue
        lx = _Lexed(text, lang)
        reads = _phasekit_surface_reads(lx, contract_is_phasekit, names)
        if not reads:
            continue
        if index is None:
            index = _source_index(root, tracked, set(engine_owned(root)))
        if not _touches_project(lx, rel, *index):
            out.append({"test": rel, "paths": sorted(reads)})
    return sorted(out, key=lambda e: e["test"])


# --- process documents as test subjects (v0.19.3) ---------------------------------
# docs/QUALITY_GATES.md "A project's tests test the project": process documents
# are governed by phasekit's gates, never re-tested by the project. The
# `process-reads` advisory names the test files that READ one by path, decided
# by the same lexer as scaffold-reads (a path in a string sample, a comment, a
# list of names, an expected value, argv of a script the test runs is data;
# `open(…, "w")` writes). Recorded in boundary-state.json `process_reads`.
# Advisory only: never a red gate, never a refusal.
PROCESS_ID = "process-reads"
PROCESS_FIELD = "process_reads"
_A = "artifacts" + "/"  # (joined: these are the PROJECT's records, not phasekit artifacts)
PROCESS_DOCS = (
    Family("docs/SPEC.md", r"docs/SPEC\.md", "SPEC"),
    Family("docs/PHASES*.md", r"docs/PHASES[^/]*\.md", "PHASES"),
    Family("docs/LEARNINGS*.md", r"docs/LEARNINGS[^/]*\.md", "LEARNINGS"),
    Family("docs/ROADMAP.md", r"docs/ROADMAP\.md", "ROADMAP"),
    Family("docs/BACKLOG.md", r"docs/BACKLOG\.md", "BACKLOG"),
    Family(_A + "deferrals.json", _A + r"deferrals\.json", "deferrals"),
    Family(_A + "decision-memo*", _A + r"decision-memo[^/]*", "decision-memo"),
    Family(_A + "iterations/", _A + r"iterations(?:/[^/]+)*", "iterations"),
    Family(_A + "ac*", _A + r"ac[0-9][^/]*", "artifacts"),
    Family(_A + "project-complete.json", _A + r"project-complete\.json", "project-complete"),
    Family(_A + "phase-approval.json", _A + r"phase-approval\.json", "phase-approval"),
)


# A supervisor's tests build OTHER projects' trees and read their SPEC and
# records back (foundry-orchestrator: `(project / "docs" / "PHASES.md")` over a
# fixture). Only a read rooted in THIS project counts:
#   * the spelling is the document's own path, under `./` or `../` at most
#     (`tests/fixtures/x/docs/SPEC.md`, `site/docs/SPEC.md` are other trees);
#   * and it is joined onto nothing (cwd-relative, or a helper joins it), or
#     onto a base derived from the test file's location or the working
#     directory (`Path(__file__)…`, `import.meta.url`, `__dirname`, `cwd()`),
#     or onto a name that is the repository root's conventional constant
#     (`ROOT`, `REPO_ROOT`, `PROJECT_ROOT`, `repoRoot`, `rootDir`) and is not
#     bound here to something else.
# A parameter, an attribute, a subscript (`fx["project"]`), a lower-case
# `root`/`repo`, or anything bound to a scratch tree (mkdtemp, tmp_path,
# TemporaryDirectory, fixtures) is a fixture. Python bindings are looked up in
# the enclosing function, then at module level.
_ROOT_MARK = re.compile(r"__file__|import\.meta|__dirname|\bcwd\b|getcwd|fileURLToPath|rev-parse"
                        r"|BASH_SOURCE|BATS_TEST_DIRNAME|dirname \"?\$0|\bpwd\b")
_TMP_MARK = re.compile(r"(?i)mkdtemp|mktemp|tmpdir|tmp_?path|tmp_?dir|temporarydirectory|tempfile"
                       r"|gettempdir|os\.tmpdir|\$TMPDIR|\bfixtures?\b|scratch|sandbox")
_ROOT_NAME = re.compile(r"_?(?:(?:REPO|PROJECT|REPOSITORY)_)?(?:ROOT|REPO)(?:_(?:DIR|PATH))?"
                        r"|(?:repo|project)Root|root(?:Dir|Path)|repoDir")
_PY_DEF = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+\w+[ \t]*\(", re.M)
_HERE_PREFIX = re.compile(r"(?:\.\.?/)*")
_SH_BASE = re.compile(r"""^['"]?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/((?:\.\.?/)*)$""")


def _rootish(name):
    return _ROOT_NAME.fullmatch(name) is not None


def _base_expr(lx, a):
    """The expression the code at `a` (a path's first literal piece, or a name)
    is joined onto: "" when none, "<literal>" when another literal precedes it
    (a longer spelling of the same path decides), else its text (`REPO_ROOT`,
    `fx["project"]`, `Path(__file__).parent`)."""
    t = lx.text
    k = a - 1
    while k >= 0 and t[k] in " \t\n":
        k -= 1
    if k < 0 or t[k] not in "/,+" or not lx.code_at(k):
        return ""
    if t[k] == "," and lx.innermost(k) == -1:
        return ""
    k -= 1
    while k >= 0 and t[k] in " \t\n":
        k -= 1
    if k >= 0 and lx.kind[k] == STRING:
        return "<literal>"
    end = k + 1
    opener = lx.__dict__.setdefault("_opener", {c: o for o, c in lx.close.items()})
    while k >= 0:
        if t[k] in ")]" and k in opener:
            k = opener[k] - 1
        elif re.match(IDENT, t[k]):
            while k >= 0 and re.match(IDENT, t[k]):
                k -= 1
        else:
            break
        if k >= 0 and t[k] == ".":
            k -= 1
            continue
        if k >= 0 and t[k] in ")]" and k in opener and t[k + 1] in "([":
            continue
        break
    return t[k + 1:end].strip()


def _py_defs(lx):
    """[(start, indent, params text)] of every `def` in code, once per file."""
    defs = lx.__dict__.get("_defs")
    if defs is None:
        defs = []
        for m in _PY_DEF.finditer(lx.text):
            o = m.end() - 1
            if lx.code_at(o):
                defs.append((m.start(), len(m.group(1)), lx.text[o + 1:lx.close.get(o, o + 1)]))
        lx._defs = defs
    return defs


def _scope(lx, at):
    """(start, params) of the Python function around `at`; (0, None) at module level."""
    if lx.lang != "py":
        return 0, None
    t = lx.text
    line_start = t.rfind("\n", 0, at) + 1
    indent = len(t[line_start:at]) - len(t[line_start:at].lstrip(" \t"))
    defs = _py_defs(lx)
    starts = lx.__dict__.setdefault("_def_starts", [d[0] for d in defs])
    i = bisect.bisect_left(starts, line_start) - 1
    while i >= 0:
        if defs[i][1] < indent:
            return defs[i][0], defs[i][2]
        i -= 1
    return 0, None


def _bound_rhs(lx, name, at):
    """The right-hand side of the binding of `name` that reaches offset `at`:
    "" for a parameter of the Python function around it, None when it is
    bound nowhere here (an import, a fixture argument)."""
    t = lx.text
    start, params = _scope(lx, at)
    if params is not None and re.search(r"(?<![\w.])" + re.escape(name) + r"\s*(?=[:,=)]|$)",
                                        params):
        return ""
    rx = re.compile(r"^[ \t]*(?:export\s+)?(?:const\s+|let\s+|var\s+|readonly\s+)?"
                    + re.escape(name) + r"\s*(?::[^=\n]*)?=(?!=)(.*)$", re.M)
    rhs = None
    for m in rx.finditer(t, start, at):
        if lx.code_at(m.start(1) - 1):
            rhs = m.group(1)
    if rhs is None and start:
        # a module-level binding, seen from inside a function
        top = re.compile(r"^" + re.escape(name) + r"\s*(?::[^=\n]*)?=(?!=)(.*)$", re.M)
        for m in top.finditer(t, 0, start):
            if lx.code_at(m.start(1) - 1):
                rhs = m.group(1)
    return rhs


def _expr_is_own(lx, expr, at, depth=0):
    """Whether a base expression names THIS project's root (see above). Memoised
    per file: a 30k-line test module calls it thousands of times."""
    key = (expr, _scope(lx, at)[0] if lx.lang == "py" else at, depth)
    cache = lx.__dict__.setdefault("_own", {})
    if key not in cache:
        cache[key] = _expr_is_own_uncached(lx, expr, at, depth)
    return cache[key]


def _expr_is_own_uncached(lx, expr, at, depth):
    if not expr:
        return True
    if expr == "<literal>" or _TMP_MARK.search(expr):
        return False
    if _ROOT_MARK.search(expr):
        return True
    # a parent of a base is as much the project's as the base (`HERE.parent`)
    expr = re.sub(r"(?:\.parent|\.parents\[\d+\]|\.resolve\(\))+$", "", expr)
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        return False  # an attribute, a subscript, a call of anything else: a fixture
    rhs = _bound_rhs(lx, expr, at) if depth < 4 else None
    if rhs == "":
        return False  # a parameter: whatever the caller passes, a fixture
    if rhs is None:
        return _rootish(expr)  # bound nowhere here (an import): the name decides
    if _TMP_MARK.search(rhs):
        return False
    if _ROOT_MARK.search(rhs):
        return True
    # bound here: to a root it derives from (`ROOT = resolve(HERE, '..')`), or not
    names = [i for i in re.findall(r"(?<![\w$.'\"])[A-Za-z_$][\w$]*", rhs) if i != expr][:6]
    return any(_expr_is_own(lx, i, at, depth + 1) for i in names
               if _rootish(i) or _bound_rhs(lx, i, at))


def _family_prefix(s, path):
    """What a spelling carries before the document's own path ("" when none)."""
    rx = path.rx if isinstance(path, Family) else re.escape(path)
    m = re.fullmatch(r"(.*?/)?(?:" + rx + r")", s)
    return (m.group(1) or "") if m else s


_RUN_DEADLINE = [None]  # process_reads' deadline, checked per read site (review R2)


def _own_root(lx, piece, spelling, path, shell=False, at=0):
    """The `own` filter of process_reads: whether a read is rooted in THIS project."""
    _budget(_RUN_DEADLINE[0])
    if shell:
        # the text before a shell spelling: `$ROOT/`, `../`, or nothing
        before = re.search(r"""['"]?[A-Za-z0-9_./${}-]*$""", spelling or "").group(0)
        if _HERE_PREFIX.fullmatch(before.lstrip("'\"")):
            return True
        m = _SH_BASE.match(before)
        return bool(m) and _expr_is_own(lx, m.group(1), at)
    if piece is None:
        # a bound name used at `at`: what it is joined onto there
        return _expr_is_own(lx, _base_expr(lx, at), at)
    m = re.match(r"\$?\{([A-Za-z_$][\w$]*)\}/", spelling)
    rest = spelling[m.end():] if m else spelling
    if not _HERE_PREFIX.fullmatch(_family_prefix(rest, path)):
        return False
    return _expr_is_own(lx, m.group(1) if m else _base_expr(lx, piece[0]), piece[0])


class _OutOfTime(Exception):
    pass


SCAN_BUDGET_S = 25  # each v0.19.3 scan; the loop runs the whole tool under `timeout 60`
RUN_BUDGET_S = 50   # the whole `scaffold-reads` run: past it, process_reads is null


def _budget(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise _OutOfTime()


def process_reads(root, tracked=None, deadline=None):
    """[{"test": <test file>, "paths": [<process-document families it reads>]}], sorted.
    Raises _OutOfTime past `deadline` (time.monotonic()), so a huge tree costs
    this record (null) and never the scaffold-reads one."""
    root = Path(root)
    tracked = _tracked(root) if tracked is None else tracked
    out = []
    _RUN_DEADLINE[0] = deadline
    try:
        for rel, text in _test_files(root, tracked, named=True):
            _budget(deadline)
            out.extend(_process_reads_in(rel, text))
    finally:
        _RUN_DEADLINE[0] = None
    return sorted(out, key=lambda e: e["test"])


def _process_reads_in(rel, text):
    """[the entry for one test file] or []."""
    cand = [p for p in PROCESS_DOCS if p.hint in text]
    hit = _reads_in(text, cand, _lang(rel), own=_own_root) if cand else []
    return [{"test": rel, "paths": [str(p) for p in hit]}] if hit else []


def process_line(reads):
    """The one-line `process-reads` advisory (empty when there is nothing to say)."""
    if not reads:
        return ""
    names = [e["test"] for e in reads]
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    return (f"ADVISORY {PROCESS_ID}: {len(names)} test file(s) read process documents ({shown}) "
            "— SPEC, PHASES, LEARNINGS, ROADMAP/BACKLOG, the deferral ledger and the records "
            "under artifacts/ are governed by phasekit's gates, not tested by the project; a test "
            "exercises the project's code. See docs/QUALITY_GATES.md \"A project's tests test the "
            "project\". Advisory only: the gate is unchanged.")


# --- per-criterion suites (v0.19.3; `phasekit check` only) ---------------------------
# A large test file whose tests are named for criteria or iterations
# (`test_ac123_…`, `iteration-41 …`, `test_spec_declares_…`) is the shape the
# 2026-10-09 sweep found growing without bound: one test per append-only SPEC
# line. Reported by `phasekit check`, never recorded, never a red gate.
SUITES_ID = "criterion-suites"
SUITE_MIN_LINES = 3000
SUITE_MIN_SHARE = 0.5
FAMILY_MIN_FILES = 5
CRITERION_NAME = re.compile(r"(?i)(?:^|[^a-z0-9])ac[\s_#-]*\d|iteration[\s_-]*\d|spec_declares")
_PY_TEST = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)", re.M)
_JS_TEST = re.compile(r"""(?<![A-Za-z0-9_$.])(?:it|test|describe)(?:\.\w+)?\s*\(\s*"""
                      r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""")
_FAMILY_FILE = re.compile(r"(?i)(?:^|[^a-z])(?:iteration|phase)[-_]?\d+[^/]*$")


def criterion_suites(root, tracked=None):
    """{"large": [{"test", "lines", "tests", "criterion_tests"}], "family": {"files",
    "lines", "examples"}}: test files over SUITE_MIN_LINES lines whose tests are mostly
    named for criteria or iterations, and the files named per iteration or phase."""
    root = Path(root)
    tracked = _tracked(root) if tracked is None else tracked
    large, family = [], []
    for rel, text in _test_files(root, tracked):
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if _FAMILY_FILE.search(posixpath.basename(rel)):
            family.append((rel, lines))
        if lines <= SUITE_MIN_LINES:
            continue
        names = ([m.group(1) for m in _PY_TEST.finditer(text)] if rel.endswith(".py")
                 else [m.group(2) for m in _JS_TEST.finditer(text)])
        hits = sum(1 for n in names if CRITERION_NAME.search(n))
        if names and hits / len(names) >= SUITE_MIN_SHARE:
            large.append({"test": rel, "lines": lines, "tests": len(names),
                          "criterion_tests": hits})
    fam = {"files": 0, "lines": 0, "examples": []}
    if len(family) >= FAMILY_MIN_FILES:
        fam = {"files": len(family), "lines": sum(n for _, n in family),
               "examples": [r for r, _ in family[:3]]}
    return {"large": large, "family": fam}


def suites_line(report):
    """The `criterion-suites` line (empty when there is nothing to say)."""
    large, fam = report["large"], report["family"]
    if not large and not fam["files"]:
        return ""
    parts = []
    if large:
        shown = ", ".join(f"{e['test']} ({e['lines']} lines, {e['criterion_tests']} of "
                          f"{e['tests']} tests)" for e in large[:5])
        shown += ", …" if len(large) > 5 else ""
        parts.append(f"{len(large)} test file(s) over {SUITE_MIN_LINES} lines are mostly tests "
                     f"named for criteria or iterations ({shown})")
    if fam["files"]:
        parts.append(f"{fam['files']} test files are named per iteration or phase "
                     f"({fam['lines']} lines; {', '.join(fam['examples'])}, …)")
    return (f"ADVISORY {SUITES_ID}: " + "; ".join(parts) + " — a criterion yields product tests, "
            "not one test per criterion (docs/QUALITY_GATES.md \"A project's tests test the "
            "project\"). Advisory only: the check's exit code is unchanged.")


def check_lines(root):
    """What `phasekit check` prints for the test-subject advisories, in both
    layouts: scaffold-reads (with the phasekit-only widening), process-reads,
    criterion-suites. Each is isolated: one failing never costs another."""
    root = Path(root)
    tracked = _tracked(root)
    out = []
    for fn in (lambda: _named(advisory_line, scaffold_reads(root)),
               lambda: _named(process_line, process_reads(root, tracked,
                                                          time.monotonic() + SCAN_BUDGET_S)),
               lambda: [suites_line(criterion_suites(root, tracked))]):
        try:
            out.extend(x for x in fn() if x)
        except Exception:  # noqa: BLE001 - an advisory never fails the check
            continue
    return out


def _named(line_fn, reads):
    line = line_fn(reads)
    if not line:
        return []
    return [line] + [f"  {e['test']}: {', '.join(e['paths'])}" for e in reads]


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
            f"({shown}) — a test reads the project's own tree, never the vendored loop, "
            "scripts, hooks or scaffold docs, and reads phasekit's declared surface "
            "($PHASEKIT_CONTRACT, `phasekit facts`) only where the project's own code consumes "
            "it: a test that reads it and no project code has phasekit as its only subject, is "
            "named here too, and belongs upstream. See docs/QUALITY_GATES.md \"Tests read the "
            "declared surface\". Advisory only: the gate is unchanged.")


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
        run_end = time.monotonic() + RUN_BUDGET_S  # the loop runs this under `timeout 60`
        reads = scaffold_reads(top)
        try:
            # the hint is isolated: it never costs the scaffold_reads record
            mreads = migration_reads(top)
        except Exception:  # noqa: BLE001 - an advisory never fails
            mreads = []
        try:
            # v0.19.3: isolated the same way; null = the scan failed
            preads = process_reads(top, deadline=min(time.monotonic() + SCAN_BUDGET_S, run_end))
        except Exception:  # noqa: BLE001 - an advisory never fails
            preads = None
        if as_json:
            record = {"advisory": ADVISORY_ID, RECORD_FIELD: reads, "line": advisory_line(reads),
                      MIGRATION_FIELD: mreads, "migration_line": migration_line(mreads),
                      PROCESS_FIELD: preads, "process_line": process_line(preads or [])}
            print(json.dumps(record, sort_keys=True))
        else:
            for line in _named(advisory_line, reads) + _named(process_line, preads or []):
                print(line)
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
