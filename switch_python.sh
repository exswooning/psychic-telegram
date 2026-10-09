#!/bin/bash
# Move /root/migration from Python 3.10 to 3.12, keeping 3.10 for a one-command rollback.
# Run as root on the box, in a deploy window: no migration, seed, reset, wipe or verify job.
#
#   switch_python.sh build      install python3.12 beside 3.10; build and import-check a trial venv
#   switch_python.sh switch     stop the units, keep .venv as .venv310, build .venv on 3.12, start
#   switch_python.sh rollback   stop the units, put .venv310 back as .venv, start
set -Eeuo pipefail
cd /root/migration
REQS="-r requirements.txt $([ -f requirements-control-plane.txt ] && echo -r requirements-control-plane.txt)"
# Every enabled bitport service that runs on the venv (not the X display ones), whatever its state now.
UNITS=$(systemctl list-unit-files --type=service --state=enabled --no-legend 'bitport*' | awk '{print $1}' \
        | grep -v -e xvfb -e x11vnc -e vnc || true)

busy() {
  # mirror.py too: a cycle in flight imports lazily from paths the switch moves away.
  pgrep -af "main.py|seed_sandbox|reset_target|wipe_t|verify_sample|tally.py|dms_migrate|full_setup|undo_migration|move_back|mirror.py" \
    | grep -v pgrep || true
}

check() {   # $1 = venv dir
  "$1/bin/python" -V
  "$1/bin/python" -m compileall -q -x '(^|/)(\.venv[0-9]*|node_modules|migration-webui)/' . >/dev/null
  "$1/bin/python" -c "import api_server, webui, main, run_watch, mirror, drive_engine, playwright.sync_api; print('imports ok')"
}

case "${1:-}" in
build)
  if ! command -v python3.12 >/dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
  fi
  rm -rf .venv312
  python3.12 -m venv .venv312
  .venv312/bin/pip install -q --upgrade pip
  .venv312/bin/pip install -q $REQS
  check .venv312
  old=$(.venv/bin/python -c "import importlib.metadata as m; print(m.version('playwright'))")
  new=$(.venv312/bin/python -c "import importlib.metadata as m; print(m.version('playwright'))")
  echo "playwright $old -> $new"
  [ "$old" = "$new" ] || .venv312/bin/python -m playwright install chromium
  echo "BUILD OK: nothing switched yet"
  ;;
switch)
  [ -d .venv312 ] || { echo "run build first" >&2; exit 1; }
  [ -z "$(busy)" ] || { echo "REFUSED: a job is running:"; busy; exit 1; }
  [ -e .venv310 ] && { echo "REFUSED: .venv310 already exists (an earlier switch?)" >&2; exit 1; }
  echo "units: $UNITS"
  systemctl stop $UNITS
  mv .venv .venv310
  trap 'echo "SWITCH FAILED -- putting 3.10 back"; systemctl stop $UNITS; rm -rf .venv; mv .venv310 .venv; systemctl start $UNITS; exit 1' ERR
  python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q $REQS          # wheels come from the cache the build just filled
  check .venv
  systemctl start $UNITS
  sleep 8
  systemctl is-active $UNITS
  trap - ERR
  rm -rf .venv312
  curl -fsS -o /dev/null -w "webui %{http_code}\n" http://127.0.0.1:8080/app/ || true
  curl -sS -o /dev/null -w "api %{http_code}\n" http://127.0.0.1:8090/api/v2/auth/me || true
  echo "SWITCHED: rollback with switch_python.sh rollback while .venv310 exists"
  ;;
rollback)
  [ -d .venv310 ] || { echo "no .venv310 to roll back to" >&2; exit 1; }
  echo "units: $UNITS"
  systemctl stop $UNITS
  rm -rf .venv
  mv .venv310 .venv
  systemctl start $UNITS
  sleep 8
  systemctl is-active $UNITS
  .venv/bin/python -V
  echo "ROLLED BACK"
  ;;
*)
  sed -n 2,8p "$0"; exit 2 ;;
esac
