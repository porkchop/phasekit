import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, posix, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CONTRACT } from './lib/phasekit-facts';

/**
 * Iteration 96 (Phase 200, SPEC AC-W1.9): the suites that used to parse
 * phasekit's scaffold-owned files read only its declared surface.
 *
 * phasekit publishes what a downstream test may rely on as the `facts` of
 * `contracts/interface.json`; anything else it ships (`ownership: scaffold` in
 * `.scaffold/manifest.json`) is its internals, replaced on every upgrade. For
 * each suite below this collects the string-literal paths handed to a file
 * read — `read('…')`, `readFileSync(…)`, `readIfPresent(…)`, the literal
 * segments of `resolve(…)` / `join(…)`, and module-level path constants
 * (including the ones imported from `lib/phasekit-facts.ts`) — and refuses any
 * whose manifest ownership is `scaffold`. One exception:
 * `contracts/interface.json`, the declared surface itself. Until phase 204
 * `lib/phasekit-facts.ts` was also allowed phasekit's loop script, for its
 * fallback over a contract that predates `facts`; the fallback is deleted, so
 * nothing here may read the loop.
 *
 * Text-level, like the hermetic advisory it sits beside: a path spelled out of
 * fragments at run time would escape it. The collector is held to the forms
 * these suites actually use by the planted rows below.
 */

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel: string): string => readFileSync(resolve(repoRoot, rel), 'utf8');

const SUITES = [
  'tests/tooling/firewall-bridge-egress.test.ts',
  'tests/tooling/iteration-55-invariants.test.ts',
  'tests/tooling/iteration-56-invariants.test.ts',
  'tests/tooling/scope-suite-archive.test.ts',
  'tests/tooling/learnings-hygiene.test.ts',
];
const FACTS_LIB = 'tests/tooling/lib/phasekit-facts.ts';
/** phasekit's loop script: scaffold-owned, and read by no file here. */
const LOOP = 'scripts/run-until-done.sh';
const FIREWALL = SUITES[0];

/** Paths whose manifest ownership is `scaffold` — read from the manifest. */
const scaffoldOwned = (): Set<string> => {
  const manifest = JSON.parse(read('.scaffold/manifest.json')) as {
    files: { path: string; ownership: string }[];
  };
  return new Set(manifest.files.filter((f) => f.ownership === 'scaffold').map((f) => f.path));
};

/** The one scaffold-owned read any file here may make: the declared contract. */
const ALLOWED = new Set([CONTRACT]);

const LITERAL = /^(['"`])([^'"`$]*)\1$/;
// Not a method call: `lines.join('|')` is array joining, never a path.
const READ_CALL = /(?<![.\w])(read|readFileSync|readIfPresent|resolve|join)\(/g;
const ENCODINGS = new Set(['utf8', 'utf-8']);
const CONST_LITERAL = /^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['"`])([^'"`$\n]*)\2\s*;/gm;
const CONST_ALIAS = /^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*;/gm;

/** Module-level path constants: `const X = '…'`, then `const Y = X` one level. */
export const pathConstants = (...texts: string[]): Map<string, string> => {
  const table = new Map<string, string>();
  for (const text of texts) for (const m of text.matchAll(CONST_LITERAL)) table.set(m[1], m[3]);
  for (const text of texts) {
    for (const m of text.matchAll(CONST_ALIAS)) {
      const value = table.get(m[2]);
      if (value !== undefined) table.set(m[1], value);
    }
  }
  return table;
};

/** Repo-relative, with `./` and leading `../` segments dropped. */
const normalise = (path: string): string =>
  posix
    .normalize(path.split('\\').join('/'))
    .split('/')
    .filter((seg) => seg !== '..' && seg !== '.' && seg !== '')
    .join('/');

/** Top-level comma split (a nested call's commas stay inside its argument). */
const splitArgs = (args: string): string[] => {
  const out: string[] = [];
  let depth = 0;
  let cur = '';
  for (const ch of args) {
    if (ch === '(' || ch === '[') depth += 1;
    if (ch === ')' || ch === ']') depth -= 1;
    if (ch === ',' && depth === 0) {
      out.push(cur.trim());
      cur = '';
    } else cur += ch;
  }
  if (cur.trim() !== '') out.push(cur.trim());
  return out;
};

/**
 * The comment-stripped code of a TypeScript file. String-aware: a `/*` or `//`
 * inside a quoted string (a test title such as `'… *-scope.test.ts …'`) opens
 * no comment, or everything up to the next `*\/` would vanish from the scan.
 * Regex literals are NOT recognised: a pattern spelling an escaped `/` before
 * `*` would open a false comment. None of the scanned files has one today.
 */
export const code = (text: string): string => {
  let out = '';
  let quote: string | null = null;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quote !== null) {
      out += ch;
      if (ch === '\\') {
        out += text[i + 1] ?? '';
        i += 1;
      } else if (ch === quote || (ch === '\n' && quote !== '`')) quote = null;
    } else if (ch === "'" || ch === '"' || ch === '`') {
      quote = ch;
      out += ch;
    } else if (ch === '/' && text[i + 1] === '/') {
      while (i < text.length && text[i] !== '\n') i += 1;
      out += '\n';
    } else if (ch === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2);
      i = end === -1 ? text.length : end + 1;
    } else out += ch;
  }
  return out;
};

/** The argument text of the call whose `(` is at `open`, balanced. */
const callArgs = (text: string, open: number): string => {
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === '(') depth += 1;
    if (text[i] === ')') {
      depth -= 1;
      if (depth === 0) return text.slice(open + 1, i);
    }
  }
  return text.slice(open + 1);
};

/**
 * Every path a file read in `text` is handed, normalised. Each call is visited
 * on its own, so a `resolve(…)` nested inside `readFileSync(…)` is read too.
 */
export const readPaths = (text: string, constants: Map<string, string>): Set<string> => {
  const found = new Set<string>();
  const src = code(text);
  for (const m of src.matchAll(READ_CALL)) {
    const parts: string[] = [];
    for (const arg of splitArgs(callArgs(src, (m.index ?? 0) + m[0].length - 1))) {
      const literal = LITERAL.exec(arg);
      if (literal !== null) {
        // an escape sequence (`'\\n'`) or an encoding name is never a path segment
        if (!ENCODINGS.has(literal[2]) && !literal[2].includes('\\')) parts.push(literal[2]);
      } else if (constants.has(arg)) parts.push(constants.get(arg) as string);
      // anything else — a root like repoRoot / __dirname, or a nested call
      // visited on its own — contributes no segment
    }
    const path = normalise(parts.join('/'));
    // a path names a directory or a file: `.join('|')`'s separator does neither
    if (/[/.]/.test(path)) found.add(path);
  }
  return found;
};

/** The scaffold-owned paths `text`'s reads reach beyond the declared contract. */
export const forbiddenReads = (
  text: string,
  owned: Set<string>,
  constants: Map<string, string>,
): string[] => {
  return [...readPaths(text, constants)].filter((p) => owned.has(p) && !ALLOWED.has(p)).sort();
};

const libText = read(FACTS_LIB);

describe('AC-W1.9: no suite reads a scaffold-owned file beyond the declared contract', () => {
  const owned = scaffoldOwned();

  it('the manifest names scaffold-owned paths, including the ones these suites used to read', () => {
    for (const p of ['.devcontainer/init-firewall.sh', 'docs/QUALITY_GATES.md', CONTRACT, LOOP]) {
      expect(owned.has(p), p).toBe(true);
    }
  });

  it.each([...SUITES, FACTS_LIB])('%s reads no scaffold-owned file it is not allowed', (file) => {
    const text = read(file);
    const constants = pathConstants(libText, text);
    if (file === FIREWALL) {
      // It reads nothing at all now — a stronger claim than "nothing forbidden".
      expect([...readPaths(text, constants)]).toEqual([]);
    } else {
      expect(readPaths(text, constants).size, `${file}: the collector found no read at all`).toBeGreaterThan(0);
    }
    expect(forbiddenReads(text, owned, constants)).toEqual([]);
  });

  it('sees a read placed after a string that spells a block-comment opener', () => {
    // scope-suite-archive's test title `'… tests/tooling/*-scope.test.ts …'` once
    // hid ~90 lines of that suite from a comment stripper that ignored strings.
    const suite = 'tests/tooling/scope-suite-archive.test.ts';
    const lines = read(suite).split('\n');
    const at = lines.findIndex((l) => l.includes("it('the glob is empty"));
    expect(at, 'the title this row anchors on moved').toBeGreaterThan(0);
    const planted = [
      ...lines.slice(0, at + 1),
      "    const leak = read('.devcontainer/init-firewall.sh');",
      ...lines.slice(at + 1),
    ].join('\n');
    const constants = pathConstants(libText, planted);
    expect(forbiddenReads(planted, owned, constants)).toEqual(['.devcontainer/init-firewall.sh']);
  });

  it('the suite table is the five suites the phase names, plus the facts library', () => {
    expect(SUITES).toHaveLength(5);
  });

  it('the facts library reads the contract and not the loop, and a planted loop read is refused there too', () => {
    const constants = pathConstants(libText);
    const reads = readPaths(libText, constants);
    expect(reads.has(CONTRACT)).toBe(true);
    expect(reads.has(LOOP)).toBe(false);
    expect([...ALLOWED], 'no exception beyond the declared contract').toEqual([CONTRACT]);
    // Phase 204: the library has no exception left — the read its fallback
    // used to make is refused there exactly as in any suite.
    const planted = `${libText}\nconst x = readFileSync(resolve(repoRoot, '${LOOP}'), 'utf8');`;
    expect(forbiddenReads(planted, owned, pathConstants(planted))).toEqual([LOOP]);
  });

  const PLANTED: [string, string, string][] = [
    [
      'a relative resolve() path constant',
      "const scriptPath = resolve(__dirname, '../../.devcontainer/init-firewall.sh');\n" +
        "const source = readFileSync(scriptPath, 'utf8');",
      '.devcontainer/init-firewall.sh',
    ],
    ['a read() helper', "expect(read('docs/QUALITY_GATES.md')).not.toContain('x');", 'docs/QUALITY_GATES.md'],
    [
      'a module constant handed to read()',
      "const DOC = 'CONTINUE_PROMPT.txt';\nconst text = read(DOC);",
      'CONTINUE_PROMPT.txt',
    ],
    [
      'an alias of a path constant',
      "const DOC = 'docs/QUALITY_GATES.md';\nconst GATE_DOC = DOC;\nconst text = read(GATE_DOC);",
      'docs/QUALITY_GATES.md',
    ],
    ['a bare readIfPresent()', "const t = readIfPresent('CONTINUE_PROMPT.txt');", 'CONTINUE_PROMPT.txt'],
    [
      "a string spelling '/*' before the read",
      "it('globs tests/*-x.ts', () => {\n  const t = read('docs/EXECUTION_MODES.md');\n});\n/* a later comment */",
      'docs/EXECUTION_MODES.md',
    ],
    [
      'resolve() segments',
      "readFileSync(resolve(repoRoot, '.devcontainer', 'Dockerfile'), 'utf8');",
      '.devcontainer/Dockerfile',
    ],
  ];

  it.each(PLANTED)('refuses a planted suite text: %s', (_name, text, path) => {
    const constants = pathConstants(libText, text);
    expect(forbiddenReads(text, owned, constants)).toEqual([path]);
  });

  it('does not count a path named only in a comment, or the declared contract', () => {
    const text =
      "// reads .devcontainer/init-firewall.sh no more\n/* read('docs/QUALITY_GATES.md') */\n" +
      "const c = read('contracts/interface.json');";
    const constants = pathConstants(text);
    expect(readPaths(text, constants).has(CONTRACT)).toBe(true);
    expect(forbiddenReads(text, owned, constants)).toEqual([]);
  });

  it.each([
    ['an array join, even of path-shaped text', "const s = parts.join('a/b.txt');"],
    ['a segment carrying an escape sequence', "const p = resolve(repoRoot, 'a\\\\b.txt');"],
    ['an encoding name, or any literal naming neither a directory nor a file', "readFileSync(p, 'latin1');"],
  ])('collects no path from %s', (_name, text) => {
    expect([...readPaths(text, new Map())]).toEqual([]);
  });

  it('the planted table holds seven rows', () => {
    expect(PLANTED).toHaveLength(7);
  });
});
