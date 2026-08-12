#!/usr/bin/env bash
# connect_vps.sh
# ==============
# Open (or check, or close) the SSH tunnel that lets a browser on this
# machine reach the VPS's webui.py (8080), api_server.py (8090), and the
# VNC server (5900) watching the virtual display dwd_helper.py's real
# Chrome runs on -- all three bind 127.0.0.1-only on the VPS on purpose,
# the tunnel is the access control, not a firewall rule to remember. This
# came up repeatedly in this session as "the tunnel dropped again"; this
# script exists so reconnecting is one idempotent command instead of
# re-deriving the ssh -L incantation each time.
#
# The VNC port matters specifically for 2FA/captcha during a Quick Setup
# DWD run: dwd_helper.py's browser is real but invisible (Xvfb has no
# physical screen), and email/password auto-fill stops at whatever prompt
# comes after the password. Point any VNC client at localhost:5900 (with
# the password from /root/.vnc/passwd on the VPS) to watch and finish it
# by hand.
#
#   ./connect_vps.sh                              # connect with defaults
#   ./connect_vps.sh user@host [key] [ui] [cp] [vnc] # override host/key/ports
#   ./connect_vps.sh --status                      # is it up right now?
#   ./connect_vps.sh --stop                        # tear it down
#
# Re-running while already connected is a no-op that reports success --
# safe to put in a "just in case" step before opening the dashboard.

set -uo pipefail

DEFAULT_TARGET="root@78.47.176.120"
DEFAULT_KEY="$HOME/.ssh/workspace_migrator_vps"
DEFAULT_UI_PORT=8080
DEFAULT_CP_PORT=8090
DEFAULT_VNC_PORT=5900

STATE_DIR="$HOME/.workspace_migrator"
PIDFILE="$STATE_DIR/tunnel.pid"
LOGFILE="$STATE_DIR/tunnel.log"
mkdir -p "$STATE_DIR"

tunnel_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

port_alive() { curl -s -o /dev/null --max-time 3 "http://localhost:$1/" 2>/dev/null; }

case "${1:-}" in
  --stop)
    if pid="$(tunnel_pid)"; then
      kill "$pid" 2>/dev/null
      rm -f "$PIDFILE"
      echo "  tunnel stopped (was pid $pid)"
    else
      echo "  no tunnel running (nothing to stop)"
    fi
    exit 0
    ;;
  --status)
    if pid="$(tunnel_pid)"; then
      echo "  tunnel up: pid $pid"
    else
      echo "  tunnel not running"
    fi
    exit 0
    ;;
esac

TARGET="${1:-$DEFAULT_TARGET}"
KEY="${2:-$DEFAULT_KEY}"
UI_PORT="${3:-$DEFAULT_UI_PORT}"
CP_PORT="${4:-$DEFAULT_CP_PORT}"
VNC_PORT="${5:-$DEFAULT_VNC_PORT}"

echo "  target : $TARGET"
echo "  key    : $KEY"
echo "  ports  : $UI_PORT (webui) + $CP_PORT (control plane) + $VNC_PORT (VNC) -> localhost"
echo

if [[ ! -f "$KEY" ]]; then
  echo "  ERROR: key not found at $KEY" >&2
  echo "  pass the right path as the 2nd argument: ./connect_vps.sh $TARGET /path/to/key" >&2
  exit 1
fi

# Already connected and actually answering -- nothing to do.
if pid="$(tunnel_pid)"; then
  if port_alive "$UI_PORT"; then
    echo "  already connected: pid $pid, http://localhost:$UI_PORT is answering"
    exit 0
  fi
  echo "  found a tracked tunnel (pid $pid) but it isn't answering -- restarting it"
  kill "$pid" 2>/dev/null
  rm -f "$PIDFILE"
fi

# A process we didn't start may already hold these ports (another terminal,
# a previous session). Don't kill something this script doesn't own.
for p in "$UI_PORT" "$CP_PORT" "$VNC_PORT"; do
  holder="$(lsof -ti "tcp:$p" -sTCP:LISTEN 2>/dev/null | head -1)"
  if [[ -n "$holder" ]]; then
    echo "  ERROR: port $p is already in use by pid $holder, and it isn't this script's tunnel." >&2
    echo "  Check what it is (ps -p $holder) before running this again, or stop it yourself." >&2
    exit 1
  fi
done

echo "  testing bare SSH connectivity..."
if ! ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
       "$TARGET" true 2>"$STATE_DIR/ssh-test.err"; then
  echo "  ERROR: could not SSH to $TARGET with $KEY" >&2
  tail -5 "$STATE_DIR/ssh-test.err" >&2
  exit 1
fi
echo "  SSH OK"

nohup ssh -i "$KEY" -N \
  -L "$UI_PORT:localhost:$UI_PORT" \
  -L "$CP_PORT:localhost:$CP_PORT" \
  -L "$VNC_PORT:localhost:$VNC_PORT" \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=accept-new \
  "$TARGET" < /dev/null > "$LOGFILE" 2>&1 &
NEW_PID=$!
disown
echo "$NEW_PID" > "$PIDFILE"

for i in 1 2 3 4 5 6 7 8; do
  kill -0 "$NEW_PID" 2>/dev/null || break
  port_alive "$UI_PORT" && break
  sleep 1
done

if ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "  ERROR: tunnel process died immediately -- see $LOGFILE" >&2
  tail -10 "$LOGFILE" >&2
  rm -f "$PIDFILE"
  exit 1
fi

if port_alive "$UI_PORT"; then
  echo "  connected: pid $NEW_PID"
  echo "  open http://localhost:$UI_PORT"
  echo "  VNC (watch/complete 2FA during a DWD run): vnc://localhost:$VNC_PORT"
  echo "  password is in /root/.vnc/passwd on the VPS"
else
  echo "  tunnel process is running (pid $NEW_PID) but $UI_PORT isn't answering yet."
  echo "  webui.py may just be slow to start on the far end -- check again with --status,"
  echo "  or see $LOGFILE if it stays down."
fi
