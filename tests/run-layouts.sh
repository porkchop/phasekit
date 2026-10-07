#!/usr/bin/env bash
# Run the loop-behaviour modules (tests/_layout.py LAYOUT_MODULES) under one
# layout, or both: the v0.19.0 invariance gate — every case must pass in the
# vendored layout AND with the engine outside the project (pinned).
#   bash tests/run-layouts.sh [pinned|vendored|both]   (default: pinned)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
which="${1:-pinned}"
mods="$(python3 -c 'import sys; sys.path.insert(0, "tests"); import _layout; print(" ".join("tests." + m for m in _layout.LAYOUT_MODULES))')"
case "$which" in
  both) layouts="vendored pinned" ;;
  pinned|vendored) layouts="$which" ;;
  *) echo "usage: $0 [pinned|vendored|both]" >&2; exit 2 ;;
esac
rc=0
for layout in $layouts; do
  echo "== PHASEKIT_TEST_LAYOUT=$layout"
  # shellcheck disable=SC2086
  PHASEKIT_TEST_LAYOUT="$layout" python3 -m unittest $mods || rc=1
done
exit "$rc"
