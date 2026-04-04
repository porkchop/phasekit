# Kickoff

The recommended entrypoint is the unattended wrapper:

```bash
./scripts/run-until-done.sh
```

That wrapper uses `KICKOFF_PROMPT.txt` on the first iteration and `CONTINUE_PROMPT.txt` on later iterations.

## Manual kickoff

If you prefer to start manually, use the contents of `KICKOFF_PROMPT.txt` in Claude Code from the repository root.


## Meta-project kickoff

To improve the scaffold itself, use `META_KICKOFF_PROMPT.txt` instead of `KICKOFF_PROMPT.txt` and keep the lead in audit mode.
