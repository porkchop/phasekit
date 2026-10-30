# Design system

The chosen visual direction, made concrete (from `docs/project/DESIGN_BRIEF.md`). Build sessions use
these tokens and components and do not invent new ones: a missing token or component is a finding to
add here, not an improvisation in code. `phasekit ui-check` holds the deterministic part (accessibility,
tap targets, fonts, visual baselines); the `design-reviewer` agent judges the rest against this file.

## Tokens

### Colour
Name, value (light / dark), and use. List the text/background pairs this system promises meet
WCAG AA contrast (4.5:1 for body text, 3:1 for large text and UI parts).

### Type
Families (system fonts, or bundled fonts also listed under `fonts` in `docs/project/ui-pages.json`),
the type scale, weights, and where tabular numerals are used.

### Spacing, radii, motion
The spacing scale, corner radii, and motion durations (and what never animates).

## Components

Each component with its states: default, hover, focus, pressed, disabled, error.

## Layout

Phone (390 px wide) and desktop (1440 px wide): what each shows, and what changes between them.
Tap targets are at least 24 px (blocking) and should be 44 px.

## Don'ts

Named anti-patterns, from the brief's "Must not".
