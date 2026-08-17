# Mutation testing (opt-in)

**This is an option phasekit offers, not a gate it imposes.** It ships only
with the `with-mutation` profile. Most projects should not enable it: it is a
heavy practice, and phasekit is a public tool that must stay useful to someone
who wants none of this.

Enable it deliberately:

```bash
phasekit --profile with-mutation .        # at enrich time
phasekit upgrade --profile with-mutation  # or later
```

## Why it exists here

It was never decided. It appeared inside one project's `docs/LEARNINGS.md` and
**self-propagated**: each session read the accumulated technique, concluded it
was expected, did it again, and added more lore. Nobody chose it, nobody scoped
it, and it existed nowhere in phasekit at all.

It also earns its place. In the phase that triggered this protocol it caught:

- a guard test that only discriminated against the **last** of six reporting
  filters,
- a fail-open that answered `"current"` for a **missing** file,
- a detector blind to **aliased imports**.

All three were real defects that the ordinary suite missed. So the practice is
kept — but as a decided, bounded protocol rather than as lore.

## The division of labour

| | who |
|---|---|
| **Designing** the mutants — choosing edits a weak test would not notice | the LLM (or you) |
| **Executing** them — patch, run, revert, record | `scripts/mutation-run.py` |

The value has always been in the design; the cost was the hand-driven run loop,
rebuilt into `/tmp` from scratch every phase. Automate only the second.

`mutation-run.py` never invents a mutant. If it did, it would generate the
cheap syntactic ones that good tests already catch, and the audit record would
fill with noise that makes the practice look effective while catching nothing.

## Chunking, not resumability

Mutants are independent — patch, run, revert — so a run is just N sequential
foreground calls. At roughly 100s each, about five fit in one tool call:

```bash
python3 scripts/mutation-run.py --spec docs/mutants/iteration-12.json --list
python3 scripts/mutation-run.py --spec docs/mutants/iteration-12.json --chunk 5 --index 0
python3 scripts/mutation-run.py --spec docs/mutants/iteration-12.json --chunk 5 --index 1
```

**Nothing is backgrounded, so nothing can be orphaned when a turn ends.** That
is not incidental — a long-running background mutation job is precisely what
lost three consecutive sessions and produced this release.

> **A resumable on-disk cache was considered and REJECTED.** Mutant results are
> only valid against the exact tree they ran on, so resuming becomes a
> cache-invalidation problem whose failure mode is a **false green on a quality
> gate**. That is worse than having no gate, because it is trusted. Chunking
> gets the same wall-clock benefit with no cache to invalidate.

## Budget

In the same spirit as the verify budget (`docs/QUALITY_GATES.md`):

```bash
python3 scripts/mutation-run.py --spec S --max-seconds 900 --timeout 180
```

Once the budget is spent, remaining mutants are recorded as `not_run` — never
dropped and never counted as passing. An unrun mutant that looked like a pass
is the false green this whole design refuses.

## The spec

A JSON file the designer writes. Keep it in the repo (e.g.
`docs/mutants/iteration-N.json`) so the design is reviewable.

```json
{
  "version": 1,
  "test_command": "python3 -m unittest discover -s tests -q",
  "mutants": [
    {
      "id": "m1",
      "file": "orchestrator/reporting.py",
      "find": "for f in filters[-1:]:",
      "replace": "for f in filters:",
      "rationale": "the guard test may only discriminate against the last filter"
    },
    {
      "id": "m2",
      "file": "orchestrator/state.py",
      "find": "return \"current\"",
      "replace": "return \"stale\"",
      "rationale": "fail-open path for a missing file"
    }
  ]
}
```

- `find` must match **exactly once** in the file. Two matches make the result
  meaningless — you would not know which site the outcome refers to. Quote more
  surrounding context instead.
- `rationale` is required. A mutant nobody can explain is a mutant nobody can
  review.

## Outcomes

| outcome | meaning |
|---|---|
| `killed` | the suite failed with the mutation applied — the tests discriminate |
| `SURVIVED` | the suite **passed** with the mutation applied — a real finding |
| `equivalent` | declared in the spec as having no observable effect; not run |
| `error` | the mutant could not be applied (missing file, ambiguous `find`) |
| `not_run` | the budget ran out before this mutant started |

Exit 0 = everything that ran was killed. Exit 1 = something survived. Exit 2 =
spec error, dirty tree, or an unapplicable mutant.

**A survivor is a finding, not a nuisance.** Strengthen the test. If the mutant
genuinely has no observable effect, mark it `"equivalent": true` in the spec
*with a rationale* — never by deleting it, which erases the reasoning.

## The audit record

Every run writes `artifacts/mutation-run.json`: what was mutated, what
survived, what was declared equivalent, and how long each took.

It is **write-only**. Nobody resumes from it, so it needs no invalidation and
carries no false-green risk. Its job is to make the practice *auditable* — the
difference between a claim in a commit message and evidence someone can check.

## Safety

The harness refuses to run on a dirty tree. Mutants are applied and reverted in
place, so an interrupted run could otherwise restore a file over the top of
uncommitted edits. Commit or stash first (`--allow-dirty` overrides, at your
own risk). The revert itself is in a `finally`, so an interrupted run still
cannot leave a mutated file behind.

## When to reach for it

Good: a phase that added a guard, a filter, a detector, a fail-open path — the
places where a test can look right and assert nothing.

Not: routine CRUD, generated code, or as a per-iteration ritual. This is a
scalpel for phases where the tests' *discrimination* is in doubt, and running
it everywhere is how it became unexamined lore in the first place.
