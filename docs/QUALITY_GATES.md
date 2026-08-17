# Quality gates

## Anti-rationalization

These are excuses agents (and humans) reach for when they want to skip a gate. Each pairs with the reality that makes the excuse wrong. Read this section before relaxing any rule below.

| Rationalization | Reality |
|---|---|
| "This is too small for the spec / phase model" | The phase model is what makes 'small' verifiable. Skipping produces work that cannot be approved. |
| "I'll add the phase-approval artifact later" | Without it, the next agent has no audit trail and the loop cannot advance. The artifact *is* the gate. |
| "I can just quickly implement this" | Audit-first exists because existing code may already satisfy the goal. Re-implementing without auditing produces drift. |
| "The test isn't strictly necessary here" | The testing gate requires a test that would fail if the change were reverted. If you can't write one, you don't understand the change. |
| "Two near-duplicates is fine for now" | The DRY gate targets logic that encodes the same invariant in two places. That kind of duplication silently rots when one side changes. |
| "I'll skip the planner — the design is obvious" | The planning gate exists for changes that cross layers, touch persistence/auth, or have multiple plausible strategies. 'Obvious' is what missed-the-tradeoff sounds like before review. |
| "I'll loosen project settings just for this run" | Project settings are shared. Permissive execution belongs in `settings.local.json` or CLI flags, never committed. |
| "Code review can wait until after merge" | The review gate catches duplication, missed drift, and architecture violations the implementer is least likely to see. After-merge review is a documentation step, not a gate. |
| "I'll push anyway, CI will catch it" | CI is shared infrastructure. Broken master pages humans, blocks deploys, and disrupts other consumers of the loop. The pre-commit verify gate exists so easy classes of failure are caught locally. |
| "I'll skip the verification sprint — nothing changed there" | Cumulative regressions are the failure mode the sprint exists to catch. 'Nothing changed' is an assumption, not evidence. |
| "The suite is too slow, I'll skip verify" | Split it instead; the budget is the fix, not the excuse. A fast tier keeps the gate under budget while the full suite still runs at the verification sprint and at completion — see "Verify budget" below. |
| "Discovered work can wait for a later phase" | Discovered work that blocks the current phase is part of the current phase. Deferring it produces phases that pass review but don't actually deliver. |

If you find yourself making one of these arguments, stop and re-read the relevant gate below.

## Universal gate
A phase is only complete when all of the following are true:
- documented acceptance criteria pass
- relevant tests pass
- `code-reviewer` approves
- `qa-playwright` approves for user-visible or browser-based work
- docs are updated for any rule or architecture decision
- `artifacts/phase-approval.json` is written with `approved: true`
- the outer orchestration layer creates a git commit before the next phase begins

## Testing gate
A phase is not complete unless:
- every new module, endpoint, or public behavior has at least one test exercising its primary path
- every bug fix includes a regression test that would fail without the fix
- tests are written before or alongside implementation code, not retrofitted after
- test names describe the behavior under test, not implementation details
- edge cases and error paths are tested for any logic identified as risky in the spec or decision memo

When coverage tooling is available in the target project, aim for meaningful branch coverage of new code. Do not pursue a numeric target at the expense of test quality — a focused test that catches real regressions is worth more than broad shallow coverage.

"Relevant tests pass" (from the universal gate) means: tests exist that would fail if the feature were removed or the bug fix reverted.

## DRY and reuse gate
- business rules and validation logic must exist in exactly one place
- if the same logic appears in more than one layer, extract it to a shared module or justify the duplication in a decision memo
- builders must check for existing utilities and shared modules before creating new ones
- code-reviewer must reject changes that introduce unjustified duplication

## Drift detection gate
Before starting a new implementation phase, the assigned builder must:
- review code produced in prior phases for overlap with the current task
- flag any inconsistency between the current plan and already-approved code
- prefer extending existing modules over creating parallel implementations
- report discovered drift as a blocking issue for project-lead to resolve before proceeding

## Verification sprint gate
Before starting a phase that builds on a completed user-visible or end-to-end foundation, run a full verification of the cumulative system to confirm prior work still functions:
- run the complete test suite (unit, integration, and browser/E2E when applicable)
- exercise the primary user workflow end-to-end via `qa-playwright` for browser projects, or via the project's domain-equivalent QA (CLI smoke test, integration harness) for non-browser projects
- fix any regressions before starting new work — do not layer new complexity onto broken foundations
- record verification results under a `verification_sprint` field in the next `artifacts/phase-approval.json`

## Planning gate
Use a planning and adversarial review cycle before implementation when any of the following are true:
- the change crosses multiple layers
- persistence or schema choices are involved
- auth, security, or public internet exposure is involved
- the refactor could invalidate prior assumptions
- there are at least two plausible implementation strategies
- scaffold control flow, capability generation, or packaging behavior is affected (self-improvement mode)

Required outputs:
- `artifacts/decision-memo.md`
- optional ADR in `docs/adr/` (use `docs/ADR_TEMPLATE.md`; follow naming convention `ADR-NNNN-short-title.md`)

## Meta-project gates
For scaffold self-improvement work, a phase is not complete until:
- the current meta-phase acceptance criteria pass
- `strategy-planner` is used for material design changes
- `architecture-red-team` is used for architectural or autonomy-affecting changes
- `code-reviewer` approves
- generated skills validate and package successfully for any skill-related phase
- docs and manifest entries are updated
- `artifacts/phase-approval.json` is written
- the outer wrapper commits the approved phase before the next one begins

## Control-loop change gate
Any change affecting:
- project-lead behavior
- quality gates
- commit flow
- hooks
- settings
- generation scripts
- skill packaging flow
- subagent definitions (`.claude/agents/`)

must include:
- rationale
- tradeoffs
- rollback path
- updated docs
- explicit reviewer approval

## Commit gate
Claude must stop after writing `artifacts/phase-approval.json`.
The host-side wrapper is responsible for:
1. reading the approval artifact
2. running the pre-commit verification gate (see below)
3. creating a git commit
4. resuming Claude for the next phase

### No-churn rule
The wrapper only commits when the iteration produced a **substantive** change.
Two kinds of churn are explicitly excluded:
- **Per-iteration logs** (`artifacts/logs/*`) are never staged. `run-phase.sh`
  rewrites them every iteration, so committing them would flood history and
  make a no-progress iteration look like real work. They stay on disk for live
  tailing and forensics.
- **Re-emitted transient signals.** A prior `phase-approval.json` persists on
  disk as the durable approval record, so a later iteration that is blocked or
  stalled (and must still write *some* signal artifact) would otherwise commit
  nothing but the re-emitted `phase-blocked.json` / `phase-verify-failed.json`.
  When those signals are the only staged change, the wrapper skips the commit;
  if `phase-blocked.json` is present it stops cleanly (exit 2) for operator
  handoff rather than committing an empty-progress change.

### Transient-signal family (v0.6.5)
Every loop-emitted signal that the loop (or an orchestrator) later deletes
behind git's back must never be committed: a tracked copy turns that deletion
into a staged deletion the no-churn rule may refuse forever, leaving the tree
permanently dirty and tripping every downstream clean-tree guard (the
motivating incidents: a tracked `phase-blocked.json`, then a tracked
`spec-change.json`).

The full family: `phase-blocked.json`, `phase-verify-failed.json`,
`spec-change.json`, `scope-warning.json`, `scope-refusal.json`,
`light-escalation.json` (plus `artifacts/logs/` and the wrap-up sentinel).
Deliberate absences — committed on purpose, not transient:
`phase-approval.json`, `phase-update.json`, `project-complete.json`,
`session-handoff.json`, `ready-to-deploy.json` (the supervisor's deploy
trigger — the session writes it, phasekit's own scripts never touch it), and
an orchestrator's `iteration-mode.json`.

Three mechanisms in `scripts/run-until-done.sh` keep the family uncommitted:
- **Status-hidden signals** (`spec-change`, `scope-warning`, `scope-refusal`,
  `light-escalation`) go into `.git/info/exclude` at loop start — they are
  consumed from disk, so hiding them from `git status` hides nothing a human
  needs. `phase-blocked.json` and `phase-verify-failed.json` deliberately stay
  visible: a live blocker must show in `git status`.
- **Never added**: after `git add -A`, the wrapper unstages any fresh add of a
  family member; for a member a legacy history still tracks it re-stages the
  untracking instead (v0.6.6) — `git add -A` cancels a heal deletion staged at
  loop start whenever the file is still on disk, so the deletion is restored
  at the last point before the commit rather than trusted to ride.
- **Self-heal**: at loop start, any family member tracked by a pre-v0.6.5
  history is untracked (`git rm --cached`); when the index is otherwise clean
  the untracking is committed immediately as an index-only chore commit
  (working tree unchanged, so the verify gate's inputs are unchanged —
  gating the heal would block it exactly when the tree is broken for
  unrelated reasons), otherwise it rides with the session's next commit.

## Evidence integrity gate

Gate results and citations are claims about a *specific tree*; they rot silently when the tree moves. (Upstreamed 2026-08-11 from foundry-dashboard, which adopted these rules at its iteration-10 close after stale evidence decided three consecutive signoffs — the phase numbers cited below are that project's, kept as the motivating record.)

### Evidence must name the tree it judged
Adopted at the iteration-10 close, after it decided three consecutive signoffs. A gate result is a claim about a *specific tree*, and it stops being true the moment that tree changes — but nothing about a stale result looks stale, so it gets cited long after it expired.

- **Every gate records the md5 of the files it judged.** A browser pass records the md5 of every file the browser fetched, at the START and again at the END of the run (the second catches a tree that shifted under the gate). Put them in the gate's own evidence file, not in prose.
- **Before citing a gate result, compare — do not infer.** Recompute the md5s and diff them against the record. "Is this evidence still valid?" then has a yes/no answer instead of an argument.
- **A fix round that touches a file the gate judged INVALIDATES that gate.** Either keep fix rounds to tests, docs and artifacts — which is what makes the original evidence still citable — or re-run the gate. Do not reason that a change "cannot have affected" what was measured.
- **Never stitch a partial run into a signoff.** An interrupted gate with no verdict is VOID; re-run it. Overlapping numbers from the dead run are not a verdict, and salvaging them is how a phase gets signed off on evidence nobody ever finished producing.

Why this is a gate and not a nicety: at Phase 29 a memo cited a browser round that predated its own final source edits, *in the same paragraph* that explained why superseded evidence must not be cited. At Phase 30 the identical defect recurred one phase later — a session updated the memo to say "all 23 md5s matched", then changed three of the 23 files before it was interrupted. Both were caught by comparison, and neither would have been caught by reading.

### A `path:line` citation is a claim about a tree, too
Same failure, cheaper instrument. Every phase that edits a source file shifts the lines below its edit, silently invalidating citations elsewhere in the repo — this is what blocked Phases 27, 28, 29 and 30, each time via the diff's *own* edits.

- **After editing source, sweep the citations that edit moved** — programmatically, then read each target line to confirm it still says what the citing text claims. "The line exists" is not the check; "the line means what I said" is.
- **A citation in a PRESENT-TENSE claim must resolve against the current tree.** If it describes what the code does *today*, re-point it.
- **A citation in a frozen record is left alone, and the record says so.** Intake documents (`docs/change-requests/*.md`), the decision log (`artifacts/decision-memo.md`), signoff artifacts (`artifacts/phase-approval.json`, `artifacts/iterations/*`), and phase blocks explicitly marked as HISTORY describe the tree *as it was*; re-pointing them would falsely imply the code still reads that way. These are exempt by virtue of being dated records — but the exemption has to be *stated in the document*, or the next reader cannot tell a frozen citation from a rotted one. A frozen record is still corrected when it was wrong **when written**, or when a later phase falsifies a standing claim it makes — in place, with the correction marked.

## Pre-commit verification gate
The wrapper runs a project-defined fast-check command before creating any phase commit, regardless of whether `AUTO_PUSH` is enabled. Mechanism lives in the scaffold (`scripts/run-until-done.sh`); policy lives in the project (`scripts/phasekit-verify.sh`).

Scope of the verify command:
- fast checks only — lint, typecheck, formatter, unit tests
- **not** full E2E or integration suites (those belong to the verification-sprint gate, which Claude drives at phase boundaries)
- target under ~30 seconds; this runs every iteration

On failure:
- the commit is **not** created
- the wrapper writes `artifacts/phase-verify-failed.json` with the failing command, exit code, attempt counter, and a tail of the output
- the next iteration's `CONTINUE_PROMPT.txt` directs Claude to fix the failure before doing any new phase work
- after `VERIFY_MAX_ATTEMPTS` (default 3) consecutive failures on the same artifact, the wrapper writes `artifacts/phase-blocked.json` and exits so a human can intervene

To recover from a circuit-break: fix the underlying failure (or set `VERIFY_SKIP=1` if the next iteration legitimately needs to commit red), then re-run `scripts/run-until-done.sh`. The wrapper resets the verify-attempt counter at the start of any fresh `CLAUDE_MODE=new` run; `phase-blocked.json` is cleared automatically by the iteration's `cleanup_artifacts` step.

Escape hatches:
- `VERIFY_SKIP=1` bypasses the gate for one iteration (use sparingly — docs-only phases or TDD phases that intentionally commit a red test)
- `PHASEKIT_VERIFY_CMD="..."` overrides the script with a one-shot command

### Cross-project contracts gate (v0.7.0)

Before the project's verify command, the wrapper runs the contracts gate. It is
**inert unless the repo has a `contracts.yaml`** declaring dependencies on other
projects' interfaces; a repo that declares nothing never sees it mentioned.

When a repo does declare, the gate refuses the commit if a declared contract is
unreadable, or if the vendored copy has drifted byte-for-byte from the
provider's authoritative one. The failure flows through the same
`phase-verify-failed.json` / `VERIFY_MAX_ATTEMPTS` machinery as any other verify
failure, and the drift message names the one command that fixes it.

Two deliberate properties, both load-bearing:

- It runs **before** the `VERIFY_SKIP` bypass. VERIFY_SKIP is set routinely for
  red TDD commits; letting it also disable contract authenticity would turn the
  gate off exactly when a red gate is applying the pressure to cheat. The
  separate hatch is `PHASEKIT_CONTRACTS_SKIP=1`, and it announces itself.
- It lives in **scaffold-owned** code (`scripts/phasekit-contracts.py`, invoked
  by `run-until-done.sh`), not only in the project-owned
  `scripts/phasekit-verify.sh`. A check the policed repo can edit away is not a
  check — the same reason the secret-lint allowlist lives on the operator side
  of the deploy boundary.

Full mechanism, file formats and the standalone story: `docs/CONTRACTS.md`.

Configuration is project-owned: edit `scripts/phasekit-verify.sh` (rendered into the project at enrich time) to declare the right fast checks for the stack. Until configured, the gate fail-opens with a warning so un-instrumented projects continue to work.

### Mutation testing (opt-in)

Not part of any gate and not enabled by default. Projects that want a check on
whether their tests *discriminate* (as opposed to merely execute) can enable the
`with-mutation` profile, which installs `docs/MUTATION_TESTING.md` and
`scripts/mutation-run.py`. It is a scalpel for phases that add guards, filters,
detectors or fail-open paths — not a per-iteration ritual.

### Verify budget (v0.6.4)

The gate's speed is part of its correctness. Verify runs before *every* phase commit — often several times per session — so its wall time multiplies directly into session productivity: a suite that quietly grows past its budget turns build sessions into no-commit sessions (the motivating case burned three consecutive sessions on a suite that had grown to ~83s).

- **Target ~30 seconds. 60 seconds is the ceiling** (the loop's advisory threshold, tunable via `PHASEKIT_VERIFY_BUDGET_SECONDS`).
- When a growing suite outgrows the budget, the sanctioned escape is a **fast/slow split**, never a skipped gate:
  - Register a `slow` marker (or the stack's equivalent) and mark tests by **measured** duration (`pytest --durations=25`), not by module or intuition.
  - The pre-commit gate runs the fast tier (e.g. `pytest -q -m "not slow"`).
  - The **full suite stays mandatory at the verification-sprint gate**, which already requires the complete suite — the split restores the two gates' intended roles; it invents nothing.
  - **Completion runs full:** when `artifacts/project-complete.json` exists, verify runs the complete suite — a project never reaches "done" on the fast tier alone. (Quality decision 2026-08-13: fast tier per-commit; full suite at the sprint AND at completion.)
- Splitting governs **when** tests run, never **whether**. A test that no gate ever runs has been deleted, not split.

The loop watches for this drift mechanically: when verify exceeds the ceiling on 2+ runs in one session, it prints a one-line advisory pointing here. The advisory is fail-open — it never blocks a commit and never edits the project's gate (the v0.5.0 keep-local guarantee: phasekit never rewrites a configured `scripts/phasekit-verify.sh`).

### Stack profiles seed a real gate (v0.5.0)

Projects enriched with a **stack profile** (`python-uv`, `static-web`, `game-canvas`, `docs-only`) skip stub mode entirely: the verify script is rendered from that stack's template with working checks and `PHASEKIT_VERIFY_CONFIGURED=1` out of the box (uv sync → ruff → mypy → pytest for `python-uv`; node tests + no-dependency assertion + ESM import-graph check for `static-web`/`game-canvas`; an internal markdown link/reference checker for `docs-only`). The file is still project-owned after seeding — evolve it freely.

On `--upgrade`, seeding fills stubs only: if the on-disk gate still carries `PHASEKIT_VERIFY_CONFIGURED=0`, the stack template replaces it (`stub-reseed` in the plan); any configured gate is never overwritten (`--keep-local` also forces preservation). Stack profiles additionally install `docs/CONVENTIONS.md` (scaffold-owned, drift-checked) describing the stack's conventions.

### Light execution mode leans on this gate (v0.6.0)

`PHASEKIT_ITERATION_MODE=light` (see `docs/EXECUTION_MODES.md`) trades phase
ceremony — planner, red-team, multi-phase decomposition — for a tighter
mechanical leash: eligibility **requires** a configured (non-stub) verify
gate, the breaker drops to 2 attempts, the iteration cap is 2, and one
default-model review pass runs before the final commit. The gate itself is
unchanged and mandatory; a light run that trips the breaker, writes a blocked
artifact, or makes a scaffold-class (out-of-scope) edit ends with
`artifacts/light-escalation.json` so the orchestrator can re-queue the task as
a standard full-ceremony iteration. Stub-gate projects are refused light mode
with a plain log line. Principle: *reduced ceremony only where mechanical
verification is strong.*

### Self-hosting gap (phasekit on phasekit)

The verify script is *rendered* into downstream projects from `templates/phasekit-verify.template.sh`; the phasekit source repo deliberately does not carry a rendered `scripts/phasekit-verify.sh` (it would shadow the template). So when the autonomous loop runs **on phasekit itself** (self-improvement runs), the gate fail-opens with a warning — phasekit does not currently enforce its own pre-commit checks. This is a known gap, not a bug: every enriched downstream project still gets a working gate.

To close it, phasekit would gain its own `scripts/phasekit-verify.sh` running the checks we already run by hand (`python3 -m unittest discover tests` + `python3 scripts/enrich-project.py --self-check`), registered so it does not collide with the rendered template. Deferred until we decide to dogfood; promote to an ADR if/when that decision is made.

## Suggested phase approval artifact

```json
{
  "phase": "phase-4",
  "approved": true,
  "summary": "Primary browser workflow verified and accepted.",
  "suggested_commit_message": "phase-4: approve browser workflow"
}
```

For phases that required a planning gate, include a `decision_memo` field referencing the governing artifact:

```json
{
  "phase": "meta-M5.1",
  "approved": true,
  "summary": "...",
  "decision_memo": "artifacts/decision-memo.md",
  "suggested_commit_message": "meta-M5.1: ..."
}
```

This field is optional for phases that did not require a planning gate, and required for those that did.

## Final completion artifact

```json
{
  "done": true,
  "summary": "Required phases completed and production requirements satisfied.",
  "final_notes": [
    "Known limitation 1",
    "Known limitation 2"
  ]
}
```

## Discovered work and phase expansion

During execution, the lead may discover that the current phase is underspecified or that additional prerequisite work is required.

In that case:

### Minor discovered work
If the new work is small, backward-compatible, and does not require external input:
- update the relevant docs directly
- append or refine acceptance criteria
- continue the current phase without asking the user

### Major but self-resolvable discovered work
If the new work is substantial but can be resolved using existing repo context and subagents:
- invoke strategy-planner and architecture-red-team as needed
- update META_SPEC.md, META_PHASES.md, ADRs, or related docs
- split the phase into subphases or append follow-on phases as needed
- do not ask the user for confirmation merely to continue planning
- write `artifacts/phase-update.json` if the phase plan changed materially

### External blocker
If required information is genuinely missing from the repo and cannot be resolved autonomously:
- write `artifacts/phase-blocked.json`
- stop without writing `artifacts/phase-approval.json`

### Scope containment + SPEC attestation (v0.4.8)

The commit gate applies two observability checks (never blocking normal work,
per the gate-recovery principle):

- **Scope:** commits touching `.claude/settings.json` or `.github/workflows/`
  are **refused** (security-critical pair; `artifacts/scope-refusal.json`
  explains the fix — unstage/revert those files and re-write your signal
  artifact). Commits touching other scaffold-class files **proceed with a
  warning** recorded in `artifacts/scope-warning.json` (drift-check also
  flags them; the orchestrator surfaces the warning to the operator).
- **SPEC attestation:** any commit that changes `docs/SPEC.md` records the
  added/removed line counts in `artifacts/spec-change.json`. SPEC evolution
  is legitimate and expected (iterations extend acceptance criteria) — this
  makes it *visible*, not forbidden.

These checks — plus the `docs/LEARNINGS.md` credential-shape scan (v0.4.7) —
live in one shared `post_verify_commit_gates` helper that **every** commit
surface runs after its verify gate (v0.6.6): the iteration commit path and
the wrap-up commit path alike. Wrap-up once reimplemented the commit sequence
without them, which let a credential-shaped LEARNINGS line land (and
auto-push) via wrap-up when an identical iteration commit would have been
refused. Any future commit path must call the same helper.

`phase-blocked.json` supports two optional operator-courtesy fields (v0.4.7):
`unblock_command` — the exact ready-to-run one-liner that resolves the
blocker, transcribed from something verified this session (never composed
from memory); and `unblock_note` — one plain-language line on what the
command does, naming any override/skip flag explicitly. Downstream tooling
renders these for the operator to copy; it never executes them. Secrets,
tokens, and credentials are forbidden in both fields.

Approved phase numbering must remain stable. Do not renumber already-approved phases retroactively.