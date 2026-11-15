#!/usr/bin/env bash
# The quick gate (tests/gate.txt): required before a deploy; the full suite runs nightly.
# Exits non-zero on any failure, and when a gate file is missing (a renamed test must not silently leave).
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "--tree-id" ]]; then      # the tracked tree as it is now: commit, uncommitted edits, new files
  { git rev-parse HEAD; git diff HEAD --binary; git ls-files --others --exclude-standard -- '*.py' '*.sh' | sort; } \
    | shasum -a 256 | cut -c1-40
  exit 0
fi
files=$(grep -v -E '^\s*(#|$)' tests/gate.txt)
for f in $files; do [[ -f "$f" ]] || { echo "gate: missing $f" >&2; exit 2; }; done
.venv/bin/python -m pytest -q -p no:cacheprovider $files "$@"
# Passed: record WHICH tree passed, so sync_vps.sh ships only that tree (a change after this is untested).
./gate.sh --tree-id > .gate-passed
