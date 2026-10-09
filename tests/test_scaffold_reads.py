#!/usr/bin/env python3
"""The `scaffold-reads` advisory (v0.18.3; queue row 1194, Aaron 2026-09-30).

Downstream tests kept parsing phasekit's INTERNAL text — the vendored loop's
bash bodies, its rm lists, a single-quoted grep literal — and every loop
reshape broke them (four fix rows: 1182, 1229, 1281, 1291). The rule
(docs/QUALITY_GATES.md "Tests read the declared surface"): a test reads the
project's own tree and phasekit's declared surface, never scaffold-owned
files. Like the hermetic-tests advisory it is adopted, not enforced: the loop
and `phasekit check` NAME offenders (advisory id `scaffold-reads`, record field
boundary-state.json `scaffold_reads`) and never refuse.

Every test here is red on v0.18.2 (no scripts/phasekit-surface.py, no advisory).

Run from the repo root: python3 -m unittest tests.test_scaffold_reads
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "phasekit-surface.py"
ENGINE = REPO_ROOT / "scripts" / "enrich-project.py"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_sr", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

SCAFFOLD = ["scripts/run-until-done.sh", "scripts/container-setup.sh", "docs/QUALITY_GATES.md",
            ".claude/hooks/deny-dangerous-commands.sh", "CONTINUE_PROMPT.txt", "contracts/interface.json"]


def manifest(paths=SCAFFOLD, extra=()):
    files = [{"path": p, "ownership": "scaffold", "text": True, "sha256": "0" * 64,
              "sha256_strict": "0" * 64} for p in paths]
    files += [{"path": p, "ownership": "bootstrap-frozen", "text": True, "sha256": "0" * 64,
               "sha256_strict": "0" * 64} for p in extra]
    return json.dumps({"schema_version": 3, "scaffold_version": "v0.18.3", "files": files}, indent=2)


OFFENDERS = {
    # Python: a joined path, then read_text through a variable (orchestrator shape)
    "tests/test_loop_parse.py":
        'from pathlib import Path\nROOT = Path(__file__).parent.parent\n'
        'LOOP = ROOT / "scripts" / "run-until-done.sh"\n'
        'def test_x():\n    body = LOOP.read_text()\n    assert "post_verify_commit_gates" in body\n',
    # Python: through an attribute (review round 1, MINOR 6)
    "tests/test_attr.py":
        'class T:\n    LOOP = ROOT / "scripts" / "container-setup.sh"\n'
        '    def test_a(self):\n        assert "x" in self.LOOP.read_text()\n',
    # Python: open() on a literal
    "tests/test_prompt.py": 'def test_p():\n    assert "rule" in open("CONTINUE_PROMPT.txt").read()\n',
    # Node: readFileSync(path.join(...)) (xmeo shape)
    "tests/tooling/learnings.test.ts":
        "import { readFileSync } from 'node:fs';\nimport path from 'node:path';\n"
        "const loop = readFileSync(path.join(root, 'scripts', 'run-until-done.sh'), 'utf8');\n",
    # Node: a helper named for what it does, on a literal
    "tests/tooling/gates.test.ts": "const doc = readText('docs/QUALITY_GATES.md');\n",
    # bash: grep over the hook
    "tests/hook_test.sh": "grep -q 'dangerous' .claude/hooks/deny-dangerous-commands.sh\n",
    # Python: os.path.join pieces on the reading line
    "tests/test_env.py":
        'import os\ndef test_e():\n    s = open(os.path.join(ROOT, "scripts", "container-setup.sh")).read()\n',
}

CLEAN = {
    # the declared surface itself, consumed by the project's own code (v0.19.3:
    # a test that reads it and touches NO project code has phasekit as its only
    # subject, and the advisory names it — tests/test_project_tests.py)
    "app.py": "KEYS = ('pass',)\n",
    "src/keys.js": "export const KEYS = ['pass'];\n",
    "tests/test_contract.py":
        'import json\nfrom app import KEYS\nfacts = json.load(open("contracts/interface.json"))["facts"]\n',
    "tests/test_vendored_contract.py":
        'from pathlib import Path\nC = Path("vendor/contracts/phasekit/interface.json").read_text()\n',
    "tests/tooling/facts.test.ts":
        "import { KEYS } from '../../src/keys.js';\n"
        "const facts = JSON.parse(execFileSync('bash', ['scripts/phasekit.sh', 'facts', '--json']));\n",
    # RUNNING a scaffold script is not reading it
    "tests/test_run_gate.py":
        'import subprocess\ndef test_g():\n'
        '    subprocess.run(["bash", "scripts/run-until-done.sh", "verify"], check=True)\n',
    # a mention in prose/comments, no read
    "tests/test_mention.py": '# the loop (scripts/run-until-done.sh) owns commits\nassert 1 + 1 == 2\n',
    # the project's own tree
    "tests/test_own.py": 'def test_o():\n    assert open("src/app.py").read()\n',
    # prose, not commands (review of the fleet dry run: each was a false hit)
    "tests/test_docstring.py":
        'def test_d():\n    """Parsed with `json.loads`. `docs/QUALITY_GATES.md` is still the source."""\n'
        '    """the source of truth is scripts/run-until-done.sh, not a copy"""\n',
    # a name bound to a LIST of paths, passed to a read-named helper, is data
    "tests/test_names.py":
        'SCAFFOLD_FILES = ["scripts/run-until-done.sh", ".claude/agents/x.md"]\n'
        'def test_n():\n    signals = _read_scope_warning(tmp, SCAFFOLD_FILES, reader)\n',
    # a captured log fixture naming a path is data, not a test
    "tests/fixtures/verify-tail.txt": "cat scripts/run-until-done.sh\n",
}


class _Scratch(unittest.TestCase):
    def make(self, files, with_manifest=True):
        root = Path(tempfile.mkdtemp(prefix="pk-sr-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        if with_manifest:
            (root / ".scaffold").mkdir()
            (root / ".scaffold" / "manifest.json").write_text(manifest())
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return root

    def reads(self, root):
        r = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", "--json", str(root)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class TheDetector(_Scratch):
    def test_every_offending_shape_is_flagged_and_named_with_its_paths(self):
        root = self.make({**OFFENDERS, **CLEAN})
        out = self.reads(root)
        self.assertEqual(out["advisory"], "scaffold-reads")
        got = {e["test"]: e["paths"] for e in out["scaffold_reads"]}
        self.assertEqual(got, {
            "tests/hook_test.sh": [".claude/hooks/deny-dangerous-commands.sh"],
            "tests/test_attr.py": ["scripts/container-setup.sh"],
            "tests/test_env.py": ["scripts/container-setup.sh"],
            "tests/test_loop_parse.py": ["scripts/run-until-done.sh"],
            "tests/test_prompt.py": ["CONTINUE_PROMPT.txt"],
            "tests/tooling/gates.test.ts": ["docs/QUALITY_GATES.md"],
            "tests/tooling/learnings.test.ts": ["scripts/run-until-done.sh"],
        })
        self.assertTrue(out["line"].startswith("ADVISORY scaffold-reads: 7 test file(s) read scaffold-owned files"))

    def test_a_contract_reading_tree_is_not_flagged(self):
        out = self.reads(self.make(CLEAN))
        self.assertEqual(out["scaffold_reads"], [])
        self.assertEqual(out["line"], "")

    def test_no_manifest_means_no_advisory(self):
        out = self.reads(self.make(OFFENDERS, with_manifest=False))
        self.assertEqual(out["scaffold_reads"], [])

    def test_the_plain_form_prints_one_named_line_then_the_offenders(self):
        root = self.make(OFFENDERS)
        r = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", str(root)],
                           capture_output=True, text=True, timeout=60)
        lines = r.stdout.splitlines()
        self.assertEqual(r.returncode, 0)
        self.assertTrue(lines[0].startswith("ADVISORY scaffold-reads:"), lines)
        self.assertIn("  tests/test_loop_parse.py: scripts/run-until-done.sh", lines)


class Facts(_Scratch):
    """`phasekit facts` answers for THIS project's phasekit (review round 1, MINOR 5)."""

    def _facts(self, cwd):
        return subprocess.run([sys.executable, str(TOOL), "facts", "--json"], cwd=str(cwd),
                              capture_output=True, text=True, timeout=60)

    def test_from_a_subdirectory_it_reads_the_projects_contract(self):
        root = self.make({"sub/x.txt": "x\n", "contracts/interface.json": MANIFEST.read_text()})
        r = self._facts(root / "sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), json.loads(MANIFEST.read_text())["facts"])

    def test_scaffold_reads_from_a_subdirectory_scans_the_whole_project(self):
        root = self.make(OFFENDERS)
        r = subprocess.run([sys.executable, str(TOOL), "scaffold-reads", "--json"], cwd=str(root / "tests"),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(len(json.loads(r.stdout)["scaffold_reads"]), len(OFFENDERS))

    def test_the_tool_never_asks_git_which_repository(self):
        # foundry-orchestrator's tree pins ONE "is this repo dirty / which repo
        # root" rule across every scripts/**/*.py, vendored files included: a
        # `rev-parse --show-toplevel` here refused its v0.18.3 upgrade gate (dry run)
        text = TOOL.read_text()
        self.assertNotIn("--show-toplevel", text)
        self.assertNotIn("--porcelain", text)

    def test_a_project_before_v0_18_3_exits_1_never_an_installs_facts(self):
        old = json.loads(MANIFEST.read_text())
        del old["facts"]
        root = self.make({"contracts/interface.json": json.dumps(old)})
        self.assertEqual(self._facts(root).returncode, 1)


# v0.18.5 (Aaron 2026-10-02, rider 1): a path that is only DATA is not a read.
# xmeo-v3's guard test tests/tooling/scaffold-read-surface.test.ts FORBIDS
# scaffold reads; it plants scaffold paths as code samples inside strings, in
# a forbidden-path list and in expected values, and v0.18.4 flagged it for
# five paths — each false positive files a "switch 1 test file" fix row.
XMEO_GUARD = REPO_ROOT / "tests" / "fixtures" / "scaffold-reads" / "xmeo-scaffold-read-surface.test.ts"
XMEO_SCAFFOLD = SCAFFOLD + [".devcontainer/Dockerfile", ".devcontainer/init-firewall.sh",
                            "docs/EXECUTION_MODES.md", ".claude/hooks/require-verdict.sh"]

DATA_ONLY = {
    # the shapes of the xmeo guard, one per file
    "tests/tooling/planted.test.ts":
        "const planted = [...lines, \"    const leak = read('.devcontainer/init-firewall.sh');\"].join('\\n');\n",
    "tests/tooling/forbidden-list.test.ts":
        "for (const p of ['.devcontainer/init-firewall.sh', 'docs/QUALITY_GATES.md']) {\n"
        "  expect(owned.has(p), p).toBe(true);\n}\n",
    "tests/tooling/expected.test.ts":
        "expect(forbiddenReads(planted, owned, constants)).toEqual(['.devcontainer/init-firewall.sh']);\n",
    "tests/tooling/table.test.ts":
        "const PLANTED = [\n  ['a read() helper', \"expect(read('docs/QUALITY_GATES.md')).not.toContain('x');\", "
        "'docs/QUALITY_GATES.md'],\n  ['resolve() segments',\n"
        "   \"readFileSync(resolve(repoRoot, '.devcontainer', 'Dockerfile'), 'utf8');\",\n"
        "   '.devcontainer/Dockerfile'],\n];\n",
    "tests/tooling/template.test.ts":
        "const LOOP = 'scripts/run-until-done.sh';\n"
        "const planted = `${libText}\\nconst x = readFileSync(resolve(repoRoot, '${LOOP}'), 'utf8');`;\n"
        "expect(forbiddenReads(planted, owned, pathConstants(planted))).toEqual([LOOP]);\n",
    "src/keys.js": "export const KEYS = ['pass'];\n",
    "tests/tooling/comment.test.ts":
        "import { KEYS } from '../../src/keys.js';\n"
        "// reads .devcontainer/init-firewall.sh no more\n/* read('docs/QUALITY_GATES.md') */\n"
        "const c = read('contracts/interface.json');\n",
    # a regex literal with quotes in it opens no string
    "tests/tooling/regex.test.ts":
        "const LITERAL = /^(['\"`])([^'\"`$]*)\\1$/;\nconst s = \"read('CONTINUE_PROMPT.txt')\";\n",
    # Python: a docstring, a tuple of paths, a constructor, an expectation
    "tests/test_data_py.py":
        'def test_d():\n    """open("scripts/run-until-done.sh") is what we refuse"""\n'
        '    e = ScaffoldRead("tests/t.py", "scripts/run-until-done.sh")\n'
        '    r = _reads(("tests/x.py", ("docs/QUALITY_GATES.md",)))\n'
        '    assert "docs/QUALITY_GATES.md" in load_report(path)\n'
        '    FORBIDDEN = ["cat scripts/run-until-done.sh"]\n',
    # shell: a heredoc fixture, an echo, a comment
    "tests/data_test.sh":
        "cat > fixture.sh <<'EOF'\ncat scripts/run-until-done.sh\nEOF\n"
        "echo \"grep x CONTINUE_PROMPT.txt\"\n"
        "true # source scripts/container-setup.sh\n",
    # review MINOR 3: a name that only contains the letters, and a write
    "tests/test_not_reads.py":
        'def test_n():\n    assert already_installed("scripts/run-until-done.sh")\n'
        '    open(os.path.join(tmp, "scripts", "container-setup.sh"), "w").write("x")\n',
    # review MAJOR 1: $'…' takes escapes, so the next line's quote is not flipped
    "tests/ansi_test.sh": "echo $'it\\'s'\necho 'never cat scripts/run-until-done.sh here'\n",
}

READS = {
    # TS: the xmeo shapes, written as CODE — real reads
    "tests/tooling/real-resolve.test.ts":
        "const t = readFileSync(resolve(repoRoot, '.devcontainer', 'Dockerfile'), 'utf8');\n",
    "tests/tooling/real-const.test.ts":
        "const scriptPath = resolve(__dirname, '../../.devcontainer/init-firewall.sh');\n"
        "const source = readFileSync(scriptPath, 'utf8');\n",
    "tests/tooling/real-expect.test.ts":
        "expect(read('docs/QUALITY_GATES.md')).not.toContain('x');\n",
    "tests/tooling/real-template.test.ts":
        "const t = readFileSync(`${root}/scripts/run-until-done.sh`, 'utf8');\n",
    "tests/tooling/real-exec.test.ts": "const t = execSync('cat CONTINUE_PROMPT.txt').toString();\n",
    "tests/tooling/after-regex.test.ts":
        "const LITERAL = /^(['\"`])$/;\nconst doc = readText('docs/EXECUTION_MODES.md');\n",
    # Python: argv, a command line, a member read on joined pieces, a call over lines
    "tests/test_argv.py":
        'import subprocess\ndef test_a():\n'
        '    subprocess.run(["grep", "-n", "x", "scripts/run-until-done.sh"], check=True)\n',
    "tests/test_cmdline.py":
        'import subprocess\nout = subprocess.check_output("cat CONTINUE_PROMPT.txt", shell=True)\n',
    "tests/test_member.py":
        'text = (ROOT / "scripts" / "container-setup.sh").read_text()\n',
    "tests/test_multiline.py":
        'import os\ns = open(\n    os.path.join(ROOT, "docs", "QUALITY_GATES.md"),\n    encoding="utf-8",\n).read()\n',
    # shell: source, `.`, a variable
    "tests/source_test.sh": 'source "$ROOT/scripts/container-setup.sh"\n. .claude/hooks/require-verdict.sh\n',
    "tests/var_test.sh": 'LOOP="$ROOT/scripts/run-until-done.sh"\ngrep -c x "$LOOP"\n',
    # review MAJOR 1: a substitution inside double quotes or an unquoted heredoc runs
    "tests/subst_test.sh":
        "body=\"$(sed -n '/^foo()/,/^}/p' \"$ROOT/scripts/run-until-done.sh\")\"\n",
    "tests/heredoc_subst_test.sh":
        'cat <<EOF\ncount: $(grep -c x "$ROOT/CONTINUE_PROMPT.txt")\nEOF\n',
    "tests/parity_test.sh":
        'x="$(grep "don\'t" "$LOG")"\ngrep -q foo "$ROOT/docs/QUALITY_GATES.md"\n'
        "echo 'never cat scripts/container-setup.sh here'\n",
    # review MINOR 4: an unquoted shell binding
    "tests/unquoted_test.sh": "LOOP=$ROOT/scripts/run-until-done.sh\ngrep -q foo \"$LOOP\"\n",
}
READS_EXPECTED = {
    "tests/tooling/real-resolve.test.ts": [".devcontainer/Dockerfile"],
    "tests/tooling/real-const.test.ts": [".devcontainer/init-firewall.sh"],
    "tests/tooling/real-expect.test.ts": ["docs/QUALITY_GATES.md"],
    "tests/tooling/real-template.test.ts": ["scripts/run-until-done.sh"],
    "tests/tooling/real-exec.test.ts": ["CONTINUE_PROMPT.txt"],
    "tests/tooling/after-regex.test.ts": ["docs/EXECUTION_MODES.md"],
    "tests/test_argv.py": ["scripts/run-until-done.sh"],
    "tests/test_cmdline.py": ["CONTINUE_PROMPT.txt"],
    "tests/test_member.py": ["scripts/container-setup.sh"],
    "tests/test_multiline.py": ["docs/QUALITY_GATES.md"],
    "tests/source_test.sh": [".claude/hooks/require-verdict.sh", "scripts/container-setup.sh"],
    "tests/var_test.sh": ["scripts/run-until-done.sh"],
    "tests/subst_test.sh": ["scripts/run-until-done.sh"],
    "tests/heredoc_subst_test.sh": ["CONTINUE_PROMPT.txt"],
    "tests/parity_test.sh": ["docs/QUALITY_GATES.md"],
    "tests/unquoted_test.sh": ["scripts/run-until-done.sh"],
}


class LiteralsAreNotReads(_Scratch):
    """Red on v0.18.4: the xmeo guard and every DATA_ONLY file were flagged."""

    def make_x(self, files):
        root = self.make(files)
        (root / ".scaffold" / "manifest.json").write_text(manifest(XMEO_SCAFFOLD))
        return root

    def test_xmeos_guard_that_forbids_scaffold_reads_is_not_flagged(self):
        root = self.make_x({"tests/tooling/scaffold-read-surface.test.ts": XMEO_GUARD.read_text()})
        self.assertEqual(self.reads(root)["scaffold_reads"], [])

    def test_a_planted_sample_a_forbidden_list_or_an_expected_value_is_data(self):
        out = self.reads(self.make_x(DATA_ONLY))
        self.assertEqual(out["scaffold_reads"], [], json.dumps(out["scaffold_reads"], indent=1))

    def test_the_same_shapes_written_as_code_are_still_reads(self):
        out = self.reads(self.make_x(READS))
        self.assertEqual({e["test"]: e["paths"] for e in out["scaffold_reads"]}, READS_EXPECTED)

    def test_unbalanced_brackets_stay_linear(self):
        # review MINOR 2: an unclosed read call spans to the end of the file;
        # v0.18.5's first build took 11 s on these 17 KB
        text = "const LOOP = 'scripts/run-until-done.sh';\n" + "".join(
            f"x{i} = parseInt([LOOP]\n" for i in range(800))
        root = self.make_x({"tests/tooling/unbalanced.test.ts": text})
        t0 = time.monotonic()
        self.reads(root)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_a_guard_beside_a_real_read_names_only_the_read(self):
        guard = XMEO_GUARD.read_text() + "\nconst leak = read('docs/QUALITY_GATES.md');\n"
        root = self.make_x({"tests/tooling/scaffold-read-surface.test.ts": guard})
        self.assertEqual(self.reads(root)["scaffold_reads"],
                         [{"test": "tests/tooling/scaffold-read-surface.test.ts",
                           "paths": ["docs/QUALITY_GATES.md"]}])


class PhasekitCheck(_Scratch):
    def _check(self, root):
        return subprocess.run([sys.executable, str(ENGINE), "--check", str(root)],
                              capture_output=True, text=True, timeout=120)

    def test_check_prints_the_advisory_and_its_exit_code_is_unchanged(self):
        with_offender = self._check(self.make(OFFENDERS))
        without = self._check(self.make(CLEAN))
        self.assertIn("ADVISORY scaffold-reads:", with_offender.stdout)
        self.assertNotIn("ADVISORY scaffold-reads:", without.stdout)
        self.assertEqual(with_offender.returncode, without.returncode, "advisory only")


class TheLoopRecordsItAndStaysGreen(unittest.TestCase):
    def test_the_gate_stays_green_and_boundary_state_names_the_offenders(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.layout.put("scripts/phasekit-surface.py", src=TOOL)
        repo.write(".scaffold/manifest.json", manifest(["scripts/run-until-done.sh", "scripts/phasekit-surface.py"]))
        repo.write("tests/test_loop_parse.py", OFFENDERS["tests/test_loop_parse.py"])
        repo.write("tests/test_contract.py", CLEAN["tests/test_contract.py"])
        repo.write("app.py", CLEAN["app.py"])
        repo.git("add", "-A")
        repo.git("commit", "-qm", "tests")
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = r.stdout + r.stderr
        self.assertEqual(out.count("ADVISORY scaffold-reads: 1 test file(s) read scaffold-owned files "
                                   "(tests/test_loop_parse.py)"), 1, "once per session\n" + out)
        self.assertIn("boundary-state: rested (step 7)", out, "advisory only — the gate stays green")
        state = json.loads((repo.repo / "artifacts" / "boundary-state.json").read_text())
        self.assertEqual(state["scaffold_reads"],
                         [{"test": "tests/test_loop_parse.py", "paths": ["scripts/run-until-done.sh"]}])

    def test_a_clean_tree_records_an_empty_list(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        repo.layout.put("scripts/phasekit-surface.py", src=TOOL)
        repo.write(".scaffold/manifest.json", manifest(["scripts/run-until-done.sh"]))
        repo.git("add", "-A")
        repo.git("commit", "-qm", "manifest")
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("ADVISORY scaffold-reads", r.stdout + r.stderr)
        state = json.loads((repo.repo / "artifacts" / "boundary-state.json").read_text())
        self.assertEqual(state.get("scaffold_reads"), [])


class ThePinnedContract(unittest.TestCase):
    def test_the_identifier_and_the_field_are_pinned(self):
        data = json.loads(MANIFEST.read_text())
        conv = {c["name"]: c for c in data["conventions"]}["declared-surface"]
        self.assertIn("`scaffold-reads`", conv["semantics"])
        self.assertIn("`scaffold_reads`", conv["semantics"])
        bs = {a["name"]: a for a in data["artifacts"]}["boundary-state.json"]
        self.assertIn("scaffold_reads", bs["keys"])
        spec = importlib.util.spec_from_file_location("pk_surface_pin", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.ADVISORY_ID, "scaffold-reads")
        self.assertEqual(mod.RECORD_FIELD, "scaffold_reads")
        loop = (REPO_ROOT / "scripts" / "run-until-done.sh").read_text()
        self.assertIn(".scaffold_reads = $r", loop)
        self.assertIn("scaffold_reads: (.scaffold_reads // null)", loop, "carried across pass starts")

    def test_the_rule_is_stated_everywhere_a_session_or_a_project_reads_rules(self):
        marker = "Tests read the declared surface"
        for rel in ("docs/QUALITY_GATES.md", "CONTINUE_PROMPT.txt", "templates/CLAUDE.template.md",
                    "templates/AGENTS.template.md", "templates/conventions.python-uv.md",
                    "templates/conventions.static-web.md", "templates/conventions.game-canvas.md",
                    "templates/conventions.docs-only.md"):
            with self.subTest(file=rel):
                text = (REPO_ROOT / rel).read_text()
                self.assertIn(marker, text)
                self.assertIn("a request to phasekit, not a parse", " ".join(text.split()))


if __name__ == "__main__":
    unittest.main()
