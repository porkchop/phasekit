# Stack conventions — docs-only

> Fleet-consistent conventions installed by the `docs-only` profile.
> This file is **scaffold-owned**: it propagates via `phasekit upgrade` and is
> drift-checked. Propose changes upstream in phasekit (`templates/
> conventions.docs-only.md`) instead of editing it here.

## The contract

A docs-only project ships markdown, and nothing but markdown. No build step,
no site generator, no code. The repo must read well raw — in a terminal, on
a git forge, or pasted into an LLM.

## Structure

- Every document is reachable by links from `README.md` (directly or through
  one hop). A doc nobody links to is a doc nobody finds.
- One document, one purpose. When a file starts serving two audiences,
  split it and cross-link.
- Top-level, load-bearing docs use `UPPERCASE.md` names (`README.md`,
  `ARCHITECTURE.md`, `ROADMAP.md`); supporting material lives in
  subdirectories with ordinary names.
- Prefer editing an existing doc over adding a new one; a small, stable set
  of documents beats a sprawl of stale ones.

## Links

- Internal references use **relative markdown links**
  (`[text](OTHER-DOC.md)`, `[section](DOC.md#heading)`) — never absolute
  paths or forge-specific URLs, which break when the repo moves.
- The pre-commit gate (`scripts/phasekit-verify.sh`) fails on broken internal
  links and dangling `#anchors`. External URLs are out of scope — the gate
  never fetches the network.
- When renaming or removing a heading, fix the references in the same
  commit; the gate will catch stragglers.

## Writing conventions

- State facts with dates when recording decisions or status ("decided
  2026-08-10"), so staleness is visible.
- Keep a single source of truth: one doc owns each fact; others link to it
  rather than restating it.
- Wrap prose at a consistent width (the fleet default is ~80 columns) so
  diffs stay reviewable.

## Quality bar

- The pre-commit gate checks internal links + references only (no prose
  lint, no spellcheck — deliberate). Keep it green.
