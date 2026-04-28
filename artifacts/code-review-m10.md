# Code review — M10 implementation

## Summary
GREEN-LIGHT WITH CONDITIONS

The implementation is small, correct, and lives in the right places. It composes cleanly with M9: profile inheritance via `extends: default` matches the existing `game-project` and `saas-project` pattern, the doc entry slots into the existing `docs:` section with a sensible class (`bootstrap-frozen`), and `DOC_TEMPLATE_MAP` is the established hook for "render this template into a downstream doc" — no parallel mechanism was invented. `--self-check` passes (82 tracked files; +1 `scaffold-template`). The default-profile no-regression test gives genuine bite to the "purely additive" claim. The conditions below are mostly tightening test/docs coverage to match what META_PHASES.md actually says, plus one factual mismatch in commit/test counts.

## Findings

- **Finding 1 — [BLOCKER] Acceptance criterion "existing project opts in via manifest edit + --upgrade" is not exercised by any test**
  - META_PHASES.md:598 explicitly demands a test for the upgrade path: "An existing project can opt in by editing its `.scaffold/manifest.json` profile to `with-design` and running `--upgrade`; the new file installs and shows up in the manifest with `bootstrap-frozen` ownership." Both M10 tests (`tests/test_m9_manifest.py:878,893`) cover only fresh-enrichment paths. The upgrade-mediated install is the most failure-prone of the three install paths (fresh / re-run / upgrade) and is the one a real user with an existing M9-era project will actually take. Per the testing gate, "new behavior with no test that would fail if reverted" is a blocker.
  - Recommended fix: add `test_existing_project_can_opt_in_via_upgrade` that (a) enriches with `default`, asserts no `docs/DESIGN.md`, (b) edits `.scaffold/manifest.json` profile to `with-design`, (c) runs `--upgrade --yes`, (d) asserts `docs/DESIGN.md` exists, manifest entry has `ownership: bootstrap-frozen`, and `--check` is clean afterward.

- **Finding 2 — [MAJOR] Test count claim is inaccurate; spec acceptance criterion not literally met**
  - `docs/META_PHASES.md:601` requires "28 (or more) M9 tests still green." The actual M9 count in `tests/test_m9_manifest.py` is 24 (verified by class enumeration). Total is 26 (24 M9 + 2 M10). The commit message also claims "30 tests green (28 M9 + 2 M10)," which is factually wrong.
  - This is at least a documentation/spec mismatch. Either (a) META_PHASES.md's "28+" target was already off by the time M10 started (in which case the spec line is stale and should be relaxed to "all M9 tests still green"), or (b) the commit/spec were copying a number nobody verified. Worth resolving so the next phase's acceptance audit doesn't hit the same drift.
  - Recommended fix: update `docs/META_PHASES.md:601` to "all existing M9 tests still green" (factual and not bound to a brittle count), and correct the count language wherever it appears in the M10 approval artifact.

- **Finding 3 — [MAJOR] CAPABILITY_MANIFEST.md not updated for the new profile and doc class membership**
  - `docs/CAPABILITY_MANIFEST.md` contains zero references to `with-design` or DESIGN (verified by grep). Per the project's stated convention, CAPABILITY_MANIFEST.md is the schema doc — `bootstrap-frozen` membership and the new profile are exactly the kind of additions that belong there. Skipping the update means a future contributor or agent reading CAPABILITY_MANIFEST.md will see an incomplete picture of what classes apply to what files and what profiles ship.
  - Recommended fix: add a line under the bootstrap-frozen section listing `docs/DESIGN.md` (with the "if `with-design` profile" qualifier) and a line under the profiles section describing `with-design` as "extends `default`; adds `DESIGN` to `include_docs`; opt-in only."

- **Finding 4 — [MINOR] Spec-mandated agent-description phrasing is paraphrased, not adopted verbatim, and reviewer-required language is missing**
  - META_PHASES.md:591 specifies the strategy-planner addition include the phrase "produces or updates `docs/DESIGN.md` when one exists." The implementation at `.claude/agents/strategy-planner.md:24` says "read it first … update after material changes" but does not say *produces*. Similarly META_PHASES.md:592 calls for "reviews the steady-state design in `docs/DESIGN.md` alongside decision memos"; `.claude/agents/architecture-red-team.md:25` says "review it alongside any decision memo" — fine in spirit, but neither agent description tells the agent it is responsible for *creating* DESIGN.md when the user opts in. A reader of strategy-planner.md alone would conclude the agent only *updates* an existing design.
  - Recommended fix: add an explicit "produces `docs/DESIGN.md` for the `with-design` profile, and updates it after material decisions" line to strategy-planner. The architecture-red-team prose is acceptable as is.

- **Finding 5 — [MINOR] Template has a few lines over 80 columns**
  - `templates/design.template.md` lines 16, 18, 20, 21, and 58 are 82–108 columns (verified by `awk` width check). Lines 16/18/20/21 are inside the ASCII box diagram so the visual width is intentional — no fix required, but the M10 acceptance criterion ("fits on one screen at 80-column width") is technically violated. Line 58 (the "Open questions" bullet) is plain prose at 82 columns and could be trivially wrapped.
  - Recommended fix: rewrap line 58. Optionally tighten the ASCII diagram to ≤80 columns; if not, add a comment in the template noting the diagram is the only intentional 80+col content so a future contributor doesn't "fix" it.

- **Finding 6 — [MINOR] Test asserts "<60 lines" as a proxy for "fits on one screen at 80-column width"; column width is unverified**
  - `tests/test_m9_manifest.py:910` asserts line count <60 but does not assert column width. The M10 spec states both constraints. Given the box-drawing characters used in the template, a line-count-only check leaves the column-width regression vector unguarded. A future innocent edit that pushes a section to 130 columns would not fail tests.
  - Recommended fix: add an assertion that no line in the rendered template exceeds, say, 100 columns (generous enough to permit box-drawing char width but tight enough to catch real overflow). Alternatively, document explicitly in the test docstring that line count is the chosen proxy and column-width is enforced manually.

- **Finding 7 — [MINOR] CLAUDE.template.md "Optional references" section is appended but lacks a trailing newline**
  - `templates/CLAUDE.template.md` ends with `{{OPTIONAL_REFERENCES}}` and "no newline at end of file" (per the diff). Pre-existing; M10 didn't introduce it but didn't fix it either. Cosmetic.

## Acceptance criteria audit

| # | Criterion | Verdict |
|---|---|---|
| 1 | Default profile produces project WITHOUT `docs/DESIGN.md` | PASS — covered by `test_default_profile_does_not_install_design` |
| 2 | `with-design` profile produces project WITH rendered `docs/DESIGN.md` | PASS — covered by `test_with_design_profile_installs_rendered_design` |
| 3 | Rendered template <60 lines and fits 80-col | PARTIAL — 58 lines (passes); 80-col asserted only by manual inspection and 4 lines technically exceed it (Finding 5). Test does not enforce column width (Finding 6). |
| 4 | Existing project opts in via manifest edit + `--upgrade` | NOT EXERCISED — no test covers this path (Finding 1, blocker). |
| 5 | USAGE_PATTERNS has "When to use DESIGN.md" with explicit skip-for-trivial guidance | PASS — section is present with falsifiable use/skip lists and accurate opt-in instructions. |
| 6 | `--self-check` passes after manifest changes | PASS — verified by hand: 82 tracked files, 0 unclassified, 0 double-classified. |
| 7 | 28+ M9 tests still green | PARTIAL — all M9 tests green, but the actual count is 24 (Finding 2). Either the spec is stale or the count was never verified. |

## Verdict

**Request changes.** One blocker (Finding 1: the upgrade-opt-in test the spec literally requires), and two should-fix-soon items (Findings 2 and 3: factual mismatch in test counts and missing CAPABILITY_MANIFEST.md update). The remaining items are minor polish that can land in M10.x. The implementation itself is solid and the additive contract is honored — once the upgrade-path test is added and the count/schema-doc drift is addressed, this should be straightforward to green-light.
