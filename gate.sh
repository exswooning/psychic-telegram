#!/usr/bin/env bash
# The quick gate (tests/gate.txt): required before a deploy; the full suite runs nightly.
# Exits non-zero on any failure, and when a gate file is missing (a renamed test must not silently leave).
set -euo pipefail
cd "$(dirname "$0")"
files=$(grep -v -E '^\s*(#|$)' tests/gate.txt)
for f in $files; do [[ -f "$f" ]] || { echo "gate: missing $f" >&2; exit 2; }; done
exec .venv/bin/python -m pytest -q -p no:cacheprovider $files "$@"
