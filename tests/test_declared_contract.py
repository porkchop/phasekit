#!/usr/bin/env python3
"""ONE layout-independent way for a project's tests to read phasekit's
declared surface (v0.19.2; Aaron 2026-10-08).

The gap (measured 2026-10-08): v0.18.3's rule tells a project test to read
phasekit's declared surface, the `facts` in `contracts/interface.json`. In the
PINNED layout that file is an engine file and is not in the project's tree,
and v0.19.0 gave a test no layout-independent way to find it: migrating
foundry-dashboard was refused by its own gate (three test files read the
project-relative path: ENOENT), and xmeo-v3 and foundry-orchestrator read it
the same way.

What this release pins:
  * PHASEKIT_CONTRACT — the absolute path of the contract of the engine that
    runs — is exported by the loop (its sessions and its gate), by `phasekit
    verify`, and by the upgrade/migrate gate (host and container), in BOTH
    layouts (vendored: the project's own copy; pinned: the engine's);
  * `phasekit facts --path` prints the same path for a test run by hand;
  * the two snippets in docs/QUALITY_GATES.md run, in both layouts;
  * `phasekit migrate` refuses, BEFORE its gate, a project whose own files
    read an engine file by its in-tree path, naming file:line and the remedy
    (`--force` skips it), and never flags the same path as data;
  * the scaffold-reads advisory names the same reads in a vendored project as
    a migration-readiness hint (advisory only).

Every test here is red on v0.19.1.

Run from the repo root: python3 -m unittest tests.test_declared_contract
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-surface.py"
CONTRACT = REPO_ROOT / "contracts" / "interface.json"
QUALITY_GATES = REPO_ROOT / "docs" / "QUALITY_GATES.md"

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_dc", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

_sspec = importlib.util.spec_from_file_location("pk_surface_dc", TOOL)
S = importlib.util.module_from_spec(_sspec)
_sspec.loader.exec_module(S)

HAVE_NODE = shutil.which("node") is not None

ENGINE_PATHS = ["contracts/interface.json", "scripts/run-until-done.sh", "scripts/phasekit.sh",
                "docs/QUALITY_GATES.md", "CONTINUE_PROMPT.txt", ".devcontainer/init-firewall.sh"]


def manifest(paths=ENGINE_PATHS):
    files = [{"path": p, "ownership": "scaffold", "text": True, "sha256": "0" * 64,
              "sha256_strict": "0" * 64} for p in paths]
    return json.dumps({"schema_version": 3, "scaffold_version": "v0.19.1", "files": files}, indent=2)


def qg_snippet(lang):
    """The snippet docs/QUALITY_GATES.md gives for `lang` (js | python), from
    the "Tests read the declared surface" section."""
    text = QUALITY_GATES.read_text()
    start = text.index("### Tests read the declared surface")
    end = text.index("\n### ", start + 10)
    m = re.search(r"```" + lang + r"\n(.*?)```", text[start:end], re.S)
    if m is None:
        raise AssertionError(f"docs/QUALITY_GATES.md gives no ```{lang} snippet in the rule's section")
    return m.group(1)


# --- the shapes the fleet measured on 2026-10-08 --------------------------------
# foundry-dashboard's three patterns (line numbers are the reads').
DASHBOARD = {
    # a read-named helper over a template URL, called with the literal
    "test/declared-surface.test.js": (
        "import { test } from 'node:test';\n"
        "import { readFileSync } from 'node:fs';\n"
        "const readRel = (rel) => readFileSync(new URL(`../${rel}`, import.meta.url), 'utf8');\n"
        "const SAMPLE = \"readFileSync('contracts/interface.json');\";\n"
        "test('the fact', () => {\n"
        "  const facts = JSON.parse(readRel('contracts/interface.json')).facts;\n"
        "  const src = SAMPLE;\n"
        "  assert.ok(src.includes('contracts/interface.json'), 'the prose names it');\n"
        "});\n"),
    # a name bound to the path, handed to a read-named helper
    "test/learnings-harvest.test.js": (
        "import { readFileSync } from 'node:fs';\n"
        "import { join } from 'node:path';\n"
        "const ROOT = new URL('..', import.meta.url).pathname;\n"
        "const CONTRACT = 'contracts/interface.json';\n"
        "const read = (rel) => readFileSync(join(ROOT, rel), 'utf8');\n"
        "const ADVISORY = JSON.parse(read(CONTRACT)).facts.learnings_size_advisory;\n"
        "assert.ok(ADVISORY, `could not read facts out of ${CONTRACT}`);\n"),
    # readFileSync(new URL('../contracts/interface.json', import.meta.url))
    "test/spec-contract-criteria.test.js": (
        "import { readFileSync } from 'node:fs';\n"
        "const LEARNINGS_ADVISORY = JSON.parse(readFileSync(new URL('../contracts/interface.json', import.meta.url), 'utf8'))\n"
        "  .facts.learnings_size_advisory;\n"
        "const ORCH = JSON.parse(readFileSync(\n"
        "  new URL('../vendor/contracts/foundry-orchestrator/interface.json', import.meta.url), 'utf8'));\n"),
}

# xmeo-v3's helper: an EXPORTED binding read in the helper, and the same name
# imported and read by a test of its own.
XMEO = {
    "tests/tooling/lib/phasekit-facts.ts": (
        "import { readFileSync } from 'node:fs';\n"
        "import { resolve } from 'node:path';\n"
        "export const CONTRACT = 'contracts/interface.json';\n"
        "export const FACT_CONTRACT_FIXTURE = 'tests/tooling/fixtures/phasekit-v0.18.4-interface.json';\n"
        "/** The installed contract's text. */\n"
        "export const installedContract = (repoRoot: string): string =>\n"
        "  readFileSync(resolve(repoRoot, CONTRACT), 'utf8');\n"),
    "tests/tooling/phasekit-facts.test.ts": (
        "import { readFileSync } from 'node:fs';\n"
        "import { resolve } from 'node:path';\n"
        "import {\n"
        "  CONTRACT,\n"
        "  installedContract,\n"
        "} from './lib/phasekit-facts';\n"
        "const read = (rel: string): string => readFileSync(resolve(repoRoot, rel), 'utf8');\n"
        "it('rows', () => {\n"
        "  expect(rows).toEqual([read(CONTRACT)]);\n"
        "  expect(`${CONTRACT} carries no facts`).toBeTruthy();\n"
        "});\n"),
    # xmeo's guard test: the path as data only (an allow-list, a loop over names)
    "tests/tooling/scaffold-read-surface.test.ts": (
        "import { CONTRACT } from './lib/phasekit-facts';\n"
        "const ALLOWED = new Set([CONTRACT]);\n"
        "for (const p of ['.devcontainer/init-firewall.sh', 'docs/QUALITY_GATES.md', CONTRACT]) {\n"
        "  expect(forbidden(p)).toBe(true);\n"
        "}\n"
        "expect(reads.has(CONTRACT)).toBe(true);\n"),
    # the fixture DATA a helper reads is not an engine file, and not code
    "tests/tooling/fixtures/phasekit-v0.18.4-interface.json": '{"interface": "phasekit", "facts": {}}\n',
}

# Source and scripts are the project's too: the pre-flight scans them.
OTHER_READS = {
    "src/tool.py": ('import json\nfrom pathlib import Path\nROOT = Path(__file__).parent.parent\n'
                    'FACTS = json.loads((ROOT / "contracts" / "interface.json").read_text())["facts"]\n'),
    "scripts/report.sh": "#!/usr/bin/env bash\ngrep -c MAJOR docs/QUALITY_GATES.md\n",
}

NOT_READS = {
    # prose and comments name the path; nothing reads it
    "docs/notes.md": "readFileSync('contracts/interface.json') is how we used to do it\n",
    "test/mention.test.js": "// the contract (contracts/interface.json) is read through PHASEKIT_CONTRACT\n",
    # a fixture CODE file is data a test reads, never a test of its own
    "tests/fixtures/old.test.js": "readFileSync('contracts/interface.json');\n",
    # the vendored provider copy of a supervisor stays in the tree after a migration
    "test/vendored.test.js": ("const c = readFileSync(new URL('../vendor/contracts/phasekit/interface.json', "
                              "import.meta.url), 'utf8');\n"),
    # running a script is not reading it
    "tests/test_run.py": ('import subprocess\nsubprocess.run(["bash", "scripts/run-until-done.sh", "verify"])\n'),
}


class _Scratch(unittest.TestCase):
    def make(self, files, with_manifest=True, pinned=False):
        root = Path(tempfile.mkdtemp(prefix="pk-dc-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        if with_manifest:
            (root / ".scaffold").mkdir()
            (root / ".scaffold" / "manifest.json").write_text(manifest())
            # the engine files themselves are in a vendored tree, and never scanned
            for p in ENGINE_PATHS:
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                if not (root / p).exists():
                    (root / p).write_text("cat contracts/interface.json\nopen('docs/QUALITY_GATES.md')\n")
        if pinned:
            (root / ".phasekit-version").write_text("v0.19.2\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return root


class TheMigrationReads(_Scratch):
    """The lexer's read-vs-literal logic (v0.18.5), applied to every project
    code file and to the declared contract's in-tree path too."""

    def reads(self, files):
        root = self.make(files)
        return {e["file"]: [(r["path"], r["line"]) for r in e["reads"]]
                for e in S.migration_reads(root)}

    def test_the_dashboards_three_patterns_are_named_with_their_lines(self):
        got = self.reads({**DASHBOARD, **NOT_READS})
        self.assertEqual(got, {
            "test/declared-surface.test.js": [("contracts/interface.json", 6)],
            "test/learnings-harvest.test.js": [("contracts/interface.json", 6)],
            "test/spec-contract-criteria.test.js": [("contracts/interface.json", 2)],
        })

    def test_xmeos_helper_and_a_test_importing_its_name_are_named(self):
        got = self.reads({**XMEO, **NOT_READS})
        self.assertEqual(got, {
            "tests/tooling/lib/phasekit-facts.ts": [("contracts/interface.json", 7)],
            "tests/tooling/phasekit-facts.test.ts": [("contracts/interface.json", 9)],
        })

    def test_an_imported_name_is_followed_only_to_the_module_that_binds_it(self):
        """Review round 1: one name exported by two files, only one of them
        bound to an engine path — an import names its own module's binding."""
        got = self.reads({
            "test/a-lib.js": "export const LOOP = 'scripts/run-until-done.sh';\n",
            "src/b.js": "export const LOOP = 'data/loop.json';\n",
            "test/c.test.js": "import { LOOP } from '../src/b.js';\nconst t = readFileSync(LOOP);\n",
            "test/d.test.js": "import { LOOP as L } from './a-lib';\nconst t = readFileSync(L);\n",
            "tests/lib/paths.py": 'CONTRACT = "contracts/interface.json"\n',
            "tests/test_e.py": ("from .lib.paths import (\n    CONTRACT as C,\n)\n"
                                "data = open(C).read()\n"),
            "tests/test_f.py": "from other.paths import CONTRACT\ndata = open(CONTRACT).read()\n",
        })
        self.assertEqual(got, {
            "test/d.test.js": [("scripts/run-until-done.sh", 2)],
            "tests/test_e.py": [("contracts/interface.json", 4)],
        })

    def test_source_and_scripts_are_scanned_too(self):
        got = self.reads(OTHER_READS)
        self.assertEqual(got, {
            "scripts/report.sh": [("docs/QUALITY_GATES.md", 2)],
            "src/tool.py": [("contracts/interface.json", 4)],
        })

    def test_the_documented_snippets_read_nothing_of_the_engine(self):
        got = self.reads({"test/surface.test.js": qg_snippet("js"),
                          "tests/test_surface.py": qg_snippet("python")})
        self.assertEqual(got, {})

    def test_no_manifest_no_reads(self):
        root = self.make(DASHBOARD, with_manifest=False)
        self.assertEqual(S.migration_reads(root), [])

    def test_the_scaffold_reads_record_is_unchanged_and_the_hint_rides_beside_it(self):
        """A vendored project's contract read is still the declared surface for
        `scaffold_reads`, never a scaffold-owned read; the migration hint names
        it. v0.19.3: these fixture files read the contract and touch NO project
        code, so they have phasekit as their only subject and the widening
        names them (docs/QUALITY_GATES.md "A project's tests test the project")."""
        root = self.make({**DASHBOARD, **XMEO})
        r = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", "--json", str(root)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual({e["test"]: e["paths"] for e in out["scaffold_reads"]}, {
            p: ["contracts/interface.json"] for p in (
                "test/declared-surface.test.js", "test/learnings-harvest.test.js",
                "test/spec-contract-criteria.test.js", "tests/tooling/phasekit-facts.test.ts")})
        self.assertEqual(sorted(e["file"] for e in out["migration_reads"]), sorted([
            "test/declared-surface.test.js", "test/learnings-harvest.test.js",
            "test/spec-contract-criteria.test.js", "tests/tooling/lib/phasekit-facts.ts",
            "tests/tooling/phasekit-facts.test.ts"]))
        self.assertTrue(out["migration_line"].startswith(
            "ADVISORY migration-readiness: 5 file(s) read engine files by their in-tree path"),
            out["migration_line"])
        self.assertIn("PHASEKIT_CONTRACT", out["migration_line"])
        plain = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", str(root)],
                               capture_output=True, text=True, timeout=60)
        self.assertIn("ADVISORY migration-readiness:", plain.stdout)
        self.assertIn("test/learnings-harvest.test.js:6: contracts/interface.json", plain.stdout)

    def test_phasekit_check_prints_the_hint_and_its_exit_code_is_unchanged(self):
        enrich = REPO_ROOT / "scripts" / "enrich-project.py"
        with_reads = subprocess.run([sys.executable, str(enrich), "--check", str(self.make(DASHBOARD))],
                                    capture_output=True, text=True, timeout=120)
        without = subprocess.run([sys.executable, str(enrich), "--check", str(self.make(NOT_READS))],
                                 capture_output=True, text=True, timeout=120)
        self.assertIn("ADVISORY migration-readiness:", with_reads.stdout)
        self.assertNotIn("ADVISORY migration-readiness:", without.stdout)
        self.assertEqual(with_reads.returncode, without.returncode, "advisory only")


class FactsPath(_Scratch):
    def test_vendored_it_is_the_projects_own_copy(self):
        root = self.make({"contracts/interface.json": CONTRACT.read_text()}, with_manifest=False)
        (root / "src").mkdir()
        r = subprocess.run([sys.executable, str(TOOL), "facts", "--path"], cwd=root / "src",
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str((root / "contracts" / "interface.json").resolve()))

    def test_pinned_it_is_the_engines(self):
        root = self.make({}, with_manifest=False, pinned=True)
        r = subprocess.run([sys.executable, str(TOOL), "facts", "--path"], cwd=root,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(CONTRACT.resolve()))


class TheDocumentedSnippetsRun(unittest.TestCase):
    """docs/QUALITY_GATES.md's snippets, run as written: through
    PHASEKIT_CONTRACT, and by hand (no variable) through the CLI on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-dc-snip-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "phasekit").write_text(f'#!/usr/bin/env bash\nexec bash "{REPO_ROOT}/scripts/phasekit.sh" "$@"\n')
        os.chmod(self.bin / "phasekit", 0o755)
        self.want = sorted(json.loads(CONTRACT.read_text())["facts"])

    def env(self, with_var):
        e = {k: v for k, v in os.environ.items() if k != "PHASEKIT_CONTRACT"}
        e["PATH"] = f"{self.bin}{os.pathsep}{e['PATH']}"
        if with_var:
            e["PHASEKIT_CONTRACT"] = str(CONTRACT)
        return e

    def test_python(self):
        code = qg_snippet("python") + "\nimport json as _j\nprint(_j.dumps(sorted(facts)))\n"
        for with_var in (True, False):
            with self.subTest(PHASEKIT_CONTRACT=with_var):
                r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                                   text=True, timeout=60, env=self.env(with_var))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), self.want)

    @unittest.skipUnless(HAVE_NODE, "node required")
    def test_js(self):
        code = qg_snippet("js") + "\nconsole.log(JSON.stringify(Object.keys(facts).sort()));\n"
        for with_var in (True, False):
            with self.subTest(PHASEKIT_CONTRACT=with_var):
                r = subprocess.run(["node", "--input-type=module", "-e", code], cwd=REPO_ROOT,
                                   capture_output=True, text=True, timeout=60, env=self.env(with_var))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), self.want)


GATE_RECORDS_CONTRACT = """#!/usr/bin/env bash
PHASEKIT_VERIFY_CONFIGURED=1
c="${PHASEKIT_CONTRACT:-UNSET}"
ok=no; jq -e '.interface == "phasekit"' "$c" >/dev/null 2>&1 && ok=yes
printf '%s %s\\n' "$c" "$ok" >> "${STUB_DIR:?}/gate-contract"
exit 0
"""

SESSION_RECORDS_CONTRACT = r"""
c="${PHASEKIT_CONTRACT:-UNSET}"
ok=no; jq -e '.interface == "phasekit"' "$c" >/dev/null 2>&1 && ok=yes
printf '%s %s\n' "$c" "$ok" >> "$STUB_DIR/session-contract"
"""


class TheLoopExportsIt(unittest.TestCase):
    """In whichever layout PHASEKIT_TEST_LAYOUT names: the session, the loop's
    gate and `phasekit verify` all see the RUNNING engine's contract, whatever
    the caller's environment said."""

    def repo(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.layout.put("contracts/interface.json", src=CONTRACT)
        repo.write("scripts/phasekit-verify.sh", GATE_RECORDS_CONTRACT, executable=True)
        repo.git("add", "-A")
        repo.git("commit", "-qm", "a gate that records the contract it sees")
        self.want = f"{(repo.layout.engine / 'contracts' / 'interface.json').resolve()} yes"
        return repo

    def lines(self, repo, name):
        p = repo.stub / name
        return p.read_text().splitlines() if p.exists() else []

    def test_the_session_and_the_gate_see_the_engines_contract(self):
        repo = self.repo()
        repo.scenario(SESSION_RECORDS_CONTRACT + H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_CONTRACT": "/nowhere/interface.json"})
        out = r.stdout + r.stderr
        self.assertEqual(self.lines(repo, "session-contract"), [self.want], out)
        gate = self.lines(repo, "gate-contract")
        self.assertTrue(gate, out)
        self.assertEqual(set(gate), {self.want}, out)
        if repo.layout.pinned:
            # whole path components: the pinned engine is a SIBLING named <repo>-engine
            self.assertFalse(Path(self.want.split()[0]).is_relative_to(repo.repo.resolve()),
                             "pinned: never a project path")

    def test_phasekit_verify_sees_it(self):
        repo = self.repo()
        repo.write("src.txt", "changed\n")
        r = repo.run_verb("verify", env={"PHASEKIT_CONTRACT": "/nowhere/interface.json"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.lines(repo, "gate-contract"), [self.want], r.stdout + r.stderr)


class TheContractDeclaresIt(unittest.TestCase):
    def test_the_env_entry(self):
        env = {e["name"]: e for e in json.loads(CONTRACT.read_text())["env"]}
        e = env.get("PHASEKIT_CONTRACT")
        self.assertIsNotNone(e, "PHASEKIT_CONTRACT is not declared")
        self.assertEqual(e["kind"], "exported")
        self.assertEqual(sorted(e["set_by"]), ["scripts/enrich-project.py", "scripts/run-until-done.sh"])
        self.assertFalse(e["container_forwarded"], "each runner sets its own; a host path means nothing inside")

    def test_the_rule_names_it(self):
        text = " ".join(QUALITY_GATES.read_text().split())
        self.assertIn("PHASEKIT_CONTRACT", text)
        self.assertIn("phasekit facts --path", text)
        conv = {c["name"]: c for c in json.loads(CONTRACT.read_text())["conventions"]}["declared-surface"]
        self.assertIn("PHASEKIT_CONTRACT", conv["semantics"])
        self.assertIn("PHASEKIT_CONTRACT", (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text())


_uspec = importlib.util.spec_from_file_location(
    "pk_upgrade_gate_dc", Path(__file__).resolve().parent / "test_upgrade_gate.py")
U = importlib.util.module_from_spec(_uspec)
_uspec.loader.exec_module(U)


class TheVendoredUpgradeGateExportsIt(U.Fixture):
    """The vendored upgrade gate: the project's own (just-upgraded) copy, on
    the host and in the runner (the project is mounted at /workspace)."""

    GATE = 'printf "%s" "${PHASEKIT_CONTRACT:-UNSET}" > "${PK_GATE_OUT:?}"'

    def test_host(self):
        out = self.tmp / "gate-contract"
        r = self.upgrade(env={"PK_GATE_OUT": str(out), "PHASEKIT_CONTRACT": "/nowhere/interface.json"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out.read_text(), str(self.project.resolve() / "contracts" / "interface.json"))

    def test_runner(self):
        r = self.upgrade(mode="container", docker="ready",
                         env={"PK_GATE_OUT": "/dev/null", "PHASEKIT_CONTRACT": "/nowhere/interface.json",
                              "PHASEKIT_FORWARD_ENV": "PHASEKIT_CONTRACT,PK_GATE_OUT"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        run = next(c for c in self.docker_calls() if c.startswith("run "))
        self.assertIn("-e PHASEKIT_CONTRACT=/workspace/contracts/interface.json", run)
        self.assertNotIn("-e PHASEKIT_CONTRACT ", run, "a host value is never forwarded into the runner")


if __name__ == "__main__":
    unittest.main()
