#!/usr/bin/env python3
"""The loop owns every commit (v0.18.2, queue row 1233; Aaron, 2026-09-30).

phasekit always meant the wrapper to commit ("committed externally", "the
wrapper owns commits") but only two special prompts said so outright and
nothing enforced it: the command guard read CLAUDE_TOOL_INPUT, which the
harness never sets (probed in scaffold-runner, claude 2.1.285: the payload
arrives on stdin), so it blocked nothing, anywhere. Models committed anyway,
and when a session committed the completion record itself the landing walk
read "recorded" as done over work that commit did not carry; the catch-up
squash then gated the worktree but squashed HEAD.

Six parts, each pinned here, each red on v0.18.1:
  1. the guard refuses a model's git writes under the loop (and reads stdin);
  2. every session prompt states the rule, and none tells a model to commit;
  3. at a completion the loop checks the WHOLE tree, whoever committed;
     the catch-up squash gates exactly what it squashes; plain mode gates a
     commit the loop did not make;
  4. commit subjects come from the PLANNED phase title, prose in the body;
  5. artifacts/scratch/ — ignored, never committed, cleared per iteration;
  6. planned paths — `Planned paths:` in docs/PHASES.md, `plan_paths` in the
     record, the evidence and boundary-state.json; warn only.

Run from the repo root: python3 -m unittest tests.test_loop_owns_commits
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "deny-dangerous-commands.sh"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
MANIFEST = REPO_ROOT / "contracts" / "interface.json"

_spec = importlib.util.spec_from_file_location(
    "pk_boundary_harness_loc", Path(__file__).resolve().parent / "test_boundary_state.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

RULE = "The loop owns every commit"
SCRATCH = "artifacts/scratch/"


# ---------------------------------------------------------------------------
# 1. the command guard
# ---------------------------------------------------------------------------

class _GuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pk-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.other = self.tmp / "other"
        for d in (self.proj, self.other):
            (d / "sub").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(d)], check=True)
        (self.proj / "artifacts").mkdir()
        self.marker = self.tmp / "iter-marker"
        self.marker.write_text("")

    def guard(self, command, loop=True, cwd=None, env=None, raw=None, path=None):
        payload = raw if raw is not None else json.dumps(
            {"tool_input": {"command": command}, "cwd": str(cwd or self.proj),
             "hook_event_name": "PreToolUse", "tool_name": "Bash"})
        e = {k: v for k, v in os.environ.items()
             if k not in ("PHASEKIT_ARTIFACTS_DIR", "PHASEKIT_ITER_MARKER", "CLAUDE_TOOL_INPUT")}
        if loop:
            e["PHASEKIT_ARTIFACTS_DIR"] = str(self.proj / "artifacts")
            e["PHASEKIT_ITER_MARKER"] = str(self.marker)
        if path is not None:
            e["PATH"] = path
        e.update(env or {})
        r = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True,
                           cwd=str(self.proj), env=e, timeout=30)
        return r.returncode, r.stderr


class GuardRefusesGitWritesUnderTheLoop(_GuardBase):
    BLOCKED = [
        "git commit -m x",
        "git commit --allow-empty -qm probe",
        "git commit-tree HEAD^{tree} -m x",
        "git add -A",
        "git add artifacts/project-complete.json",
        "git rm --cached f",
        "git rm -q BAD",
        "git mv a b",
        "git reset HEAD f",
        "git reset --soft HEAD~1",
        "git restore --staged f",
        "git restore f",
        "git checkout -- f",
        "git checkout master",
        "git checkout -b new",
        "git switch master",
        "git stash",
        "git stash -u",
        "git stash pop",
        "git merge master",
        "git rebase master",
        "git cherry-pick abc",
        "git revert HEAD",
        "git update-ref refs/heads/x HEAD",
        "git update-index --assume-unchanged f",
        "git branch newbranch",
        "git branch -f master HEAD",
        "git branch -D old",
        "git worktree add ../w",
        "git pull",
        "git fetch origin",
        "git read-tree HEAD",
        "git symbolic-ref HEAD refs/heads/x",
        "git apply --index p.diff",
        # the forms a model reaches them by
        "git -C . add -A",
        "git -C sub commit -m x",
        "git --git-dir=.git --work-tree=. commit -m x",
        "git -c user.name=x commit -m y",
        "GIT_DIR=.git git commit -m x",
        "env GIT_AUTHOR_NAME=x git commit -m y",
        "FOO=1 BAR=2 git add .",
        "cd sub && git add .",
        "cd sub; git add .",
        "git status && git commit -am x",
        "git status || git commit -am x",
        "git diff | head; git add -A",
        "bash -c 'git commit -m x'",
        "sh -c \"git add -A && git commit -m x\"",
        "bash -lc 'git stash'",
        "bash -o pipefail -c 'git add x'",
        "eval git commit -m x",
        "echo $(git stash)",
        "echo \"$(git reset HEAD f)\"",
        "echo `git add f`",
        "ls | xargs git add",
        "find . -name '*.py' -exec git add {} \\;",
        "nohup git commit -m x &",
        "timeout 30 git commit -m x",
        "command git commit -m x",
        "if true; then git add x; fi",
        "for f in a b; do git rm $f; done",
        "(cd /tmp && git status); git mv a b",
        "git log --grep=#12; git add x",
        "cat <<'EOF' | bash\ngit commit -m x\nEOF",
        "bash -s <<EOF\ngit add -A\nEOF",
        "git -c alias.ci=commit ci -m x",
        "/usr/bin/git commit -m x",
        # review round 1 (m1): each passed the round-1 parse
        "pushd /tmp; popd; git commit -m x",
        "cd /tmp | true; git commit -m x",
        "cd /tmp & git commit -m x",
        "tr a b <<< x\ngit commit -m y",
        "git init -q .. && git commit -m x",
        "cd /tmp && git init -q && cd - && git commit -m x",
        "git config alias.ci commit; git ci -m x",
        # review round 2 (m4)
        "sh <<< \"git commit -m x\"",
        "echo \"x <<EOF\"\ngit commit -m y",
        "echo $((1<<3))\ngit add .",
        "env -S 'git commit -m x'",
        "git commit -m --dry-run",
        # review round 3 (MINOR 1)
        "echo \"git commit -am x\" | bash",
        "printf 'git add -A\\n' | sh",
        "cat <(git commit -am x)",
        ": >(git add -A)",
        ". /dev/stdin <<< 'git commit -am x'",
        "env -S'git commit -am x'",
        "env --split-string='git commit -am x'",
        # review round 4 (MINOR 5)
        "echo git commit -m x | bash /dev/stdin",
        "echo $'git add -A' | bash",
        "echo 'git add -A' | tee /dev/null | bash",
        "echo git commit -m x | xargs -0 bash -c",
        "printf 'git %s\\n' commit | sh",
        # review round 5 (MINOR 5): bash expands $(…) in an unquoted heredoc body
        "cat <<X > f\n$(git commit -am x)\nX",
        "cat <<X > f\n`git add -A`\nX",
        # review round 6 (MINOR 1): quotes are literal in a heredoc body
        "cat > n.txt <<EOF\nit's $(git commit -qam x)\nEOF",
        "cat > n.txt <<EOF\n'$(git add -A)'\nEOF",
    ]

    def test_every_write_form_is_refused_with_one_plain_line(self):
        for c in self.BLOCKED:
            with self.subTest(command=c):
                rc, err = self.guard(c)
                self.assertEqual(rc, 2, f"not refused: {c!r}\n{err}")
                lines = [ln for ln in err.splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1, err)
                self.assertIn("the loop owns every commit", lines[0])
                self.assertIn("write your verdict", lines[0])

    def test_env_chdir_into_the_project(self):
        # review round 3 (MINOR 1)
        rc, _ = self.guard(f"cd /tmp && env -C {self.proj} git commit -am x", cwd=self.other)
        self.assertEqual(rc, 2)

    def test_the_legacy_list_through_a_pipe_or_a_process_substitution(self):
        for c in ("cat <(git push)", "echo \"git push origin main\" | bash"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)

    def test_nesting_deeper_than_the_parse_follows_is_never_a_silent_pass(self):
        import shlex
        c = "git commit -m x"
        for _ in range(8):
            c = "bash -c " + shlex.quote(c)
        self.assertEqual(self.guard(c)[0], 2)

    def test_a_configured_alias_is_what_it_expands_to(self):
        subprocess.run(["git", "-C", str(self.proj), "config", "alias.ci", "commit"], check=True)
        subprocess.run(["git", "-C", str(self.proj), "config", "alias.sv", "!git add -A"], check=True)
        for c in ("git ci -m x", "git sv"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)


class GuardAllowsReadOnlyGit(_GuardBase):
    ALLOWED = [
        "git status", "git status --porcelain", "git diff", "git diff --cached HEAD",
        "git log --oneline -5", "git log --grep='#12' --format=%s", "git show HEAD:src.txt",
        "git show HEAD:src.txt > src.txt", "git grep -n foo", "git blame f", "git ls-files",
        "git rev-parse HEAD", "git rev-list --count HEAD", "git cat-file -p HEAD",
        "git merge-base A B", "git branch", "git branch -a", "git branch --list 'iter/*'",
        "git branch --show-current", "git branch --contains HEAD", "git stash list",
        "git stash show", "git worktree list", "git tag -l", "git tag --list 'v*'",
        "git describe --tags", "git remote -v", "git config --get user.name",
        "git checkout", "git commit --help", "git add -h", "git status 2>&1 | head",
        "git branch 2>/dev/null", "git for-each-ref refs/heads", "git reflog",
        "grep -rn 'git commit' docs", "echo 'use git add to stage'  # git commit",
        "bash scripts/phasekit.sh verify", "python3 -m unittest",
        "cat > notes.sh <<'EOF'\ngit commit -m x\nEOF",
        "ssh host bash <<EOF\ngit commit -m x\nEOF",
        "bash x.sh",
        # review round 1 (m2): each was refused in round 1
        "git tag --sort=-v:refname | head", "git tag -n5", "git tag --format='%(refname)'",
        "echo '- never run `git add` here' >> docs/LEARNINGS.md",
        "git commit --dry-run",
        "cat >> docs/NOTES.md <<'END-NOTES'\nit's fine to mention git add and sudo here\nEND-NOTES",
        "cat <<\\EOF > n.md\ndon't git add\nEOF",
        # review round 2 (m4)
        "git branch --format \"%(refname)\"", "git tag --sort -v:refname", "git branch --contains HEAD",
        "grep -c x <<< \"git commit\"",
        # review round 4 (MINOR 5)
        "git log | sh", "echo foo | bash -c 'cat'", "echo \"git status\" | bash",
        "diff <(git show HEAD:a) b", "echo 'git commit' | cat",
        # a QUOTED delimiter expands nothing
        "cat <<'X' > f\n$(git commit -am x)\nX",
    ]

    def test_read_only_git_and_plain_commands_pass(self):
        (self.proj / "x.sh").write_text("git commit -m 'a script is the whole-tree check's business'\n")
        for c in self.ALLOWED:
            with self.subTest(command=c):
                rc, err = self.guard(c)
                self.assertEqual(rc, 0, f"refused: {c!r}\n{err}")

    def test_a_write_to_another_repository_is_not_this_rules_business(self):
        for c in (f"git -C {self.other} commit -qm x",
                  f"cd {self.other} && git add -A && git commit -qm x",
                  "d=/tmp/pk-guard-scratch-$$ && mkdir -p $d && cd $d && git init -q && git commit -qm x",
                  f"git --git-dir={self.other}/.git commit -m x"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 0, c)
        # ... but a repository `git init` makes INSIDE the project is judged by its own .git
        rc, _ = self.guard(f"git init -q sub/nested && git -C sub/nested commit -m x")
        self.assertEqual(rc, 0)

    def test_the_payload_cwd_is_where_the_chain_starts(self):
        self.assertEqual(self.guard("git commit -m x", cwd=self.other)[0], 0)
        self.assertEqual(self.guard(f"cd {self.proj} && git commit -m x", cwd=self.other)[0], 2)


class GuardOutsideTheLoop(_GuardBase):
    def test_an_interactive_session_may_commit(self):
        for c in ("git commit -m x", "git add -A", "git stash", "git checkout -b x"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_the_dangerous_list_is_refused_in_every_session(self):
        # RED on v0.18.1: the payload arrives on stdin, which that hook never read.
        for loop in (False, True):
            for c in ("git push", "git push origin main", "git tag v1", "git reset --hard",
                      "git clean -fd", "git clean -f -d -x", "sudo ls", "shred f",
                      "bash -c 'git push'"):
                with self.subTest(command=c, loop=loop):
                    rc, err = self.guard(c, loop=loop)
                    self.assertEqual(rc, 2, f"{c!r}: {err}")
                    self.assertIn("Blocked dangerous command pattern", err)

    def test_a_prose_mention_is_not_a_command(self):
        for c in ("echo 'never run sudo here'", "grep -n 'git push' README.md", "git tag -l"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_the_legacy_environment_variable_is_still_read(self):
        rc, err = self.guard("", raw="", env={"CLAUDE_TOOL_INPUT": "git push origin main"})
        self.assertEqual(rc, 2, err)

    def test_an_empty_payload_allows(self):
        self.assertEqual(self.guard("", raw="")[0], 0)


class GuardFailsOpenNarrowly(_GuardBase):
    def _no_python_path(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in ("bash", "cat", "jq", "tr", "grep", "head", "tail", "git", "printf", "env"):
            src = shutil.which(tool)
            if src:
                os.symlink(src, bindir / tool)
        return str(bindir)

    def test_without_python3_a_regex_stands_in(self):
        path = self._no_python_path()
        self.assertEqual(self.guard("git commit -m x", path=path)[0], 2)
        self.assertEqual(self.guard("git push", loop=False, path=path)[0], 2)
        self.assertEqual(self.guard("git status", path=path)[0], 0)
        self.assertEqual(self.guard("git commit -m x", loop=False, path=path)[0], 0)

    def test_an_unparseable_command_falls_back_not_open(self):
        rc, _ = self.guard("echo 'unbalanced ; git commit -m x")
        self.assertEqual(rc, 2)


# ---------------------------------------------------------------------------
# 2. every session prompt states the rule; none tells a model to commit
# ---------------------------------------------------------------------------

def _loop_block(start, end):
    return H._extract_block(start, end)


def _fn(name):
    """A loop function, whole (the harness's extractor stops before the brace)."""
    return H._extract_block(rf"^{re.escape(name)}\(\) \{{", r"^\}") + "\n}\n"


class EveryPromptStatesTheRule(unittest.TestCase):
    FILES = ["CONTINUE_PROMPT.txt", "templates/CLAUDE.template.md", "templates/AGENTS.template.md",
             ".claude/CLAUDE.md", "AGENTS.md", ".claude/agents/project-lead.md"]

    def test_the_static_prompts(self):
        for rel in self.FILES:
            with self.subTest(file=rel):
                text = (REPO_ROOT / rel).read_text()
                self.assertIn(RULE, text)
                self.assertIn(SCRATCH, text)

    def test_the_prompts_the_loop_composes(self):
        src = LOOP.read_text()
        for start, end in ((r"^compose_light_prompt\(\) \{", r"^\}"),
                           (r"^run_light_final_review\(\) \{", r"^\}")):
            with self.subTest(block=start):
                block = _loop_block(start, end)
                self.assertIn(RULE, block)
                self.assertIn(SCRATCH, block)
        verdict = src[src.index("VERDICT_RETRY_EOF'"):src.index("\nVERDICT_RETRY_EOF\n")]
        self.assertIn(RULE, verdict)

    def test_the_wrapup_nudge(self):
        self.assertIn("The loop owns every commit", (REPO_ROOT / ".claude/hooks/wrapup-nudge.sh").read_text())

    def test_no_prompt_or_recovery_path_tells_the_model_to_run_a_git_write(self):
        corpus = {rel: (REPO_ROOT / rel).read_text() for rel in self.FILES}
        corpus["scripts/run-until-done.sh (strings)"] = "\n".join(
            ln for ln in LOOP.read_text().splitlines() if not ln.lstrip().startswith("#"))
        bad = re.compile(r"git restore --staged|finish or revert the rest|then re-commit|"
                         r"(?<!never )\brun `?git (commit|add|stash|checkout|reset)\b|"
                         r"(?<!never )\b(run|use) git (commit|add)\b", re.I)
        for rel, text in corpus.items():
            with self.subTest(file=rel):
                self.assertIsNone(bad.search(text), rel)


# ---------------------------------------------------------------------------
# 3. the whole tree at a completion, whoever committed
# ---------------------------------------------------------------------------

RECORD = """jq -n '{done: true, summary: "complete", suggested_commit_message: "Project complete: v0 shipped"}' > artifacts/project-complete.json"""


class _Loop(unittest.TestCase):
    def _repo(self, squash, scenario):
        repo = H.Repo(squash=squash)
        self.addCleanup(repo.cleanup)
        repo.scenario(scenario)
        return repo

    def out(self, r):
        return r.stdout + r.stderr


class ARecordOnlyCommitOverOlderWork(_Loop):
    """Row 1233 shape 1a. RED on v0.18.1: the older work rests dirty."""

    SCENARIO = "echo 'the older work' >> src.txt\ntouch new-file.txt\n" + RECORD + """
git add artifacts/project-complete.json
git commit -qm "only the record (a bypass: the guard would refuse this)"
"""

    def test_the_rest_lands_through_the_loops_gated_commit(self):
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, self.SCENARIO)
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                self.assertIn("the tree is not — the rest of the iteration's work lands", out)
                self.assertEqual(repo.porcelain_all(), [], out)
                self.assertIn("the older work", repo.git("show", "main:src.txt"), out)
                self.assertTrue(repo.tracked("new-file.txt", "main"), out)
                self.assertGreaterEqual(repo.verify_calls(), 1, "the rest was never gated\n" + out)
                body = repo.git("log", "-1", "--first-parent", "--format=%B", "--grep=did not carry",
                                "HEAD" if not squash else "iter/1-test")
                self.assertIn("Phasekit-Kind: completion", body, out)
                self.assertEqual((repo.record() or {}).get("step"), H.RESTED, out)
                self.assertNotIn("unlanded", repo.record() or {})

    def test_the_next_session_lands_it_with_no_model_commit(self):
        # A previous session's model left a record-only commit and the work
        # uncommitted, then died. Squash mode: the loop-start walk lands the
        # rest with ZERO model turns. Plain mode: the turn re-writes the
        # record — it never commits — and the loop lands the rest.
        for squash in (True, False):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, "git show HEAD:artifacts/project-complete.json > artifacts/project-complete.json\n")
                subprocess.run(["bash", "-c", self.SCENARIO], cwd=repo.repo, check=True)
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                self.assertEqual(repo.calls(), 0 if squash else 1, out)
                self.assertIn("the older work", repo.git("show", "main:src.txt"), out)
                self.assertEqual(repo.porcelain_all(), [], out)


class TheRestIsRedAndNamed(_Loop):
    """The rest cannot be verified: it is never landed, never discarded, named
    in boundary-state.json `unlanded` and on stderr; the walk stops at step 3."""

    def test_named_and_never_a_red_tree_on_the_target(self):
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, "echo 'older work' >> src.txt\ntouch BAD\n" + RECORD + """
git add artifacts/project-complete.json
git commit -qm "only the record"
""")
                r = repo.run(env={"MAX_ITERATIONS": "1"})
                out = self.out(r)
                rec = repo.record() or {}
                unl = rec.get("unlanded") or ((rec.get("previous") or {}).get("unlanded")) or {}
                self.assertIn("BAD", unl.get("paths", []), out)
                self.assertIn("src.txt", unl.get("paths", []), out)
                self.assertIn("phase-verify-failed.json", unl.get("reason", ""), out)
                self.assertIn("NOT landed", r.stderr)
                self.assertFalse(repo.tracked("BAD", "main"), out)
                self.assertTrue((repo.repo / "BAD").exists(), "the work was discarded")
                self.assertIn("older work", (repo.repo / "src.txt").read_text())


class ALightReviewThatLeavesTheRecordAsIs(_Loop):
    """Row 1233 shape 1b. RED on v0.18.1: the review's fix is never committed."""

    def test_the_reviews_fix_lands(self):
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, r"""
if [ "$PHASEKIT_ITER" = light-review ]; then
  echo "the review's fix" >> src.txt
  exit 0
fi
echo "the task" >> src.txt
""" + RECORD + """
git add -A; git commit -qm "the build turn committed everything (a bypass)"
""")
                r = repo.run(env={"PHASEKIT_ITERATION_MODE": "light"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                if not squash:
                    # plain mode keeps the review (v0.18.1): its fix must land
                    self.assertIn("the review's fix", repo.git("show", "main:src.txt"), out)
                self.assertEqual(repo.porcelain_all(), [], out)
                self.assertIn("the task", repo.git("show", "main:src.txt"), out)


class PlainModeGatesACommitTheLoopDidNotMake(_Loop):
    """Row 1233 shape 1c. RED on v0.18.1: nothing gates the model's commit."""

    def test_a_red_self_commit_is_repaired_before_the_rest(self):
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt; touch BAD
""" + RECORD + """
  git add -A; git commit -qm "completion over a red tree (a bypass)"
  exit 0
fi
rm -f BAD
git show HEAD:artifacts/project-complete.json > artifacts/project-complete.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.calls(), 2, "the red commit was never gated\n" + out)
        self.assertIn("was not made by the loop's verify-gated commit", out)
        self.assertFalse(repo.tracked("BAD", "main"), out)
        self.assertEqual(repo.porcelain_all(), [], out)

    def test_a_green_self_commit_is_gated_once_and_rests(self):
        repo = self._repo(False, "echo work >> src.txt\n" + RECORD + """
git add -A; git commit -qm "completion (a bypass)"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.verify_calls(), 1, out)
        self.assertEqual((repo.record() or {}).get("step"), H.RESTED, out)

    def test_a_loop_made_commit_is_not_gated_again(self):
        repo = self._repo(False, "echo work >> src.txt\n" + RECORD + "\n")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        self.assertEqual(r.returncode, 0, self.out(r))
        self.assertEqual(repo.verify_calls(), 1, self.out(r))


class TheCatchUpSquashGatesWhatItSquashes(_Loop):
    """Row 1233 shape 2. RED on v0.18.1: the repaired worktree passed the gate
    and HEAD (red) was squashed onto the target."""

    def test_the_target_never_carries_the_red_tree(self):
        repo = self._repo(True, r"""
if [ "$CALL_N" = 1 ]; then
  echo "work" >> src.txt
""" + RECORD + """
  touch BAD
  git add -A; git commit -qm "completion, self-committed over a red tree (a bypass)"
  exit 0
fi
rm -f BAD; echo "the repair" >> src.txt
git show HEAD:artifacts/project-complete.json > artifacts/project-complete.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertFalse(repo.tracked("BAD", "main"), "a red tree reached the target\n" + out)
        self.assertIn("the repair", repo.git("show", "main:src.txt"), out)
        self.assertEqual(repo.porcelain_all(), [], out)
        self.assertEqual(repo.head_branch(), "main", out)

    def test_a_squash_never_judges_a_worktree_that_differs_from_head(self):
        fn = H._extract_block(r"^squash_to_target\(\) \{", r"^\}")
        i_dirty = fn.index("_boundary_dirty_paths")
        i_gate = fn.index("run_verify_gate")
        self.assertLess(i_dirty, i_gate)
        self.assertIn("squash deferred — the worktree differs from HEAD", fn)


# ---------------------------------------------------------------------------
# 4. subjects from the plan
# ---------------------------------------------------------------------------

APPROVE = r"""
echo "work" >> src.txt
jq -n --arg m "$MSG" --arg p "$PHASE" '{phase: $p, approved: true, summary: "the real summary",
   final_phase: false, suggested_commit_message: $m}' > artifacts/phase-approval.json
"""


class SubjectsComeFromThePlan(_Loop):
    def _run(self, phases, phase, msg, iteration=None, squash=False):
        repo = self._repo(squash, APPROVE)
        repo.write("docs/PHASES.md", phases)
        if iteration is not None:
            repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": iteration}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "plan", "--allow-empty")
        if squash:
            repo.git("branch", "-f", "main", "HEAD")
        r = repo.run(env={"MAX_ITERATIONS": "1", "MSG": msg, "PHASE": phase})
        return repo, r

    def _phase_commit(self, repo, ref="HEAD"):
        return repo.git("log", "-1", "--format=%B", "--grep=Phasekit-Kind: phase", ref)

    def test_the_subject_is_the_planned_title_and_the_prose_is_the_body(self):
        repo, r = self._run("# Phases\n\n## Phase 1 — Build the widget\n\nText.\n\n### Phase 1 progress record\n",
                            "phase-1", "Phase 1 (APPROVED): built it, with care")
        msg = self._phase_commit(repo)
        self.assertEqual(msg.splitlines()[0], "phase 1: Build the widget", self.out(r))
        self.assertIn("built it, with care", msg)

    def test_the_iteration_88_shape(self):
        # 494fb6f: iteration 88's work under iteration 87's words.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo, r = self._run("## Phase 187 — Old\n\n## Phase 188 — The phase the plan names\n",
                                    "phase-188", "iteration 87 phase 187: iteration 87's words",
                                    iteration=88, squash=squash)
                refs = ["main", "iter/1-test"] if squash else ["main"]
                for ref in refs:
                    msg = repo.git("log", "-1", "--format=%B", "--grep=Phasekit-Phase: 188", ref)
                    self.assertEqual(msg.splitlines()[0],
                                     "iteration 88 phase 188: The phase the plan names", self.out(r))
                    self.assertNotIn("iteration 87", msg)
                    self.assertIn("the real summary", msg)

    def test_no_planned_title_falls_back_to_the_prose_and_says_so(self):
        repo, r = self._run("# Phases\n", "phase-1", "Phase 1 (APPROVED): built it")
        self.assertEqual(self._phase_commit(repo).splitlines()[0], "phase 1: built it", self.out(r))
        self.assertIn("plans no title for phase 1", r.stderr)


def _plan_fn(phases_text, mode, pid, since=None, cwd=None):
    """Run the loop's own _phase_plan_py in a scratch dir holding docs/PHASES.md."""
    fn = _fn("_phase_plan_py")
    d = Path(cwd or tempfile.mkdtemp(prefix="pk-plan-"))
    (d / "docs").mkdir(exist_ok=True)
    (d / "docs" / "PHASES.md").write_text(phases_text)
    args = [mode, pid] + ([since] if since else [])
    r = subprocess.run(["bash", "-c", fn + '\n_phase_plan_py "$@"', "x"] + args,
                       cwd=str(d), capture_output=True, text=True, timeout=30)
    if cwd is None:
        shutil.rmtree(d, ignore_errors=True)
    return r.stdout.strip()


class PlannedTitleParsing(unittest.TestCase):
    def test_heading_shapes_across_the_fleet(self):
        cases = [
            ("## Phase 193 — The creating page publishes a play link\n### Phase 193 progress record (session 1) — NOT reproduced\n## Phase 193 continuation — gate\n", "193",
             "The creating page publishes a play link"),
            ("## Phase 0 - project bootstrap\n", "0", "project bootstrap"),
            ("## Phase 6.1 — Detector tuning\n## Phase 6 — Six\n", "6.1", "Detector tuning"),
            ("## Phase 6.1 — Detector tuning\n## Phase 6 — Six\n", "6", "Six"),
            ("## Phase 28 — Drill log round 10c entry — DONE\n", "28", "Drill log round 10c entry"),
            ("## Phase 19 — x\n## Phase 1 — the one\n", "1", "the one"),
            ("## Phase 1 — first plan\n# Iteration 2\n## Phase 1 — restarted plan\n", "1", "restarted plan"),
            ("## M9.4 — Manifest self-check\n", "M9.4", "Manifest self-check"),
            ("```\n## Phase 3 — in a fence\n```\n", "3", ""),
            ("## Phase 7 — `--model` flag: CLI → backend selection\n", "7", "`--model` flag: CLI → backend selection"),
            # review round 1 (M3): live foundry-dashboard shape, a bare-number appendix, Meta Phase
            ("## Phase 109 — The smoke-tick capability\ntext\n### Phase 109 — progress record (2026-09-09; FINAL)\n", "109", "The smoke-tick capability"),
            ("## Phase 1 — Real\n## Appendix\n### 1 - old note about something\n", "1", "Real"),
            ("## Meta Phase M2.5 — Claude startup file generation\n", "M2.5", "Claude startup file generation"),
            # review round 3 (MINOR 2): a title that starts with Status/Progress is a title
            ("## Phase 4 — Status page for operators\n", "4", "Status page for operators"),
            ("## Phase 5 — Progress bar\n### Phase 5 — progress record (x)\n", "5", "Progress bar"),
        ]
        for text, pid, want in cases:
            with self.subTest(pid=pid, text=text[:40]):
                self.assertEqual(_plan_fn(text, "title", pid), want)

    def test_a_long_title_is_bounded(self):
        t = "## Phase 130 — " + "Find a task by its number " * 10 + "(Aaron's items 1 and 3; new SPEC AC #101; standard mode)\n"
        got = _plan_fn(t, "title", "130")
        self.assertLessEqual(len(got), 101)
        self.assertTrue(got.startswith("Find a task by its number"))
        self.assertNotIn("standard mode", got)


# ---------------------------------------------------------------------------
# 5. scratch
# ---------------------------------------------------------------------------

class Scratch(_Loop):
    def test_scratch_is_never_committed(self):
        # RED on v0.18.1: `git add -A` swept it into the completion commit.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, "mkdir -p artifacts/scratch\necho probe > artifacts/scratch/notes.txt\necho work >> src.txt\n" + RECORD + "\n")
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                for ref in ("main", "HEAD"):
                    self.assertFalse(repo.tracked("artifacts/scratch/notes.txt", ref), out)
                self.assertEqual(repo.porcelain_all(), [], out)

    def test_scratch_stays_out_even_when_a_project_gitignore_re_includes_it(self):
        repo = self._repo(False, "mkdir -p artifacts/scratch\necho probe > artifacts/scratch/n.txt\necho work >> src.txt\n" + RECORD + "\n")
        repo.write(".gitignore", "!artifacts/scratch/\n!artifacts/scratch/**\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "gitignore")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        self.assertFalse(repo.tracked("artifacts/scratch/n.txt", "HEAD"), self.out(r))

    def _clear(self, root, label):
        fn = _fn("clear_scratch_at_iteration_start")
        sij = _fn("supervising_iteration_json")
        if label is None:
            (root / "artifacts" / "iteration-mode.json").unlink(missing_ok=True)
        else:
            (root / "artifacts" / "iteration-mode.json").write_text(json.dumps({"iteration": label}))
        r = subprocess.run(["bash", "-c", f'ARTIFACTS_DIR="{root}/artifacts"\n{sij}\n{fn}\nclear_scratch_at_iteration_start'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cleared_when_an_iteration_starts_and_kept_within_one(self):
        root = Path(tempfile.mkdtemp(prefix="pk-scratch-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "artifacts").mkdir()
        f = root / "artifacts" / "scratch" / "n.txt"
        self._clear(root, 7)
        f.write_text("x")
        self._clear(root, 7)
        self.assertTrue(f.exists(), "a second session of the same iteration keeps its scratch")
        self._clear(root, 8)
        self.assertFalse(f.exists(), "a new iteration starts with an empty scratch space")
        f.write_text("x")
        self._clear(root, None)
        self.assertFalse(f.exists(), "no supervisor: every session start is a new iteration")
        self.assertTrue((root / "artifacts" / "scratch").is_dir())

    def test_the_loop_excludes_it_and_every_commit_path_unstages_it(self):
        src = LOOP.read_text()
        self.assertIn('"artifacts/scratch/"', H._extract_block(r"^ensure_transients_excluded\(\) \{", r"^\}"))
        for fn in ("stage_landing_tree", "wrapup_commit", "deadline_lastresort_commit"):
            with self.subTest(fn=fn):
                self.assertIn('git reset -q -- "$ARTIFACTS_DIR/scratch"',
                              H._extract_block(rf"^{fn}\(\) \{{", r"^\}"))
        self.assertIn("clear_scratch_at_iteration_start || true", src)


# ---------------------------------------------------------------------------
# 6. planned paths
# ---------------------------------------------------------------------------

class PlannedPaths(_Loop):
    def _run(self, phases, extra_writes, iteration=7):
        repo = self._repo(False, "echo work >> src.txt\n" + extra_writes + r"""
jq -n '{phase: "phase-1", approved: true, summary: "built it", final_phase: false,
        suggested_commit_message: "built it"}' > artifacts/phase-approval.json
""")
        repo.write("docs/PHASES.md", phases)
        if iteration is not None:
            repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": iteration}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "plan")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        rec = json.loads(repo.git("show", "HEAD:artifacts/phase-approval.json"))
        return repo, r, rec

    def test_unplanned_paths_are_recorded_and_the_landing_is_never_refused(self):
        repo, r, rec = self._run("## Phase 1 — Build\nPlanned paths: src.txt, `lib/**`\n\n## Phase 2 — Next\nPlanned paths: extra.txt\n",
                                 "echo x > extra.txt\nmkdir -p lib/a; echo y > lib/a/b.txt\n")
        pp = rec.get("plan_paths") or {}
        self.assertEqual(pp.get("status"), "outside", self.out(r))
        self.assertEqual(pp.get("unplanned"), ["extra.txt"])
        self.assertEqual(pp.get("unplanned_count"), 1)
        self.assertTrue(pp.get("declared"))
        self.assertTrue(repo.tracked("extra.txt", "HEAD"), "warn only: the path landed")
        self.assertIn("OUTSIDE its planned paths", r.stderr)
        ev = json.loads(repo.git("show", "HEAD:artifacts/iterations/7/1.json"))
        self.assertEqual(ev.get("plan_paths"), pp)
        self.assertEqual((repo.record() or {}).get("plan_paths") or ((repo.record() or {}).get("previous") or {}).get("plan_paths"), pp)

    def test_a_progress_record_heading_never_hides_the_plan(self):
        # review round 1 (M3): the progress record's heading once won the section.
        repo, r, rec = self._run("## Phase 1 — Build\nPlanned paths: src.txt\n\n### Phase 1 — progress record (2026-09-30)\nnotes\n", "")
        self.assertEqual(rec["plan_paths"]["status"], "inside", self.out(r))

    def test_inside_the_plan(self):
        repo, r, rec = self._run("## Phase 1 — Build\n- Planned paths: src.txt\n", "")
        pp = rec["plan_paths"]
        self.assertEqual((pp["status"], pp["unplanned_count"], pp["unplanned"]), ("inside", 0, []), self.out(r))

    def test_no_declaration_reports_no_plan_declared_never_zero(self):
        repo, r, rec = self._run("## Phase 1 — Build\n", "echo x > extra.txt\n")
        pp = rec["plan_paths"]
        self.assertEqual(pp["status"], "no-plan-declared", self.out(r))
        self.assertIsNone(pp["unplanned_count"])
        self.assertFalse(pp["declared"])
        self.assertIn("no plan declared", r.stderr)

    def test_standalone_runs_record_it_too(self):
        repo, r, rec = self._run("## Phase 1 — Build\nPlanned paths: none\n", "", iteration=None)
        self.assertEqual(rec["plan_paths"]["status"], "outside", self.out(r))
        self.assertEqual(rec["plan_paths"]["unplanned"], ["src.txt"])

    def test_glob_semantics(self):
        d = Path(tempfile.mkdtemp(prefix="pk-plan-"))
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "--allow-empty", "-m", "base"],
                       check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        files = ["src/a.py", "src/deep/b.py", "tests/test_a.py", "tests/unit/test_b.py",
                 "docs/SPEC.md", "docs/LEARNINGS.md", "artifacts/x.json", "README.md", "web/app.ts"]
        for f in files:
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text("x\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        base = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        out = _plan_fn("## Phase 4 — P\nPlanned paths: src/ tests/*.py, docs/SPEC.md\nPlanned paths: web\n",
                       "check", "4", base, cwd=d)
        pp = json.loads(out)
        self.assertEqual(pp["unplanned"], ["README.md", "tests/unit/test_b.py"])
        # a character class (review round 3, MINOR 2)
        pp2 = json.loads(_plan_fn("## Phase 4 — P\nPlanned paths: src/ tests/test_[a].py tests/unit/ docs/SPEC.md web README.m[d]\n",
                                  "check", "4", base, cwd=d))
        self.assertEqual(pp2["unplanned"], [], pp2)
        self.assertEqual(pp["changed_count"], 7)
        self.assertEqual(pp["globs"], ["src/", "tests/*.py", "docs/SPEC.md", "web"])


class ReviewRound1(_Loop):
    """Regressions for the v0.18.2 review, round 1 (each red on the round-1 build)."""

    def test_m1_submodule_content_never_spins_the_loop(self):
        # Dirt INSIDE a submodule no commit of this repository can carry.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, "echo work >> src.txt\n" + RECORD + """
git add -A; git commit -qm "completion (a bypass)"
echo scratch > sub/untracked-in-the-submodule.txt
""")
                subsrc = repo.tmp / "subsrc"
                subprocess.run(["git", "init", "-q", "-b", "main", str(subsrc)], check=True)
                subprocess.run(["git", "-C", str(subsrc), "-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "-q", "--allow-empty", "-m", "s"], check=True)
                repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(subsrc), "sub")
                repo.git("commit", "-qm", "a submodule")
                if squash:
                    repo.git("branch", "-f", "main", "HEAD")
                r = repo.run(env={"MAX_ITERATIONS": "3"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                self.assertEqual(repo.calls(), 1, "the loop spun\n" + out)
                self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"), out)

    def test_round2_m1_a_refusal_one_repair_turn_can_fix_is_never_a_blocker(self):
        # Round 1's generic "nothing to repair" stop fired on the LEARNINGS
        # credential scan (rc 1, green gate, no artifact) — v0.18.1 repaired
        # it in one turn. The key is built at runtime (no literal in the tree).
        key = "AKIA" + "Q" * 16
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt
  echo "- a note: """ + key + r"""" >> docs/LEARNINGS.md
""" + RECORD + """
  exit 0
fi
sed -i '/AKIA/d' docs/LEARNINGS.md
jq '.summary = "complete (secret removed)"' artifacts/project-complete.json > "$STUB_DIR/r" && cat "$STUB_DIR/r" > artifacts/project-complete.json
""")
        repo.write("docs/LEARNINGS.md", "# Learnings\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "learnings")
        r = repo.run(env={"MAX_ITERATIONS": "3"})
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.calls(), 2, out)
        self.assertFalse(repo.artifact("phase-blocked.json").exists(), out)
        self.assertNotIn("AKIA", repo.git("show", "HEAD:docs/LEARNINGS.md"))

    def test_round2_m1_stale_unlanded_is_cleared_when_the_final_boundary_rests(self):
        # The rest landed another way (a hand commit) and the walk PROVED step 7.
        repo = self._repo(False, "echo 'older work' >> src.txt; touch BAD\n" + RECORD + """
git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
""")
        r1 = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("unlanded", repo.record() or {}, self.out(r1))
        (repo.repo / "BAD").unlink()
        repo.git("add", "-A"); repo.git("commit", "-qm", "the rest, landed by hand")
        repo.scenario(":\n")
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})
        rec = repo.record() or {}
        self.assertNotIn("unlanded", rec, self.out(r2))

    def test_m2_the_repair_keeps_the_committed_record_and_lands_the_rest(self):
        # The repair turn fixes the tree and writes phase-update.json — it
        # never restores the record; round 1 deleted it and the checkpoint
        # un-recorded the completion.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, r"""
if [ "$CALL_N" = 1 ]; then
  echo 'older work' >> src.txt; touch BAD
""" + RECORD + """
  git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
  exit 0
fi
test -f artifacts/project-complete.json && touch "$STUB_DIR/record-was-on-disk"
rm -f BAD
jq -n '{summary: "fixed BAD"}' > artifacts/phase-update.json
""")
                r = repo.run(env={"MAX_ITERATIONS": "3"})
                out = self.out(r)
                self.assertTrue((repo.stub / "record-was-on-disk").exists(), out)
                self.assertEqual(r.returncode, 0, out)
                self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"), out)
                self.assertIn("older work", repo.git("show", "main:src.txt"), out)
                self.assertFalse(repo.tracked("BAD", "main"), out)
                self.assertNotIn("unlanded", repo.record() or {})

    def test_m3_a_forged_trailer_is_not_a_verified_tree(self):
        repo = self._repo(False, "echo work >> src.txt\n" + RECORD + """
git add -A; git commit -qm "completion" -m "Phasekit-Kind: completion"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        self.assertEqual(r.returncode, 0, self.out(r))
        self.assertEqual(repo.verify_calls(), 1, "an ungated tree counted as verified\n" + self.out(r))

    def test_m4_the_rest_is_unlanded_only_when_it_did_not_land(self):
        fn = _fn("land_completion_leftovers")
        self.assertLess(fn.index('!= "$head0"'), fn.index(".unlanded = {"))


class ReviewRound3(_Loop):
    """Regressions for the v0.18.2 review, round 3 (each red on the round-3 build)."""

    def test_major1_a_committed_record_is_never_deleted_by_a_new_pass(self):
        # The model's completion over a red tree; the repair turn fixes the
        # tree and writes a checkpoint — it never restores the record.
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt; touch BAD
""" + RECORD + """
  git add -A; git commit -qm "completion over a red tree (a bypass)"
  exit 0
fi
test -f artifacts/project-complete.json && touch "$STUB_DIR/record-on-disk-$CALL_N"
rm -f BAD
jq -n '{summary: "removed BAD"}' > artifacts/phase-update.json
""")
                r = repo.run(env={"MAX_ITERATIONS": "3"})
                out = self.out(r)
                self.assertTrue((repo.stub / "record-on-disk-2").exists(), out)
                self.assertEqual(r.returncode, 0, out)
                self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"), "the completion was un-recorded\n" + out)
                self.assertFalse(repo.tracked("BAD", "main"), out)

    def test_major1c_a_carried_committed_record_is_restored_never_deleted(self):
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo 'older work' >> src.txt; touch BAD
""" + RECORD + """
  git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
  exit 0
fi
jq '.summary = "complete (edited, still red)"' artifacts/project-complete.json > "$STUB_DIR/r" && cat "$STUB_DIR/r" > artifacts/project-complete.json
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out = self.out(r)
        self.assertNotIn(" D artifacts/project-complete.json", repo.porcelain_all(), out)
        self.assertTrue(repo.artifact("project-complete.json").exists(), out)
        self.assertEqual(repo.artifact("project-complete.json").read_text(),
                         repo.git("show", "HEAD:artifacts/project-complete.json") + "\n", out)

    def test_major2_a_commit_gate_refusal_reaches_the_repair_turn(self):
        key = "AKIA" + "Q" * 16
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt
  echo "- a note: """ + key + r"""" >> docs/LEARNINGS.md
""" + RECORD + """
  git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
  exit 0
fi
if grep -q "commit gate" artifacts/phase-verify-failed.json 2>/dev/null; then
  touch "$STUB_DIR/told-why"
  cp artifacts/phase-verify-failed.json "$STUB_DIR/pvf.json"
  sed -i '/AKIA/d' docs/LEARNINGS.md
fi
""")
                repo.write("docs/LEARNINGS.md", "# Learnings\n")
                repo.git("add", "-A"); repo.git("commit", "-qm", "learnings")
                if squash:
                    repo.git("branch", "-f", "main", "HEAD")
                r = repo.run(env={"MAX_ITERATIONS": "4"})
                out = self.out(r)
                self.assertTrue((repo.stub / "told-why").exists(), out)
                self.assertEqual(r.returncode, 0, out)
                self.assertLessEqual(repo.calls(), 2, out)
                self.assertIn("work", repo.git("show", "main:src.txt"), out)
                self.assertNotIn("AKIA", repo.git("show", "main:docs/LEARNINGS.md"), out)
                # the capture names the refusal, never the secret itself
                self.assertNotIn(key, (repo.stub / "pvf.json").read_text())
                self.assertIn("docs/LEARNINGS.md", (repo.stub / "pvf.json").read_text())

    def test_major3_a_path_git_cannot_stage_is_refused_and_named_never_skipped(self):
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, r"""
if [ "$CALL_N" = 1 ]; then
  echo "the real work" >> src.txt
  mkdir -p vendor/x && git init -q vendor/x
""" + RECORD + """
  exit 0
fi
if grep -q "git add" artifacts/phase-verify-failed.json 2>/dev/null; then
  touch "$STUB_DIR/told-why"
  rm -rf vendor
  # CONTINUE_PROMPT step 2: the carried record is re-written once fixed
  cp artifacts/project-complete.json "$STUB_DIR/r" && cat "$STUB_DIR/r" > artifacts/project-complete.json
fi
""")
                r = repo.run(env={"MAX_ITERATIONS": "4"})
                out = self.out(r)
                self.assertTrue((repo.stub / "told-why").exists(), out)
                self.assertEqual(r.returncode, 0, out)
                self.assertIn("the real work", repo.git("show", "main:src.txt"), "the work was never landed\n" + out)
                self.assertEqual(repo.porcelain_all(), [], out)

    def test_minor3_unlanded_is_not_carried_once_the_record_is_gone(self):
        repo = self._repo(False, "echo 'older work' >> src.txt; touch BAD\n" + RECORD + """
git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
""")
        repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("unlanded", repo.record() or {})
        # an intake retires the completion: the record's deletion is committed
        repo.git("rm", "-q", "artifacts/project-complete.json")
        (repo.repo / "BAD").unlink()
        repo.git("add", "-A"); repo.git("commit", "-qm", "intake: iteration 2")
        repo.scenario("jq -n '{summary: \"x\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertNotIn("unlanded", repo.record() or {}, self.out(r))


class ReviewRound4(_Loop):
    """Regressions for the v0.18.2 review, round 4 (each red on the round-4 build)."""

    def _completed(self, squash, label=7):
        repo = self._repo(squash, H.APPROVE_SCENARIO)
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": label}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "intake: iteration %d" % label)
        if squash:
            repo.git("branch", "-f", "main", "HEAD")
        r = repo.run(env={"MAX_ITERATIONS": "1", "FINAL_KIND": "flag"})
        self.assertIn("Run finished successfully.", self.out(r))
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"))
        repo.reset_stub()
        repo.scenario("echo task2-half >> src.txt\njq -n '{summary: \"half of task 2\"}' > artifacts/phase-update.json\n")
        return repo

    def _assert_not_recompleted(self, repo, r, ref="HEAD"):
        out = self.out(r)
        self.assertNotIn("Run finished successfully.", out)
        self.assertEqual(repo.calls(), 1, out)
        self.assertIn("task2-half", repo.git("show", ref + ":src.txt"), out)
        subj = repo.git("log", "-1", "--format=%s", ref)
        self.assertNotIn("completion", subj, out)
        self.assertFalse(repo.tracked("artifacts/project-complete.json", ref), "an earlier completion re-recorded new work\n" + out)

    def test_blocker_a_new_iteration_label_without_an_intake_deletion(self):
        repo = self._completed(False)
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 8}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "intake: iteration 8 (the record NOT deleted)")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self._assert_not_recompleted(repo, r)

    def test_blocker_a_wiped_boundary_record(self):
        repo = self._completed(False)
        repo.artifact("boundary-state.json").unlink()
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        self._assert_not_recompleted(repo, r)

    def test_blocker_a_new_work_branch_in_squash_mode(self):
        repo = self._completed(True)
        repo.git("checkout", "-q", "-b", "iter/2-test", "main")
        # no supervisor label: the iteration is known by its work branch
        repo.git("rm", "-q", "artifacts/iteration-mode.json"); repo.git("commit", "-qm", "standalone")
        r = repo.run(env={"MAX_ITERATIONS": "1", "PHASEKIT_WORK_BRANCH": "iter/2-test"})
        self.assertNotIn("task2-half", repo.git("show", "main:src.txt"), self.out(r))
        self._assert_not_recompleted(repo, r, ref="iter/2-test")

    def test_major_a_stale_index_lock_is_released_and_the_rest_lands(self):
        for squash in (False, True):
            with self.subTest(mode="squash" if squash else "plain"):
                repo = self._repo(squash, "echo 'older work' >> src.txt\n" + RECORD + """
git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
touch -d '-1 minute' "$(git rev-parse --git-path index.lock)"
""")
                r = repo.run(env={"MAX_ITERATIONS": "2"})
                out = self.out(r)
                self.assertEqual(r.returncode, 0, out)
                self.assertFalse(repo.artifact("phase-blocked.json").exists(), out)
                self.assertIn("older work", repo.git("show", "main:src.txt"), out)

    def test_minor3_captures_never_carry_a_secret(self):
        key = "AKIA" + "Q" * 16
        # (a) a LEARNINGS line that itself says REFUSED
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt
  echo "- commit REFUSED when key """ + key + r""" was pasted" >> docs/LEARNINGS.md
""" + RECORD + """
  exit 0
fi
cp artifacts/phase-verify-failed.json "$STUB_DIR/pvf.json"
""")
        repo.write("docs/LEARNINGS.md", "# Learnings\n"); repo.git("add", "-A"); repo.git("commit", "-qm", "l")
        repo.run(env={"MAX_ITERATIONS": "2"})
        self.assertNotIn(key, (repo.stub / "pvf.json").read_text())
        # (b) a project git hook that prints a secret and refuses the commit
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt
""" + RECORD + """
  exit 0
fi
cp artifacts/phase-verify-failed.json "$STUB_DIR/pvf.json"
""")
        repo.write(".githooks/pre-commit", "#!/bin/sh\necho 'leak: " + key + "' >&2\nexit 1\n", executable=True)
        repo.git("add", "-A"); repo.git("commit", "-qm", "hooks", "--no-verify")
        repo.git("config", "core.hooksPath", ".githooks")
        repo.run(env={"MAX_ITERATIONS": "2"})
        text = (repo.stub / "pvf.json").read_text()
        self.assertNotIn(key, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("git commit", text)

    def test_minor4_every_gated_loop_commit_clears_a_commit_refusal(self):
        for fn in ("wrapup_commit", "land_kept_out_claim", "_commit_from_artifact"):
            with self.subTest(fn=fn):
                self.assertIn("clear_commit_refusal", _fn(fn))


class ReviewRound5(_Loop):
    """Regressions for the v0.18.2 review, round 5 (each red on the round-5 build)."""

    def test_blocker_an_earlier_iterations_unlanded_never_keeps_its_record(self):
        repo = self._repo(False, "echo 'older work' >> src.txt; touch BAD\n" + RECORD + """
git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
""")
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 7}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "intake: iteration 7")
        repo.run(env={"MAX_ITERATIONS": "1"})
        self.assertIn("unlanded", repo.record() or {})
        # iteration 8's intake does NOT delete the record; iteration 7's rest
        # is still red, so the next start's recovery stops again and
        # iteration 8's first pass runs with `unlanded` still recorded.
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 8}) + "\n")
        repo.git("add", "artifacts/iteration-mode.json"); repo.git("commit", "-qm", "intake: iteration 8")
        repo.reset_stub()
        repo.scenario("rm -f BAD\necho task2-half >> src.txt\njq -n '{summary: \"half of task 2\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = self.out(r)
        self.assertEqual(repo.calls(), 1, out)
        self.assertNotIn("Run finished successfully.", out)
        self.assertIn("task2-half", repo.git("show", "HEAD:src.txt"), out)
        self.assertNotIn("completion", repo.git("log", "-1", "--format=%s"), out)
        self.assertFalse(repo.tracked("artifacts/project-complete.json", "HEAD"), "iteration 8's work landed under iteration 7's completion\n" + out)

    def test_major1_a_long_private_key_a_hook_prints_never_reaches_the_capture(self):
        body = ["MIIJ" + ("%02d" % i) * 30 for i in range(60)]
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END RSA PRIVATE KEY-----"
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt
""" + RECORD + """
  exit 0
fi
cp artifacts/phase-verify-failed.json "$STUB_DIR/pvf.json"
""")
        repo.write(".githooks/pre-commit", "#!/bin/sh\ncat >&2 <<'PEM'\n" + pem + "\nPEM\nexit 1\n", executable=True)
        repo.git("add", "-A"); repo.git("commit", "-qm", "hooks", "--no-verify")
        repo.git("config", "core.hooksPath", ".githooks")
        repo.run(env={"MAX_ITERATIONS": "2"})
        text = (repo.stub / "pvf.json").read_text()
        for line in (body[5], body[40], body[59]):
            self.assertNotIn(line, text)

    def test_minor1_a_fresh_dead_lock_costs_no_turn(self):
        repo = self._repo(False, "echo 'older work' >> src.txt\n" + RECORD + """
git add artifacts/project-complete.json; git commit -qm "only the record (a bypass)"
touch "$(git rev-parse --git-path index.lock)"
""")
        r = repo.run(env={"MAX_ITERATIONS": "2"})
        out = self.out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(repo.calls(), 1, out)
        self.assertIn("older work", repo.git("show", "main:src.txt"), out)

    def test_minor3_phasekit_verify_honours_a_locked_index(self):
        self.assertIn("STAGE_LOCKED", _fn("phasekit_verify"))


class ReviewRound6(_Loop):
    """Regressions for the v0.18.2 review, round 6 (each red on the round-6 build)."""

    def test_major_a_session_that_ends_before_its_walk_never_orphans_the_completion(self):
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt; touch BAD
""" + RECORD + """
  git add -A; git commit -qm "completion over a red tree (a bypass)"
  exit 0
fi
if [ "$CALL_N" = 2 ]; then exit 1; fi
rm -f BAD; echo repair >> src.txt
jq -n '{summary: "repaired"}' > artifacts/phase-update.json
""")
        repo.run(env={"MAX_ITERATIONS": "1"})                       # red at step 4
        r2 = repo.run(env={"MAX_ITERATIONS": "1"})                  # the CLI fails after the pass began
        self.assertIn("retry budget exhausted", self.out(r2))
        r3 = repo.run(env={"MAX_ITERATIONS": "2"})
        out = self.out(r3)
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"), "the completion was un-recorded on the target\n" + out)
        self.assertEqual(r3.returncode, 0, out)
        self.assertFalse(repo.tracked("BAD", "main"), out)
        self.assertIn("repair", repo.git("show", "main:src.txt"), out)

    def test_minor2_redaction_of_one_line_and_unterminated_keys_and_more_token_shapes(self):
        defs = "\n".join(l for l in LOOP.read_text().splitlines() if l.startswith(("CREDENTIAL_TOKEN_RE=", "PRIVATE_KEY_RE=")))
        pat = "github_pat_" + "A1" * 11 + "_" + "b2c" * 19 + "d2"   # the real shape (round 7)
        text = ("pre\nx -----BEGIN OPENSSH PRIVATE KEY----- abc -----END OPENSSH PRIVATE KEY----- tail\n"
                "mid " + pat + " gho_" + "b" * 22 + "\n-----BEGIN EC PRIVATE KEY-----\nunterminated-body\n")
        r = subprocess.run(["bash", "-c", defs + "\n" + _fn("redact_credentials") + "\nredact_credentials"],
                           input=text, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for secret in ("abc", pat, "gho_", "unterminated-body"):
            self.assertNotIn(secret, r.stdout)
        self.assertIn("pre", r.stdout)
        self.assertIn("tail", r.stdout)
        self.assertEqual(r.stdout.count("[REDACTED PRIVATE KEY]"), 2, r.stdout)

    def test_minor3_the_wrap_up_stages_through_the_loud_path_and_names_what_it_could_not(self):
        fn = _fn("wrapup_commit")
        self.assertIn("stage_all", fn)
        self.assertIn("EXCEPT paths git cannot stage", fn)
        self.assertIn("stage_all", _fn("stage_landing_tree"))

    def test_minor4_no_lock_is_removed_inside_a_live_model_turn(self):
        fn = _fn("git_add_all")
        self.assertLess(fn.index('VERIFY_INVOKER:-loop}" == model'), fn.index('release_stale_index_lock "run-until-done (staging)"'))


class ReviewRound7(_Loop):
    """Regressions for the v0.18.2 review, round 7 (each red on the round-7 build)."""

    _completed = ReviewRound4._completed

    def test_blocker_a_final_boundary_of_this_iteration_never_claims_an_earlier_record(self):
        repo = self._completed(False)                     # iteration 7 complete, record tracked
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 8}) + "\n")
        repo.git("add", "-A"); repo.git("commit", "-qm", "intake: iteration 8 (the record NOT deleted)")
        repo.reset_stub()
        repo.scenario(r"""
if [ "$CALL_N" = 1 ]; then
  touch BAD; echo "phase 1 of 8" >> src.txt
  jq -n '{phase: "phase-1", approved: true, summary: "final?", final_phase: true,
          suggested_commit_message: "phase 1"}' > artifacts/phase-approval.json
  exit 0
fi
git show HEAD:artifacts/phase-approval.json > artifacts/phase-approval.json
rm -f BAD; echo task2-half >> src.txt
exit 1
""")
        repo.run(env={"MAX_ITERATIONS": "3"})             # red at step 1, then a CLI failure
        repo.reset_stub()
        repo.scenario("jq -n '{summary: \"continuing task 2\"}' > artifacts/phase-update.json\n")
        r = repo.run(env={"MAX_ITERATIONS": "1"})
        out = self.out(r)
        self.assertNotIn("Run finished successfully.", out)
        self.assertNotIn("completion record", repo.git("log", "-1", "--format=%s"), out)
        self.assertFalse(repo.tracked("artifacts/project-complete.json", "HEAD"),
                         "iteration 7's record was re-recorded over iteration 8's work\n" + out)

    def test_minor2_github_pat_matches_only_its_real_shape(self):
        defs = "\n".join(l for l in LOOP.read_text().splitlines() if l.startswith("CREDENTIAL_TOKEN_RE="))
        real = "github_pat_" + "A1" * 11 + "_" + "b2c" * 19 + "d2"
        for text, hit in (("github_pat_rotation_procedure_notes_here", False),
                          ("github_pat_XXXXXXXXXXXXXXXXXXXXXXXXXXXX", False), (real, True)):
            with self.subTest(text=text[:30]):
                r = subprocess.run(["bash", "-c", defs + '\ngrep -qE "$CREDENTIAL_TOKEN_RE"'], input=text, text=True)
                self.assertEqual(r.returncode == 0, hit)


class ReviewRound8(_Loop):
    """Regression for the v0.18.2 review, round 8 (red on the round-8 build)."""

    def test_minor_the_completion_commit_that_first_carries_the_marker_is_this_iterations(self):
        repo = self._repo(False, r"""
if [ "$CALL_N" = 1 ]; then
  echo work >> src.txt; touch BAD
""" + RECORD + """
  git add -A; git commit -qm "completion over a red tree, the marker with it (a bypass)"
  exit 0
fi
if [ "$CALL_N" = 2 ]; then exit 1; fi
rm -f BAD; echo repair >> src.txt
jq -n '{summary: "repaired"}' > artifacts/phase-update.json
""")
        # the iteration's marker is on disk but nobody committed it
        repo.write("artifacts/iteration-mode.json", json.dumps({"mode": "standard", "iteration": 7}) + "\n")
        repo.run(env={"MAX_ITERATIONS": "1"})
        repo.run(env={"MAX_ITERATIONS": "1"})
        r3 = repo.run(env={"MAX_ITERATIONS": "2"})
        self.assertTrue(repo.tracked("artifacts/project-complete.json", "main"), self.out(r3))


class ContractPins(unittest.TestCase):
    def setUp(self):
        self.m = json.loads(MANIFEST.read_text())
        self.art = {a["name"]: a for a in self.m["artifacts"]}
        self.conv = {c["name"]: c for c in self.m["conventions"]}

    def test_the_new_record_fields_are_pinned(self):
        for name in ("phase-approval.json", "project-complete.json", "iterations"):
            self.assertIn("plan_paths", self.art[name]["keys"], name)
        for key in ("plan_paths", "unlanded"):
            self.assertIn(key, self.art["boundary-state.json"]["keys"])
        self.assertIn("scratch", self.art)

    def test_the_conventions_are_declared_where_they_say(self):
        for name, marker in (("loop-owns-commits", RULE), ("planned-paths", "Planned paths:")):
            c = self.conv[name]
            self.assertEqual(c["marker"], marker)
            for rel in c["declared_in"]:
                with self.subTest(convention=name, file=rel):
                    self.assertIn(marker, (REPO_ROOT / rel).read_text())

    def test_the_guard_reads_stdin_first(self):
        text = HOOK.read_text()
        self.assertLess(text.index('_payload="$(cat'), text.index('_payload="${CLAUDE_TOOL_INPUT'))


if __name__ == "__main__":
    unittest.main()
