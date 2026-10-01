# Stack conventions — python-uv

> Stack conventions seeded by the `python-uv` profile. This file is
> **project-owned**: phasekit wrote it once and never overwrites it. Amend it
> in place when this project's reality differs from the stack default — a
> correction belongs here. When phasekit's template
> (`templates/conventions.python-uv.md`) changes, `phasekit check
> --include-templates` reports it as advisory; adopt what fits.

## Toolchain

- **uv** manages the environment. No hand-managed venvs, no pip installs
  outside `pyproject.toml`.
- `pyproject.toml` is the single source of truth for metadata, dependencies,
  and tool config. Dev tools live in the `dev` optional group:

  ```toml
  [project.optional-dependencies]
  dev = ["pytest", "ruff", "mypy"]
  ```

- Commit `uv.lock`. Don't regenerate it casually — lockfile churn hides real
  dependency changes in review.

## Layout

- One top-level package directory named after the project (underscores, not
  hyphens). Scripts that aren't importable code go in `scripts/`.
- Tests in `tests/`, files named `test_*.py`, run with pytest. Test the
  behavior at the boundary you own (functions/HTTP handlers), not internals.

## Quality bar

- **ruff** for lint (line length 100 unless the repo says otherwise).
- **mypy** for types — prefer `strict = true` scoped to the package via
  `files = [...]` in `[tool.mypy]`. New code lands typed; don't accumulate
  `# type: ignore` without a comment saying why.
- The pre-commit gate (`scripts/phasekit-verify.sh`) runs
  `uv sync --extra dev` → `ruff check` → `mypy` → `pytest` and must stay
  green within the verify budget (`docs/QUALITY_GATES.md` "Verify budget"):
  the fast tier per commit (`-m "not slow"`), the full suite at the
  verification sprint and at completion.

- Tests are hermetic: a test reads the tree and declared fixtures, never git
  history (no `git log`/`rev-list`/`show <rev>:`/`blame` of this repository).
  A fact about a past phase is an evidence file — phasekit commits
  `artifacts/iterations/<N>/<phase>.json` at every phase close — or a golden
  file (docs/QUALITY_GATES.md "Hermetic tests").
- Tests read the declared surface: a test reads this project's own tree and
  phasekit's DECLARED surface (`contracts/interface.json` `facts`, or
  `bash scripts/phasekit.sh facts --json`), never the files phasekit owns (the
  vendored loop and scripts, the hooks, the scaffold docs). A fact a test
  needs that the surface lacks is a request to phasekit, not a parse
  (docs/QUALITY_GATES.md "Tests read the declared surface").

## Dependency policy

- Prefer the stdlib. Every new runtime dependency needs a one-line
  justification in the SPEC or an ADR — pulling in a framework is an
  architecture decision, not a convenience.
- Pin nothing in code; versions live in `pyproject.toml`/`uv.lock`.

## Idioms

- Small modules with explicit `__all__`-free public surfaces; avoid
  `from x import *`.
- Configuration via environment variables read in one place (a `config.py`
  or equivalent), never scattered `os.environ` lookups.
- Logging via the stdlib `logging` module; no bare `print` in library code
  (CLI entrypoints may print).
