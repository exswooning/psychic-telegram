#!/usr/bin/env bash
# start_control_plane.sh
# =======================
# (Re)starts api_server.py on whatever host this runs on, with CP_OPERATORS
# set. Exists because the plain `nohup python3 api_server.py &` from
# CONTROL_PLANE.md's own "Running it" section is easy to run once without
# CP_OPERATORS exported -- which silently leaves every operator a viewer
# (unlisted = viewer, by design, but that means a bare start is fail-closed
# in a way that looks like a bug from the UI: "'aryan' is a viewer; this
# action needs admin" with no clue that the fix is an env var on a restart
# nobody remembers doing). This script is the one place that mapping is
# supposed to live, so a restart can never again drop it by accident.
#
#   ./start_control_plane.sh                 # uses the list below
#   CP_OPERATORS="alice:admin,bob:viewer" ./start_control_plane.sh

set -uo pipefail
cd "$(dirname "$0")"

export CP_OPERATORS="${CP_OPERATORS:-aryan:admin}"
PORT=8090
mkdir -p logs

OLD="$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
if [[ -n "${OLD:-}" ]]; then
  kill "$OLD" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8; do
    ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN || break
    sleep 1
  done
fi

nohup .venv/bin/python api_server.py --port "$PORT" > logs/api_server.log 2>&1 &
NEW=$!
echo "$NEW" > api_server.pid
disown

sleep 3
LIVE="$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v2/whoami")"
if [[ "${LIVE:-}" == "$NEW" && "$CODE" == "200" ]]; then
  echo "  started: pid $NEW serving HTTP 200 -- operators: $CP_OPERATORS"
else
  echo "  DID NOT START CLEANLY: wanted pid $NEW, port owned by ${LIVE:-none} (HTTP $CODE)" >&2
  tail -10 logs/api_server.log >&2
  exit 1
fi
