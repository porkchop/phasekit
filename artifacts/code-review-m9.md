# Code review — M9 implementation

## Summary

**GREEN-LIGHT WITH CONDITIONS.**

24/24 M9 tests pass; the four red-team v2 BLOCKERs (F1/F2/F3/F7) all have shipped, falsifiable acceptance tests; the schema is honest (the conditional NI-2 — `templates/settings.template.json` — was materialized in Slice B.1). However, M9 introduces one ownership class (`scaffold-orphan`) that bypasses the centralized taxonomy, leaves three hardcoded path lists that the memo's §10 Slice C explicitly committed to migrating into the manifest, and ships two Slice B.1 templates (`AGENTS.template.md`, `settings.template.json`) that are written to disk but never actually used by `cmd_enrich`. Phase approval is recommended once the BLOCKERs below are fixed (or explicitly deferred to M9.x with a tracking note in `phase-approval.json`).

## Findings

### Finding 1 — [BLOCKER] `scaffold-orphan` is a sixth ownership class that bypasses `OWNERSHIP_CLASSES` validation

**What's wrong.** `scripts/enrich-project.py:874` writes `ownership: "scaffold-orphan"` into the downstream manifest when an upgrade plan downgrades a removed scaffold file. `cmd_uninstall` at line 1042 hardcodes `"scaffold-orphan"` as a deletable class. But:

- `OWNERSHIP_CLASSES` (line 306) is a frozenset of exactly five classes; `scaffold-orphan` is NOT in it.
- `collect_classified_paths` raises `RuntimeError` for any class not in `OWNERSHIP_CLASSES` (lines 406, 419). If a downstream manifest containing a `scaffold-orphan` entry were ever fed back through `collect_classified_paths` (e.g. for symmetry with the scaffold-side audit), it would explode.
- The class is undocumented in `docs/CAPABILITY_MANIFEST.md`, `docs/INSTALL_LIFECYCLE.md`, and the YAML's class commentary.

**Why it matters.** The memo §2 promises a 5-class taxonomy enforced centrally. This is a sixth class smuggled in via two hardcoded string literals. Exactly the kind of duplication the DRY gate flags ("logic that encodes the same invariant in two places... silently rots when one side changes"). If a future contributor reads `OWNERSHIP_CLASSES` as authoritative and removes the orphan-handling branch from `cmd_uninstall`, orphan files leak forever.

**Fix.** Add `"scaffold-orphan"` to `OWNERSHIP_CLASSES` (or split `OWNERSHIP_CLASSES_SCAFFOLD_SIDE` from `OWNERSHIP_CLASSES_DOWNSTREAM`); document the class in `docs/CAPABILITY_MANIFEST.md`; reference the constant from `cmd_uninstall` instead of the literal.

Cites blocking criterion: *hidden constants or magic values with no named constant or configuration*.

---

### Finding 2 — [BLOCKER] Three hardcoded copy lists in `cmd_enrich` were supposed to migrate into the manifest

The decision memo §10 Slice C committed to retiring hardcoded copy lists in favor of the manifest. That migration is partial. Specifically:

**a. `cmd_enrich` lines 1763–1769** hardcode the container files list. It is duplicated in `ALWAYS_INSTALLED_FILE_PATHS` at lines 1349–1357 (used by `enumerate_install_targets`). Two-way sync hazard.

**b. `cmd_enrich` lines 1750** hardcodes `["CONTINUE_PROMPT.txt"]` as workflow root files. Also in `ALWAYS_INSTALLED_FILE_PATHS`. Two-way sync hazard.

**c. `cmd_enrich` lines 1661–1667** declares an inline `template_map` and `scaffold_only_docs`. These are duplicates of `DOC_TEMPLATE_MAP` (line 1333) and `SCAFFOLD_ONLY_DOCS` (line 1341). They happen to agree today; they will silently diverge when a new template is added.

**d. `cmd_enrich` line 1726** hardcodes `workflow_scripts = ["run-phase", "run-until-done"]`, duplicating `WORKFLOW_SCRIPTS` at line 1345.

The constants exist; they are just not used by the function whose behavior they're supposed to mirror.

**Why it matters.** The whole point of M9 is "manifest is the single source of truth for what gets installed." If `cmd_enrich` and `enumerate_install_targets` ever disagree, `--reconcile` and `--upgrade` will see a different installed-set than what `cmd_enrich` actually wrote — and the bug will appear as "manifest has stale entries / missing entries" in production. The DRY gate explicitly rejects this kind of duplication.

**Fix.** Have `cmd_enrich` consume `enumerate_install_targets(manifest, resolved)` directly: walk the spec list, copy each file (handling `rendered_from` via the existing `render_claude_md` path or a generalized renderer), and never list paths twice. The current ~150-line copy/skip cascade in `cmd_enrich` collapses to a single loop.

Cites blocking criterion: *business logic is duplicated across layers without explicit justification*.

---

### Finding 3 — [BLOCKER] `cmd_enrich` does not actually use Slice B.1's templates; the manifest lies

**What's wrong.** Slice B.1 added `templates/AGENTS.template.md` (commit `be1d24d`) and `templates/settings.template.json`. Both were materialized to fix red-team v2 NI-2 ("the schema commits `.claude/settings.json` to `bootstrap-with-template-tracking` semantics; without a template, manifest will write `template_sha: null`").

But `cmd_enrich`:
- Line 1716 copies `.claude/settings.json` from the **scaffold's own** `.claude/settings.json`, NOT from `templates/settings.template.json`.
- Never installs `AGENTS.md` downstream at all. (Verified empirically: a fresh enrich produces 28 files; no `AGENTS.md`.)

Meanwhile, `enumerate_install_targets` records `rendered_from: templates/settings.template.json` for `.claude/settings.json` and `lookup_template_info` computes the `template_sha` from `templates/settings.template.json`. The two files happen to be byte-identical today (`diff` returns empty), so the lie is invisible. The YAML even concedes this: *"the scaffold's own `.claude/settings.json` must stay in sync (enforced by future M9.x lint)."*

**Why it matters.** The day someone edits `.claude/settings.json` in the scaffold without updating `templates/settings.template.json` (or vice versa), every downstream `--check --include-templates` will report false advisory drift, and `--upgrade --take-new` on `.claude/settings.json` will copy the *wrong* file. This is precisely the failure mode that NI-2 was supposed to prevent.

**Fix (one of):**
- (a) Make `cmd_enrich` copy from `templates/settings.template.json` (the rendered_from source of truth).
- (b) Delete `.claude/settings.json` from the scaffold repo entirely; the only canonical source is the template; reclassify the scaffold-side path as `scaffold-template`.
- (c) Wire `AGENTS.template.md` through `cmd_enrich` (render with `{{PROJECT_NAME}}` like CLAUDE.md does) so it actually installs as `AGENTS.md` downstream — otherwise delete the template from the manifest until M9.x adopts it.

Cites blocking criterion: *implementation contradicts an approved architecture decision* (the red-team v2 condition NI-2 is shipped in name only).

---

### Finding 4 — [MAJOR] `_scaffold_source_for_spec` returns the template path for rendered files; `--upgrade --take-new` would copy the raw template

**What's wrong.** `_scaffold_source_for_spec` (line 611) returns `REPO_ROOT / rendered_from` when `rendered_from` is set. `apply_upgrade_plan`'s `ACTION_TAKE_NEW` branch (line 819, 824) uses this path as the source for `safe_install`. For `.claude/CLAUDE.md`, that means a `--take-new` would copy `templates/CLAUDE.template.md` (with literal `{{PROJECT_NAME}}` placeholders) over the downstream's rendered file.

**Why it matters.** Practically, this would only fire if a user ran `--upgrade --take-new .claude/CLAUDE.md`. The default for bootstrap-with-template-tracking is keep-local on drift (line 712), so it's gated behind explicit user opt-in. But the explicit opt-in is exactly when "broken behavior" hurts — the user who types `--take-new` is asking for the canonical version and getting an unrendered template.

**Fix.** When `rendered_from` is set, `apply_upgrade_plan` should call a render path (factoring out `render_claude_md`'s substitution logic into a generic `render_template(template_path, dest, project_name)`) instead of plain `safe_install`. Add a test that `--upgrade --take-new .claude/CLAUDE.md` produces a file *with* the project name substituted.

---

### Finding 5 — [MAJOR] `bootstrap-with-template-tracking` files always show "update-available-advisory" on `--upgrade`

**What's wrong.** `compute_upgrade_plan` line 660–663 computes `scaffold_new_sha` by hashing `_scaffold_source_for_spec(spec)`, which for rendered files is the *template's* sha. That is then compared against the manifest's recorded sha for the rendered downstream file. They will essentially never match — the manifest records the rendered output, the comparison hashes the unrendered template.

**Why it matters.** Every `--upgrade` against a clean project will report `.claude/CLAUDE.md` and `.claude/settings.json` as `update-available-advisory`. Cosmetic noise today; signal-to-noise erosion long-term — the user starts ignoring advisories, including the real ones.

**Fix.** For rendered-file specs, compute `scaffold_new_sha` as the *post-render* sha (render the template into a `tempfile`, hash the result). Or, simpler: skip `scaffold_new_sha` computation entirely when `rendered_from` is set and `manifest_sha` matches `current_sha` — the file is clean by definition.

---

### Finding 6 — [MAJOR] `--rename-local`, `--adopt`, `--accept-removal`, `--interactive` have no positive-path tests

`tests/test_m9_manifest.py::UpgradeCollisionNovelRefuses` confirms the negative path (without these flags, planner produces REFUSE). No test exercises:

- `--adopt PATH` actually causing `ACTION_ADOPT` to write a manifest entry recording the on-disk content as canonical
- `--rename-local PATH=NEWPATH` actually moving the on-disk file aside and installing the scaffold version
- `--accept-removal PATH` actually deleting the file and producing a manifest without that entry
- `--interactive` walking refusals and applying user choices

**Why it matters.** Roughly 60 lines of `apply_upgrade_plan` (the ADOPT, RENAME_LOCAL, DELETE, ORPHAN branches and `_interactive_resolve`) are untested. Per the testing gate: *"a test that would fail if the change were reverted."* Reverting the body of `ACTION_RENAME_LOCAL` to a no-op breaks no test.

**Fix.** Add four small tests, each replaying the collision-novel/drift fixture but with the appropriate flag, asserting (a) exit 0, (b) on-disk effect (file moved/deleted/adopted), (c) post-state manifest reflects the chosen action.

---

### Finding 7 — [MAJOR] Partial-failure recovery in `apply_upgrade_plan` leaves manifest stale-but-consistent

**What's wrong.** When `safe_install` fails on file N of a multi-file plan (line 824–827), `apply_upgrade_plan` returns 1 without writing the manifest. Files 1..N-1 are now scaffold-canonical on disk but the manifest still records pre-upgrade shas for them. A subsequent `--check` reports drift on N-1 files that are actually correct.

**Why it matters.** The memo §5 promises "manifest write last; partial copies become orphans for next sweep" — and orphan tmps ARE swept. But the manifest stale-shas are not "orphans," they're false positives. User confusion guaranteed; data loss not. Recovery: re-run `--upgrade --yes` which will see clean files (current_sha == new scaffold sha) and `update-available` action will no-op them. So it's salvageable but not graceful.

**Fix.** Either (a) write a partial manifest reflecting whatever did succeed (making files 1..N-1 clean) before returning, or (b) document the "re-run after partial failure" recovery in `INSTALL_LIFECYCLE.md` so users know to do it.

---

### Finding 8 — [MINOR] `cmd_enrich`'s container-files block has a stale comment

Line 1761 says `"included by default since CONTAINERIZATION.md is also included and users without Docker simply do not run them"` — but the install logic doesn't gate on whether `CONTAINERIZATION.md` is in the doc set. The comment is aspirational. Either gate the container-files install on `CONTAINERIZATION` being in `resolved["include_docs"]`, or remove the misleading comment.

---

### Finding 9 — [MINOR] `enrich-project.py` has organically grown; recommend a Slice D refactor

The file is 1911 lines. Sections are clear (M9 helpers, manifest writer, install enumeration, --check, --reconcile, --upgrade, --uninstall, default enrich) but `cmd_enrich` (lines 1627–1802) is a 175-line procedural function that should consume `enumerate_install_targets` instead of re-deriving the install set. Once Finding 2's consolidation lands, the file naturally drops 100+ lines and the install path becomes a single source of truth.

---

## Acceptance criteria audit

| # | Criterion | Test exercises real behavior? | Notes |
|---|---|---|---|
| 1 | `--self-check` exits 0 on clean scaffold | YES | `LiveScaffoldPasses` runs the script as a subprocess. |
| 2 | Falsifiable controls (positive/negative + ignore-glob constraint) | YES | `PositiveControl`, `NegativeControl`, `IgnoreConstraintEnforced`. |
| 3 | `--reconcile` shas reproducible by independent script | YES | `_independent_normalized_sha256` is a separate implementation; cannot share a bug. |
| 4 | `--upgrade` collision-novel + drift produces exit 3 naming both paths | PARTIAL | Negative-path only; no test for resolution flags (Finding 6). |
| 5 | SIGKILL-equivalent partial copy recovers cleanly | YES (with caveat) | The test monkey-patches `os.replace` to raise after 3 calls. The Python `OSError` propagates out of `cmd_enrich` (no try/except eats it). The `*.scaffold-tmp` left by `shutil.copy2` (which already wrote the file) is then visible to the next-run sweep. This actually models the SIGKILL path correctly because `atomic_copy` deliberately does NOT clean up tmps on exception (per its docstring). Caveat: the test does not exercise an *uncatchable* signal (Python can't simulate true SIGKILL without `os.kill`); but this is the closest viable simulation. |
| 6 | `--upgrade --keep-local` round-trip preserves edits and `--check` clean | YES | `UpgradeKeepLocalRoundTrip` is end-to-end via subprocess. |
| 7 | `--uninstall --include-once` removes scaffold + bootstrap-* | YES | Both default and include-once branches tested; project-original file untouched. |
| 8 | Every entry has `overlays: []` | YES | `OverlaysReservedField` walks every entry. Trivial but correct. |
| 9 | Linear-chain migration (synthetic v0 → v1) | YES | Pure-function, idempotency, end-to-end `--migrate-only`, all covered. |
| 10 | Concurrent enrichment lock | YES | `_hold_lock_then_signal` worker uses the *same* `target_lock` context manager via a spawn-safe import. The contender exits non-zero AND stderr contains "another" — the test verifies both, so passing-for-the-wrong-reason is unlikely. |
| 11 | Template_sha advisory drift | YES (caveat) | The test corrupts the recorded `template_sha` rather than the live template. This exercises the engine's *comparison* logic but not the "scaffold modifies template post-install" workflow end-to-end. Equivalent in observable behavior (sha mismatch detection is symmetric); fully sufficient for the criterion as written, but a stronger test would tweak a real template file in a synthetic scaffold copy. |

All 11 criteria have falsifiable tests. Two are partial (#4 negative-only, #11 corrupts manifest not template); both are defensible reads of the criterion text.

## DRY / duplication audit

Confirmed duplications:

1. **`DOC_TEMPLATE_MAP` (line 1333) ↔ inline `template_map` (line 1661)** — same dict, two places. Same for `SCAFFOLD_ONLY_DOCS` ↔ inline `scaffold_only_docs`, and `WORKFLOW_SCRIPTS` ↔ inline `workflow_scripts`. (Finding 2.)
2. **`ALWAYS_INSTALLED_FILE_PATHS` (line 1349) ↔ inline `container_files` + `["CONTINUE_PROMPT.txt"]` (lines 1750, 1763–1769)** — three lists for the same invariant. (Finding 2.)
3. **`scaffold-orphan` literal (line 874) ↔ literal (line 1042)** — undocumented sixth class, two hardcoded uses. (Finding 1.)
4. **Scaffold-side `.claude/settings.json` ↔ `templates/settings.template.json`** — two files that must stay byte-identical, with no mechanical enforcement. The YAML comment admits this. (Finding 3.)
5. **`render_claude_md` substitution logic** — only handles `{{PROJECT_NAME}}` and `{{OPTIONAL_REFERENCES}}`. `AGENTS.template.md` references `{{PROJECT_NAME}}` but no code path renders it. If/when wired up, the substitution should be extracted into a `render_template(src, dest, ctx)` helper rather than copy-pasted.

## Verdict

**GREEN-LIGHT WITH CONDITIONS.** Three blockers (Findings 1, 2, 3) and four majors (4, 5, 6, 7). All blockers are localized fixes (10–80 lines each); none requires a redesign. Recommended path:

- Fix Findings 1, 2, 3 in this phase (M9). Add four positive-path tests to address Finding 6.
- Defer Findings 4, 5, 7 to M9.1 with explicit tracking entries in `phase-approval.json`.
- Note in `phase-approval.json` that Findings 8, 9 are non-blocking and tracked for the next refactor pass.

The implementation is solid where it lands — atomic copy, lock contention, secrets scan, symlink refusal, normalized hashing, linear-chain migrations, and the 5-class taxonomy all behave correctly under their tests. The blockers are about *consistency between three near-identical lists* and *one undeclared class*. Exactly the kind of rot the DRY gate is designed to catch before it metastasizes.

---
Files reviewed: `/home/aaron/dev/projects/aaron/competent-developer-template/scripts/enrich-project.py`, `/home/aaron/dev/projects/aaron/competent-developer-template/capabilities/project-capabilities.yaml`, `/home/aaron/dev/projects/aaron/competent-developer-template/tests/test_m9_self_check.py`, `/home/aaron/dev/projects/aaron/competent-developer-template/tests/test_m9_manifest.py`, `/home/aaron/dev/projects/aaron/competent-developer-template/docs/INSTALL_LIFECYCLE.md`, `/home/aaron/dev/projects/aaron/competent-developer-template/templates/AGENTS.template.md`, `/home/aaron/dev/projects/aaron/competent-developer-template/templates/settings.template.json`, `/home/aaron/dev/projects/aaron/competent-developer-template/artifacts/red-team-review-v2.md`, `/home/aaron/dev/projects/aaron/competent-developer-template/artifacts/decision-memo.md`.
