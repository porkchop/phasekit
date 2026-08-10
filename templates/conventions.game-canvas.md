# Stack conventions — game-canvas

> Fleet-consistent conventions installed by the `game-canvas` profile.
> This file is **scaffold-owned**: it propagates via `phasekit upgrade` and is
> drift-checked. Propose changes upstream in phasekit (`templates/
> conventions.game-canvas.md`) instead of editing it here.

## The contract

A game-canvas project is a static-web project (see below) whose product is a
browser game rendered to `<canvas>`. Everything a static server can't host is
out of scope; everything the static-web contract requires still applies.

## Static-web rules (inherited)

- **Zero runtime dependencies** — plain browser ESM, no engine libraries, no
  bundler. Breaking this requires an ADR. `package.json` is dev-only and its
  `dependencies` must stay empty (the verify gate asserts this).
- Relative imports include the `.js` extension; the verify gate checks the
  import graph resolves.
- `index.html` at the repo root; the repo deploys as-is.

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
  point of the split.
- Rendering and input are verified in a real browser at phase boundaries
  (qa-playwright), not in the pre-commit gate.

## Quality bar

- The pre-commit gate (`scripts/phasekit-verify.sh`) runs unit tests, the
  no-dependency assertion, and the import-graph check. Keep it green and
  fast (< ~30s).
