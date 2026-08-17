#!/usr/bin/env python3
"""Producer-side pins for contracts/interface.json — phasekit's own interface
manifest (env consumed, artifacts written, exit codes emitted).

Both original incidents of the cross-project-contracts class were THIS
interface, in opposite directions: the loop consumed PHASEKIT_SESSION_DEADLINE
that nothing emitted (#83: consumer built, producer missing), and a supervisor
exported META_REPO_PATH that nothing consumed for months (producer built,
consumer missing). A hand-written manifest with no pins would rot exactly like
prose, so these tests hold it against the code from both directions:

  * declared ⊆ read  — every env var / artifact / exit code the manifest
    declares is actually present in the script it names. A manifest entry
    nothing reads is META_REPO_PATH wearing a contract.
  * read ⊆ declared  — every PHASEKIT_* name, every artifacts/ filename and
    every literal exit code in the shipped scripts/hooks appears in the
    manifest. This is the pin that catches #83 on day one: a new consumer
    cannot be added without declaring it.

Where a direction is NOT mechanically checkable, it is stated here rather than
silently narrowed:

  * read ⊆ declared for NON-PHASEKIT env names (MAX_ITERATIONS, VERIFY_SKIP,
    ANTHROPIC_MODEL, …) cannot be swept — there is no lexical marker
    separating an interface name from a script-local variable. Only the
    PHASEKIT_* namespace is swept; the others are pinned in the declared⊆read
    direction alone.
  * exit-code passthrough behaviour (a child's code propagating verbatim, the
    124-means-timeout convention) is a runtime property, not a lexical one.
    The literal-code sweep proves no script exits a literal code the manifest
    omits; it cannot prove the passthrough prose.
  * implicit exit 0 (falling off the end of a script) leaves no literal to
    sweep, so declared codes are not required to appear literally — only the
    reverse holds.

The sweeps themselves are proven non-vacuous by planting a synthetic read and
asserting the sweep sees it (the v0.8.0 aliased-import lesson: a sweep that
silently matches nothing passes forever).

Run from the repo root: python3 -m unittest tests.test_interface_manifest
"""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "contracts" / "interface.json"
LOOP = REPO_ROOT / "scripts" / "run-until-done.sh"
CONTAINER_SETUP = REPO_ROOT / "scripts" / "container-setup.sh"
CHECKER = REPO_ROOT / "scripts" / "phasekit-contracts.py"
CAPABILITIES = REPO_ROOT / "capabilities" / "project-capabilities.yaml"
QUALITY_GATES = REPO_ROOT / "docs" / "QUALITY_GATES.md"

ENV_NAME_RE = re.compile(r"PHASEKIT_[A-Z0-9_]+")
# Everything under artifacts/ named by a literal path: "artifacts/<name>",
# "$ARTIFACTS_DIR/<name>", "${ARTIFACTS_DIR}/<name>", and the hook-side
# "$PHASEKIT_ARTIFACTS_DIR/<name>" (the suffix match covers all four).
ARTIFACT_REF_RE = re.compile(r"ARTIFACTS_DIR\}?/([A-Za-z0-9_.-]+)|artifacts/([A-Za-z0-9_.-]+)")


def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def swept_files():
    """The shipped runtime surface the manifest claims to cover."""
    files = sorted(REPO_ROOT.glob("scripts/*.sh"))
    files += sorted(REPO_ROOT.glob("scripts/*.py"))
    files += sorted(REPO_ROOT.glob(".claude/hooks/*.sh"))
    assert files, "sweep scope resolved to no files — the sweep is vacuous"
    return files


def strip_comments(text):
    """Drop full-line comments (bash # / python #). Inline trailing comments
    are kept — cheap lexing would misparse # inside strings, and a false
    positive here fails loudly rather than hiding an interface name."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def contains_name(text, name):
    """Exact-token match: PHASEKIT_ITER must not match PHASEKIT_ITER_RETRY."""
    return re.search(
        r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text
    ) is not None


def sweep_env_names(paths):
    found = {}
    for path in paths:
        for name in ENV_NAME_RE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, set()).add(path.name)
    return found


def sweep_artifact_names(paths):
    found = {}
    for path in paths:
        code = strip_comments(path.read_text(encoding="utf-8"))
        for match in ARTIFACT_REF_RE.finditer(code):
            name = (match.group(1) or match.group(2)).rstrip(".")
            if name:
                found.setdefault(name, set()).add(path.name)
    return found


def literal_exit_codes(path):
    text = strip_comments(path.read_text(encoding="utf-8"))
    if path.suffix == ".py":
        codes = {int(c) for c in re.findall(r"sys\.exit\((\d+)\)", text)}
        codes |= {int(c) for c in re.findall(r"^EXIT_[A-Z_]+ = (\d+)", text, re.M)}
        return codes
    return {int(c) for c in re.findall(r"\bexit\s+(\d+)\b", text)}


class ManifestShape(unittest.TestCase):
    def test_manifest_parses_and_declares_the_three_families(self):
        m = manifest()
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["interface"], "phasekit")
        for family in ("env", "artifacts", "exit_codes"):
            self.assertIn(family, m)
        self.assertTrue(m["env"] and m["artifacts"] and m["exit_codes"])

    def test_env_entries_are_well_formed_and_unique(self):
        names = []
        for entry in manifest()["env"]:
            for key in ("name", "kind", "default", "semantics", "container_forwarded"):
                self.assertIn(key, entry, f"env entry missing {key}: {entry}")
            self.assertIn(entry["kind"],
                          ("consumed", "exported", "both",
                           "internal-constant", "file-sentinel"))
            self.assertTrue(entry.get("read_by") or entry.get("set_by"),
                            f"{entry['name']}: declares neither a reader nor a setter")
            names.append(entry["name"])
        self.assertEqual(len(names), len(set(names)), "duplicate env names")

    def test_artifact_entries_are_well_formed_and_unique(self):
        names = []
        for entry in manifest()["artifacts"]:
            for key in ("name", "path", "writers", "lifecycle", "when", "keys",
                        "verdict", "transient_signal", "hidden"):
                self.assertIn(key, entry, f"artifact entry missing {key}: {entry}")
            self.assertTrue(entry["writers"])
            names.append(entry["name"])
        self.assertEqual(len(names), len(set(names)), "duplicate artifact names")


class EnvDeclaredIsRead(unittest.TestCase):
    """Direction 1: a manifest entry nothing reads is META_REPO_PATH wearing a
    contract. Every declared env name must be present in every file the entry
    names as reader or setter."""

    def test_every_declared_env_var_appears_in_each_file_it_names(self):
        for entry in manifest()["env"]:
            for rel in entry.get("read_by", []) + entry.get("set_by", []):
                path = REPO_ROOT / rel
                self.assertTrue(path.is_file(),
                                f"{entry['name']}: declared file {rel} does not exist")
                self.assertTrue(
                    contains_name(path.read_text(encoding="utf-8"), entry["name"]),
                    f"{entry['name']}: declared as read/set by {rel}, "
                    f"but that file never mentions it — a dangling declaration",
                )

    def test_container_forwarded_flags_match_container_setup(self):
        """The #83 shape hides here too: the loop consumes a var a
        containerized supervisor sets on the host, and container-setup.sh
        silently never forwards it. The flag makes that a declared fact, and
        this pin keeps the flag honest: true iff the name appears in
        container-setup.sh code."""
        code = strip_comments(CONTAINER_SETUP.read_text(encoding="utf-8"))
        for entry in manifest()["env"]:
            self.assertEqual(
                entry["container_forwarded"], contains_name(code, entry["name"]),
                f"{entry['name']}: container_forwarded is "
                f"{entry['container_forwarded']} but container-setup.sh says otherwise",
            )


class EnvReadIsDeclared(unittest.TestCase):
    """Direction 2 — the pin that catches #83 on day one: a new PHASEKIT_*
    consumer cannot be added to any shipped script or hook without declaring
    it in the manifest."""

    def test_every_phasekit_name_in_shipped_code_is_declared(self):
        declared = {e["name"] for e in manifest()["env"]}
        for name, files in sorted(sweep_env_names(swept_files()).items()):
            self.assertIn(
                name, declared,
                f"{name} (in {', '.join(sorted(files))}) is read or mentioned in "
                f"shipped code but not declared in contracts/interface.json — "
                f"declare it (or reclassify it) before shipping",
            )

    def test_the_sweep_sees_a_planted_synthetic_read(self):
        """Non-vacuity: a sweep that silently matches nothing passes forever
        (the v0.8.0 aliased-import lesson)."""
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "planted.sh"
            planted.write_text(': "${PHASEKIT_SYNTHETIC_PROBE:-}"\n', encoding="utf-8")
            found = sweep_env_names([planted])
        self.assertIn("PHASEKIT_SYNTHETIC_PROBE", found)
        self.assertNotIn(
            "PHASEKIT_SYNTHETIC_PROBE", {e["name"] for e in manifest()["env"]},
            "the probe name must stay undeclared or this proof proves nothing",
        )

    def test_internal_constants_really_are_constants_not_env_inputs(self):
        """The manifest may exempt a PHASEKIT_* name from being an input only
        by declaring it internal-constant. Keep that honest: the name must be
        assigned unconditionally in its declaring file and never expanded
        with a ${NAME:-default} / ${NAME:=default} fallback anywhere — either
        would make it an environment input in disguise."""
        constants = [e for e in manifest()["env"] if e["kind"] == "internal-constant"]
        self.assertTrue(constants, "expected at least one internal-constant entry")
        all_text = "".join(p.read_text(encoding="utf-8") for p in swept_files())
        for entry in constants:
            for rel in entry["set_by"]:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertRegex(
                    text, rf"(?m)^{entry['name']}=",
                    f"{entry['name']}: no unconditional assignment in {rel}",
                )
            self.assertNotRegex(
                all_text, r"\$\{" + entry["name"] + r"[:\-=?]",
                f"{entry['name']}: expanded with a fallback somewhere — that is "
                f"an env input, not an internal constant; redeclare it",
            )

    def test_the_file_sentinel_is_never_read_from_the_environment(self):
        """PHASEKIT_VERIFY_CONFIGURED is a line inside the project-owned
        verify script, grepped as text. If any shipped script ever expands it
        as $PHASEKIT_VERIFY_CONFIGURED it has become an env var and must be
        redeclared."""
        sentinels = [e for e in manifest()["env"] if e["kind"] == "file-sentinel"]
        self.assertTrue(sentinels, "expected the PHASEKIT_VERIFY_CONFIGURED entry")
        all_text = "".join(p.read_text(encoding="utf-8") for p in swept_files())
        for entry in sentinels:
            self.assertNotRegex(
                all_text, r"\$\{?" + entry["name"],
                f"{entry['name']}: expanded as an environment variable somewhere "
                f"— it is declared as a file sentinel; redeclare it",
            )


class ArtifactsBothDirections(unittest.TestCase):
    def test_every_artifact_referenced_in_code_is_declared(self):
        """read ⊆ declared, artifacts leg. Swept over comment-stripped code so
        prose mentions (e.g. the orchestrator's own iteration-mode.json, which
        is deliberately NOT phasekit interface) don't force declarations."""
        declared = {e["name"] for e in manifest()["artifacts"]}
        for name, files in sorted(sweep_artifact_names(swept_files()).items()):
            self.assertIn(
                name, declared,
                f"artifacts/{name} (in {', '.join(sorted(files))}) is referenced "
                f"in shipped code but not declared in contracts/interface.json",
            )

    def test_every_declared_artifact_is_referenced_somewhere(self):
        """declared ⊆ read. Comments and docs/QUALITY_GATES.md count as
        references here: ready-to-deploy.json is deliberately never touched by
        phasekit's scripts (it is the supervisor's deploy trigger, defined by
        phasekit's workflow docs), so its anchor is the doc that defines it."""
        corpus = "".join(p.read_text(encoding="utf-8") for p in swept_files())
        corpus += QUALITY_GATES.read_text(encoding="utf-8")
        for entry in manifest()["artifacts"]:
            self.assertTrue(
                contains_name(corpus, entry["name"]),
                f"{entry['name']}: declared but never mentioned in any shipped "
                f"script, hook, or docs/QUALITY_GATES.md — a dangling declaration",
            )

    def test_the_artifact_sweep_sees_a_planted_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "planted.sh"
            planted.write_text('cp x "$ARTIFACTS_DIR/synthetic-probe.json"\n',
                               encoding="utf-8")
            found = sweep_artifact_names([planted])
        self.assertIn("synthetic-probe.json", found)

    def _bash_array(self, name):
        text = LOOP.read_text(encoding="utf-8")
        match = re.search(rf"(?m)^{name}=\((.*?)^\)", text, re.S)
        self.assertIsNotNone(match, f"{name} array not found in run-until-done.sh")
        items = re.findall(r'"([^"]+)"', match.group(1))
        self.assertTrue(items, f"{name} array parsed empty — parser is vacuous")
        return set(items)

    def test_verdict_vocabulary_matches_the_loops_array(self):
        """The loop's VERDICT_ARTIFACTS array is the single source of truth
        for 'what counts as a session ending with an answer' (it is exported
        to the Stop hook for exactly that reason). The manifest must carry the
        same set, or a consumer waiting on a verdict disagrees with the loop."""
        declared = {e["name"] for e in manifest()["artifacts"] if e["verdict"]}
        self.assertEqual(declared, self._bash_array("VERDICT_ARTIFACTS"))

    def test_transient_signal_set_matches_the_loops_array(self):
        declared = {e["name"] for e in manifest()["artifacts"] if e["transient_signal"]}
        self.assertEqual(declared, self._bash_array("TRANSIENT_SIGNALS"))

    def test_hidden_set_matches_the_loops_array(self):
        declared = {e["name"] for e in manifest()["artifacts"] if e["hidden"]}
        self.assertEqual(declared, self._bash_array("HIDDEN_TRANSIENTS"))


class ExitCodes(unittest.TestCase):
    def test_no_script_exits_a_literal_code_the_manifest_omits(self):
        """read ⊆ declared, exit-code leg, for every script the manifest
        covers. (Passthrough codes are non-literal — exit "$rc" — and implicit
        exit 0 leaves no literal, so only this direction is mechanical.)"""
        for rel, spec in manifest()["exit_codes"].items():
            if rel == "conventions":
                continue
            declared = {int(c) for c in spec["codes"]}
            found = literal_exit_codes(REPO_ROOT / rel)
            self.assertLessEqual(
                found, declared,
                f"{rel} exits literal code(s) {sorted(found - declared)} the "
                f"manifest does not declare",
            )

    def test_hook_exit_codes_are_pinned_exactly(self):
        """The hooks are small enough for equality: every declared code is a
        literal and every literal is declared."""
        for rel in (".claude/hooks/require-verdict.sh",
                    ".claude/hooks/deny-dangerous-commands.sh",
                    ".claude/hooks/compact-reanchor.sh"):
            declared = {int(c) for c in manifest()["exit_codes"][rel]["codes"]}
            self.assertEqual(declared, literal_exit_codes(REPO_ROOT / rel), rel)

    def test_checker_exit_constants_match_the_manifest(self):
        """phasekit-contracts.py's EXIT_* constants are its contract; the
        manifest adds only code 5, which the LOOP emits on its behalf when the
        checker itself is missing (fail-closed, v0.7.1)."""
        constants = {
            int(c) for c in re.findall(
                r"(?m)^EXIT_[A-Z_]+ = (\d+)",
                CHECKER.read_text(encoding="utf-8"))
        }
        self.assertEqual(constants, {0, 2, 3, 4},
                         "checker constants changed — update the manifest and this pin")
        spec = manifest()["exit_codes"]["scripts/phasekit-contracts.py"]
        self.assertEqual({int(c) for c in spec["codes"]}, constants | {5})
        self.assertIn("LOOP", spec["codes"]["5"].upper(),
                      "code 5's entry must say the loop, not the checker, emits it")
        self.assertRegex(
            LOOP.read_text(encoding="utf-8"),
            r'record_verify_failure\s+"phasekit upgrade"\s+"contracts"\s+5\b',
            "the loop no longer records synthetic exit 5 for a missing checker — "
            "update the manifest's code-5 entry",
        )

    def test_the_loop_and_wrapper_scripts_are_covered(self):
        """The manifest must cover at least the surfaces a supervisor waits
        on; deleting one of these entries is an interface change, not tidying."""
        covered = set(manifest()["exit_codes"])
        for rel in ("scripts/run-until-done.sh", "scripts/run-phase.sh",
                    "scripts/container-setup.sh", "scripts/phasekit-contracts.py",
                    "conventions"):
            self.assertIn(rel, covered)
        self.assertIn("124", manifest()["exit_codes"]["conventions"],
                      "the 124-means-timeout convention is part of the contract")


class ShipsDownstream(unittest.TestCase):
    """The vendored loop in every managed repo IS the thing this manifest
    describes, so the manifest ships downstream with it (scaffold class).
    A supervisor of a managed project codes against the project's own copy."""

    def test_registered_in_always_installed_file_paths(self):
        spec = importlib.util.spec_from_file_location(
            "enrich_ifmanifest_test", REPO_ROOT / "scripts" / "enrich-project.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertIn("contracts/interface.json", module.ALWAYS_INSTALLED_FILE_PATHS)

    def test_registered_in_the_capability_manifest(self):
        self.assertIn("contracts/interface.json",
                      CAPABILITIES.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
