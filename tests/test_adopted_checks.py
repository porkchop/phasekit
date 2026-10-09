#!/usr/bin/env python3
"""Checks the 2026-10-09 fleet sweep found living only in project suites,
adopted upstream as phasekit's own tests (v0.19.3).

A project's tests test the project (docs/QUALITY_GATES.md); a guarantee a
project relies on is proven HERE. The sweep (foundry-meta
reviews/REVIEW-2026-10-09-process-tests.md) found three that phasekit did not
prove, each pinned downstream instead:

  * the container firewall lets traffic out to the host's bridge network
    BEFORE its catch-all REJECT (xmeo-v3's deploy path depends on it);
  * the python-uv verify template's tiers: the completion record selects the
    full tier, the fast tier drops `slow`, a tier can be forced, `--plan` runs
    nothing (foundry-orchestrator's checks) — as behaviour, not template text;
  * the static-web verify template refuses "zero tests reported" when the
    tree has tests, and nothing masks a failure (foundry-dashboard's checks),
    against node's REAL reporter output (its TAP-only grep went red on
    node 22+, where the piped default reporter is spec).

And the runner image's browser is pinned, so the same engine builds the same
browser (the 2026-10-08 drift: Chrome for Testing 153 -> 156, no Dockerfile
change).

Every test here is red on v0.19.2.

Run from the repo root: python3 -m unittest tests.test_adopted_checks
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _suite_tmp  # noqa: F401  (every test under its own TMPDIR; tests/_suite_tmp.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIREWALL = REPO_ROOT / ".devcontainer" / "init-firewall.sh"
DOCKERFILE = REPO_ROOT / ".devcontainer" / "Dockerfile"
PY_GATE = REPO_ROOT / "templates" / "phasekit-verify.template.python-uv.sh"
WEB_GATE = REPO_ROOT / "templates" / "phasekit-verify.template.static-web.sh"
HAVE_JQ = shutil.which("jq") is not None
HAVE_NODE = shutil.which("node") is not None


def _stub(bindir, name, body):
    p = Path(bindir) / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


class _Tmp(unittest.TestCase):
    def tmp(self):
        d = Path(tempfile.mkdtemp(prefix="pk-adopt-"))
        self.addCleanup(shutil.rmtree, d, True)
        return d


@unittest.skipUnless(HAVE_JQ, "jq required (the script parses GitHub's meta with it)")
class FirewallRuleOrder(_Tmp):
    """Run the real script with every network/kernel command stubbed to LOG,
    then read the OUTPUT chain the way iptables evaluates it: in order."""

    def run_firewall(self, gateway="172.18.0.1"):
        d = self.tmp()
        bindir, log = d / "bin", d / "iptables.log"
        bindir.mkdir()
        _stub(bindir, "iptables", f'printf "%s\\n" "$*" >> "{log}"\n')
        _stub(bindir, "iptables-save", "exit 0\n")
        _stub(bindir, "ipset", "exit 0\n")
        _stub(bindir, "aggregate", "cat\n")
        _stub(bindir, "dig", 'echo "$3. 60 IN A 203.0.113.7"\n')
        _stub(bindir, "ip", f'echo "default via {gateway} dev eth0"\n')
        _stub(bindir, "curl", 'case "$*" in\n'
              '  *api.github.com/meta*) echo \'{"web":["192.0.2.0/24"],"api":["198.51.100.0/24"],"git":[]}\' ;;\n'
              '  *example.com*) exit 7 ;;\n'
              '  *) exit 0 ;;\nesac\n')
        r = subprocess.run(["bash", str(FIREWALL)], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return [line for line in log.read_text().splitlines()]

    def test_the_bridge_allow_precedes_the_catch_all_reject(self):
        rules = self.run_firewall()
        output = [r for r in rules if r.startswith("-A OUTPUT")]
        bridge = [i for i, r in enumerate(output) if r == "-A OUTPUT -d 172.18.0.0/24 -j ACCEPT"]
        reject = [i for i, r in enumerate(output) if "-j REJECT" in r]
        self.assertEqual(len(bridge), 1, output)
        self.assertEqual(len(reject), 1, output)
        self.assertLess(bridge[0], reject[0], "the bridge allow must come before the REJECT")
        self.assertEqual(reject[0], len(output) - 1, "the REJECT is the chain's last rule")
        self.assertIn("-A INPUT -s 172.18.0.0/24 -j ACCEPT", rules)

    def test_the_bridge_network_follows_the_default_route(self):
        rules = self.run_firewall(gateway="10.89.3.1")
        self.assertIn("-A OUTPUT -d 10.89.3.0/24 -j ACCEPT", rules)


class PythonUvTiers(_Tmp):
    """The seeded python-uv gate, run with a `uv` that logs its argv."""

    def project(self, complete=False):
        d = self.tmp()
        (d / "scripts").mkdir()
        shutil.copy(PY_GATE, d / "scripts" / "phasekit-verify.sh")
        (d / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        if complete:
            (d / "artifacts").mkdir()
            (d / "artifacts" / "project-complete.json").write_text("{}\n")
        bindir = d / "bin"
        bindir.mkdir()
        _stub(bindir, "uv", f'printf "%s\\n" "$*" >> "{d}/uv.log"\n')
        return d

    def gate(self, d, *args):
        r = subprocess.run(["bash", str(d / "scripts" / "phasekit-verify.sh"), *args],
                           capture_output=True, text=True, timeout=60, cwd=str(d),
                           env={**os.environ, "UV_BIN": str(d / "bin" / "uv")})
        log = d / "uv.log"
        pytest = [l for l in (log.read_text().splitlines() if log.exists() else []) if "pytest" in l]
        return r, pytest

    def test_no_completion_record_runs_the_fast_tier_which_drops_slow(self):
        r, pytest = self.gate(self.project())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pytest, ["run pytest -q -m not slow"])
        self.assertIn("fast tier", r.stdout)

    def test_the_completion_record_selects_the_full_tier(self):
        r, pytest = self.gate(self.project(complete=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pytest, ["run pytest -q"])
        self.assertIn("full tier", r.stdout)

    def test_a_tier_can_be_forced_either_way(self):
        _, full = self.gate(self.project(), "--full")
        _, fast = self.gate(self.project(complete=True), "--fast")
        self.assertEqual(full, ["run pytest -q"])
        self.assertEqual(fast, ["run pytest -q -m not slow"])

    def test_plan_runs_nothing(self):
        d = self.project(complete=True)
        r, pytest = self.gate(d, "--plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((d / "uv.log").exists(), "--plan ran a command")
        self.assertIn("full tier", r.stdout)
        self.assertIn("nothing was run", r.stdout)

    def test_plan_without_a_pyproject_says_nothing_would_run(self):
        d = self.project()
        (d / "pyproject.toml").unlink()
        r, _ = self.gate(d, "--plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no pyproject.toml yet, so no check would run", r.stdout)

    def test_conflicting_or_unknown_arguments_are_refused(self):
        self.assertEqual(self.gate(self.project(), "--fast", "--full")[0].returncode, 2)
        self.assertEqual(self.gate(self.project(), "--bogus")[0].returncode, 2)


@unittest.skipUnless(HAVE_NODE, "node required (the static-web gate runs node --test)")
class StaticWebVerdict(_Tmp):
    """The seeded static-web gate over real node test runs."""

    PASS = "import { test } from 'node:test';\ntest('adds', () => { if (1 + 1 !== 2) throw new Error('x'); });\n"
    FAIL = "import { test } from 'node:test';\ntest('breaks', () => { throw new Error('red'); });\n"

    def project(self, tests=None, script=None):
        d = self.tmp()
        (d / "scripts").mkdir()
        shutil.copy(WEB_GATE, d / "scripts" / "phasekit-verify.sh")
        pkg = {"name": "x", "private": True, "type": "module"}
        if script:
            pkg["scripts"] = {"test": script}
        import json
        (d / "package.json").write_text(json.dumps(pkg))
        for rel, text in (tests or {}).items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        return d

    def gate(self, d, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("NODE_TEST")}
        e.update(env)
        return subprocess.run(["bash", str(d / "scripts" / "phasekit-verify.sh")], capture_output=True,
                              text=True, timeout=120, cwd=str(d), env=e)

    def test_green_tests_pass(self):
        r = self.gate(self.project({"test/a.test.js": self.PASS}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_failing_test_is_red_through_the_pipe(self):
        r = self.gate(self.project({"test/a.test.js": self.FAIL}))
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_a_failure_the_status_hides_is_still_red(self):
        # a test script that swallows the runner's status: `|| true`
        r = self.gate(self.project({"test/a.test.js": self.FAIL}, script="node --test || true"))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("failing test(s) under exit status 0", r.stderr)

    def test_zero_tests_reported_is_refused_when_the_tree_has_tests(self):
        # the script runs node where there are no tests: it reports 0 and exits 0
        r = self.gate(self.project({"test/a.test.js": self.PASS, "empty/README.md": "no tests here\n"},
                                   script="cd empty && node --test"))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ZERO tests", r.stderr)

    def test_the_refusal_survives_a_large_tree(self):
        # review F1: `git ls-files | grep -q` SIGPIPEd under pipefail on a big tree
        files = {"test/a.test.js": self.PASS, "empty/README.md": "no tests here\n"}
        files.update({f"src/m{i}.js": "" for i in range(4000)})
        r = self.gate(self.project(files, script="cd empty && node --test"))
        self.assertIn("ZERO tests", r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_a_test_printing_fail_lines_is_not_a_reported_failure(self):
        # review R2/R1: only node's own summary block counts, never a line a test prints
        noisy = ("import { test } from 'node:test';\ntest('a', () => { console.log('fail 1');"
                 " console.log('# fail 2'); console.log('ℹ fail 3'); });\n")
        r = self.gate(self.project({"test/a.test.js": noisy}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_two_runs_are_summed(self):
        # review F2: a script running node twice; the second run has no tests yet
        r = self.gate(self.project({"test/unit/a.test.js": self.PASS, "empty/README.md": "no tests here\n"},
                                   script="node --test test/unit/a.test.js && cd empty && node --test"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_brand_new_project_with_no_tests_is_not_wedged(self):
        r = self.gate(self.project())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_both_reporters_are_read(self):
        for reporter in ("spec", "tap"):
            with self.subTest(reporter=reporter):
                d = self.project({"test/a.test.js": self.PASS, "empty/README.md": "no tests here\n"},
                                 script=f"cd empty && node --test --test-reporter={reporter}")
                r = self.gate(d)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("ZERO tests", r.stderr)

    def test_it_survives_node_test_context(self):
        # a gate started from inside a node test inherits NODE_TEST_CONTEXT; the
        # child runner then reports to a parent that is not there, printing no totals
        for tests, want_red in (({"test/a.test.js": self.PASS}, False), ({"test/a.test.js": self.FAIL}, True)):
            with self.subTest(red=want_red):
                r = self.gate(self.project(tests), NODE_TEST_CONTEXT="child-v8")
                self.assertEqual(r.returncode != 0, want_red, r.stdout + r.stderr)
                self.assertRegex(r.stdout, r"(?m)^\s*(ℹ|#)\s+tests\s+1\s*$")

    def test_the_gate_leaves_no_footprint(self):
        d = self.project({"test/a.test.js": self.PASS})

        def status():
            return subprocess.run(["git", "-C", str(d), "status", "--porcelain", "--ignored"],
                                  capture_output=True, text=True).stdout
        before = status()
        self.assertEqual(self.gate(d).returncode, 0)
        self.assertEqual(status(), before, "the gate wrote into the tree")


class BrowserPin(unittest.TestCase):
    """The runner image builds one browser, and says so."""

    def test_playwright_core_and_the_browser_are_pinned_exactly(self):
        text = DOCKERFILE.read_text()
        core = re.search(r"(?m)^ARG PLAYWRIGHT_CORE_VERSION=(\S+)$", text)
        browser = re.search(r"(?m)^ARG CHROMIUM_VERSION=(\S+)$", text)
        self.assertIsNotNone(core)
        self.assertIsNotNone(browser)
        self.assertRegex(core.group(1), r"^\d+\.\d+\.\d+$", "an exact release, never latest or a range")
        self.assertRegex(browser.group(1), r"^\d+\.\d+\.\d+\.\d+$")
        # the fleet's image on 2026-10-09 (scaffold-runner:rebuild-20261008)
        self.assertEqual((core.group(1), browser.group(1)), ("1.64.0", "156.0.8078.4"))
        # every playwright-core invocation names the pinned release
        calls = re.findall(r"(?m)^RUN npx\b[^\n]*playwright-core\S*", text)
        self.assertTrue(calls)
        for call in calls:
            self.assertIn('playwright-core@${PLAYWRIGHT_CORE_VERSION}', call)

    def test_the_build_asserts_the_browser_it_installed(self):
        text = DOCKERFILE.read_text()
        self.assertIn('test "$reported" = "Google Chrome for Testing ${CHROMIUM_VERSION}"', text)

    def test_verify_in_container_refuses_another_browser(self):
        text = (REPO_ROOT / "scripts" / "verify-in-container.sh").read_text()
        self.assertIn("ARG CHROMIUM_VERSION=", text)
        self.assertIn("the image's browser is not the pinned one", text)


if __name__ == "__main__":
    unittest.main()
