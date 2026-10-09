"""The engine outside the repository (v0.19.0): design §9's acceptance, the
fail-closed guard, the pin that never switches a running loop, and the CLI
verbs of a pinned project (init, check, migrate, upgrade, engines, plugin).

Fixture: a scratch git clone of THIS working tree tagged v0.19.0 (and a
second commit tagged v0.19.1), standing in for the canonical install; an
engine store beside it; a fake `claude` that plays the model turn, answers
`claude plugin list`, and — for the loop's guard probe — runs the plugin's
own UserPromptSubmit hook exactly as Claude Code runs a --plugin-dir hook.

Run from the repo root: python3 -m unittest tests.test_engine_outside
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
HAVE_TOOLS = all(shutil.which(c) for c in ("bash", "git", "jq", "python3"))

FAKE_CLAUDE = r"""#!/usr/bin/env bash
st="${FAKE_STATE:?}"
if [[ "${1:-}" == plugin ]]; then
  echo "$*" >> "$st/plugin-calls"
  case "$2 $3" in
    "list --json") if [[ -f "$st/plugin-installed" ]]; then echo '[{"id":"phasekit@phasekit","enabled":true}]'; else echo '[]'; fi ;;
    "marketplace add") touch "$st/marketplace-added" ;;
    "install phasekit@phasekit") [[ -f "$st/marketplace-added" ]] && touch "$st/plugin-installed" ;;
  esac
  exit 0
fi
if [[ -n "${PHASEKIT_GUARD_PROBE:-}" ]]; then
  echo probe >> "$st/probes"
  [[ -f "$st/ignore-plugin-dir" ]] && exit 0     # a CLI that silently ignores --plugin-dir
  a=("$@")
  for ((i = 0; i < ${#a[@]}; i++)); do
    if [[ "${a[$i]}" == --plugin-dir ]]; then
      CLAUDE_PLUGIN_ROOT="${a[$((i + 1))]}" bash "${a[$((i + 1))]}/hooks/run-hook.sh" guard-probe </dev/null
    fi
  done
  exit 0
fi
n=$(( $(cat "$st/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$st/calls"
printf '%s\n' "$@" > "$st/argv-$n"
sid=""; prompt=""
while (( $# )); do
  case "$1" in
    --session-id|--resume) sid="$2"; shift ;;
    --model|--permission-mode|--output-format|--plugin-dir) shift ;;
    -p) prompt="$2"; shift ;;
  esac
  shift
done
printf '%s' "$prompt" > "$st/prompt-$n"
printf '%s\n' "${PHASEKIT_ENGINE_DOCS:-}" > "$st/engine-docs-$n"
printf '%s\n' "${PHASEKIT_PROJECT_DIR-unset}" > "$st/project-dir-$n"
command -v phasekit > "$st/phasekit-path-$n" 2>&1 || true
phasekit docs > "$st/phasekit-docs-$n" 2>&1 || true
[[ -n "$sid" ]] || sid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
echo '{"type":"system","subtype":"init","session_id":"'"$sid"'"}'
rc=0
turn="$st/turn-$n.sh"; [[ -f "$turn" ]] || turn="$st/turn.sh"
if [[ -f "$turn" ]]; then CALL_N="$n" bash "$turn" || rc=$?; fi
echo '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"session_id":"'"$sid"'"}'
exit "$rc"
"""

APPROVE_TURN = r"""
echo "# Hello" > docs/HELLO.md
jq -n '{phase: "phase-1", approved: true, summary: "hello doc",
        suggested_commit_message: "phase 1: hello doc"}' > artifacts/phase-approval.json
"""

PHASES = "# Phases\n\n## Phase 1 — hello doc\n\nPlanned paths: docs/HELLO.md\n"


def _tree_digest(root):
    """Every file's path, mode and bytes under root (symlinks by target)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        dirnames.sort()
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            h.update(rel.encode() + b"\0" + oct(st.st_mode).encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            elif stat.S_ISREG(st.st_mode):
                h.update(Path(p).read_bytes())
    return h.hexdigest()


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def _writable(root):
    for dirpath, _d, files in os.walk(root):
        os.chmod(dirpath, 0o755)
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, 0o644 | (os.stat(p).st_mode & 0o111))


def make_engine_clone(base):
    """A git clone of this working tree (tracked + new files) with release
    tags v0.19.0 and v0.19.1, as the canonical install would hold them."""
    clone = Path(base) / "phasekit-clone"
    files = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-co", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.split("\n")
    clone.mkdir(parents=True)
    for rel in sorted({f for f in files if f}):
        src = REPO_ROOT / rel
        if not (src.exists() or src.is_symlink()):
            continue
        dest = clone / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dest)
        elif src.is_file():
            shutil.copy2(src, dest)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", "-b", "master"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "engine"],
                 ["tag", "v0.19.0"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "next"],
                 ["tag", "v0.19.1"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, env=env)
    return clone


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class EngineFixture(unittest.TestCase):
    clone = None

    @classmethod
    def setUpClass(cls):
        cls._base = tempfile.mkdtemp(prefix="pk-engine-")
        cls.clone = make_engine_clone(cls._base)

    @classmethod
    def tearDownClass(cls):
        _writable(cls._base)
        shutil.rmtree(cls._base, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-eo-"))
        self.addCleanup(lambda: (_writable(self.tmp), shutil.rmtree(self.tmp, ignore_errors=True)))
        self.store = self.tmp / "store"
        self.state = self.tmp / "fake"
        self.state.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "claude").write_text(FAKE_CLAUDE)
        os.chmod(self.bin / "claude", 0o755)

    # -- plumbing -----------------------------------------------------------
    def env(self, **extra):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("PHASEKIT_", "CLAUDE_")) and k not in ("MAX_ITERATIONS",)}
        e.update({
            "PHASEKIT_ENGINE_STORE": str(self.store),
            "PHASEKIT_HOME": str(self.clone),
            "PHASEKIT_NO_UPDATE_CHECK": "1",
            "PHASEKIT_UPGRADE_VERIFY": "host",
            "PHASEKIT_ITER_RETRY": "0",
            "FAKE_STATE": str(self.state),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        e.update({k: v for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
        return e

    def cli(self, cwd, *args, timeout=300, **extra):
        return subprocess.run(["bash", str(self.clone / "scripts" / "phasekit.sh"), *args],
                              cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              env=self.env(**extra))

    def new_project(self, name="proj", profile="docs-only"):
        proj = self.tmp / name
        proj.mkdir()
        _git(proj, "init", "-q", "-b", "master")
        _git(proj, "config", "user.name", "t")
        _git(proj, "config", "user.email", "t@t")
        _git(proj, "config", "commit.gpgsign", "false")
        r = self.cli(proj, "init", profile, "--pin", "v0.19.0", "--no-plugin")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return proj

    def engine(self, tag="v0.19.0"):
        return self.store / tag

    def calls(self):
        p = self.state / "calls"
        return int(p.read_text()) if p.exists() else 0

    def manifest_scaffold_paths(self):
        """Every path the engine's capability manifest calls scaffold-class."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("pk_enrich_eo", REPO_ROOT / "scripts" / "enrich-project.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        man = m.load_manifest()
        paths = set()
        for prof in man.get("profiles", {}):
            for s in m.enumerate_install_targets(man, m.resolve_profile(man["profiles"], prof)):
                if s.get("ownership") == "scaffold":
                    paths.add(s["path"])
        return paths


class Init(EngineFixture):
    def test_init_writes_the_pin_and_the_projects_own_files_only_in_one_commit(self):
        proj = self.new_project()
        self.assertEqual((proj / ".phasekit-version").read_text(), "v0.19.0\n")
        tracked = set(_git(proj, "ls-files").splitlines())
        self.assertFalse(tracked & self.manifest_scaffold_paths(), tracked)
        self.assertFalse(any(p.startswith(".scaffold/") for p in tracked))
        self.assertIn("scripts/phasekit-verify.sh", tracked)
        self.assertEqual(len(_git(proj, "log", "--format=%H").splitlines()), 1)
        settings = json.loads((proj / ".claude" / "settings.json").read_text())
        self.assertNotIn("hooks", settings)
        self.assertIn("permissions", settings)
        # the engine was installed into the store, read-only, by its tag
        eng = self.engine()
        self.assertEqual((eng / ".engine-version").read_text().strip(), "v0.19.0")
        self.assertEqual((eng / ".engine-commit").read_text().strip(),
                         _git(self.clone, "rev-parse", "v0.19.0^{commit}"))
        self.assertFalse(os.access(eng / "scripts" / "run-until-done.sh", os.W_OK))
        self.assertFalse(os.access(eng, os.W_OK))

    def test_the_templates_name_engine_docs_not_vendored_paths(self):
        proj = self.new_project()
        for rel in (".claude/CLAUDE.md", "AGENTS.md"):
            text = (proj / rel).read_text()
            self.assertNotIn("`docs/QUALITY_GATES.md`", text, rel)
            self.assertNotIn("bash scripts/phasekit.sh", text, rel)
            self.assertIn("phasekit docs", text, rel)

    def test_init_twice_is_a_no_op(self):
        proj = self.new_project()
        head = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "init", "docs-only", "--pin", "v0.19.0", "--no-plugin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already pinned", r.stdout)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), head)


class HostLoopPinned(EngineFixture):
    """§9 AC2 + the cross-talk audit: a pinned project with NO engine file,
    the engine read-only, one loop pass lands one commit; nothing under the
    engine changes and the commit carries no engine path."""

    def _ready(self, proj):
        (proj / "docs" / "PHASES.md").write_text(PHASES)
        with open(proj / "scripts" / "phasekit-verify.sh", "a") as f:
            f.write(f'\necho "${{PHASEKIT_PROJECT_DIR-unset}}" >> "{self.state}/gate-project-dir"\n')
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "plan")
        (self.state / "turn.sh").write_text(APPROVE_TURN)

    def test_one_bounded_pass_lands_one_commit_and_the_engine_is_untouched(self):
        proj = self.new_project()
        self._ready(proj)
        before_engine = _tree_digest(self.engine())
        base = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "loop", MAX_ITERATIONS="1")
        out = r.stdout + r.stderr
        self.assertNotIn("REFUSING", out)
        self.assertEqual(self.calls(), 1, out)
        new = _git(proj, "log", "--format=%s", f"{base}..HEAD").splitlines()
        self.assertEqual(len(new), 1, out)
        self.assertTrue(new[0].startswith("iteration") or "phase 1" in new[0], new)
        changed = set(_git(proj, "diff", "--name-only", base, "HEAD").splitlines())
        self.assertIn("docs/HELLO.md", changed)
        self.assertFalse(changed & self.manifest_scaffold_paths(), changed)
        self.assertFalse(any(p.startswith(".scaffold") for p in changed))
        self.assertEqual(_tree_digest(self.engine()), before_engine, "the engine changed")
        # the session got the engine's docs, its CLI, and the plugin
        self.assertEqual((self.state / "engine-docs-1").read_text().strip(), str(self.engine() / "docs"))
        argv = (self.state / "argv-1").read_text().splitlines()
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(self.engine() / "plugin"))
        self.assertEqual((self.state / "phasekit-docs-1").read_text().strip(), str(self.engine() / "docs"))
        prompt = (self.state / "prompt-1").read_text()
        self.assertIn("PHASEKIT ENGINE (this session)", prompt)
        self.assertIn(str(self.engine() / "docs" / "QUALITY_GATES.md"), prompt)
        self.assertIn("docs/PHASES.md", prompt)          # the project's own doc keeps its path
        self.assertNotIn(str(self.engine() / "docs" / "PHASES.md"), prompt)
        self.assertEqual(_git(proj, "status", "--porcelain"), "")
        # the engine's pointer at the tree reaches neither the model nor the gate
        self.assertEqual((self.state / "project-dir-1").read_text().strip(), "unset")
        self.assertEqual((self.state / "gate-project-dir").read_text().strip().splitlines(), ["unset"] * len(
            (self.state / "gate-project-dir").read_text().strip().splitlines()))
        # only the engine's process docs are offered, never its seeds or internals
        self.assertNotIn(str(self.engine() / "docs" / "CAPABILITY_MANIFEST.md"), prompt)

    def test_decoys_at_every_engine_path_in_the_project_are_never_read(self):
        """The project tree holds a decoy at every path a vendored engine
        file would have; a read or run of any of them leaves a mark."""
        proj = self.new_project()
        self._ready(proj)
        mark = self.tmp / "decoy-touched"
        decoy_sh = f'#!/usr/bin/env bash\necho "$0" >> "{mark}"\nexit 99\n'
        for rel in ("scripts/run-until-done.sh", "scripts/run-phase.sh", "scripts/phasekit-log-fmt.sh",
                    "scripts/phasekit.sh", ".claude/hooks/deny-dangerous-commands.sh"):
            (proj / rel).parent.mkdir(parents=True, exist_ok=True)
            (proj / rel).write_text(decoy_sh)
            os.chmod(proj / rel, 0o755)
        (proj / "CONTINUE_PROMPT.txt").write_text("DECOY PROMPT\n")
        (proj / "scripts" / "phasekit-contracts.py").write_text(f"open({str(mark)!r}, 'a').write('py')\n")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "decoys")
        eng = self.engine()
        r = subprocess.run(["bash", str(eng / "scripts" / "run-until-done.sh")], cwd=str(proj),
                           capture_output=True, text=True, timeout=300,
                           env=self.env(PHASEKIT_PROJECT_DIR=str(proj), MAX_ITERATIONS="1"))
        out = r.stdout + r.stderr
        self.assertFalse(mark.exists(), f"an engine path was read from the project:\n{mark.read_text() if mark.exists() else ''}\n{out}")
        self.assertNotIn("DECOY PROMPT", (self.state / "prompt-1").read_text())
        self.assertEqual(self.calls(), 1, out)

    def test_the_pin_never_switches_the_engine_of_a_running_loop(self):
        """Turn 1 updates; between turns an operator commits a pin bump to
        v0.19.1 (installed); turn 2 still runs v0.19.0 — its docs, its CLI —
        and the landing is v0.19.0's."""
        proj = self.new_project()
        self._ready(proj)
        r = self.cli(proj, "engines", "install", "v0.19.1")
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.state / "turn-1.sh").write_text(
            "echo step >> docs/HELLO.md\n"
            "jq -n '{suggested_commit_message: \"phase-1: step\"}' > artifacts/phase-update.json\n"
            # an operator's bump, committed on the branch while the run is
            # open: everything turn 2 records comes after it
            "echo v0.19.1 > .phasekit-version\n"
            "git add .phasekit-version && git -c user.name=op -c user.email=op@op commit -qm 'pin bump'\n")
        (self.state / "turn-2.sh").write_text(APPROVE_TURN)
        r = self.cli(proj, "loop", MAX_ITERATIONS="2")
        out = r.stdout + r.stderr
        self.assertEqual(self.calls(), 2, out)
        v0 = str(self.engine("v0.19.0") / "docs")
        self.assertEqual((self.state / "engine-docs-2").read_text().strip(), v0, out)
        self.assertEqual((self.state / "phasekit-docs-2").read_text().strip(), v0, out)
        argv = (self.state / "argv-2").read_text().splitlines()
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(self.engine("v0.19.0") / "plugin"))
        self.assertEqual((proj / ".phasekit-version").read_text().strip(), "v0.19.1")

    def test_a_session_never_commits_a_pin_change(self):
        proj = self.new_project()
        self._ready(proj)
        (self.state / "turn.sh").write_text("echo v0.19.1 > .phasekit-version\n" + APPROVE_TURN)
        base = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "loop", MAX_ITERATIONS="1")
        out = r.stdout + r.stderr
        self.assertIn("REFUSED", out)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), base, out)
        self.assertEqual(_git(proj, "show", "HEAD:.phasekit-version"), "v0.19.0")


class PinnedLayoutWins(EngineFixture):
    def test_a_stray_vendored_loop_never_runs_and_check_names_it(self):
        proj = self.new_project()
        mark = self.tmp / "stray-ran"
        (proj / "scripts" / "run-until-done.sh").write_text(f'#!/usr/bin/env bash\necho ran >> "{mark}"\nexit 0\n')
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "a session committed a loop")
        r = self.cli(proj, "verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(mark.exists(), "the project's stray loop ran")
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("scripts/run-until-done.sh", r.stderr)

    def test_facts_come_from_the_pinned_engine(self):
        proj = self.new_project()
        r = self.cli(proj, "facts", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsInstance(json.loads(r.stdout), dict)

    def test_pin_moving_verbs_refuse_inside_a_loop_session(self):
        proj = self.new_project()
        for verb in ("upgrade", "migrate"):
            r = self.cli(proj, verb, PHASEKIT_ITER_MARKER=str(self.tmp / "marker"))
            self.assertEqual(r.returncode, 2, (verb, r.stdout + r.stderr))
            self.assertIn("inside a phasekit loop session", r.stderr)

    def test_init_refuses_a_staged_index_in_a_repo_without_commits(self):
        proj = self.tmp / "unborn"
        proj.mkdir()
        _git(proj, "init", "-q")
        (proj / "mine.txt").write_text("x")
        _git(proj, "add", "mine.txt")
        r = self.cli(proj, "init", "docs-only", "--pin", "v0.19.0", "--no-plugin")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse((proj / ".phasekit-version").exists())


class GuardFailsClosed(EngineFixture):
    """The plugin's guard must be PROVEN live before every pinned turn; when
    it cannot be, the turn is refused — never run unguarded."""

    def _engine_copy(self, mutate):
        src = self.engine()
        dst = self.tmp / "engine-copy"
        shutil.copytree(src, dst, symlinks=True)
        _writable(dst)
        mutate(dst)
        return dst

    def _run(self, proj, engine):
        (proj / "docs" / "PHASES.md").write_text(PHASES)
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "plan")
        (self.state / "turn.sh").write_text(APPROVE_TURN)
        return subprocess.run(["bash", str(engine / "scripts" / "run-until-done.sh")], cwd=str(proj),
                              capture_output=True, text=True, timeout=300,
                              env=self.env(PHASEKIT_PROJECT_DIR=str(proj), MAX_ITERATIONS="1"))

    def _assert_refused(self, r, proj, base):
        out = r.stdout + r.stderr
        self.assertIn("REFUSING the turn", out)
        self.assertEqual(self.calls(), 0, "a model turn ran without its guard:\n" + out)
        self.assertNotEqual(r.returncode, 0, out)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), base)

    def test_the_plugin_removed(self):
        proj = self.new_project()
        eng = self._engine_copy(lambda d: shutil.rmtree(d / "plugin"))
        base = _git(proj, "rev-parse", "HEAD")
        r = self._run(proj, eng)
        self._assert_refused(r, proj, _git(proj, "rev-parse", "HEAD"))
        del base

    def test_a_cli_that_ignores_plugin_dir(self):
        proj = self.new_project()
        (self.state / "ignore-plugin-dir").touch()
        r = self._run(proj, self.engine())
        self._assert_refused(r, proj, _git(proj, "rev-parse", "HEAD"))

    def test_a_guard_that_does_not_refuse(self):
        proj = self.new_project()
        eng = self._engine_copy(lambda d: (d / ".claude" / "hooks" / "deny-dangerous-commands.sh")
                                .write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n"))
        r = self._run(proj, eng)
        self._assert_refused(r, proj, _git(proj, "rev-parse", "HEAD"))
        self.assertIn("did not refuse", r.stdout + r.stderr)

    def test_a_missing_stop_hook(self):
        proj = self.new_project()
        eng = self._engine_copy(lambda d: (d / ".claude" / "hooks" / "require-verdict.sh").unlink())
        r = self._run(proj, eng)
        self._assert_refused(r, proj, _git(proj, "rev-parse", "HEAD"))

    def test_hooks_json_without_the_stop_hook(self):
        proj = self.new_project()

        def drop_stop(d):
            hj = d / "plugin" / "hooks" / "hooks.json"
            data = json.loads(hj.read_text())
            del data["hooks"]["Stop"]
            hj.write_text(json.dumps(data))
        r = self._run(proj, self._engine_copy(drop_stop))
        self._assert_refused(r, proj, _git(proj, "rev-parse", "HEAD"))
        self.assertIn("does not wire require-verdict on Stop", r.stdout + r.stderr)

    def test_a_planted_project_wiring_cannot_switch_the_guard_off(self):
        """Review M1: a session writes .claude/hooks/<guard>.sh plus a settings
        line; the dispatcher must not step aside in a pinned project."""
        proj = self.new_project()
        (proj / ".claude" / "hooks").mkdir(parents=True)
        for h in ("deny-dangerous-commands", "require-verdict"):
            (proj / ".claude" / "hooks" / f"{h}.sh").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
            os.chmod(proj / ".claude" / "hooks" / f"{h}.sh", 0o755)
        s = json.loads((proj / ".claude" / "settings.json").read_text())
        s["hooks"] = {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "./.claude/hooks/deny-dangerous-commands.sh"}]}]}
        (proj / ".claude" / "settings.json").write_text(json.dumps(s))
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD"}})
        r = subprocess.run(["bash", str(self.engine() / "plugin" / "hooks" / "run-hook.sh"),
                            "deny-dangerous-commands"], input=payload, capture_output=True, text=True,
                           cwd=str(proj), env=self.env(CLAUDE_PROJECT_DIR=str(proj)))
        self.assertEqual(r.returncode, 2, r.stderr)
        r = self._run(proj, self.engine())
        self.assertNotIn("REFUSING", r.stdout + r.stderr)   # the guard is live, so the turn runs

    def test_the_live_plugin_passes_and_the_probe_reaches_no_model(self):
        proj = self.new_project()
        r = self._run(proj, self.engine())
        out = r.stdout + r.stderr
        self.assertNotIn("REFUSING", out)
        self.assertEqual(self.calls(), 1, out)
        self.assertGreaterEqual(len((self.state / "probes").read_text().split()), 1)


class Check(EngineFixture):
    """§9 AC4."""

    def test_clean_is_zero(self):
        proj = self.new_project()
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_tracked_engine_path_is_three(self):
        proj = self.new_project()
        (proj / "docs" / "QUALITY_GATES.md").write_text("leftover\n")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "leftover")
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("docs/QUALITY_GATES.md", r.stderr)

    def test_stale_hook_wiring_is_three(self):
        proj = self.new_project()
        s = json.loads((proj / ".claude" / "settings.json").read_text())
        s["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": "./.claude/hooks/require-verdict.sh"}]}]}
        (proj / ".claude" / "settings.json").write_text(json.dumps(s, indent=2) + "\n")
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_a_pin_not_installed_is_six_and_names_engines_install(self):
        proj = self.new_project()
        (proj / ".phasekit-version").write_text("v0.19.1\n")
        r = self.cli(proj, "check")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("phasekit engines install v0.19.1", r.stderr)
        self.assertFalse((self.store / "v0.19.1").exists(), "check never fetches")

    def test_a_missing_plugin_is_a_loud_warning(self):
        proj = self.new_project()
        r = self.cli(proj, "check")
        self.assertIn("WARNING: the phasekit plugin is not installed", r.stderr)
        (self.state / "plugin-installed").touch()
        r = self.cli(proj, "check")
        self.assertNotIn("WARNING", r.stderr)


class EnginesStore(EngineFixture):
    def test_install_is_idempotent_and_read_only(self):
        r1 = self.cli(self.tmp, "engines", "install", "v0.19.1")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        d = _tree_digest(self.engine("v0.19.1"))
        r2 = self.cli(self.tmp, "engines", "install", "v0.19.1")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("already installed", r2.stderr)
        self.assertEqual(_tree_digest(self.engine("v0.19.1")), d)

    def test_bad_and_pre_engine_tags_are_refused(self):
        for tag in ("../x", "v0.19", "latest", "v0.18.8", "v0.19.0;rm"):
            r = self.cli(self.tmp, "engines", "install", tag)
            self.assertEqual(r.returncode, 2, (tag, r.stderr))
        self.assertFalse(self.store.exists() and any(self.store.iterdir()))

    def test_a_pin_the_store_lacks_is_fetched_on_first_use_and_not_when_offline(self):
        # a canonical clone that lacks the tag; its origin has it
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.clone), str(origin)], check=True)
        local = self.tmp / "canonical"
        subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
        _git(local, "tag", "-d", "v0.19.1")
        proj = self.new_project()
        (proj / ".phasekit-version").write_text("v0.19.1\n")
        _git(proj, "commit", "-qam", "pin")
        off = self.cli(proj, "verify", PHASEKIT_HOME=str(local), PHASEKIT_NO_AUTO_FETCH="1")
        self.assertEqual(off.returncode, 6, off.stdout + off.stderr)
        self.assertFalse((self.store / "v0.19.1").exists())
        on = self.cli(proj, "verify", PHASEKIT_HOME=str(local))
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        self.assertEqual((self.store / "v0.19.1" / ".engine-commit").read_text().strip(),
                         _git(self.clone, "rev-parse", "v0.19.1^{commit}"))


class Upgrade(EngineFixture):
    def test_a_pin_bump_is_one_gated_one_line_commit(self):
        proj = self.new_project()
        base = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "upgrade", "--to", "v0.19.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_git(proj, "diff", "--name-only", base, "HEAD"), ".phasekit-version")
        self.assertEqual(_git(proj, "show", "HEAD:.phasekit-version"), "v0.19.1")
        self.assertIn("v0.19.0 -> v0.19.1", _git(proj, "log", "-1", "--format=%s"))

    def test_a_red_gate_changes_nothing(self):
        proj = self.new_project()
        (proj / "docs" / "SPEC.md").write_text("[broken](nowhere.md)\n")
        _git(proj, "commit", "-qam", "a broken link the docs gate refuses")
        base = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "upgrade", "--to", "v0.19.1")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), base)
        self.assertEqual((proj / ".phasekit-version").read_text().strip(), "v0.19.0")
        self.assertEqual(_git(proj, "status", "--porcelain"), "")

    def test_the_gate_sees_the_new_engines_contract_as_PHASEKIT_CONTRACT(self):
        """v0.19.2: the pin bump's gate reads the declared surface by the
        exported path — the NEW engine's — on the host and in the runner."""
        proj = self.new_project()
        seen = self.tmp / "gate-contract"
        r = self.cli(proj, "upgrade", "--to", "v0.19.1",
                     PHASEKIT_CONTRACT="/nowhere/interface.json",
                     PHASEKIT_VERIFY_CMD=f'printf "%s" "$PHASEKIT_CONTRACT" > "{seen}"; '
                                         'jq -e ".interface == \\"phasekit\\"" "$PHASEKIT_CONTRACT" >/dev/null')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Path(seen.read_text()).resolve(),
                         (self.engine("v0.19.1") / "contracts" / "interface.json").resolve())

    def test_the_runner_gate_is_given_the_mounted_engines_contract(self):
        proj = self.new_project()
        log = self.tmp / "docker.log"
        (self.bin / "docker").write_text(
            '#!/usr/bin/env bash\necho "$*" >> "' + str(log) + '"\n'
            'case "$1" in info) echo 27.0 ;; esac\nexit 0\n')
        os.chmod(self.bin / "docker", 0o755)
        r = self.cli(proj, "upgrade", "--to", "v0.19.1", PHASEKIT_UPGRADE_VERIFY="container",
                     PHASEKIT_ROOTLESS_DOCKER="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        run = next(c for c in log.read_text().splitlines() if c.startswith("run "))
        self.assertIn("-e PHASEKIT_CONTRACT=/opt/phasekit/contracts/interface.json", run)


READS_THE_CONTRACT = ("import { readFileSync } from 'node:fs';\n"
                      "const c = JSON.parse(readFileSync(new URL('../contracts/interface.json', "
                      "import.meta.url), 'utf8'));\n")
NAMES_IT_AS_DATA = ("// contracts/interface.json is read through PHASEKIT_CONTRACT\n"
                    "const SAMPLE = \"readFileSync('docs/QUALITY_GATES.md')\";\n"
                    "const FORBIDDEN = ['contracts/interface.json', 'scripts/run-until-done.sh'];\n")


class Migrate(EngineFixture):
    """§9 AC5, on a fixture enriched by the PREVIOUS release (v0.18.8)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.prev = Path(cls._base) / "prev"
        cls.prev.mkdir()
        tar = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "v0.18.8"], capture_output=True)
        cls.have_prev = tar.returncode == 0
        if cls.have_prev:
            subprocess.run(["tar", "-x", "-C", str(cls.prev)], input=tar.stdout, check=True)

    def vendored(self):
        if not self.have_prev:
            self.skipTest("tag v0.18.8 not in this clone")
        proj = self.tmp / "vend"
        proj.mkdir()
        _git(proj, "init", "-q", "-b", "master")
        _git(proj, "config", "user.name", "t")
        _git(proj, "config", "user.email", "t@t")
        r = subprocess.run(["python3", str(self.prev / "scripts" / "enrich-project.py"), str(proj),
                            "--profile", "docs-only"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "vendored at v0.18.8")
        return proj

    def snapshot(self, proj):
        return {p: (proj / p).read_bytes() for p in _git(proj, "ls-files").splitlines()}

    def test_exact_and_idempotent(self):
        proj = self.vendored()
        before = self.snapshot(proj)
        man = json.loads((proj / ".scaffold" / "manifest.json").read_text())
        scaffold = {e["path"] for e in man["files"] if e["ownership"] == "scaffold"}
        r = self.cli(proj, "migrate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.snapshot(proj)
        removed = set(before) - set(after)
        self.assertEqual(removed - {p for p in before if p.startswith(".scaffold/")}, scaffold)
        self.assertFalse(any(p.startswith(".scaffold/") for p in after))
        self.assertFalse((proj / ".scaffold").exists())
        self.assertEqual(set(after) - set(before), {".phasekit-version"})
        own = _git(self.clone, "describe", "--tags")   # migrate pins the CLI's own release
        self.assertEqual(after[".phasekit-version"], (own + "\n").encode())
        for p in set(before) & set(after):
            if p == ".claude/settings.json":
                old, new = json.loads(before[p]), json.loads(after[p])
                self.assertEqual(old["permissions"], new["permissions"])
                self.assertNotIn("hooks", new)
            else:
                self.assertEqual(before[p], after[p], p)
        self.assertEqual(len(_git(proj, "log", "--format=%H").splitlines()), 2)
        head = _git(proj, "rev-parse", "HEAD")
        r2 = self.cli(proj, "migrate")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), head, "a second run changes nothing")
        self.assertEqual(self.snapshot(proj), after)
        # and the result is a clean pinned project
        self.assertEqual(self.cli(proj, "check").returncode, 0)

    def test_the_projects_own_hooks_are_kept(self):
        proj = self.vendored()
        s = json.loads((proj / ".claude" / "settings.json").read_text())
        s["hooks"].setdefault("PostToolUse", []).append(
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "./.claude/hooks/my-lint.sh"}]})
        (proj / ".claude" / "settings.json").write_text(json.dumps(s, indent=2) + "\n")
        (proj / ".claude" / "hooks" / "my-lint.sh").write_text("#!/bin/sh\nexit 0\n")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "a project hook")
        r = self.cli(proj, "migrate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = json.loads((proj / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for g in after["hooks"]["PostToolUse"] for h in g["hooks"]]
        self.assertEqual(cmds, ["./.claude/hooks/my-lint.sh"])
        self.assertTrue((proj / ".claude" / "hooks" / "my-lint.sh").exists())
        self.assertEqual(self.cli(proj, "check").returncode, 0)

    def test_a_red_gate_leaves_the_tree_exactly_as_it_was(self):
        proj = self.vendored()
        (proj / "docs" / "SPEC.md").write_text("[broken](nowhere.md)\n")
        _git(proj, "commit", "-qam", "broken link")
        before = self.snapshot(proj)
        head = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "migrate")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), head)
        self.assertEqual(self.snapshot(proj), before)
        self.assertEqual(_git(proj, "status", "--porcelain"), "")
        self.assertTrue((proj / ".scaffold" / "manifest.json").exists())

    def test_local_changes_to_engine_files_are_refused_not_discarded(self):
        proj = self.vendored()
        with open(proj / "docs" / "QUALITY_GATES.md", "a") as f:
            f.write("\nlocal amendment\n")
        _git(proj, "commit", "-qam", "amend a scaffold doc")
        head = _git(proj, "rev-parse", "HEAD")
        r = self.cli(proj, "migrate")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("docs/QUALITY_GATES.md", r.stderr)
        self.assertEqual(_git(proj, "rev-parse", "HEAD"), head)

    def test_a_dirty_tree_is_refused(self):
        proj = self.vendored()
        (proj / "wip.txt").write_text("x")
        r = self.cli(proj, "migrate")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue((proj / ".scaffold" / "manifest.json").exists())

    # -- v0.19.2: the pre-flight --------------------------------------------
    def with_tests(self, proj, files):
        for rel, text in files.items():
            (proj / rel).parent.mkdir(parents=True, exist_ok=True)
            (proj / rel).write_text(text)
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "tests")

    def test_a_file_reading_an_engine_path_is_refused_before_the_gate_with_its_line(self):
        proj = self.vendored()
        self.with_tests(proj, {"test/facts.test.js": READS_THE_CONTRACT,
                               "test/prose.test.js": NAMES_IT_AS_DATA})
        before, head = self.snapshot(proj), _git(proj, "rev-parse", "HEAD")
        for args in (("migrate",), ("migrate", "--dry-run")):
            with self.subTest(args=args):
                r = self.cli(proj, *args, PHASEKIT_VERIFY_CMD="echo GATE-RAN")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("test/facts.test.js:2: contracts/interface.json", r.stderr)
                self.assertNotIn("prose.test.js", r.stderr + r.stdout, "data and prose are never reads")
                self.assertIn("PHASEKIT_CONTRACT", r.stderr)
                self.assertIn("--force", r.stderr)
                self.assertNotIn("GATE-RAN", r.stdout + r.stderr, "refused BEFORE the gate")
                self.assertEqual(self.snapshot(proj), before)
                self.assertEqual(_git(proj, "rev-parse", "HEAD"), head)
                self.assertEqual(_git(proj, "status", "--porcelain"), "")

    def test_force_skips_the_pre_flight_and_the_gate_decides(self):
        proj = self.vendored()
        self.with_tests(proj, {"test/facts.test.js": READS_THE_CONTRACT})
        r = self.cli(proj, "migrate", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((proj / ".phasekit-version").exists())

    def test_a_project_reading_PHASEKIT_CONTRACT_migrates_and_its_gate_sees_the_engines(self):
        proj = self.vendored()
        self.with_tests(proj, {"test/facts.test.js": (
            "import { readFileSync } from 'node:fs';\n"
            "const c = JSON.parse(readFileSync(process.env.PHASEKIT_CONTRACT, 'utf8'));\n")})
        seen = self.tmp / "gate-contract"
        r = self.cli(proj, "migrate", PHASEKIT_VERIFY_CMD=f'printf "%s" "$PHASEKIT_CONTRACT" > "{seen}"')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        own = _git(self.clone, "describe", "--tags")
        self.assertEqual(Path(seen.read_text()).resolve(),
                         (self.engine(own) / "contracts" / "interface.json").resolve())


class Plugin(EngineFixture):
    def test_install_is_idempotent(self):
        r1 = self.cli(self.tmp, "plugin", "install")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        calls1 = (self.state / "plugin-calls").read_text().splitlines()
        self.assertIn(f"plugin marketplace add {self.clone}", calls1)
        self.assertIn("plugin install phasekit@phasekit", calls1)
        r2 = self.cli(self.tmp, "plugin", "install")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        calls2 = (self.state / "plugin-calls").read_text().splitlines()[len(calls1):]
        self.assertEqual(calls2, ["plugin list --json"], "an installed plugin is left alone")

    def test_init_installs_it(self):
        proj = self.tmp / "p2"
        proj.mkdir()
        _git(proj, "init", "-q")
        _git(proj, "config", "user.name", "t")
        _git(proj, "config", "user.email", "t@t")
        r = self.cli(proj, "init", "docs-only", "--pin", "v0.19.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.state / "plugin-installed").exists())


class StaticPins(unittest.TestCase):
    """§9 AC6, AC7, AC9 and the path audit."""

    def test_no_prompt_hook_or_agent_names_the_vendored_cli_or_checker(self):
        corpus = {"CONTINUE_PROMPT.txt": (REPO_ROOT / "CONTINUE_PROMPT.txt").read_text()}
        for p in sorted((REPO_ROOT / ".claude" / "hooks").glob("*.sh")) + \
                sorted((REPO_ROOT / ".claude" / "agents").glob("*.md")):
            corpus[str(p.relative_to(REPO_ROOT))] = p.read_text()
        import re
        for rel in ("scripts/run-until-done.sh", "scripts/run-phase.sh"):
            text = (REPO_ROOT / rel).read_text()
            blocks = re.findall(r"<<'?([A-Z_]+_EOF)'?\n(.*?)\n\1\n", text, re.S)
            corpus[f"{rel} heredocs"] = "\n".join(body for _tag, body in blocks)
        corpus["run-until-done.sh heredocs"] = corpus["scripts/run-until-done.sh heredocs"]
        self.assertIn("LIGHT MODE", corpus["run-until-done.sh heredocs"])
        for name, text in corpus.items():
            for banned in ("bash scripts/phasekit.sh", "scripts/phasekit-contracts.py"):
                self.assertNotIn(banned, text, f"{name} names {banned}")

    def test_the_verify_templates_call_the_cli(self):
        for p in sorted((REPO_ROOT / "templates").glob("phasekit-verify.template*.sh")):
            code = [ln.strip() for ln in p.read_text().splitlines() if not ln.lstrip().startswith("#")]
            self.assertIn("phasekit contracts check", code, p.name)

    def test_the_prompt_offers_exactly_the_engines_process_docs(self):
        """run-phase.sh's ENGINE_PROCESS_DOCS = the scaffold-class docs a
        vendored project carries (one list, pinned to the manifest)."""
        import importlib.util
        import re
        spec = importlib.util.spec_from_file_location("pk_enrich_eo2", REPO_ROOT / "scripts" / "enrich-project.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        man = m.load_manifest()
        docs = set()
        for prof in man["profiles"]:
            for t in m.enumerate_install_targets(man, m.resolve_profile(man["profiles"], prof)):
                if t["ownership"] == "scaffold" and t["path"].startswith("docs/"):
                    docs.add(t["path"][5:-3])
        listed = re.search(r'^ENGINE_PROCESS_DOCS="([^"]*)"', (REPO_ROOT / "scripts" / "run-phase.sh").read_text(), re.M)
        self.assertEqual(set(listed.group(1).split()), docs)

    def test_the_plugin_hooks_are_the_settings_templates_hooks(self):
        def entries(hooks, via_plugin):
            out = set()
            for event, groups in hooks.items():
                for g in groups:
                    for h in g["hooks"]:
                        cmd = h["command"]
                        name = cmd.split()[-1] if via_plugin else cmd.rsplit("/", 1)[-1][:-3]
                        out.add((event, g.get("matcher"), name))
            return out
        tmpl = json.loads((REPO_ROOT / "templates" / "settings.template.json").read_text())["hooks"]
        plug = json.loads((REPO_ROOT / "plugin" / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(entries(plug, True) - {("UserPromptSubmit", None, "guard-probe")},
                         entries(tmpl, False))
        self.assertIn(("UserPromptSubmit", None, "guard-probe"), entries(plug, True))

    def test_the_plugin_carries_the_engines_own_agents_and_hooks(self):
        self.assertEqual(os.readlink(REPO_ROOT / "plugin" / "agents"), "../.claude/agents")
        self.assertEqual(os.readlink(REPO_ROOT / "plugin" / "hooks" / "scripts"), "../../.claude/hooks")
        self.assertEqual(sorted(p.name for p in (REPO_ROOT / "plugin" / "agents").iterdir()),
                         sorted(p.name for p in (REPO_ROOT / ".claude" / "agents").iterdir()))

    def test_the_contract_declares_the_engine_variables(self):
        env = {e["name"] for e in json.loads((REPO_ROOT / "contracts" / "interface.json").read_text())["env"]}
        for name in ("PHASEKIT_PROJECT_DIR", "PHASEKIT_ENGINE_DOCS", "PHASEKIT_ENGINE_DIR",
                     "PHASEKIT_ENGINE_STORE", "PHASEKIT_NO_AUTO_FETCH", "PHASEKIT_CONTRACT"):
            self.assertIn(name, env)

    def test_the_engine_reads_only_the_projects_gate_from_its_tree(self):
        """Every `$ROOT_DIR/scripts/…` the engine names is the project's own
        gate; every other engine file comes from ENGINE_DIR."""
        import re
        for rel in ("scripts/run-until-done.sh", "scripts/run-phase.sh", "scripts/container-setup.sh"):
            text = (REPO_ROOT / rel).read_text()
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            for m in re.findall(r"(?<!:-)\$\{?ROOT_DIR\}?/(scripts/[A-Za-z0-9_.-]+|CONTINUE_PROMPT\.txt|\.claude/(?:hooks|agents)\S*|plugin\S*)", code):
                self.assertEqual(m, "scripts/phasekit-verify.sh", f"{rel} reads {m} from the project")


class ContainerSetupPinned(unittest.TestCase):
    """§9 AC8 against a fake docker, and the per-project config root (P9)."""

    FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  version) echo "1.54 1.54" ;;
  image) exit 0 ;;
  volume) exit 0 ;;
  run)
    for a in "$@"; do if [[ "$a" == "-c" ]]; then prep=1; fi; done
    if [[ "${prep:-}" == 1 && "$*" == *--network\ none* ]]; then
      echo seeded:none; echo config:seeded; echo creds:present
      [[ -n "${FAKE_EXPIRES:-}" ]] && echo "creds-expires:$FAKE_EXPIRES"
    elif [[ "${prep:-}" == 1 && "$*" == *phasekit-login* ]]; then
      echo "${FAKE_REFRESHED:-0}"
    fi ;;
esac
exit 0
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-cs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(self.FAKE_DOCKER)
        os.chmod(self.bin / "docker", 0o755)
        self.log = self.tmp / "docker.log"
        self.proj = self.tmp / "myproj"
        self.proj.mkdir()

    def run_cs(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("PHASEKIT_")}
        e.update({"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                  "FAKE_DOCKER_LOG": str(self.log), "PHASEKIT_PROJECT_DIR": str(self.proj)})
        e.update(env)
        return subprocess.run(["bash", str(REPO_ROOT / "scripts" / "container-setup.sh"), *args],
                              capture_output=True, text=True, env=e, timeout=60, stdin=subprocess.DEVNULL)

    def session_args(self):
        runs = [ln for ln in self.log.read_text().splitlines() if ln.startswith("run ")]
        return runs[-1]

    def test_mounts_project_engine_read_only_path_and_no_build(self):
        r = self.run_cs("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log.read_text()
        self.assertNotIn("build", [ln.split()[0] for ln in log.splitlines()])
        self.assertNotIn("\ntag ", "\n" + log)
        run = self.session_args()
        self.assertIn(f"-v {self.proj}:/workspace", run)
        self.assertIn(f"-v {REPO_ROOT}:/opt/phasekit:ro", run)
        self.assertIn("-e PHASEKIT_PROJECT_DIR=/workspace", run)
        self.assertIn('export PATH="/opt/phasekit/bin:$PATH"', run)
        self.assertIn("/opt/phasekit/scripts/run-until-done.sh", run)
        self.assertEqual(len([ln for ln in log.splitlines() if ln.startswith("run ")]), 2,
                         "one prep run and one session run, as v0.18.8")

    def test_a_per_project_config_root_never_the_whole_volume(self):
        r = self.run_cs("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        run = self.session_args()
        self.assertNotIn("-v scaffold-claude-config:/home/node/.claude", run)
        self.assertIn("type=volume,src=scaffold-claude-config,dst=/home/node/.claude,"
                      "volume-subpath=project-config/myproj", run)
        self.assertIn("dst=/home/node/.claude/projects/-workspace,volume-subpath=project-sessions/myproj", run)
        self.assertIn("dst=/home/node/.claude/.credentials.json,volume-subpath=.credentials.json", run)
        log = self.log.read_text()
        prep = log[log.index("--network none"):]
        self.assertIn("project-config", prep[:prep.index("\nrun ")])

    def test_old_docker_is_refused_not_given_the_volume(self):
        (self.bin / "docker").write_text(self.FAKE_DOCKER.replace('echo "1.54 1.54"', 'echo "1.43 1.43"'))
        r = self.run_cs("run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)
        self.assertEqual(len([ln for ln in self.log.read_text().splitlines() if ln.startswith("run ")]), 1,
                         "only the prep ran; no session")

    def _runs(self):
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith("run ")]

    def test_a_login_that_outlives_the_session_is_left_alone(self):
        import time
        r = self.run_cs("run", FAKE_EXPIRES=str((int(time.time()) + 6 * 3600) * 1000))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("phasekit-login", self.log.read_text())

    def test_a_login_that_expires_inside_the_session_is_refreshed_first_in_place(self):
        import time
        now = int(time.time())
        r = self.run_cs("run", FAKE_EXPIRES=str((now + 600) * 1000),
                        FAKE_REFRESHED=str((now + 8 * 3600) * 1000))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("refreshing it first", r.stdout)
        self.assertEqual(self.log.read_text().count("mktemp -d /tmp/phasekit-login"), 1)
        self.assertIn('cat "$tmp/.credentials.json" > "$vol/.credentials.json"', self.log.read_text(),
                      "written back in place (same inode), never renamed")
        self.assertIn("type=volume,src=scaffold-claude-config,dst=/home/node/.claude/.credentials.json",
                      self.session_args())

    def test_a_refresh_that_fails_refuses_the_session(self):
        import time
        now = int(time.time())
        r = self.run_cs("run", FAKE_EXPIRES=str((now + 600) * 1000), FAKE_REFRESHED=str((now + 600) * 1000))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not refresh the shared login", r.stderr)
        self.assertFalse(any("/opt/phasekit/scripts/run-until-done.sh" in ln for ln in self._runs()))

    def test_refresh_off_refuses_instead(self):
        import time
        r = self.run_cs("run", FAKE_EXPIRES=str((int(time.time()) + 600) * 1000), PHASEKIT_LOGIN_REFRESH="0")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("phasekit-login", self.log.read_text())

    def test_a_missing_image_is_built_once_from_the_engine(self):
        (self.bin / "docker").write_text(self.FAKE_DOCKER.replace("  image) exit 0 ;;", "  image) exit 1 ;;"))
        r = self.run_cs("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        builds = [ln for ln in self.log.read_text().splitlines() if ln.startswith("build ")]
        self.assertEqual(len(builds), 1)
        self.assertTrue(builds[0].endswith(f"{REPO_ROOT}/.devcontainer/"), builds)


class PluginDispatcher(unittest.TestCase):
    RUN = REPO_ROOT / "plugin" / "hooks" / "run-hook.sh"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-disp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def hook(self, project, name, payload, **env):
        e = {k: v for k, v in os.environ.items()
             if k not in ("PHASEKIT_ARTIFACTS_DIR", "PHASEKIT_ITER_MARKER", "CLAUDE_PROJECT_DIR")}
        e["CLAUDE_PROJECT_DIR"] = str(project)
        e.update(env)
        return subprocess.run(["bash", str(self.RUN), name], input=json.dumps(payload),
                              capture_output=True, text=True, env=e, cwd=str(project), timeout=30)

    RESET = {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD"}}

    def test_a_pinned_project_is_guarded(self):
        p = self.tmp / "pinned"
        p.mkdir()
        (p / ".phasekit-version").write_text("v0.19.0\n")
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET).returncode, 2)

    def test_another_repository_is_none_of_its_business(self):
        p = self.tmp / "other"
        p.mkdir()
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET).returncode, 0)

    def test_a_vendored_projects_own_wiring_wins_but_a_dead_path_does_not(self):
        # a vendored project (no pin) under its own loop, with the plugin installed
        p = self.tmp / "vend"
        (p / ".claude" / "hooks").mkdir(parents=True)
        (p / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "./.claude/hooks/deny-dangerous-commands.sh"}]}]}}))
        loop = {"PHASEKIT_ARTIFACTS_DIR": str(p / "artifacts")}
        # wiring present but its file is gone: the plugin still guards
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET, **loop).returncode, 2)
        (p / ".claude" / "hooks" / "deny-dangerous-commands.sh").write_text("#!/bin/sh\nexit 0\n")
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET, **loop).returncode, 0,
                         "the project's own hook fires; the plugin steps aside")
        # a mention outside the hooks (a permissions entry) is not wiring
        (p / ".claude" / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(./.claude/hooks/deny-dangerous-commands.sh)"]}}))
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET, **loop).returncode, 2)

    def test_a_pinned_project_never_steps_aside(self):
        p = self.tmp / "pin"
        (p / ".claude" / "hooks").mkdir(parents=True)
        (p / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "./.claude/hooks/deny-dangerous-commands.sh"}]}]}}))
        (p / ".claude" / "hooks" / "deny-dangerous-commands.sh").write_text("#!/bin/sh\nexit 0\n")
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET,
                                   PHASEKIT_ENGINE_DOCS="/engine/docs").returncode, 2, "under a pinned loop")
        (p / ".phasekit-version").write_text("v0.19.0\n")
        self.assertEqual(self.hook(p, "deny-dangerous-commands", self.RESET).returncode, 2, "in a pinned project")

    def test_the_probe_writes_its_token_and_blocks(self):
        p = self.tmp / "probe"
        p.mkdir()
        f = self.tmp / "probe.out"
        r = self.hook(p, "guard-probe", {}, PHASEKIT_GUARD_PROBE=str(f), PHASEKIT_GUARD_PROBE_TOKEN="tok")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(f.read_text(), "tok ok\n")

    def test_the_probe_is_inert_without_its_variable(self):
        p = self.tmp / "probe2"
        p.mkdir()
        self.assertEqual(self.hook(p, "guard-probe", {}).returncode, 0)


if __name__ == "__main__":
    unittest.main()


import importlib.util as _ilu  # noqa: E402
import re as _re  # noqa: E402

_hs = _ilu.spec_from_file_location("pk_boundary_harness_eo", Path(__file__).resolve().parent / "test_boundary_state.py")
_H = _ilu.module_from_spec(_hs)
_hs.loader.exec_module(_H)


@unittest.skipUnless(HAVE_TOOLS, "bash+git+jq+python3 required")
class LayoutDifferential(unittest.TestCase):
    """The same sessions through a vendored and a pinned project land the
    SAME commits: subjects, bodies, trailers, and the paths each carries
    (shas and dates aside). A pinned landing never carries an engine path or
    .scaffold/."""

    SCENARIOS = {
        "approve-final": ("both", _H.APPROVE_SCENARIO),
        "approve-then-complete": ("no", _H.APPROVE_SCENARIO),
        "update-then-approve": ("both",
                                'if [ "$CALL_N" = 1 ]; then echo step >> src.txt; '
                                "jq -n '{suggested_commit_message: \"phase-1: step\"}' > artifacts/phase-update.json; "
                                "else " + _H.APPROVE_SCENARIO.replace("\n", "\n  ") + "\nfi\n"),
    }

    @staticmethod
    def _shape(repo):
        log = repo.git("log", "--reverse", "--format=%x01%s%n%b%x02", "--name-status", "--no-renames",
                       f"{repo.base}..main")
        log = _re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", log)
        log = _re.sub(r"\d{4}-\d\d-\d\dT[\d:]+Z", "<time>", log)
        return log

    def test_landings_are_identical_in_both_layouts(self):
        for name, (fk, scenario) in self.SCENARIOS.items():
            for squash in (False, True):
                with self.subTest(scenario=name, squash=squash):
                    shapes = {}
                    for pinned in (False, True):
                        repo = _H.Repo(squash=squash, pinned=pinned)
                        self.addCleanup(repo.cleanup)
                        repo.scenario(scenario)
                        r = repo.run(env={"FINAL_KIND": fk, "MAX_ITERATIONS": "3"})
                        out = r.stdout + r.stderr
                        self.assertIn("boundary-state: rested (step 7)", out, out)
                        shapes[pinned] = self._shape(repo)
                        if pinned:
                            changed = repo.git("log", "--format=", "--name-only", f"{repo.base}..main").split()
                            self.assertFalse([p for p in changed if p.startswith((".scaffold", "scripts/run-",
                                              "CONTINUE_PROMPT", ".claude/hooks", ".claude/agents", "plugin/"))],
                                             changed)
                            self.assertNotIn(".phasekit-version", changed)
                    self.assertEqual(shapes[False], shapes[True])


class LayoutCoverage(unittest.TestCase):
    def test_every_module_on_a_layout_harness_runs_in_both_layouts(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _layout
        users = set()
        for p in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
            text = p.read_text()
            if any(k in text for k in ("from _layout import", "H.Repo(", "LoopHarness")):
                users.add(p.stem)
        self.assertTrue(users)
        self.assertEqual(sorted(users - set(_layout.LAYOUT_MODULES)), [])
        for m in _layout.LAYOUT_MODULES:
            self.assertTrue((Path(__file__).resolve().parent / f"{m}.py").is_file(), m)


@unittest.skipUnless(all(shutil.which(c) for c in ("sh", "jq", "flock")), "sh+jq+flock required")
class LoginRefreshScript(unittest.TestCase):
    """container-setup.sh's LOGIN_REFRESH_SH itself, run by sh with a fake
    `claude` (review round 2): in place, fail-closed, serialised, re-checked."""

    FAKE = r"""#!/bin/sh
echo call >> "$FAKE_LOG"
[ -n "${FAKE_FAIL:-}" ] && exit 1
sleep "${FAKE_SLEEP:-0}"
jq '.claudeAiOauth.accessToken = "NEW-ACCESS-TOKEN-0123456789abcdef" | .claudeAiOauth.refreshToken = "NEW-REFRESH-TOKEN-0123456789abcdef" | .claudeAiOauth.expiresAt = 9999999999000' \
  "$CLAUDE_CONFIG_DIR/.credentials.json" > "$CLAUDE_CONFIG_DIR/x" && mv "$CLAUDE_CONFIG_DIR/x" "$CLAUDE_CONFIG_DIR/.credentials.json"
echo "SECRET-LEAK-CANARY" >&2
"""

    def setUp(self):
        import re as _r
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-login-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        text = (REPO_ROOT / "scripts" / "container-setup.sh").read_text()
        m = _r.search(r"^LOGIN_REFRESH_SH='(.*?)'\n\n", text, _r.S | _r.M)
        self.script = m.group(1).replace("'\"'\"'", "'")
        self.vol = self.tmp / "vol"
        self.vol.mkdir()
        self.creds = self.vol / ".credentials.json"
        self.creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "OLD-ACCESS-TOKEN-0123456789abcdef", "refreshToken": "OLD-REFRESH-TOKEN-0123456789abcdef",
            "expiresAt": 1000}}))
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "claude").write_text(self.FAKE)
        os.chmod(bin_ / "claude", 0o755)
        self.log = self.tmp / "claude.log"
        self.env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)}

    def run_script(self, need_ms="5000", **extra):
        return subprocess.run(["sh", "-c", self.script, "sh", str(self.vol), need_ms],
                              capture_output=True, text=True, env={**self.env, **extra}, timeout=60)

    def test_refreshes_in_place_and_prints_only_the_expiry(self):
        ino = self.creds.stat().st_ino
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "9999999999000")
        self.assertEqual(self.creds.stat().st_ino, ino, "written in place, never renamed")
        self.assertIn("NEW-REFRESH-TOKEN", self.creds.read_text())
        self.assertNotIn("TOKEN", r.stdout + r.stderr)
        self.assertNotIn("CANARY", r.stdout + r.stderr)

    def test_a_failed_refresh_leaves_the_login_untouched(self):
        before = self.creds.read_bytes()
        r = self.run_script(FAKE_FAIL="1")
        self.assertEqual(r.stdout.strip(), "1000")
        self.assertEqual(self.creds.read_bytes(), before)

    def test_a_login_already_fresh_under_the_lock_is_not_refreshed_again(self):
        r = self.run_script(need_ms="500")
        self.assertEqual(r.stdout.strip(), "1000")
        self.assertFalse(self.log.exists(), "no refresh when the expiry already clears the need")

    def test_concurrent_preps_refresh_once(self):
        procs = [subprocess.Popen(["sh", "-c", self.script, "sh", str(self.vol), "5000"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={**self.env, "FAKE_SLEEP": "1"}) for _ in range(3)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(outs, ["9999999999000"] * 3)
        self.assertEqual(self.log.read_text().count("call"), 1, "one refresh, the others re-checked under the lock")


class VendoredShim(unittest.TestCase):
    """Review round 2: in a vendored loop the `phasekit` shim answers the
    project-facing verbs from the vendored copy and hands every other verb to
    the CLI installed before the loop started — by absolute path, so a PATH
    prefix added later can never make it exec itself."""

    def test_other_verbs_reach_the_installed_cli_even_under_a_prefixed_path(self):
        repo = _H.Repo(squash=False, pinned=False)
        self.addCleanup(repo.cleanup)
        installed = repo.tmp / "installed"
        installed.mkdir()
        (installed / "phasekit").write_text('#!/bin/sh\necho "INSTALLED $*"\n')
        os.chmod(installed / "phasekit", 0o755)
        other = repo.tmp / "other"
        other.mkdir()
        repo.put_engine("scripts/phasekit.sh", (REPO_ROOT / "scripts" / "phasekit.sh").read_text(),
                        executable=True, commit="cli")
        repo.scenario('PATH="' + str(other) + ':$PATH" timeout 10 phasekit status > "$STUB_DIR/st.out" 2>&1; '
                      'echo $? > "$STUB_DIR/st.rc"\n' + _H.APPROVE_SCENARIO)
        repo.run(env={"MAX_ITERATIONS": "1", "PATH": f"{installed}:{os.environ['PATH']}"})
        self.assertEqual((repo.stub / "st.rc").read_text().strip(), "0", (repo.stub / "st.out").read_text())
        self.assertEqual((repo.stub / "st.out").read_text().strip(), "INSTALLED status")
