"""`python3 -m unittest tests.test_x` imports this package first: put tests/ on
sys.path so every module's `import _suite_tmp` resolves (under `unittest
discover -s tests` it already is), and install the per-test TMPDIR."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _suite_tmp  # noqa: E402,F401
