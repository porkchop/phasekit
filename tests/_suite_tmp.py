"""Every test runs under its own TMPDIR, inside one directory this module removes (v0.18.5).

The suite drives the loop hundreds of times, and the loop, the stub model and
the tests themselves make temporaries with `mktemp` and `tempfile`. Before
v0.18.5 a full run left ~1,226 empty tmp.* files in $TMPDIR; release sessions
running it repeatedly as one host user left 122k in /tmp (2026-10-01, part of
a host OOM diagnosis). Now:

  * on import, the suite gets ONE directory, `phasekit-suite-*` under the
    TMPDIR it was started with, and TMPDIR (for every child process) and
    `tempfile.tempdir` (for this process) point into it;
  * every test (`unittest.TestCase.run`) gets its own subdirectory, removed
    when the test ends;
  * the suite directory is removed when the process exits.

So a run leaves its starting TMPDIR as it found it. A SIGKILLed run leaves one
directory, never a flood. One mechanism, wired by one import line in every
tests/test_*.py (and tests/__init__.py for `python3 -m unittest tests.x`):
`unittest discover -s tests` imports the test modules only, so there is no
package hook it would run. tests/test_suite_tmpdir.py pins that every module
imports this one and that a run leaves TMPDIR empty.
"""

import atexit
import os
import shutil
import stat
import sys
import tempfile
import unittest

_MARK = "_phasekit_suite_tmp"


def _rmtree(path):
    """Remove `path`, making read-only directories writable on the way."""
    def retry(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), stat.S_IRWXU)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, stat.S_IRWXU)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:  # phasekit's floor is Python 3.9
        shutil.rmtree(path, onerror=retry)


def _install():
    if getattr(unittest.TestCase.run, _MARK, False):
        return
    root = tempfile.mkdtemp(prefix="phasekit-suite-", dir=tempfile.gettempdir())
    owner = os.getpid()

    def _cleanup():
        if os.getpid() == owner:  # never from a forked child
            _rmtree(root)
    atexit.register(_cleanup)
    os.environ["TMPDIR"] = root
    tempfile.tempdir = root
    original = unittest.TestCase.run

    def run(self, result=None):
        own = tempfile.mkdtemp(prefix="test-", dir=root)
        saved_env, saved = os.environ.get("TMPDIR"), tempfile.tempdir
        os.environ["TMPDIR"], tempfile.tempdir = own, own
        try:
            return original(self, result)
        finally:
            if saved_env is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved_env
            tempfile.tempdir = saved
            _rmtree(own)
    setattr(run, _MARK, True)
    unittest.TestCase.run = run


_install()
