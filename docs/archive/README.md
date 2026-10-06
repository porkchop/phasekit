# Archived docs

These files are kept for provenance only. Nothing reads them: no loop, hook,
prompt or downstream project, and phasekit never installs them.

`META_SPEC.md` and `META_PHASES.md` were phasekit's self-improvement spec and
phase plan (meta-phases M0–M10, last edited 2026-04-27), and
`SELF_APPLICATION_EXAMPLE.md` the worked example of that cycle (M7). They went
dormant once phasekit moved to hand-built releases: every change reaches the
whole fleet, so a release is built, gated and reviewed by hand (see
`docs/RELEASING.md`), and no phase loop runs on this repository. They were
retired in v0.18.6 (2026-10-06), when running phasekit as its own phasekit
project was considered and declined; the audit is recorded in Foundry's
architecture repository as `designs/DESIGN-phasekit-self-application.md`.
Approved meta-phase numbers stay as they were, so older commits, artifacts and
ADRs that cite them (for example "Phase M9") still resolve here.
