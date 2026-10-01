#!/usr/bin/env python3
"""The command guard's scope (v0.18.3; Aaron, 2026-09-30).

v0.18.2 made .claude/hooks/deny-dangerous-commands.sh actually work, and its
old "dangerous" list — which included a plain `git push` and `git tag v1` —
started firing in INTERACTIVE sessions too, breaking the operator lane (an
operator session pushing an `operator-<topic>` branch from its own clone).
The scope now:

  every session   the truly destructive commands: reset --hard, clean -fd,
                  force/deleting pushes, tag deletion/overwrite, sudo, shred,
                  a recursive rm of the repository root or its .git;
  loop only       every git write to this repository's history, refs or
                  index, and ANY git push (the loop owns every commit);
  interactive     an ordinary push and ordinary git writes are allowed.

"Under the loop" = PHASEKIT_ARTIFACTS_DIR and PHASEKIT_ITER_MARKER both
non-empty — NOT that their paths exist (a model can delete a /tmp marker; that
must never switch the loop's rule off).

Run from the repo root: python3 -m unittest tests.test_guard_scope
"""

import importlib.util
import os
import shutil
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pk_loop_owns_commits_scope", Path(__file__).resolve().parent / "test_loop_owns_commits.py")
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


class InteractiveSessions(L._GuardBase):
    def test_an_ordinary_push_is_allowed(self):
        # RED on v0.18.2: `git push` was on the every-session list.
        for c in ("git push origin operator-x", "git push", "git push -u origin operator-x",
                  "git push --set-upstream origin operator-docs", "git push origin HEAD:operator-x",
                  "git push --tags", "git push --follow-tags origin operator-x",
                  f"git -C {self.proj} push origin operator-x",
                  "git push -o ci.skip origin operator-x", "git push --dry-run origin x"):
            with self.subTest(command=c):
                rc, err = self.guard(c, loop=False)
                self.assertEqual(rc, 0, f"{c!r}: {err}")

    def test_ordinary_git_writes_are_allowed(self):
        for c in ("git commit -m x", "git add -A", "git tag v1", "git tag -a v1 -m 'fix: x'",
                  "git tag -mfix v2", "git branch -D old", "git checkout -b operator-x",
                  "git stash", "git fetch origin", "git pull --rebase", "git reset HEAD~1",
                  "git clean -n", "rm -rf node_modules", "rm -r sub", "rm -rf /tmp/scratch-x"):
            with self.subTest(command=c):
                rc, err = self.guard(c, loop=False)
                self.assertEqual(rc, 0, f"{c!r}: {err}")

    def test_a_force_push_is_refused(self):
        for c in ("git push --force", "git push --force origin main", "git push -f origin main",
                  "git push -fu origin x", "git push -uf origin x", "git push --force-with-lease",
                  "git push --force-with-lease=main:abc origin main", "git push origin +main",
                  "git push origin +HEAD:refs/heads/main", "git push origin :operator-x",
                  "git push --delete origin v1", "git push -d origin v1", "git push --mirror",
                  "git push --prune origin", "git -C sub push --force"):
            with self.subTest(command=c):
                rc, err = self.guard(c, loop=False)
                self.assertEqual(rc, 2, f"not refused: {c!r}")
                self.assertIn("Blocked dangerous command pattern", err)

    def test_the_rest_of_the_destructive_list_is_refused(self):
        for c in ("git reset --hard", "git reset --hard origin/main", "git clean -fd", "git clean -fdx",
                  "git clean -f -d", "git clean -dfx", "git tag -d v1", "git tag --delete v1",
                  "git tag -f v1", "git tag -fa v1 -m x", "git tag --force v1",
                  "git update-ref -d refs/tags/v1", "git update-ref refs/tags/v1 HEAD",
                  "sudo rm x", "shred f", "rm -rf .git", "rm -rf ./.git/", "rm -rf .git/refs",
                  "rm -fr .", "rm -Rf .", "rm -r --force .", "rm --recursive --force .git",
                  f"rm -rf {self.proj}", "rm -rf ..", "rm -rf /", "rm -rf ~" if str(self.proj).startswith(os.path.expanduser("~")) else "rm -rf /",
                  "rm -rf *", "rm -rf .g*", "cd sub && rm -rf ..", "bash -c 'rm -rf .git'",
                  "git -c alias.nuke='reset --hard' nuke"):
            with self.subTest(command=c):
                rc, err = self.guard(c, loop=False)
                self.assertEqual(rc, 2, f"not refused: {c!r}")
                self.assertIn("Blocked dangerous command pattern", err)
                self.assertEqual(len([ln for ln in err.splitlines() if ln.strip()]), 1, err)

    def test_a_recursive_rm_of_another_repository_is_not_this_guards_business(self):
        for c in (f"rm -rf {self.other}", f"rm -rf {self.other}/.git", "rm -rf sub", "rm .git-blame-ignore"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)


class LoopSessions(L._GuardBase):
    def test_every_push_is_refused(self):
        for c in ("git push", "git push origin operator-x", "git push -u origin iter/3",
                  f"git -C {self.other} push origin main", "bash -c 'git push'", "cat <(git push)"):
            with self.subTest(command=c):
                rc, err = self.guard(c)
                self.assertEqual(rc, 2, f"not refused: {c!r}")
                self.assertIn("the loop owns every commit", err)

    def test_a_commit_is_refused(self):
        rc, err = self.guard("git commit -m x")
        self.assertEqual(rc, 2)
        self.assertIn("the loop owns every commit", err)

    def test_the_destructive_list_is_refused_too(self):
        for c in ("git push --force", "git reset --hard", "rm -rf .git", "sudo ls"):
            with self.subTest(command=c):
                rc, err = self.guard(c)
                self.assertEqual(rc, 2, c)
                self.assertIn("Blocked dangerous command pattern", err)


class WhatUnderTheLoopMeans(L._GuardBase):
    def test_a_deleted_marker_never_switches_the_rule_off(self):
        # RED on v0.18.2: the guard also required the marker FILE; a model's
        # `rm "$PHASEKIT_ITER_MARKER"` made the next call an interactive one.
        self.marker.unlink()
        for c in ("git commit -m x", "git push origin main"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)

    def test_a_moved_artifacts_dir_never_switches_it_off(self):
        shutil.rmtree(self.proj / "artifacts")
        self.assertEqual(self.guard("git add -A")[0], 2)

    def test_either_variable_missing_or_empty_is_interactive(self):
        for env in ({"PHASEKIT_ITER_MARKER": ""}, {"PHASEKIT_ARTIFACTS_DIR": ""}):
            with self.subTest(env=env):
                self.assertEqual(self.guard("git push origin operator-x", env=env)[0], 0)
        self.assertEqual(self.guard("git push origin operator-x", loop=False)[0], 0)

    def test_the_loop_exports_both_before_its_first_model_turn(self):
        text = L.LOOP.read_text()
        first_turn = text.index('run_once "$PROMPT_FILE" "new"')
        for var in ('export PHASEKIT_ARTIFACTS_DIR=', 'export PHASEKIT_ITER_MARKER='):
            self.assertIn(var, text)
            self.assertLess(text.index(var), first_turn, var)


class TheFallbackAgrees(L._GuardBase):
    """Without python3 (or when the parse fails) one pair of regexes decides."""
    def _no_python_path(self):
        return L.GuardFailsOpenNarrowly._no_python_path(self)

    def test_without_python3(self):
        path = self._no_python_path()
        cases = [("git push origin operator-x", False, 0), ("git push --force", False, 2),
                 ("git push origin +main", False, 2), ("git reset --hard", False, 2),
                 ("git clean -fdx", False, 2), ("git tag -d v1", False, 2), ("git tag v1", False, 0),
                 ("rm -rf .git", False, 2), ("git commit -m x", False, 0),
                 ("git push origin operator-x", True, 2), ("git commit -m x", True, 2),
                 ("git status", True, 0)]
        for c, loop, want in cases:
            with self.subTest(command=c, loop=loop):
                self.assertEqual(self.guard(c, loop=loop, path=path)[0], want, c)

    def test_an_unparseable_command(self):
        self.assertEqual(self.guard("echo 'unbalanced ; git push origin x", loop=False)[0], 0)
        self.assertEqual(self.guard("echo 'unbalanced ; git push --force", loop=False)[0], 2)
        self.assertEqual(self.guard("echo 'unbalanced ; git push origin x")[0], 2)

    def test_one_source_for_the_regexes(self):
        text = L.HOOK.read_text()
        self.assertEqual(text.count("PK_GUARD_DESTRUCTIVE_RE='"), 1)
        self.assertEqual(text.count("PK_GUARD_LOOP_RE='"), 1)
        self.assertIn('os.environ.get("PK_GUARD_DESTRUCTIVE_RE"', text)
        self.assertIn('os.environ.get("PK_GUARD_LOOP_RE"', text)


class ReviewRound1(L._GuardBase):
    """Fresh-context review, round 1 (each red before its fix)."""

    def test_major1_the_fallback_sees_a_flag_right_after_push(self):
        path = L.GuardFailsOpenNarrowly._no_python_path(self)
        for c in ("git push -f origin main", "git push -d origin x", "git push -uf origin x",
                  "git push -f", "git push +main", "git push :x"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False, path=path)[0], 2, c)
                # and the python parse-failure path (an unbalanced quote)
                self.assertEqual(self.guard(c + '; echo "oops', loop=False)[0], 2, c)
        for c in ("git push origin operator-x", "git tag -F notes.txt v1", "git push origin :"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False, path=path)[0], 0, c)

    def test_minor1_a_long_option_prefix_is_that_option(self):
        for c in ("git push --del origin x", "git push --mir", "git push --force-w", "git push --pru",
                  "git tag --del v1", "git tag --forc v1", "git reset --har", "git clean --forc -d"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)
        for c in ("git push --dry-run origin x", "git push --follow-tags origin x", "git reset --soft HEAD~1"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_minor2_a_mirror_by_config_on_the_command_line(self):
        for c in ("git -c remote.origin.mirror=true push origin",
                  "git -c 'remote.origin.push=+refs/heads/*:refs/heads/*' push origin"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)

    def test_minor3_the_loop_refuses_the_other_push_doors(self):
        for c in ("git send-pack origin main", "git subtree push --prefix=x origin main",
                  "/usr/lib/git-core/git-push origin", "/usr/lib/git-core/git-commit -m x"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)

    def test_minor4_ordinary_recovery_is_not_refused(self):
        (self.proj / "link").symlink_to(self.proj)
        for c in ("git push origin :", "rm -rf .git/index.lock", "rm -rf .git/rebase-merge",
                  "git clean -fdn", "git clean -fd --dry-run", "rm -rf link"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)
        for c in ("rm -rf .git/objects", "rm -rf .git/refs/heads"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)


class ReviewRound2(L._GuardBase):
    def test_major1_three_character_abbreviations(self):
        for c in ("git reset --h", "git clean --f -d", "git push --m origin", "git tag --d v9"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)

    def test_major2_subtree_with_options_before_its_subcommand(self):
        for c in ("git subtree -P x push ../bare subbr", "git subtree --prefix=x push o m",
                  "git subtree --prefix=x add o m"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)

    def test_major3_a_glob_above_the_root_and_rm_abbreviations(self):
        for c in ("rm -rf ../*", f"rm -rf {self.tmp}/*", "rm -rf /*", "rm --recur -f .git", "rm --r -f .git",
                  "find .git -delete", "find . -mindepth 1 -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)
        for c in ("rm -rf sub/*", "find . -name '*.pyc' -delete", "find sub -type f -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_major5_a_command_word_built_from_an_expansion(self):
        for c in ("G=git; $G push origin main", "${GIT:-git} push", '"$(which git)" commit -am x'):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)
        self.assertEqual(self.guard("$EDITOR notes.md", loop=True)[0], 0)

    def test_minors(self):
        (self.proj / "link").symlink_to(self.proj)
        for c in ("git -c remote.origin.mirror push origin", "git -c remote.origin.mirror=2 push origin",
                  "git -c remote.origin.push=:refs/heads/x push origin", "git clean -fden",
                  "git -c clean.requireForce=false clean -dx", "git tag --column -d v7", "rm -rf link/",
                  "git send-pack --force origin main"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)


class ReviewRound3(L._GuardBase):
    def test_major1_a_quoted_lookup_never_unbalances_the_parse(self):
        for c, loop in (('echo "git: $(command -v git)"; git -P commit -am x', True),
                        ('echo "git: $(which git)"; git branch -f main HEAD~1', True),
                        ('echo "git: $(which git)"; git -P reset --hard HEAD~3', False),
                        ('echo "git: $(which git)"; git -P push --force origin main', False)):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=loop)[0], 2, c)

    def test_major2_global_options_and_unquoted_lookups_before_the_subcommand(self):
        for c in ("G=git; $G -C . push origin HEAD", "$G --no-pager commit -am x",
                  "${GIT:-git} -c user.name=x commit -am x", '"$(which git)" -C . push origin HEAD'):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)

    def test_minor1_the_expansion_rule_is_the_loops_only(self):
        for c in ("$DOCKER tag -f a b", "$MAKE clean -fd", "$DOCKER push -d img", "$UV add requests"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_minor2_find_evaluates_in_order(self):
        for c in ('find . -delete -name "*.pyc"', "find . -name x -o -delete", "find . -print -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)

    def test_minor3_a_glob_above_that_cannot_reach_the_repository(self):
        for c in ("rm -rf ../scratch-*", "rm -rf ../*.tar.gz"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)
        self.assertEqual(self.guard("rm -rf ../p*", loop=False)[0], 2)

    def test_minor6_tag_and_branch_under_the_loop(self):
        for c in ("git tag --sort=refname v9", "git tag -i v9", "git tag --column v1"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)
        self.assertEqual(self.guard("git branch --sort -committerdate")[0], 0)


class ReviewRound4(L._GuardBase):
    """Round 4/5. DECLINED WITH RECORD (v0.18.3 release note; follow-up queue row):
    an UNQUOTED substitution used as the command word (`$(which git) push`,
    `git$(true) push`) — the parse splits a simple command at `$(`, exactly as
    v0.18.2 did. Closing it by rewriting substitutions before the lexer (round
    4) opened two new holes in round 5 (`case … )` inside `$(…)`, glued path
    prefixes); the class needs a real shell parse, not another regex. Only
    deliberate obfuscation reaches it; a commit made that way still meets the
    whole-tree check at completion."""

    def test_major1_no_false_refusal_of_ordinary_substitutions(self):
        for c in ("ls $(git rev-parse --show-toplevel)", 'cd "$(git rev-parse --show-toplevel)" && git status',
                  "for f in $(git ls-files); do wc -l $f; done", 'echo "branch: $(git branch --show-current)"',
                  "python3 -m pytest $(git diff --name-only HEAD | grep test_)"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 0, c)
        self.assertEqual(self.guard("echo $(git stash)")[0], 2, "a body is still checked")

    def test_major2_a_trailing_slash_glob(self):
        for c in ("rm -rf ../*/", "rm -r -f ../*/", "rm -rf ../p*/", f"rm -rf {self.tmp}/*/", "rm -rf */"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)

    def test_major3_quoted_and_escaped_find_groups(self):
        for c in ("find . \\( -name x \\) -exec git push \\;", "find . '(' -name x ')' -exec git commit -m y \\;"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c)[0], 2, c)
        for c in ("find . \\( -name x -o -true \\) -delete", "find . \\( -true \\) -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)
        self.assertEqual(self.guard("find . \\( -name '*.pyc' \\) -delete", loop=False)[0], 0)

    def test_minor3_find_exec_plus_is_not_a_force_refspec(self):
        self.assertEqual(self.guard("find . -name x -exec echo {} +", loop=False)[0], 0)


class ReviewRound5(L._GuardBase):
    def test_major3_the_fallback_sees_a_subcommand_before_an_operator(self):
        path = L.GuardFailsOpenNarrowly._no_python_path(self)
        for c in ("git push;", "git stash&&true", "git push|cat", "(git commit -am x)"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, path=path)[0], 2, c)
        self.assertEqual(self.guard('echo $(echo ")"); git push;')[0], 2)

    def test_the_round4_rewrite_is_gone_and_its_holes_with_it(self):
        for c, loop in (("echo $(case x in x) git push --force origin main;; esac)", False),
                        ("true $(case x in x) git commit -am x;; esac)", True),
                        ('cd ../$(basename "$PWD") && git commit -am x', True)):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=loop)[0], 2, c)
        self.assertEqual(self.guard("rm -rf ./$(cat .last)", loop=False)[0], 0)

    def test_minor2_find_alternatives_inside_a_group_still_filter(self):
        for c in ("find . \\( -name '*.pyc' -o -name '*.pyo' \\) -delete",
                  "find . -type f \\( -name a -o -name b \\) -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)
        for c in ("find . \\( -name x -o -true \\) -delete", "find . -name x -o -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)


class ReviewRound6(L._GuardBase):
    def test_major1_a_glued_quoted_case_pattern_never_hides_the_command(self):
        for c, loop in (("""case x in "x")"git" commit -am y;; esac""", True),
                        ("""case x in 'x')'git' add -A;; esac""", True),
                        ("""case x in "x")"git" push --force origin main;; esac""", False),
                        ("""case x in 'x')'git' reset --hard;; esac""", False)):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=loop)[0], 2, c)

    # DECLINED WITH RECORD (follow-up row: a real shell parse): an unquoted
    # `$(…)` ARGUMENT splits the simple command (`git -C $(pwd) add -A`), as on
    # v0.18.2; round 6 made the substitution a word of the outer command and
    # round 7 found that rework hid commands behind glued case patterns and let
    # its extra segment move the parse's cwd. Same record: a case pattern's `)`
    # or a paren inside `${…}` unbalances the subshell cwd stack (v0.18.2 too).

    def test_minors_find(self):
        for c in ("find . -name zzz , -delete", "find -O3 .. -delete", "find -D tree .. -delete"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)


class ReviewRound8(L._GuardBase):
    def test_an_unset_variable_is_empty_in_an_rm_target(self):
        env = {"PK_UNSET_DIR": ""}
        for c in ('rm -rf "$PK_NEVER_SET_X"/*', "rm -rf ${PK_NEVER_SET_X}/*", 'rm -rf "$PK_NEVER_SET_X/"'):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)
        for c in ('D=build; rm -rf "$D"/*', 'rm -rf "$HOME/.cache/pk-x"', 'for d in a b; do rm -rf "$d"; done',
                  f'cd {self.other} && rm -rf "$PWD"'):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)
        self.assertEqual(self.guard('cd sub && rm -rf "$OLDPWD"', loop=False)[0], 2)
        self.assertEqual(self.guard('cd sub && cd .. && rm -rf "$PWD"', loop=False)[0], 2)

    def test_function_keyword_bodies_and_time_p(self):
        for c, loop in (("function f { git push --force; }; f", False), ("function f { rm -rf .git; }; f", False),
                        ("function f { git commit -am x; }; f", True), ("time -p git reset --hard", False)):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=loop)[0], 2, c)


class ReviewRound9(L._GuardBase):
    def test_no_false_refusal_of_variable_rm_targets(self):
        for c in ('rm -rf "$tmp"', 'source .env && rm -rf "$DIST"', 'rm -rf -- "$X"', 'rm -rf "$A$B"',
                  'd=build; rm -rf "$d"_old', 'for d in */; do rm -rf "$d"node_modules; done',
                  'out=dist; rm -rf "$out"2/*', 'source .env; rm -rf "$BUILD_DIR"/*', 'read; rm -rf "$REPLY"/*',
                  'getopts o: opt; rm -rf "$OPTARG"/*', "rm -rf ./\"$X\""):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)

    def test_the_unset_default_forms_and_time_dashdash(self):
        for c, loop in (('rm -rf "${PK_NEVER_SET_X:-}"/*', False), ('rm -rf "${PK_NEVER_SET_X-}"/*', False),
                        ("time -- git push --force", False), ("time -p -- rm -rf .git", False),
                        ("time -- git commit -m x", True)):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=loop)[0], 2, c)


class ReviewRound10(L._GuardBase):
    def test_minors(self):
        for c in ('cd "$(mktemp -d)" && rm -rf "$PWD"/*', 'd=$(mktemp -d); cd "$d"; rm -rf "$PWD"',
                  'rm -rf "$RANDOM"/*', 'rm -rf "$HOSTNAME"/*'):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 0, c)
        for c in ("/usr/bin/time -f %e git push --force", '\\time -f "%e" git push --force',
                  "/usr/bin/time -o /tmp/t -f %e rm -rf .git"):
            with self.subTest(command=c):
                self.assertEqual(self.guard(c, loop=False)[0], 2, c)
        self.assertEqual(self.guard("/usr/bin/time -f %e git commit -m x")[0], 2)


if __name__ == "__main__":
    unittest.main()
