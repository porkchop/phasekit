"""Every Python file phasekit ships downstream must pass a downstream project's lint.

WHY. A downstream project's verify gate lints its WHOLE tree — foundry-orchestrator
runs `ruff check .` at line-length 100 with E,F,I,B,UP,W — and a vendored scaffold
file is part of that tree. v0.15.0 shipped `scripts/phasekit-roadmap.py` with 12
violations under that config (E501, B904, UP017); the fleet rollout would have
turned the orchestrator's next session red on a file it does not own, and
`phasekit upgrade` commits run no gate. Caught at rollout, fixed in v0.15.1.

HOW, WITHOUT A LINTER. phasekit is stdlib-only and the release host has no ruff,
so a ruff-based test would skip exactly where it matters. This pins, with the
standard library, the two rules that fired and are the likeliest to recur:

  * E501  — no line longer than 100 columns;
  * B904  — no `raise X` inside an `except` clause without `from`.

The full ruff pass is a release-gate step (docs/RELEASING.md), run with the
strictest fleet consumer's own config.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENRICH = REPO_ROOT / "scripts" / "enrich-project.py"
MAX_COLUMNS = 100


def _shipped_python_files() -> list[Path]:
    spec = importlib.util.spec_from_file_location("enrich_downstream_lint_test", ENRICH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [REPO_ROOT / rel for rel in module.ALWAYS_INSTALLED_FILE_PATHS if rel.endswith(".py")]


def _unchained_raises(tree: ast.AST) -> list[int]:
    """Line numbers of `raise X` inside an except handler with no `from` —
    not counting a bare `raise`, and not descending into nested functions,
    whose raises are not "inside the except clause"."""
    found = []

    def walk(node: ast.AST, in_handler: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                  ast.ClassDef)):
                walk(child, False)
            elif isinstance(child, ast.ExceptHandler):
                walk(child, True)
            elif isinstance(child, ast.Raise):
                if in_handler and child.exc is not None and child.cause is None:
                    found.append(child.lineno)
                walk(child, in_handler)
            else:
                walk(child, in_handler)

    walk(tree, False)
    return found


class DownstreamLint(unittest.TestCase):
    def test_there_are_shipped_python_files_to_check(self):
        names = {p.name for p in _shipped_python_files()}
        self.assertIn("phasekit-roadmap.py", names)
        self.assertIn("phasekit-contracts.py", names)

    def test_no_line_exceeds_a_downstream_line_length(self):
        for path in _shipped_python_files():
            long = [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                    if len(line) > MAX_COLUMNS]
            self.assertEqual(long, [], f"{path.name}: lines over {MAX_COLUMNS} columns")

    def test_every_raise_in_an_except_clause_is_chained(self):
        for path in _shipped_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            self.assertEqual(_unchained_raises(tree), [],
                             f"{path.name}: raise inside except without `from`")

    def test_the_raise_check_catches_what_it_claims(self):
        bad = ast.parse("try:\n    x()\nexcept ValueError:\n    raise KeyError('k')\n")
        good = ast.parse("try:\n    x()\nexcept ValueError as e:\n    raise KeyError('k') from e\n"
                         "except OSError:\n    raise\n")
        nested = ast.parse("try:\n    x()\nexcept ValueError:\n"
                           "    def f():\n        raise KeyError('k')\n")
        self.assertEqual(_unchained_raises(bad), [4])
        self.assertEqual(_unchained_raises(good), [])
        self.assertEqual(_unchained_raises(nested), [])


if __name__ == "__main__":
    unittest.main()
