"""The two layouts every loop-behaviour test runs under (v0.19.0).

phasekit's engine runs a project in one of two layouts:

  * VENDORED (every project before v0.19.0): the engine's files — the loop,
    run-phase.sh, CONTINUE_PROMPT.txt, the tools — sit in the project's own
    tree. ENGINE_DIR == ROOT_DIR, PHASEKIT_PROJECT_DIR unset.
  * PINNED: the project tracks only its pin; the engine is a separate,
    READ-ONLY directory, and the loop is pointed at the project with
    PHASEKIT_PROJECT_DIR.

A test that builds a fixture project places the engine's files through a
`Layout`, never by hand, so the same test runs in both: `PHASEKIT_TEST_LAYOUT`
(vendored | pinned; default vendored) picks one for the whole run. The full
suite runs vendored; LAYOUT_MODULES (below) run a second time pinned
(tests/run-layouts.sh, CI's `pinned layout` step); a case that passes
vendored and fails pinned is a release blocker. Under `pinned`, the engine directory is made read-only the
first time the loop is about to run (`env()`), and the project tree holds no
engine file at all, so a project-relative read of an engine path fails and a
write under the engine raises — the cross-talk the layout split must never
have.
"""

import os
import shutil
import stat
from pathlib import Path

LAYOUT = os.environ.get("PHASEKIT_TEST_LAYOUT", "vendored").strip() or "vendored"
if LAYOUT not in ("vendored", "pinned"):
    raise RuntimeError(f"PHASEKIT_TEST_LAYOUT={LAYOUT!r}: expected vendored or pinned")
PINNED = LAYOUT == "pinned"

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every loop-behaviour module: the ones that build fixtures through this file
# (or the boundary / v0.6.0 harnesses built on it), and the function-
# extraction suites of the loop and hooks. tests/test_engine_outside.py pins
# that no module using a layout harness is missing here.
LAYOUT_MODULES = (
    "test_boundary_state", "test_branch_squash", "test_compact_reanchor",
    "test_completion_commit_order", "test_completion_key_order",
    "test_contracts_awareness_v070", "test_contracts_gate_v070", "test_declared_contract",
    "test_deadline_watchdog", "test_deadman_handoff", "test_declared_surface",
    "test_engine_outside", "test_guard_scope", "test_iteration_facts",
    "test_loop_owns_commits", "test_meta_retired", "test_multiphase_invariance", "test_no_verdict_retry",
    "test_output_discipline", "test_post_completion", "test_require_verdict_hook",
    "test_run_until_done_v060", "test_run_until_done_v066", "test_scaffold_lock",
    "test_scaffold_reads", "test_session_resume", "test_suite_tmpdir",
    "test_wrapup_nudge_hook",
)

# The stub run-phase scripts tests write find the project the way the real one
# does: PHASEKIT_PROJECT_DIR (pinned), else their own parent (vendored).
STUB_ROOT_LINE = 'ROOT_DIR="${PHASEKIT_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"'


# What Claude Code does for a --plugin-dir plugin's UserPromptSubmit hook when
# the loop's guard probe starts `claude` (pinned runs): the real hook script
# runs, writes the probe's token and blocks the prompt — no model call. Any
# other call goes to the next `claude` on PATH (the test's own fake), which
# therefore never sees, or counts, a probe.
CLAUDE_PROBE_SHIM = r"""#!/usr/bin/env bash
if [[ -n "${PHASEKIT_GUARD_PROBE:-}" ]]; then
  _a=("$@")
  for ((_i = 0; _i < ${#_a[@]}; _i++)); do
    if [[ "${_a[$_i]}" == --plugin-dir ]]; then
      CLAUDE_PLUGIN_ROOT="${_a[$((_i + 1))]}" bash "${_a[$((_i + 1))]}/hooks/run-hook.sh" guard-probe </dev/null
    fi
  done
  exit 0
fi
self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IFS=: read -r -a _dirs <<< "$PATH"
for d in "${_dirs[@]}"; do
  [[ "$(cd "$d" 2>/dev/null && pwd)" == "$self" ]] && continue
  if [[ -x "$d/claude" ]]; then exec "$d/claude" "$@"; fi
done
echo "claude probe shim: no claude after $self on PATH" >&2
exit 127
"""


def _chmod_tree(root, writable):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                continue
            mode = stat.S_IMODE(os.lstat(p).st_mode)
            os.chmod(p, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))
        mode = stat.S_IMODE(os.lstat(dirpath).st_mode)
        os.chmod(dirpath, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))


class Layout:
    """Where a fixture's engine files go, and how its loop is run.

    repo   -- the fixture project (a git work tree, or about to be one)
    engine -- the engine directory: `repo` itself when vendored; a sibling
              directory `<repo>-engine` when pinned
    """

    def __init__(self, repo, pinned=None):
        self.repo = Path(repo)
        self.pinned = PINNED if pinned is None else pinned
        self.engine = (self.repo.parent / (self.repo.name + "-engine")) if self.pinned else self.repo
        self.sealed = False
        if self.pinned:
            self.engine.mkdir(parents=True, exist_ok=True)
            self.put_plugin()

    def put_plugin(self):
        """The engine's plugin (pinned runs pass it with --plugin-dir) and the
        hooks and agents its links point at — the shipped bytes."""
        for rel in ("plugin", ".claude/hooks", ".claude/agents"):
            dest = self.engine / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(REPO_ROOT / rel, dest, symlinks=True)

    # -- placing engine files --------------------------------------------
    def path(self, rel):
        """The absolute path of an engine file `rel` in this layout."""
        return self.engine / rel

    def put(self, rel, content=None, src=None, executable=False):
        """Write an engine file: `content` (text) or a copy of `src`."""
        if self.sealed:
            self.unseal()
        dest = self.engine / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src is not None:
            shutil.copy(src, dest)
        else:
            dest.write_text(content)
        if executable:
            os.chmod(dest, 0o755)
        return dest

    def remove(self, rel):
        """Delete an engine file (a test of a missing tool)."""
        if self.sealed:
            self.unseal()
        (self.engine / rel).unlink()

    def put_shipped(self, *rels):
        """Copy shipped engine files (paths relative to the phasekit repo)."""
        for rel in rels:
            self.put(rel, src=REPO_ROOT / rel,
                     executable=os.access(REPO_ROOT / rel, os.X_OK))

    def loop(self):
        return self.engine / "scripts" / "run-until-done.sh"

    def loop_cmd(self, *args):
        return ["bash", str(self.loop()), *args]

    # -- running ----------------------------------------------------------
    def seal(self):
        """Pinned: make the engine read-only (idempotent)."""
        if self.pinned and not self.sealed:
            _chmod_tree(self.engine, writable=False)
            self.sealed = True

    def unseal(self):
        if self.pinned and self.sealed:
            _chmod_tree(self.engine, writable=True)
            self.sealed = False

    def env(self, base=None):
        """The environment a loop run needs in this layout (sealing the
        engine first when pinned)."""
        env = dict(os.environ if base is None else base)
        env.pop("PHASEKIT_PROJECT_DIR", None)
        env.pop("PHASEKIT_ENGINE_DIR", None)
        if self.pinned:
            self.seal()
            env["PHASEKIT_PROJECT_DIR"] = str(self.repo)
        return env

    def claude_path(self, path):
        """PATH for a run whose `claude` is a test's fake: pinned, the probe
        shim first (see CLAUDE_PROBE_SHIM)."""
        if not self.pinned:
            return path
        shim_dir = self.engine.parent / (self.engine.name + "-claude-shim")
        shim = shim_dir / "claude"
        if not shim.exists():
            shim_dir.mkdir(parents=True, exist_ok=True)
            shim.write_text(CLAUDE_PROBE_SHIM)
            os.chmod(shim, 0o755)
        return str(shim_dir) + os.pathsep + path

    def bash_prelude(self):
        """Lines a function-extraction test puts before the extracted code."""
        return [f'ENGINE_DIR="{self.engine}"', f'ROOT_DIR="{self.repo}"']

    def cleanup(self):
        self.unseal()
        if self.pinned:
            shutil.rmtree(self.engine, ignore_errors=True)


def engine_dir_for(root):
    """For a function-extraction test: the ENGINE_DIR its prelude sets next
    to ROOT_DIR — the project itself when vendored, a separate (empty unless
    the test puts files there) directory when pinned."""
    root = Path(root)
    if not PINNED:
        return root
    engine = root.parent / (root.name + "-engine")
    engine.mkdir(parents=True, exist_ok=True)
    return engine


def hook_argv(hook_path, project=None):
    """How a hook is run: by its own path when vendored (the project's
    settings.json wires it), through the engine plugin's dispatcher when
    pinned (what --plugin-dir / an installed plugin runs). `project` is the
    session's project (Claude Code's CLAUDE_PROJECT_DIR); by default a
    neutral directory — never wherever the suite itself was started (the
    phasekit checkout wires its own hooks, and the dispatcher would defer)."""
    hook_path = Path(hook_path)
    if not PINNED:
        return ["bash", str(hook_path)]
    import tempfile
    return ["env", f"CLAUDE_PROJECT_DIR={project or tempfile.gettempdir()}", "bash",
            str(REPO_ROOT / "plugin" / "hooks" / "run-hook.sh"), hook_path.stem]


def mark_pinned(project, pinned=None):
    """Pinned: make `project` a pinned project (its pin file), so the plugin's
    hooks treat an interactive session there as phasekit's business."""
    if PINNED if pinned is None else pinned:
        (Path(project) / ".phasekit-version").write_text("v0.19.0\n")
