# Design — {{PROJECT_NAME}}

> One-page steady-state design. Update whenever a decision memo approves a
> material change. Pairs with `docs/SPEC.md` (what users see),
> `docs/ARCHITECTURE.md` (how code is organized), `docs/PHASES.md` (when),
> and `docs/PROD_REQUIREMENTS.md` (production). Skip this artifact for
> trivial projects — see `docs/USAGE_PATTERNS.md` for guidance.

## System sketch

```
              ┌──────────┐
              │   user   │
              └────┬─────┘
                   ▼
   ┌─────────────────────────────────┐
   │   <name a subsystem>            │
   │   ┌──────┐      ┌──────────┐    │
   │   │  ?   │ ───▶ │    ?     │    │
   │   └──────┘      └──────────┘    │
   └─────────────────────────────────┘
                   │
                   ▼
            <persistence?>
```

Replace with your subsystem layout. 5–8 named boxes, readable in 10 seconds,
with the 3–5 most important dependencies.

## Data flows

For the top 2–3 user actions, name the steps in order. 3–7 lines each. If a
flow needs more, that sub-system probably deserves its own sketch below.

- **Action 1** — user → ? → ? → ? → response
- **Action 2** — ...

## Hot spots

Where bottlenecks, hot writes, large reads, or external-call serializations
will live. Anticipate the top 3–5; this is where scaling concerns belong —
not afterthoughts surfaced in QA.

- **<spot 1>** — what's hot, why, and the scale at which it becomes a problem
- **<spot 2>** — ...

## Boundaries

The hard edges. Address each as relevant:

- **Sync vs async** — what blocks, what queues, what fires-and-forgets
- **Transaction boundaries** — what's atomic; isolation level
- **Deploy units** — what ships together, what ships independently
- **Trust boundaries** — what's authoritative; where untrusted input enters

## Open questions

- ? (track unresolved items most likely to need a decision-memo + red-team review)
