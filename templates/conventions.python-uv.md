# Stack conventions — python-uv

> Fleet-consistent conventions installed by the `python-uv` profile.
> This file is **scaffold-owned**: it propagates via `phasekit upgrade` and is
> drift-checked. Propose changes upstream in phasekit (`templates/
> conventions.python-uv.md`) instead of editing it here.

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
  `uv sync --extra dev` → `ruff check` → `mypy` → `pytest -q` and must stay
  green and fast (< ~30s). Long integration/E2E suites belong to the
  verification sprint, not this gate.

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
