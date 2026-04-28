# Red-team review — M9 Install Lifecycle and Provenance

Reviewer: `architecture-red-team`. Target: `artifacts/decision-memo.md` v1.

---

## Finding 1 — `--self-check` falsifiability is gameable [BLOCKER]

**Claim.** "`--self-check` must currently fail on `master`, then pass after Slice A."

**Why weak.** "Pass" = "every tracked file classified exactly once." Trivially satisfied by a catch-all `ignore: ["**/*"]` or by classifying anything ambiguous as `scaffold-internal`. The criterion tests *uniqueness*, not *correctness*.

**Failure scenario.** Slice A author adds `ignore: ["docs/**"]` to silence the long tail. `--self-check` passes. Months later someone updates `docs/QUALITY_GATES.md` and it never re-installs downstream because the file is now `scaffold-ignored`. Silent regression of the very drift M9 is meant to fix.

**Remediation.**
- Constrain `ignore:` semantics: only generated/build paths (`dist/`, `artifacts/*.json`, `.git/`). Forbid matches against anything in `git ls-files docs/ .claude/ scripts/ templates/ .devcontainer/ capabilities/`. Enforce as a runtime assertion *inside* `--self-check`.
- Positive-control test: assert three known files (`docs/QUALITY_GATES.md`, `templates/CLAUDE.template.md`, `LICENSE`) land in their expected classes.
- Negative-control test: inject an unclassified file in CI; assert exit 1 with that path named.

---

## Finding 2 — Rendered-template ownership is undefined for upgrades [BLOCKER]

**Claim.** §2: `templates/AGENTS.template.md` is `scaffold-template`; rendered downstream `AGENTS.md` is `scaffold-generated-once`. §6 provides upgrade behavior only for `scaffold` files.

**Why weak.** `scaffold-generated-once` is never touched on upgrade by class definition. So when the scaffold updates `CLAUDE.template.md` to add a critical instruction, every existing downstream gets zero notification. Rendered docs are write-once, forever — the asymmetry between template updates and frozen rendered output is silent.

**Failure scenario.** Scaffold v2 adds "never run `rm -rf /`" to `CLAUDE.template.md`. Every v1-enriched project keeps the v1 instruction. `--check` is silent because the file is project-owned by class.

**Remediation.**
- Split `scaffold-generated-once` into: `bootstrap-frozen` (SPEC.md, ARCHITECTURE.md — truly never re-render) and `bootstrap-with-template-tracking` (CLAUDE.md, AGENTS.md — store `template_sha` in manifest; surface advisory drift on upgrade when source template changed; never auto-overwrite).
- Add `--check --include-templates` to report template-source drift so teams can manually pull updates.

---

## Finding 3 — Allowlist collision on novel scaffold paths is unspecified [BLOCKER]

**Claim.** §2: "Anything not enumerated in `.scaffold/manifest.json` is implicitly project-owned."

**Why weak.** True for the *existing* manifest. Says nothing about `--upgrade` when the *new* scaffold version enumerates a path the project already owns.

**Failure scenario.** Scaffold v2 adds `docs/RUNBOOK.md` as `scaffold`. meewar2 today already has `docs/RUNBOOK.md` (project-original). On `--upgrade`:
- treat as new install → silent overwrite, data loss.
- treat as drift → no sha pair to compare; framing is wrong.
- error → unblockable without manual intervention.

The memo picks none.

**Remediation.**
- Define a fourth upgrade state: **"collision-novel"** — manifest absent, scaffold-new present, on-disk file exists. Default: refuse, exit 3, name path. Require explicit `--adopt PATH` (start tracking existing content) or `--rename-local PATH=NEWPATH`. Never silent overwrite.
- Slice A acceptance test: synthetic project with `docs/RUNBOOK.md`; scaffold adds same path; `--upgrade --dry-run` exits 3 and names it.

---

## Finding 4 — Schema migration is stubbed, not designed [MAJOR]

**Claim.** "Migrations table keyed by `(from_version, to_version)`. Test v0→v3 jump."

**Why weak.** A 2D table doesn't scale (N² entries). Memo does not specify linear-chain vs. pairwise, nor ownership/location/testing.

**Failure scenario.** Scaffold ships v3 directly (v2 abandoned mid-branch). User with v1 manifest upgrades. (v1→v3) entry missing because author assumed chaining. Engine errors; user has no path forward.

**Remediation.**
- **Linear chain only**: each release ships exactly one `migrations/vN_to_vN+1.py` exporting `def migrate(manifest_dict) -> manifest_dict`. Engine composes in order.
- Each migration ships with a paired test: fixture old, expected new, round-trip and idempotency assertions.
- Migrations are pure functions (no I/O); engine handles read/write at the boundary.
- Add `--migrate-only` flag.

---

## Finding 5 — Lockfile concurrency model is wrong for the deployment surface [MAJOR]

**Claim.** "Advisory `.scaffold/manifest.json.lock` with PID + timestamp."

**Why weak.** PID+timestamp lockfiles fail on NFS (atomic create not guaranteed), container restarts (stale PID, lock retained), and Windows (stat semantics differ). CI matrices with parallel jobs sharing homedir hit this routinely.

**Remediation.**
- Use `fcntl.flock` (POSIX advisory lock) on the manifest file directly. Kernel-released on process exit; no stale-lock recovery. Warn-and-proceed on filesystems without flock support.
- Lock is **per-target-dir**, never per-scaffold-repo. Concurrent enrichment of *different* targets is supported.
- `--no-lock` escape hatch for CI with its own mutexes.
- Regression test: two subprocesses, same target, exactly one wins.

---

## Finding 6 — Drift detection has no normalization story [MAJOR]

**Claim.** §9.3: "engine hashes bytes verbatim; document that whitespace re-saves count as drift."

**Why weak.** Documented friction is still friction. CRLF on Windows in mixed teams converts every scaffold file to "drifted" the moment a Windows dev opens it. Trailing-newline strippers (editor default) trip drift on every save. "Document it" converts a tooling problem into a human-policy problem, which always loses — teams learn to ignore exit 3, then miss a real drift.

**Remediation.**
- Hash a **normalized** stream by default: LF line endings, strip trailing whitespace per line, single trailing newline. Store normalization recipe alongside `schema_version`.
- `--check --strict` for byte-exact audit.
- Per-file `text: true|false` annotation; binary files skip normalization.
- Fixtures: same file with LF/CRLF/CR-only → equal hash under normalized; distinct under `--strict`.

---

## Finding 7 — Deferring M9.4 strands every customized downstream project [BLOCKER]

**Claim.** §7: "Acceptable friction for one upgrade cycle."

**Why weak.** meewar2 has 5 drifted agents today. After `--upgrade`, the team `--keep-local`s each, and receives **zero** scaffold improvements to those agents going forward — the prompt repeats every cycle, so they `--keep-local` forever. Not friction; permanent stranding. The M9.4 deferral implicitly assumes it ships soon, with no commitment and no design hook in M9 to make later implementation possible without a schema break.

**Argument:** Don't block M9 on M9.4. But M9 *must* reserve the design space.

**Remediation (schema hooks M9 must ship):**
- Add optional `overlays: []` per manifest file entry, empty in M9. Reserves the field; no `schema_version` bump needed in M9.4.
- Reserve the `*.project.md` convention: `--check`/`--upgrade` ignore these files entirely in M9. M9.4 introduces concat semantics.
- Document `--keep-local` as a stop-gap for M9.0–M9.3, retired by M9.4.
- Add to acceptance: manifest entries include `overlays` field, even if unused.

---

## Finding 8 — Per-file copy is not atomic; "atomic manifest" is insufficient [MAJOR]

**Claim.** §5: "atomic manifest rename last; on copy failure, leave `.tmp` and exit 1." §9.2: "rerun is idempotent."

**Why weak.** `shutil.copy2` (current engine, line 108) is not atomic. SIGKILL/disk-full mid-copy leaves a partially written file. Manifest also unchanged → engine cannot distinguish partial-copy from real drift. Idempotency claim depends on destination matching scaffold-sha or absent; partial files break this.

**Failure scenario.** `--upgrade` on disk-full target. `docs/QUALITY_GATES.md` half-written. Manifest unchanged. User fixes disk, runs `--check` → "drifted." User assumes hand-edit, runs `--keep-local`. Half-written file becomes canonical. Silent corruption.

**Remediation (Slice C):**
- All copies write to `<dest>.scaffold-tmp` then `os.replace` (atomic same-filesystem rename). Drop `.tmp` on interrupt.
- On engine startup, sweep `**/*.scaffold-tmp`, log and remove orphans.
- Acceptance test: monkeypatch raise mid-write; assert no partial dest, manifest unchanged, second run completes.

---

## Finding 9 — Taxonomy gaps the memo doesn't acknowledge [MAJOR]

**Claim.** §2 declares the 4-class taxonomy "symmetric and complete."

**Why weak.** Real files don't fit cleanly:

- **`capabilities/project-capabilities.yaml`** — `scaffold-internal` in the scaffold, but downstream profile authors may want to *extend* it. Memo silent.
- **`.claude/settings.json`** — copied by `enrich-project.py` (line 230); downstream teams routinely modify (allow rules, hooks). Is it `scaffold-generated-once`? `scaffold` with expected drift? Not addressed.
- **`.scaffold/manifest.json`** itself — committed or gitignored? Memo silent. (Should be committed; provenance is team-shared.)
- **`scripts/enrich-project.py` downstream** — copied (manifest line 211). Re-running enrich uses the *local* copy, now stale vs. upstream. `scaffold` class with severe drift implications, unacknowledged.

**Remediation.**
- Add a class-assignment appendix to the memo: every file in `git ls-files` with its class. Gaps or coin-flips reveal taxonomy errors.
- Settle: `.claude/settings.json` = `bootstrap-with-template-tracking` (per Finding 2). `.scaffold/manifest.json` = project-committed. Downstream `enrich-project.py` = `scaffold`; engine warns when invoked from a downstream copy disagreeing with upstream sha.

---

## Finding 10 — Acceptance criteria are too soft for the planning gate [MAJOR]

**Claim.** Section 13 lists three weak criteria for planning-step completion.

**Remediation (proposed concrete acceptance criteria, replacing §13):**

1. `--self-check` on `master` exits non-zero before Slice A; exits zero after Slice A; the diff between the two states is exclusively a manifest extension and an implementation in `enrich-project.py`. No `ignore:` glob added that matches a path in `git ls-files docs/`.
2. Slice A includes a positive-control test asserting three named files land in their expected classes (per Finding 1).
3. Slice B `--reconcile` on a synthetic v0 project (no `.scaffold/`) produces a manifest where every file's `sha256` is reproducible by an independent script computing sha256 over the same byte normalization recipe.
4. Slice C `--upgrade` on a synthetic project with one drifted scaffold-owned file plus one collision-novel file (Finding 3) produces exit 3 and names both paths in the plan.
5. Slice C `--upgrade` interrupted by SIGKILL between two file copies leaves the target with no `*.scaffold-tmp` files after a clean re-run, and the manifest reflects the post-rerun state (Finding 8).
6. Slice C round-trip: enrich → modify scaffold-owned file → `--upgrade --keep-local` preserves the edit, updates only the manifest sha, and `--check` exits 0 thereafter.
7. `--uninstall --include-once` on a synthetic project removes all `scaffold` and `scaffold-generated-once` files; the project's non-manifest files are byte-identical before and after.
8. Manifest schema includes `overlays: []` per file entry as a reserved field (Finding 7).
9. Migration test fixture: synthetic v0 manifest → run engine that ships at v1 → manifest is now v1, content equivalent (Finding 4).
10. Concurrent enrichment of the same target by two processes: exactly one succeeds, the other exits with usage error and a non-zero code; no corrupted manifest (Finding 5).

---

## Finding 11 — Failure modes the memo missed [MAJOR]

- **11a. Symlinks.** `shutil.copy2` follows symlinks. If downstream `docs/` is a symlink to `external-repo/docs/`, `--upgrade` writes into someone else's repo. **Fix:** refuse when target subdirs realpath-escape the target; treat scaffold-side symlinks as `--self-check` errors.
- **11b. Secrets in scaffold files.** Contributor pastes a private key fixing a typo. Every downstream `--upgrade` distributes it. **Fix:** Slice C engine regex-scans (`AKIA[0-9A-Z]{16}`, `BEGIN PRIVATE KEY`, etc.) every file pre-install; refuses with exit 1 on match.
- **11c. Scaffold removes a file across versions.** §9.8 mentions a `removed` section but not behavior when teams depend on the file. **Fix:** require `--accept-removal PATH` per file; default leaves file, downgrades class to `scaffold-orphan`; `--reconcile --drop-orphans` cleans up.
- **11d. Manifest commit policy.** Mandate `.scaffold/manifest.json` is **committed**; otherwise upgrade history is per-clone and CI is non-deterministic.

---

## Finding 12 — No `--interactive` mode hampers cautious operators [MINOR]

**Claim.** §6: "No per-file interactive prompt — too slow, not scriptable."

**Why weak.** Conflates two cases. Scripted batch upgrades want non-interactive; a first-time operator on a year-old project with 30 drifted files wants per-file inspection. Both are legitimate.

**Remediation.** `--interactive` flag (mutex with `--yes`) prompts per file with `[k]eep / [t]ake / [d]iff / [s]top`. Default remains plan-then-confirm.

---

## Verdict

**Reject and re-plan.** Not a full re-plan — the 4-class taxonomy, the manifest-as-yaml-extension decision, and the 3-slice structure are sound. But three blockers (Findings 1, 2, 3) and the M9.4 reservation question (Finding 7) materially affect the *schema* that Slice A ships. Once the manifest format is in `capabilities/project-capabilities.yaml` and a `schema_version` is locked in, fixing these later costs a migration. Address Findings 1, 2, 3, and 7 in the memo, tighten acceptance criteria per Finding 10, and re-circulate. The remaining MAJOR findings (4, 5, 6, 8, 9, 11) can be addressed with binding commitments in the memo and concrete tests in Slice B/C without re-planning.

Estimated incremental memo work: 2–4 hours. Cheaper than discovering Finding 3 in a downstream `--upgrade` six months from now.
