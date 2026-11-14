#!/usr/bin/env bash
# bitport -- the one command for a Bitport server. The installer puts it on your PATH.
#
#   bitport ui       open the live dashboard in this terminal (press q to leave it)
#   bitport url      show the address of the web app
#   bitport status   is Bitport running?
#   bitport logs     watch what Bitport is doing (press Ctrl+C to stop watching)
#   bitport help     this list
#
# You do not need to be root or to remember any paths: this command takes care of both.
INSTALL_DIR="__INSTALL_DIR__"

if [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null 2>&1 || { echo "Please run this as root (su -), or install sudo."; exit 1; }
  exec sudo "$0" "$@"
fi
cd "$INSTALL_DIR" 2>/dev/null || { echo "Bitport is not installed at $INSTALL_DIR."; exit 1; }
URL=$(sed -n 's/^URL=//p' "$INSTALL_DIR/install-info.txt" 2>/dev/null | head -1)

case "${1:-help}" in
  ui)
    shift
    export LANG="${LANG:-C.UTF-8}"
    exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" ui "$@"
    ;;
  url)
    echo "${URL:-Not recorded. Try http://YOUR-SERVER-ADDRESS/app}"
    ;;
  status)
    for unit in bitport-api bitport-webui caddy; do
      printf '  %-16s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || echo not-found)"
    done
    [ -n "$URL" ] && echo "  web app:         $URL"
    ;;
  logs)
    exec journalctl -u bitport-api -u bitport-webui -f --no-pager
    ;;
  help|-h|--help)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "I do not know '$1'. Here is what I can do:"; echo
    sed -n '4,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
