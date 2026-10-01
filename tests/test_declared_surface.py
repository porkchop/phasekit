#!/usr/bin/env python3
"""The declared surface is TRUE of the loop (v0.18.3; queue row 1194, Aaron 2026-09-30).

contracts/interface.json `facts` publishes what downstream tests used to parse
out of phasekit's vendored files (commit surfaces and their gates, the
LEARNINGS credential scan, the completion-record lifecycle, the container's
secret forwarding, …) — four fix rows (1182, 1229, 1281, 1291) paid for
that coupling. This suite is what makes the facts a contract: each one is
checked against the loop's BEHAVIOUR (the real functions, run in a scratch
repository) or, where only structure can say it, against phasekit's own
source. phasekit may read its own internals; consumers read the facts.

A reshape of the loop that keeps every fact true passes here and cannot break
a consumer. A reshape that changes a fact fails here until the fact (and its
`version`) changes with it — a visible contract change, never a silent one.

Run from the repo root: python3 -m unittest tests.test_declared_surface
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
CONTAINER = REPO_ROOT / "scripts" / "container-setup.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"
SRC = LOOP.read_text()
FACTS = json.loads(MANIFEST.read_text())["facts"]

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_ds", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def fn(name):
    """A loop function's full definition (phasekit reading its OWN loop)."""
    start = SRC.index("\n" + name + "() {\n") + 1
    return SRC[start:SRC.index("\n}\n", start) + 3]


def top_assignments(*names):
    return "\n".join(ln for ln in SRC.splitlines() if any(ln.startswith(n + "=") for n in names))


def synthesize(pattern):
    """A string the (simple) declared ERE matches: classes take their first
    member, {n} / {n,} repeat n times, * and ? take none."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[":
            j = pattern.index("]", i + 1)
            body = pattern[i + 1:j]
            atom = body[0]
            i = j + 1
        elif c == "\\":
            atom = pattern[i + 1]
            i += 2
        else:
            atom = c
            i += 1
        n = 1
        if i < len(pattern) and pattern[i] == "{":
            j = pattern.index("}", i)
            n = int(pattern[i + 1:j].split(",")[0])
            i = j + 1
        elif i < len(pattern) and pattern[i] in "*?":
            n = 0
            i += 1
        elif i < len(pattern) and pattern[i] == "+":
            i += 1
        out.append(atom * n)
    return "".join(out)


class _Gates(unittest.TestCase):
    """post_verify_commit_gates, the real function, in a scratch repository."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-ds-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "artifacts").mkdir(parents=True)
        (self.repo / "docs").mkdir()
        (self.repo / "base.txt").write_text("base\n")
        for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                  ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)

    def stage(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "-C", str(self.repo), "add", "--", rel], check=True)

    def gates(self, context="iteration", mode="standard"):
        script = (top_assignments("CREDENTIAL_TOKEN_RE", "PRIVATE_KEY_RE") + "\n"
                  + fn("post_verify_commit_gates")
                  + f'ROOT_DIR="{self.repo}"; ARTIFACTS_DIR="{self.repo}/artifacts"; '
                  f'ITERATION_MODE="{mode}"\ncd "$ROOT_DIR"\npost_verify_commit_gates {context}\n')
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True,
                              text=True, timeout=60)


class LearningsCredentialScan(_Gates):
    F = FACTS["learnings_credential_scan"]

    def test_the_declared_ere_is_the_patterns_joined_and_the_loops_own(self):
        self.assertEqual(self.F["ere"], "|".join(self.F["patterns"]))
        self.assertEqual(self.F["patterns"], self.F["token_patterns"] + [self.F["private_key_pattern"]])
        r = subprocess.run(["bash", "-c", top_assignments("CREDENTIAL_TOKEN_RE", "PRIVATE_KEY_RE")
                            + '\nprintf %s "$CREDENTIAL_TOKEN_RE|$PRIVATE_KEY_RE"'],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout, self.F["ere"])

    def test_every_declared_pattern_refuses_a_staged_learnings_file(self):
        for pat in self.F["patterns"]:
            with self.subTest(pattern=pat):
                control = synthesize(pat)
                self.assertRegex(control, pat)
                self.stage("docs/LEARNINGS.md", f"- 2026-09-30: a pasted log line {control} here\n")
                r = self.gates()
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("REFUSED", r.stderr)

    def test_the_staged_files_selector(self):
        control = synthesize(self.F["token_patterns"][0])
        sel = re.compile(self.F["staged_files_ere"])
        for rel, refused in (("docs/LEARNINGS.md", True), ("docs/LEARNINGS-archive.md", True),
                             ("docs/notes/LEARNINGS.md", False), ("LEARNINGS.md", False),
                             ("docs/NOTES.md", False)):
            with self.subTest(path=rel):
                self.setUp()
                self.assertEqual(bool(sel.search(rel)), refused)
                self.stage(rel, f"x {control}\n")
                self.assertEqual(self.gates().returncode, 1 if refused else 0, rel)

    def test_prose_passes(self):
        self.stage("docs/LEARNINGS.md", "- never paste an sk-ant key or a github_pat_ token here\n")
        self.assertEqual(self.gates().returncode, 0)

    def test_the_wrapup_context_refuses_too(self):
        self.stage("docs/LEARNINGS.md", synthesize(self.F["private_key_pattern"]) + "\n")
        self.assertEqual(self.gates("wrapup").returncode, 1)


class ScopeWarning(_Gates):
    F = FACTS["scope_warning"]

    def _manifest(self):
        self.stage(".scaffold/manifest.json", json.dumps({"files": [
            {"path": "scripts/owned.sh", "ownership": self.F["manifest_ownership"]},
            {"path": "docs/SPEC.md", "ownership": "bootstrap-frozen"}]}))
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "m"], check=True)

    def test_a_staged_scaffold_owned_path_is_recorded_and_the_commit_proceeds(self):
        self._manifest()
        self.stage("scripts/owned.sh", "edited\n")
        self.stage("docs/SPEC.md", "project\n")
        r = self.gates()
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads((self.repo / self.F["record"]).read_text())
        self.assertEqual(sorted(rec), sorted(self.F["record_keys"]))
        self.assertEqual(rec["files"], ["scripts/owned.sh"])

    def test_light_mode_escalates_in_context_iteration_only(self):
        self._manifest()
        self.stage("scripts/owned.sh", "edited\n")
        self.assertEqual(self.gates("iteration", "light").returncode, 4)
        self.assertEqual(self.gates("wrapup", "light").returncode, 0)

    def test_no_hit_writes_nothing(self):
        self._manifest()
        self.stage("src.txt", "x\n")
        self.assertEqual(self.gates().returncode, 0)
        self.assertFalse((self.repo / self.F["record"]).exists())


class SpecChange(_Gates):
    def test_a_staged_spec_is_recorded_never_refused(self):
        F = FACTS["spec_change"]
        self.stage("docs/SPEC.md", "a\nb\n")
        self.assertEqual(self.gates().returncode, 0)
        rec = json.loads((self.repo / F["record"]).read_text())
        self.assertEqual(sorted(rec), sorted(F["record_keys"]))
        self.assertEqual((rec["spec_changed"], rec["added_lines"]), (True, 2))


class CommitSurfaces(unittest.TestCase):
    F = FACTS["commit_surfaces"]
    # where each declared surface lives TODAY — phasekit's own knowledge, never published
    WHERE = {"phase": "_commit_from_artifact", "wrapup": "wrapup_commit",
             "kept_out_claim": "land_kept_out_claim", "wip": "deadline_lastresort_commit",
             "heal": "heal_tracked_transients", "squash": "squash_to_target",
             "reentry_merge": "ensure_work_branch", "merge_back": "merge_back_from_target"}
    CALLS = {"verify": "run_verify_gate", "security_pair": "staged_touches_security_pair",
             "post_verify": "post_verify_commit_gates"}

    def test_every_commit_the_loop_makes_is_a_declared_surface(self):
        self.assertEqual({s["id"] for s in self.F["surfaces"]}, set(self.WHERE))
        makers = set()
        for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{\n(.*?)^\}$", SRC, re.S | re.M):
            code = "\n".join(ln for ln in m.group(2).splitlines() if not ln.lstrip().startswith("#"))
            if re.search(r"(^|[\s;(!])git (-[cC] \S+ )*(commit(-tree)?\b(?! --dry-run)|merge\b(?! --abort)"
                         r"(?!-)|cherry-pick|revert|am\b|rebase|pull\b)", code, re.M):
                makers.add(m.group(1))
        self.assertEqual(makers, set(self.WHERE.values()),
                         "a new commit path must be declared in facts.commit_surfaces")

    def test_each_surface_runs_exactly_its_declared_gates(self):
        for s in self.F["surfaces"]:
            body = "\n".join(ln for ln in fn(self.WHERE[s["id"]]).splitlines()
                             if not ln.lstrip().startswith("#"))
            for gate, call in self.CALLS.items():
                with self.subTest(surface=s["id"], gate=gate):
                    self.assertEqual(call in body, gate in s["gates"], body[:200])
                    self.assertNotRegex(body, call + r"[^\n]*\|\|\s*true", "a gate whose verdict is ignored")
            commit_at = min((m.start() for m in re.finditer(r"(?:^\s*|\$\(|!\s|if\s)git (-[cC] \S+ )*(-q )?(commit(-tree)?|merge)(?![-\w])(?! --abort)",
                                                  body, re.M)),
                            default=len(body))
            for gate in s["gates"]:
                self.assertLess(body.index(self.CALLS[gate]), commit_at, f"{s['id']}: {gate} runs before the commit")
            if "post_verify" in s["gates"]:
                self.assertRegex(body, r"post_verify_commit_gates " + s["post_verify_context"] + r"\b")
            self.assertEqual("--no-verify" in body, s["id"] == "wip", s["id"])

    def test_the_post_verify_members(self):
        body = fn("post_verify_commit_gates")
        for member, marker in (("scope_warning", "scope-warning.json"), ("spec_change", "spec-change.json"),
                               ("learnings_credential_scan", "$CREDENTIAL_TOKEN_RE|$PRIVATE_KEY_RE")):
            self.assertIn(member, self.F["post_verify"]["members"])
            self.assertIn(marker, body)
        self.assertEqual(set(re.findall(r"return (\d)", body)), set(self.F["post_verify"]["returns"]))

    def test_the_wip_commit_never_carries_what_the_gates_refuse(self):
        body = fn("deadline_lastresort_commit")
        for p in ('"$ROOT_DIR/docs/LEARNINGS"*.md', '"$ROOT_DIR/.claude/settings.json"',
                  '"$ROOT_DIR/.github/workflows"'):
            self.assertIn(p, body)


class CompletionRecord(unittest.TestCase):
    """retire_completion_record, the real function, around a stubbed in-flight answer."""

    def _run(self, in_flight, committed, on_disk):
        tmp = Path(tempfile.mkdtemp(prefix="pk-ds-cr-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "artifacts").mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(tmp), *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
        (tmp / "f").write_text("x\n")
        pc = tmp / "artifacts" / "project-complete.json"
        if committed:
            pc.write_text('{"summary": "committed"}\n')
        run("add", "-A"); run("commit", "-qm", "base")
        if on_disk is not None:
            pc.write_text(on_disk)
        script = (fn("artifact_never_landed") + fn("retire_completion_record")
                  + f"completion_in_flight() {{ return {0 if in_flight else 1}; }}\n"
                  + f'ARTIFACTS_DIR="{tmp}/artifacts"; cd "{tmp}"\nretire_completion_record\n')
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return pc

    def test_removed_at_iteration_start_unless_this_iterations_completion_is_landing(self):
        self.assertTrue(FACTS["completion_record"]["removed_at_iteration_start"])
        self.assertFalse(self._run(False, False, '{"summary": "untracked"}\n').exists())
        self.assertFalse(self._run(False, True, None).exists(), "an EARLIER committed completion goes")
        kept = self._run(True, True, '{"summary": "edited"}\n')
        self.assertEqual(kept.read_text(), '{"summary": "committed"}\n', "restored, never deleted")

    def test_the_loop_retires_it_at_every_iteration_start_and_drops_a_stale_carry(self):
        self.assertIn("retire_completion_record", fn("cleanup_artifacts"))
        self.assertIn("  cleanup_artifacts\n  boundary_begin", SRC)
        self.assertIn("retire_completion_record", fn("drop_unclaimed_carried_record"))
        self.assertIn("\ndrop_unclaimed_carried_record || true\n", SRC, "loop start")
        self.assertIn("drop_unclaimed_carried_record", fn("run_until_done_exit_trap"), "session exit")


class _LoopRun(unittest.TestCase):
    def repo(self):
        repo = H.Repo(squash=False)
        self.addCleanup(repo.cleanup)
        return repo


class RecoveryOnlyRun(_LoopRun):
    def test_max_iterations_zero_runs_no_model_turn(self):
        F = FACTS["recovery_only_run"]
        repo = self.repo()
        repo.scenario(H.APPROVE_SCENARIO)
        r = repo.run(env={F["env"]: str(F["value"])})
        self.assertEqual(r.returncode, 3, "nothing to recover: the limit, not a completion\n" + r.stdout)
        self.assertEqual(list(repo.stub.glob("prompt-*.txt")), [], "no model turn")


class LearningsSizeAdvisory(_LoopRun):
    def test_the_threshold_is_whole_kib_at_or_over(self):
        F = FACTS["learnings_size_advisory"]
        for size, noted in ((F["default_kb"] * 1024, True), (F["default_kb"] * 1024 - 1, False)):
            with self.subTest(size=size):
                repo = self.repo()
                repo.write("docs/LEARNINGS.md", "x" * size)
                repo.git("add", "-A"); repo.git("commit", "-qm", "l")
                r = repo.run(env={"MAX_ITERATIONS": "0"})
                self.assertEqual("advisory threshold" in r.stderr, noted, r.stderr[-400:])
        self.assertIn(f'"${{{F["env"]}:-{F["default_kb"]}}}"', SRC)


class BoundarySteps(unittest.TestCase):
    def test_the_step_names_in_order(self):
        r = subprocess.run(["bash", "-c", top_assignments("BOUNDARY_STEP_NAMES")
                            + '\nprintf "%s\\n" "${BOUNDARY_STEP_NAMES[@]}"'], capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), FACTS["boundary_steps"]["names"])


class HermeticTests(unittest.TestCase):
    def test_the_declared_eres_are_the_loops_and_they_select_what_they_say(self):
        F = FACTS["hermetic_tests"]
        body = fn("hermetic_tests_advisory")
        re_line = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("re=$'")][0]
        tp = re.search(r"grep -E '([^']+)'", body).group(1)
        r = subprocess.run(["bash", "-c", re_line + '\nprintf "%s" "$re"'], capture_output=True, text=True)
        self.assertEqual([tp, r.stdout], [F["test_path_ere"], F["history_read_ere"]])
        grep = lambda ere, s: subprocess.run(["grep", "-qE", ere], input=s, text=True).returncode == 0  # noqa: E731
        self.assertTrue(grep(F["test_path_ere"], "tests/tooling/x.test.ts"))
        self.assertFalse(grep(F["test_path_ere"], "src/app.ts"))
        self.assertTrue(grep(F["history_read_ere"], "git('rev-list', '--grep=^iteration 57', 'HEAD')"))
        self.assertFalse(grep(F["history_read_ere"], "expect(add(1, 2)).toBe(3)"))


class LoopLines(unittest.TestCase):
    def test_every_declared_line_is_printed_on_its_declared_stream(self):
        for key, line in FACTS["loop_lines"]["lines"].items():
            with self.subTest(line=key):
                sites = [ln for ln in SRC.splitlines()
                         if re.search(r'\b(echo|printf)\b[^\n]*"' + re.escape(line["prefix"].split("$")[0]), ln)]
                self.assertTrue(sites, key)
                if line["stream"].startswith("artifacts/logs/"):
                    # printed inside the watchdog subshell, whose stdout AND stderr go to the log
                    fnname = "deadline_lastresort_commit"
                    self.assertTrue(all(ln in fn(fnname) for ln in sites), key)
                    call = re.search(r'\n    deadline_lastresort_commit\n  \) >>"\$ARTIFACTS_DIR/'
                                     + re.escape(line["stream"][len("artifacts/"):]) + r'" 2>&1', SRC)
                    self.assertTrue(call, "the kill-mode caller must redirect to " + line["stream"])
                    continue
                for ln in sites:
                    self.assertEqual(">&2" in ln, line["stream"] == "stderr", ln)


class VerifyBreaker(unittest.TestCase):
    def test_the_breaker_reason(self):
        self.assertIn('reason: "' + FACTS["verify_breaker"]["reason"] + '"', fn("record_verify_failure"))


class ScaffoldManifest(unittest.TestCase):
    def test_the_engine_writes_what_is_declared(self):
        F = FACTS["scaffold_manifest"]
        spec = importlib.util.spec_from_file_location("pk_engine_ds", REPO_ROOT / "scripts" / "enrich-project.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m.NORMALIZATION_RECIPE, F["normalization_recipe"])
        self.assertEqual(m.SCHEMA_VERSION_CURRENT, F["schema_version"])
        self.assertEqual(set(m.OWNERSHIP_CLASSES_SCAFFOLD_SIDE) | {m.OWNERSHIP_CLASS_ORPHAN}, set(F["ownership"]))
        tmp = Path(tempfile.mkdtemp(prefix="pk-ds-m-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "enrich-project.py"), str(tmp),
                            "--profile", "default"], capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        man = json.loads((tmp / F["path"]).read_text())
        self.assertEqual(list(man), F["keys"])
        for e in man["files"]:
            self.assertTrue(set(F["file_keys_always"]) <= set(e), e)
            self.assertTrue(set(e) <= set(F["file_keys_always"]) | set(F["file_keys_optional"]), e)
            self.assertIn(e["ownership"], F["ownership"])
        self.assertNotIn(F["path"], {e["path"] for e in man["files"]})


STUB_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$DOCKER_ARGS_LOG"
printf 'ENV:ANTHROPIC_API_KEY=%s\\n' "${ANTHROPIC_API_KEY:+set}" >> "$DOCKER_ARGS_LOG"
printf -- '---\\n' >> "$DOCKER_ARGS_LOG"
exit 0
"""


class Container(unittest.TestCase):
    F = FACTS["container"]
    SECRET = "sk-ant-" + "q" * 20

    def _run(self, extra):
        tmp = Path(tempfile.mkdtemp(prefix="pk-ds-c-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "bin").mkdir()
        (tmp / "home").mkdir()
        stub = tmp / "bin" / "docker"
        stub.write_text(STUB_DOCKER)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        log = tmp / "log"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PHASEKIT_", "CLAUDE_", "ANTHROPIC_", "GH_", "GITHUB_"))}
        env.update({"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}", "DOCKER_ARGS_LOG": str(log),
                    "HOME": str(tmp / "home")}, **extra)
        p = subprocess.run(["bash", str(CONTAINER), "shell"], capture_output=True, text=True, env=env)
        runs = [b.strip().splitlines() for b in log.read_text().split("---\n") if b.strip()]
        runs = [c for c in runs if c and c[0] == "run"]
        self.assertEqual(len(runs), 1, p.stdout + p.stderr)
        return p, runs[0]

    def test_secrets_cross_by_name_never_on_the_command_line(self):
        self.assertFalse(self.F["value_on_command_line"])
        for name in self.F["secrets_forwarded_by_name"]:
            with self.subTest(name=name):
                _, argv = self._run({name: self.SECRET})
                self.assertNotIn(self.SECRET, "\n".join(a for a in argv if not a.startswith("ENV:")))
                envs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e" and i + 1 < len(argv)]
                self.assertIn(name, envs)

    def test_forward_env_reserved_names_are_refused(self):
        fe = self.F["forward_env"]
        p, argv = self._run({fe["env"]: ",".join(fe["reserved_names"])})
        for n in fe["reserved_names"]:
            self.assertIn(f"refusing '{n}'", p.stderr)

    def test_iteration_mode_only_when_set_and_the_claude_config_and_caps(self):
        _, argv = self._run({})
        self.assertFalse(any(a.startswith("PHASEKIT_ITERATION_MODE") for a in argv))
        _, argv2 = self._run({"PHASEKIT_ITERATION_MODE": "light"})
        self.assertIn("PHASEKIT_ITERATION_MODE=light", argv2)
        cc = self.F["claude_config"]
        self.assertIn(f"{cc['volume_default']}:{cc['mount']}", argv)
        self.assertIn(f"CLAUDE_CONFIG_DIR={cc['CLAUDE_CONFIG_DIR']}", argv)
        caps = self.F["capabilities"]
        for c in caps["drop"]:
            self.assertIn(f"--cap-drop={c}", argv)
        for c in caps["add"]:
            self.assertIn(f"--cap-add={c}", argv)


class TheSurfaceIsPrintable(unittest.TestCase):
    def test_phasekit_facts_json_is_the_section(self):
        r = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "phasekit.sh"), "facts", "--json"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), FACTS)

    def test_every_fact_names_a_proof_that_exists(self):
        for name, fact in FACTS.items():
            if not isinstance(fact, dict):
                continue
            for ref in fact["proven_by"]:
                with self.subTest(fact=name, ref=ref):
                    path, _, cls = ref.partition("::")
                    self.assertTrue((REPO_ROOT / path).is_file(), ref)
                    if cls:
                        self.assertIn(f"\nclass {cls}(", (REPO_ROOT / path).read_text(), ref)


if __name__ == "__main__":
    unittest.main()
