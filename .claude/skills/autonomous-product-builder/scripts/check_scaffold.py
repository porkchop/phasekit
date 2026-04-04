#!/usr/bin/env python3
from pathlib import Path
import json
import sys

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
required = [
    'docs/SPEC.md',
    'docs/ARCHITECTURE.md',
    'docs/PHASES.md',
    'docs/QUALITY_GATES.md',
    '.claude/agents',
    '.claude/settings.json',
    'scripts/run-phase.sh',
    'scripts/run-until-done.sh',
]
missing = [p for p in required if not (ROOT / p).exists()]
result = {
    'root': str(ROOT),
    'missing': missing,
    'ok': len(missing) == 0,
}
print(json.dumps(result, indent=2))
