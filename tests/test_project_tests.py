#!/usr/bin/env python3
"""A project's tests test the project (v0.19.3; Aaron 2026-10-09).

The fleet sweep (foundry-meta reviews/REVIEW-2026-10-09-process-tests.md)
found about 84k lines and 1,860 tests in three projects that tested SPEC
wording, ledgers, records and phasekit's own loop, not the product. The rule
(docs/QUALITY_GATES.md "A project's tests test the project") is adopted, not
enforced. What this release pins, all advisory (never a red gate, never a
refusal):

  * `process-reads`: test files that READ a process document by path, decided
    by the scaffold-reads lexer, counting only reads rooted in THIS project (a
    supervisor's fixture trees are data); recorded in boundary-state.json
    `process_reads` by the loop, printed by `phasekit check` in both layouts;
  * the `scaffold-reads` widening: a test file whose only subject is phasekit
    (it reads the contract or `phasekit facts` and imports no project module);
  * `criterion-suites` in `phasekit check`: large files of per-criterion tests
    and families of per-iteration files;
  * `phasekit migrate` reports the process-reads count and never refuses on it;
  * the rule's text where a session, a lead and a reviewer read rules.

Every test here is red on v0.19.2.

Run from the repo root: python3 -m unittest tests.test_project_tests
"""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-surface.py"
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
CONTRACT = REPO_ROOT / "contracts" / "interface.json"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load("pk_boundary_harness_pt", Path(__file__).resolve().parent / "test_boundary_state.py")
EO = _load("pk_engine_outside_pt", Path(__file__).resolve().parent / "test_engine_outside.py")
S = _load("pk_surface_pt", TOOL)


def manifest(paths=("scripts/run-until-done.sh", "contracts/interface.json")):
    files = [{"path": p, "ownership": "scaffold", "text": True, "sha256": "0" * 64,
              "sha256_strict": "0" * 64} for p in paths]
    return json.dumps({"schema_version": 3, "scaffold_version": "v0.19.3", "files": files}, indent=2)


# --- process documents, read by a test (the sweep's shapes) -----------------------
PROCESS_READS = {
    # xmeo-v3: a read-named helper over the bare path
    "tests/tooling/iteration-29-invariants.test.ts":
        "const spec = read('docs/SPEC.md');\nconst roadmap = read('docs/ROADMAP.md');\n",
    # xmeo-v3: pieces joined under a root computed from the test's location
    "server/src/WindowedPace.test.ts":
        "const before = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "
        "'..', '..', 'artifacts', 'ac123-pace-cost-before.json'), 'utf8'));\n",
    # foundry-dashboard: new URL relative to the test file
    "test/spec-contract-criteria.test.js":
        "const SPEC = readFileSync(new URL('../docs/SPEC.md', import.meta.url), 'utf8');\n"
        "const memo = readFileSync(new URL('../artifacts/decision-memo-phase-12.md', import.meta.url), 'utf8');\n",
    # foundry-orchestrator: a root bound to __file__, then a family member
    "tests/test_learnings_harvest.py":
        "from pathlib import Path\nROOT = Path(__file__).resolve().parent.parent\n"
        "ARCHIVE = ROOT / \"docs\" / \"LEARNINGS-ARCHIVE.md\"\n"
        "def test_ledger():\n    assert ARCHIVE.read_text()\n",
    # a root bound through another name (xmeo: ROOT = resolve(HERE, '..'))
    "tests/tooling/sealclock.test.ts":
        "const HERE = dirname(fileURLToPath(import.meta.url));\nconst ROOT = resolve(HERE, '..', '..');\n"
        "const ROADMAP_MD = readFileSync(resolve(ROOT, 'docs', 'ROADMAP.md'), 'utf8');\n",
    # anything under artifacts/iterations, and the completion record
    "tests/test_records.py":
        "import json\ndef test_r():\n    n = json.loads(open(\"artifacts/iterations/21/complete.json\").read())\n"
        "    c = json.load(open(\"artifacts/project-complete.json\"))\n",
    # shell
    "tests/ledger_test.sh": "grep -q 'AC#12' \"$ROOT/docs/PHASES.md\"\ncat artifacts/deferrals.json\n",
    # review round 2 (R4): a parent of the test file's directory; the shell's own root
    "tests/test_here_parent.py":
        "from pathlib import Path\nHERE = Path(__file__).parent\n"
        "def test_a():\n    assert (HERE.parent / \"docs\" / \"SPEC.md\").read_text()\n",
    "tests/roadmap-check.sh":
        "ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)\"\ngrep -q OPEN \"$ROOT/docs/ROADMAP.md\"\n",
}
PROCESS_EXPECTED = {
    "server/src/WindowedPace.test.ts": ["artifacts/ac*"],
    "test/spec-contract-criteria.test.js": ["artifacts/decision-memo*", "docs/SPEC.md"],
    "tests/ledger_test.sh": ["artifacts/deferrals.json", "docs/PHASES*.md"],
    "tests/test_learnings_harvest.py": ["docs/LEARNINGS*.md"],
    "tests/test_here_parent.py": ["docs/SPEC.md"],
    "tests/roadmap-check.sh": ["docs/ROADMAP.md"],
    "tests/test_records.py": ["artifacts/iterations/", "artifacts/project-complete.json"],
    "tests/tooling/iteration-29-invariants.test.ts": ["docs/ROADMAP.md", "docs/SPEC.md"],
    "tests/tooling/sealclock.test.ts": ["docs/ROADMAP.md"],
}

NOT_PROCESS_READS = {
    # a supervisor's fixture trees: a parameter, a subscript, a scratch binding
    "tests/test_supervisor.py":
        "import json\nfrom pathlib import Path\n"
        "def _read_approval(project_path: Path):\n"
        "    return json.loads((project_path / \"artifacts\" / \"phase-approval.json\").read_text())\n"
        "def test_onramp(tmp_path):\n    fx = make(tmp_path)\n    project = fx[\"project\"]\n"
        "    phases = (project / \"docs\" / \"PHASES.md\").read_text()\n"
        "    again = (fx[\"project\"] / \"docs\" / \"SPEC.md\").read_text()\n",
    "test/fixture.test.js":
        "const dir = mkdtempSync(join(tmpdir(), 'x-'));\n"
        "const spec = readFileSync(join(dir, 'docs', 'SPEC.md'), 'utf8');\n",
    # data, not reads: prose, a comment, a list, a sample in a string, a write
    "tests/test_prose.py":
        "def test_p():\n    \"\"\"acceptance per docs/PHASES.md Phase 4 (open('docs/SPEC.md'))\"\"\"\n"
        "    # see docs/SPEC.md\n    DOCS = [\"docs/SPEC.md\", \"docs/ROADMAP.md\"]\n"
        "    open(\"artifacts/phase-approval.json\", \"w\").write(\"{}\")\n",
    "test/sample.test.js": "const planted = \"readFileSync('docs/SPEC.md')\";\n// read('docs/LEARNINGS.md')\n",
    # the product's own docs and data
    "tests/test_product.py": "def test_x():\n    assert open(\"docs/PROTOCOL.md\").read()\n"
                             "    assert open(\"artifacts/logs/run.json\").read()\n",
    # a fixture CODE file is never a test
    "tests/fixtures/old.test.js": "readFileSync('docs/SPEC.md');\n",
    # review round 1 (F3): fixture roots named like roots, a module-level scratch
    # root, a scratch tree under the working directory, a helper over tmp_path
    "tests/test_self_root.py":
        "import tempfile\nfrom pathlib import Path\nclass T:\n    def setUp(self):\n"
        "        self.root = Path(tempfile.mkdtemp())\n    def test_a(self):\n"
        "        assert (self.root / \"docs\" / \"SPEC.md\").read_text()\n",
    "tests/test_param_root.py":
        "def test_a(repo, project_root):\n    assert (repo / \"docs\" / \"PHASES.md\").read_text()\n"
        "    assert (project_root / \"artifacts\" / \"deferrals.json\").read_text()\n",
    "tests/test_module_tmp.py":
        "import tempfile\nfrom pathlib import Path\nROOT = Path(tempfile.mkdtemp())\n"
        "def test_a():\n    assert (ROOT / \"docs\" / \"SPEC.md\").read_text()\n",
    "test/tmp-root.test.js":
        "const tmpRoot = mkdtempSync(join(tmpdir(), 'x'));\nreadFileSync(`${tmpRoot}/docs/SPEC.md`);\n"
        "const tmp = mkdtempSync(join(process.cwd(), '.tmp-'));\nreadFileSync(join(tmp, 'docs', 'SPEC.md'));\n",
    "tests/test_convention_repo.py":
        "def test_a(tmp_path):\n    outside = _convention_repo(tmp_path / \"outside\")\n"
        "    spec = outside / \"docs\" / \"SPEC.md\"\n    assert spec.read_text()\n",
    # (F4) a fixture tree spelled in the literal; a product's own doc of that name
    "tests/test_fixture_tree.py":
        "def test_a():\n    assert open(\"tests/fixtures/sample-project/docs/SPEC.md\").read()\n"
        "    assert open(\"site/docs/SPEC.md\").read()\n",
    # (F5) a shell test over a scratch tree
    "tests/scratch.bats":
        "TMP=$(mktemp -d)\necho x > \"$TMP/docs/SPEC.md\"\nrun grep -c x \"$TMP/docs/SPEC.md\"\n",
    # (F7) a relative constant joined onto a fixture
    "tests/test_rel_const.py":
        "SPEC_REL = \"docs/SPEC.md\"\ndef test_a(tmp_path):\n    assert load_spec(tmp_path / SPEC_REL)\n",
    # review round 2 (R3): a relative constant joined onto a fixture, read as a
    # member and in shell; (R5) a parameter named like a root
    "tests/test_rel_member.py":
        "SPEC_REL = \"docs/SPEC.md\"\ndef test_a(tmp_path):\n    assert (tmp_path / SPEC_REL).read_text()\n",
    "tests/rel_test.sh": "SPEC=docs/SPEC.md\nT=\"$(mktemp -d)\"\ngrep -q x \"$T/$SPEC\"\n",
    "tests/test_root_param.py":
        "def test_x(PROJECT_ROOT):\n    assert (PROJECT_ROOT / \"docs\" / \"SPEC.md\").read_text()\n",
    # a helper module (not a test by name) is support code
    "tests/tooling/lib/records.ts": "export const spec = () => read('docs/SPEC.md');\n",
}


class _Scratch(unittest.TestCase):
    def make(self, files, with_manifest=True, pinned=False):
        root = Path(tempfile.mkdtemp(prefix="pk-pt-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel, text in {**files, **({"bin/tool": "#!/bin/sh\necho ok\n"} if "tests/test_bin_tool.py" in files
                                       else {})}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
            if rel.startswith("bin/"):
                (root / rel).chmod(0o755)
        if with_manifest:
            (root / ".scaffold").mkdir()
            (root / ".scaffold" / "manifest.json").write_text(manifest())
        if pinned:
            (root / ".phasekit-version").write_text("v0.19.3\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return root

    def cli(self, root, *args):
        r = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", *args, str(root)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r


class ProcessReads(_Scratch):
    def test_a_large_file_of_bound_names_stays_fast(self):
        """Review F8: six mutually bound names and reads over a 30k-line module
        took 69 s (past the loop's 60 s timeout, losing scaffold_reads too)."""
        head = "".join(f"n{i} = n{(i + 1) % 6} / 'x'\n" for i in range(6))
        body = "".join(f"def test_{i}():\n    x = {i}\n" for i in range(15000))
        reads = "".join("def test_r():\n    (n0 / 'docs' / 'SPEC.md').read_text()\n" for _ in range(5))
        root = self.make({"tests/test_big.py": head + body + reads})
        import time
        t0 = time.monotonic()
        S.process_reads(root)
        self.assertLess(time.monotonic() - t0, 15.0)

    def test_one_huge_file_cannot_outlive_the_budget(self):
        """Review R2: the deadline is checked per read site, not per file."""
        head = "".join(f"const n{i} = join(n{(i + 1) % 6}, 'x');\n" for i in range(6))
        body = "".join(f"const v{i} = {i};\n" for i in range(30000))
        reads = "".join(f"it('r{i}', () => readFileSync(join(n0, 'docs', 'SPEC.md')));\n" for i in range(200))
        root = self.make({"test/huge.test.js": head + body + reads})
        import time
        t0 = time.monotonic()
        try:
            S.process_reads(root, deadline=time.monotonic() + 2)
        except S._OutOfTime:
            pass
        self.assertLess(time.monotonic() - t0, 10.0)

    def test_a_scan_past_its_budget_raises_and_the_cli_records_null(self):
        root = self.make(PROCESS_READS)
        with self.assertRaises(S._OutOfTime):
            S.process_reads(root, deadline=0)

    def test_every_shape_the_sweep_found_is_named_with_its_documents(self):
        got = {e["test"]: e["paths"] for e in S.process_reads(self.make(PROCESS_READS))}
        self.assertEqual(got, PROCESS_EXPECTED)

    def test_fixture_trees_data_and_product_docs_are_not_process_reads(self):
        self.assertEqual(S.process_reads(self.make(NOT_PROCESS_READS)), [])

    def test_the_json_record_and_the_named_line(self):
        root = self.make({**PROCESS_READS, **NOT_PROCESS_READS})
        out = json.loads(self.cli(root, "--json").stdout)
        self.assertEqual({e["test"]: e["paths"] for e in out["process_reads"]}, PROCESS_EXPECTED)
        self.assertTrue(out["process_line"].startswith(
            "ADVISORY process-reads: 9 test file(s) read process documents ("), out["process_line"])
        self.assertIn("A project's tests test the project", out["process_line"])
        plain = self.cli(root).stdout.splitlines()
        self.assertIn("  tests/test_records.py: artifacts/iterations/, artifacts/project-complete.json", plain)

    def test_a_clean_tree_records_an_empty_list_and_prints_nothing(self):
        out = json.loads(self.cli(self.make(NOT_PROCESS_READS), "--json").stdout)
        self.assertEqual(out["process_reads"], [])
        self.assertEqual(out["process_line"], "")

    def test_it_needs_no_manifest(self):
        root = self.make(PROCESS_READS, with_manifest=False, pinned=True)
        self.assertEqual(len(S.process_reads(root)), len(PROCESS_EXPECTED))

    def test_the_documents_are_the_declared_ones(self):
        fact = json.loads(CONTRACT.read_text())["facts"]["process_reads"]
        self.assertEqual(fact["documents"], [str(p) for p in S.PROCESS_DOCS])
        self.assertEqual(fact["advisory"], S.PROCESS_ID)
        self.assertEqual(fact["record_field"], S.PROCESS_FIELD)
        self.assertTrue(S.process_line([{"test": "t", "paths": []}]).startswith(fact["line_prefix"]))
        self.assertTrue(S.suites_line({"large": [], "family": {"files": 5, "lines": 9, "examples": ["a"]}})
                        .startswith(fact["check_line_prefix"]))


# --- phasekit as a test's only subject ----------------------------------------------
PHASEKIT_ONLY = {
    # the exported path, read and nothing of the project imported
    "test/declared-surface.test.js":
        "import { readFileSync } from 'node:fs';\n"
        "const facts = JSON.parse(readFileSync(process.env.PHASEKIT_CONTRACT, 'utf8')).facts;\n",
    "tests/test_facts.py":
        "import json, os\ndef test_f():\n    p = os.environ.get(\"PHASEKIT_CONTRACT\")\n"
        "    assert json.load(open(p))[\"facts\"]\n",
    # the CLI, as argv
    "tests/tooling/facts.test.ts":
        "import { execFileSync } from 'node:child_process';\n"
        "const f = JSON.parse(execFileSync('phasekit', ['facts', '--json'], { encoding: 'utf8' }));\n",
    # the in-tree contract, through a helper's exported binding
    "tests/tooling/lib/phasekit-facts.ts":
        "export const CONTRACT = 'contracts/interface.json';\n"
        "export const installed = (root: string) => readFileSync(resolve(root, CONTRACT), 'utf8');\n",
    "tests/tooling/phasekit-facts.test.ts":
        "import { CONTRACT } from './lib/phasekit-facts';\nexpect(read(CONTRACT)).toBeTruthy();\n",
}
PHASEKIT_ONLY_EXPECTED = {
    "test/declared-surface.test.js": ["$PHASEKIT_CONTRACT"],
    "tests/test_facts.py": ["$PHASEKIT_CONTRACT"],
    "tests/tooling/facts.test.ts": ["phasekit facts"],
    # (the helper lib/phasekit-facts.ts is support code, not named; review F6)
    "tests/tooling/phasekit-facts.test.ts": ["contracts/interface.json"],
}
CONSUMERS = {
    # the project's own code reads phasekit's artifacts: a pin is legitimate
    "orchestrator/__init__.py": "",
    "orchestrator/boundary_state.py": "FIELDS = ('pass',)\n",
    "tests/test_boundary_pin.py":
        "import json, os\nfrom orchestrator.boundary_state import FIELDS\n"
        "def test_declared():\n    c = json.load(open(os.environ[\"PHASEKIT_CONTRACT\"]))\n"
        "    assert set(FIELDS) <= set(c['artifacts'][0]['keys'])\n",
    "src/reader.js": "export const KEYS = ['pass'];\n",
    "test/reader.test.js":
        "import { KEYS } from '../src/reader.js';\n"
        "const c = JSON.parse(readFileSync(process.env.PHASEKIT_CONTRACT, 'utf8'));\n",
    # a test that runs a project script beside the contract read
    "scripts/report.sh": "#!/usr/bin/env bash\necho ok\n",
    "tests/report_test.sh": "jq . \"$PHASEKIT_CONTRACT\" >/dev/null\nbash scripts/report.sh\n",
    # a supervisor's vendored provider copy is the project's own file
    "test/vendored.test.js":
        "const c = readFileSync(new URL('../vendor/contracts/phasekit/interface.json', import.meta.url));\n",
    # review round 1 (F6): a script run by joined pieces, an extension-less tool
    "scripts/check.py": "print('ok')\n",
    "tests/test_joined_script.py":
        "import os, subprocess\nfrom pathlib import Path\nROOT = Path(__file__).parent.parent\n"
        "def test_a():\n    c = os.environ[\"PHASEKIT_CONTRACT\"]\n"
        "    subprocess.run([\"python3\", ROOT / \"scripts\" / \"check.py\", c], check=True)\n",
    "tests/test_bin_tool.py":
        "import os, subprocess\ndef test_a():\n    c = os.environ.get(\"PHASEKIT_CONTRACT\")\n"
        "    subprocess.run([\"bin/tool\", c], check=True)\n",
    # names, not reads
    "tests/test_env_names.py": "NAMES = [\"PHASEKIT_CONTRACT\", \"PHASEKIT_ITER_MARKER\"]\n"
                               "# reads $PHASEKIT_CONTRACT\n",
}


class PhasekitOnly(_Scratch):
    def test_a_test_whose_only_subject_is_phasekit_is_named(self):
        got = {e["test"]: e["paths"] for e in S.phasekit_only_reads(self.make({**PHASEKIT_ONLY, **CONSUMERS}))}
        self.assertEqual(got, PHASEKIT_ONLY_EXPECTED)

    def test_it_rides_in_scaffold_reads_in_both_layouts(self):
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                root = self.make({**PHASEKIT_ONLY, **CONSUMERS}, with_manifest=not pinned, pinned=pinned)
                out = json.loads(self.cli(root, "--json").stdout)
                self.assertEqual({e["test"]: e["paths"] for e in out["scaffold_reads"]}, PHASEKIT_ONLY_EXPECTED)
                self.assertTrue(out["line"].startswith(
                    "ADVISORY scaffold-reads: 4 test file(s) read scaffold-owned files ("), out["line"])
                self.assertIn("has phasekit as its only subject", out["line"])

    def test_the_declared_spellings(self):
        fact = json.loads(CONTRACT.read_text())["facts"]["scaffold_reads"]
        self.assertEqual(fact["phasekit_only_paths"],
                         [S.ONLY_ENV_SPELLING, S.ONLY_CLI_SPELLING, S.CONTRACT_PATH])


# --- per-criterion suites -----------------------------------------------------------
def _criterion_suite(n=500, criterion=0.9):
    body = []
    for i in range(n):
        name = f"test_ac{i}_names_what_the_code_spells" if i < n * criterion else f"test_behaviour_{i}"
        body.append(f"def {name}():\n    x = {i}\n    assert x == {i}\n    assert True\n    pass\n\n\n")
    return "".join(body)


class CriterionSuites(_Scratch):
    def test_a_large_file_of_criterion_tests_and_a_family_of_iteration_files(self):
        files = {"tests/test_resolver_spec.py": _criterion_suite(),
                 "tests/test_big_product.py": _criterion_suite(criterion=0.2),
                 "tests/test_small_ac.py": _criterion_suite(n=10)}
        files.update({f"tests/tooling/iteration-{n}-invariants.test.ts": "it('x', () => {});\n"
                      for n in (18, 21, 25, 26, 29)})
        report = S.criterion_suites(self.make(files))
        self.assertEqual([e["test"] for e in report["large"]], ["tests/test_resolver_spec.py"])
        self.assertEqual(report["large"][0]["criterion_tests"], 450)
        self.assertEqual(report["family"]["files"], 5)
        line = S.suites_line(report)
        self.assertTrue(line.startswith("ADVISORY criterion-suites: 1 test file(s) over 3000 lines"), line)
        self.assertIn("5 test files are named per iteration or phase", line)

    def test_js_test_names_count_and_four_files_are_not_a_family(self):
        js = "".join(f"it('AC #{i} exists and is the highest', () => {{\n  expect(1).toBe(1);\n"
                     "  expect(2).toBe(2);\n  expect(3).toBe(3);\n  expect(4).toBe(4);\n  expect(5).toBe(5);\n"
                     "  expect(6).toBe(6);\n});\n\n" for i in range(400))
        files = {"test/spec-contract-criteria.test.js": js}
        files.update({f"tests/iteration-{n}.test.ts": "x\n" for n in range(4)})
        report = S.criterion_suites(self.make(files))
        self.assertEqual([e["test"] for e in report["large"]], ["test/spec-contract-criteria.test.js"])
        self.assertEqual(report["family"]["files"], 0)

    def test_a_clean_tree_says_nothing(self):
        self.assertEqual(S.suites_line(S.criterion_suites(self.make(NOT_PROCESS_READS))), "")


class PhasekitCheck(_Scratch):
    def test_vendored_check_prints_the_three_advisories_and_its_exit_code_is_unchanged(self):
        noisy = {**PROCESS_READS, **PHASEKIT_ONLY, "tests/test_resolver_spec.py": _criterion_suite()}
        a = subprocess.run([sys.executable, str(ENRICH), "--check", str(self.make(noisy))],
                           capture_output=True, text=True, timeout=120)
        b = subprocess.run([sys.executable, str(ENRICH), "--check", str(self.make(NOT_PROCESS_READS))],
                           capture_output=True, text=True, timeout=120)
        for prefix in ("ADVISORY process-reads:", "ADVISORY scaffold-reads:", "ADVISORY criterion-suites:"):
            self.assertIn(prefix, a.stdout)
            self.assertNotIn(prefix, b.stdout)
        self.assertEqual(a.returncode, b.returncode, "advisory only")


class PinnedCheckAndMigrate(EO.EngineFixture):
    def test_a_pinned_check_prints_them_and_stays_clean(self):
        proj = self.new_project()
        (proj / "tests").mkdir()
        (proj / "tests" / "spec.test.js").write_text(PROCESS_READS["tests/tooling/iteration-29-invariants.test.ts"])
        (proj / "tests" / "facts.test.js").write_text(PHASEKIT_ONLY["test/declared-surface.test.js"])
        EO._git(proj, "add", "-A")
        EO._git(proj, "commit", "-qm", "tests")
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ADVISORY process-reads: 1 test file(s)", r.stdout)
        self.assertIn("ADVISORY scaffold-reads: 1 test file(s)", r.stdout)

    def test_the_migrate_preflight_reports_process_reads_and_never_refuses_on_them(self):
        pin = _load("pk_pin_pt", REPO_ROOT / "scripts" / "phasekit-pin.py")
        root = Path(tempfile.mkdtemp(prefix="pk-pt-mig-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel, text in PROCESS_READS.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        (root / ".scaffold").mkdir()
        (root / ".scaffold" / "manifest.json").write_text(manifest())
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pin.migrate_preflight(root)  # no engine-path reads: returns, never raises
        self.assertIn("pre-flight: 9 test file(s) read process documents", buf.getvalue())


class TheLoopRecordsIt(unittest.TestCase):
    def _repo(self, files):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.layout.put("scripts/phasekit-surface.py", src=TOOL)
        repo.write(".scaffold/manifest.json", manifest(["scripts/run-until-done.sh", "scripts/phasekit-surface.py"]))
        for rel, text in files.items():
            repo.write(rel, text)
        repo.git("add", "-A")
        repo.git("commit", "-qm", "tests")
        repo.scenario(H.APPROVE_SCENARIO)
        return repo

    def test_the_gate_stays_green_and_boundary_state_names_the_files(self):
        repo = self._repo({"tests/test_records.py": PROCESS_READS["tests/test_records.py"]})
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual(out.count("ADVISORY process-reads: 1 test file(s) read process documents "
                                   "(tests/test_records.py)"), 1, "once per session\n" + out)
        self.assertIn("boundary-state: rested (step 7)", out, "advisory only — the gate stays green")
        state = json.loads((repo.repo / "artifacts" / "boundary-state.json").read_text())
        self.assertEqual(state["process_reads"], [{"test": "tests/test_records.py", "paths": [
            "artifacts/iterations/", "artifacts/project-complete.json"]}])
        self.assertTrue(state["process_reads_at"])

    def test_a_clean_tree_records_an_empty_list(self):
        repo = self._repo({"tests/test_product.py": NOT_PROCESS_READS["tests/test_product.py"]})
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("ADVISORY process-reads", r.stdout + r.stderr)
        state = json.loads((repo.repo / "artifacts" / "boundary-state.json").read_text())
        self.assertEqual(state.get("process_reads"), [])

    def test_the_record_is_carried_across_pass_starts(self):
        loop = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()
        self.assertIn("process_reads: (.process_reads // null), process_reads_at: (.process_reads_at // null)", loop)


class TheContractAndTheRule(unittest.TestCase):
    def test_the_record_and_the_convention_are_declared(self):
        data = json.loads(CONTRACT.read_text())
        bs = {a["name"]: a for a in data["artifacts"]}["boundary-state.json"]
        self.assertIn("process_reads", bs["keys"])
        self.assertIn("process_reads_at", bs["keys"])
        conv = {c["name"]: c for c in data["conventions"]}["project-tests"]
        self.assertEqual(conv["marker"], "A project's tests test the project")
        for rel in conv["declared_in"]:
            with self.subTest(file=rel):
                self.assertIn(conv["marker"], (REPO_ROOT / rel).read_text())

    def test_the_completion_records_keys_name_its_phase(self):
        pc = {a["name"]: a for a in json.loads(CONTRACT.read_text())["artifacts"]}["project-complete.json"]
        self.assertIn("phase", pc["keys"])
        self.assertIn("phase_as_written", pc["keys"])
        self.assertNotIn("this iteration's approval phase, when absent", pc["lifecycle"])

    def test_the_rule_reaches_sessions_leads_and_reviewers(self):
        qg = " ".join((REPO_ROOT / "docs" / "QUALITY_GATES.md").read_text().split())
        self.assertIn("### A project's tests test the project", qg)
        self.assertIn("at least one test that exercises the project's code on its primary path", qg)
        self.assertIn("a test whose only subject is a phasekit fact is a phasekit test: it belongs upstream",
                      qg.lower())
        self.assertIn("Records are not test fixtures", qg)
        # the worked examples that taught downstream to test phasekit are gone
        self.assertNotIn("The commit gate refuses a credential in LEARNINGS\": read", qg)
        self.assertIn("Tests test the product.", (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text())
        lead = (REPO_ROOT / ".claude" / "agents" / "project-lead.md").read_text()
        self.assertIn("reject a builder deliverable whose new tests read `docs/SPEC.md`", lead)
        reviewer = (REPO_ROOT / ".claude" / "agents" / "code-reviewer.md").read_text()
        self.assertIn("is a finding (remove it), not coverage", reviewer)


if __name__ == "__main__":
    unittest.main()
