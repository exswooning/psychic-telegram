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

# Without this, api_server.py starts with none of env.sh's config --
# SOURCE_DOMAIN, SOURCE_ADMIN, the SA key paths, TRANSFER_MODE, everything
# main.py/benchmark_run.py/reset_target.py read via Settings(). It was
# never caught because every subprocess this API launches (migrate,
# benchmark, provision, coverage) inherits THIS process's environment via
# `dict(os.environ)`, and every test of those features in this project so
# far was run by SSHing in and sourcing env.sh by hand rather than through
# the API -- so a write launched from the UI itself would have failed on
# missing config the whole time, silently, since Settings() defaults most
# of these to empty strings rather than raising.
if [[ -f env.sh ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./env.sh
  set +a
fi

# No default. A name in CP_OPERATORS is not a secret: defaulting it meant a
# publicly reachable host granted admin to anyone who guessed the name.
#
# The name alone no longer suffices either -- api_server only honours an
# X-Operator claim when the request also presents X-Operator-Token matching
# BITPORT_OPERATOR_TOKEN. Set both, or neither.
export CP_OPERATORS="${CP_OPERATORS:-}"
export BITPORT_OPERATOR_TOKEN="${BITPORT_OPERATOR_TOKEN:-}"
if [ -n "$CP_OPERATORS" ] && [ -z "$BITPORT_OPERATOR_TOKEN" ]; then
  echo "CP_OPERATORS is set but BITPORT_OPERATOR_TOKEN is not -- the header" >&2
  echo "will be ignored and those operators will have no access. Set both." >&2
fi
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
