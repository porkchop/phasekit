# Stack conventions — static-web

> Fleet-consistent conventions installed by the `static-web` profile.
> This file is **scaffold-owned**: it propagates via `phasekit upgrade` and is
> drift-checked. Propose changes upstream in phasekit (`templates/
> conventions.static-web.md`) instead of editing it here.

## The contract

A static-web project is a set of files a dumb static server can host:
`index.html` + browser-native ES modules + CSS. No build step, no bundler,
no framework. What's in the repo is what ships.

## Dependencies

- **Zero runtime dependencies.** The browser loads plain ESM; nothing is
  installed to deploy. Breaking this rule requires an ADR.
- `package.json`, if present, exists only for dev convenience (a `test`
  script). Dev tooling goes in `devDependencies`; `dependencies` must stay
  empty — the verify gate asserts this.

## Modules and imports

- ES modules everywhere (`<script type="module">`); no globals except what
  the platform provides.
- Relative imports **must include the `.js` extension** — browsers don't
  resolve extensionless specifiers. The verify gate checks every relative
  import resolves to a real file.
- Keep the module graph shallow: an entry module wires things up; leaf
  modules export pure logic.

## Testing

- Logic is separated from DOM so it can run in node: pure modules are tested
  with the built-in `node:test` runner (`node --test`, or `npm test` when a
  test script exists) and `node:assert`.
- DOM glue stays thin and is exercised by browser verification at phase
  boundaries (qa-playwright), not by the pre-commit gate.
- Test files: `*.test.js`, colocated or under `tests/`.

## Layout

- `index.html` at the repo root (the deploy target serves the repo as-is).
- One CSS file (or a small, purposeful set); no preprocessors.
- PWA extras (`manifest.json`, service worker) are welcome but optional;
  keep the service worker cache list in sync with the files that exist.

## Quality bar

- The pre-commit gate (`scripts/phasekit-verify.sh`) runs unit tests, the
  no-dependency assertion, and the import-graph check. Keep it green and
  fast (< ~30s).
