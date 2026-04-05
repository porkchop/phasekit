# Kickoff

The recommended entrypoint is the unattended wrapper:

```bash
./scripts/run-until-done.sh
```

The wrapper uses `CONTINUE_PROMPT.txt` for all iterations. It instructs Claude to find the earliest unapproved phase automatically by reading `artifacts/phase-approval.json`.

## Manual kickoff

If you prefer to start manually, use the contents of `CONTINUE_PROMPT.txt` in Claude Code from the repository root.

## Custom prompt

To use a different prompt file:

```bash
./scripts/run-until-done.sh ./my-custom-prompt.txt
```
