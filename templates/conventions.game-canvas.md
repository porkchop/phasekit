# Stack conventions — game-canvas

> Stack conventions seeded by the `game-canvas` profile. This file is
> **project-owned**: phasekit wrote it once and never overwrites it. Amend it
> in place when this project's reality differs from the stack default — a
> correction belongs here. When phasekit's template
> (`templates/conventions.game-canvas.md`) changes, `phasekit check
> --include-templates` reports it as advisory; adopt what fits.

## The contract

A game-canvas project is a browser game rendered to `<canvas>`. It is not a
static-web project: it may have a build step (a bundler, TypeScript, an
asset pipeline) and runtime dependencies (a renderer, a physics or audio
library), both under the policy below. What makes it a game-canvas project
are the game rules that follow: a deterministic core split from rendering, a
fixed-timestep loop, seedable randomness, and a unit-tested core.

## Dependency policy

- Runtime dependencies are an **allowlist, not a prohibition** — the
  python-uv policy applied to npm. Prefer the platform; every runtime
  dependency is an architecture decision, not a convenience.
- Each one is declared, per workspace, in `runtime-dependencies.json` at the
  repo root, naming the ADR that decided it:

  ```json
  {
    ".": {},
    "packages/render": {"pixi.js": "docs/adr/ADR-0003-pixi-renderer.md"}
  }
  ```

  Keys are workspace directories as the root `package.json`'s `workspaces`
  resolves them (`.` is the root itself); values map a dependency name to its
  ADR. **One ADR per addition**, reviewed like any architecture change. No
  file means an empty allowlist.
- The verify gate reads `dependencies` and `optionalDependencies` in the root
  `package.json` and in every workspace's, and rejects a dependency with no
  entry and an entry whose ADR file does not exist. Another workspace's
  package (by its `name`) needs no entry. Dev tooling goes in
  `devDependencies`, which the policy does not cover.
- Versions live in `package.json` and the committed lockfile, never in code.

## Build

- A build step is allowed. Record it in `docs/ARCHITECTURE.md` — the
  command, what it emits, what deploys — and keep build outputs out of git.
- Without one, the repo deploys as-is: `index.html` at the root, plain
  browser ESM, relative imports with the `.js` extension (the seeded gate
  checks that every relative import in `.js`/`.mjs` files resolves).

## Engine / rendering split

- The **deterministic core** — game rules, simulation, entity state, scoring,
  RNG — lives in pure ES modules with no `document`, `window`, or canvas
  references. This is what the engine-builder agent owns.
- The **rendering layer** is thin: it reads state and draws. Input handling
  translates events into game commands; it never mutates game state directly.
- Game state stays serializable (plain data) — it makes save/restore, replay,
  and testing cheap.

## The loop

- Fixed-timestep simulation update; `requestAnimationFrame` for rendering.
  Never step the simulation from rAF deltas directly — variable timesteps
  make behavior frame-rate-dependent and untestable.
- All randomness flows through a seedable RNG module so tests can replay
  deterministic runs.

## Testing

- Unit-test the deterministic core in node (`node --test` / `npm test`) —
  rules, collisions, scoring, edge cases. No canvas required; that's the
  point of the split. If the core itself is compiled (TypeScript), the verify
  gate builds it before running its tests.
- Rendering and input are verified in a real browser at phase boundaries
  (qa-playwright), not in the pre-commit gate.

- Tests are hermetic: a test reads the tree and declared fixtures, never git
  history (no `git log`/`rev-list`/`show <rev>:`/`blame` of this repository).
  A fact about a past phase is an evidence file — phasekit commits
  `artifacts/iterations/<N>/<phase>.json` at every phase close — or a golden
  file (docs/QUALITY_GATES.md "Hermetic tests").

## Quality bar

- The pre-commit gate (`scripts/phasekit-verify.sh`) runs unit tests, the
  dependency-allowlist check, and the import-graph check. Keep it green
  within the verify budget (`docs/QUALITY_GATES.md` "Verify budget"): a fast
  tier per commit, the full suite at the verification sprint and at
  completion.
