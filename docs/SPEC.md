# Product specification

Replace this file with the concrete product spec for your project.

<!-- Spec quality principles (delete this block after filling in):
- Every requirement should be testable — if you can't describe how to verify it, it's too vague.
- Prefer concrete over abstract: "API responds in <200ms at p95" not "should be fast."
- Write workflows as step-by-step scenarios: "user does X → sees Y."
- State what is out of scope explicitly — this prevents agents from gold-plating.
- Mark requirements as must-have or nice-to-have so agents can triage.
- Include edge cases: empty states, error paths, concurrent access, permission boundaries.
- If something is intentionally left to the implementer's judgment, say so.
-->

## Product goal
<!-- One sentence: what outcome does the user get? How do you know it succeeded? -->

## Users and use cases
<!-- Who are the primary users? What are their jobs to be done? -->

## Main workflows
<!-- Step-by-step scenarios for each core workflow. Example:
1. User opens the app → sees dashboard with recent items
2. User clicks "New" → sees creation form with required fields highlighted
3. User submits form → item appears in list, confirmation shown
-->

## Scope
### In scope
<!-- Concrete capabilities this version must deliver. Mark priority:
- [must] Core capability 1
- [must] Core capability 2
- [nice] Optional enhancement
-->

### Out of scope
<!-- Capabilities explicitly deferred. Naming these prevents scope creep. -->

## Functional requirements
<!-- User-visible requirements. Each should be verifiable:
- BAD: "Users can manage their account"
- GOOD: "Users can update their display name and email; changes take effect on next page load"
-->

## Non-functional requirements
<!-- Measurable where possible:
- Performance: p95 response time, throughput targets
- Reliability: uptime target, failure modes, retry behavior
- Security: auth method, data sensitivity, access control model
- Observability: what gets logged, what gets alerted on
-->

## Data and state model
<!-- Domain entities, their relationships, and critical state transitions.
Include: what states exist, what triggers transitions, what is irreversible. -->

## Acceptance criteria
<!-- What must be true for the product to count as complete.
Write these as testable statements:
- BAD: "App works well"
- GOOD: "User can complete the main workflow end-to-end without errors; all API endpoints return valid responses; test suite passes with no skipped tests"
-->
