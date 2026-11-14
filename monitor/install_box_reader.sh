#!/bin/sh
# monitor/install_box_reader.sh -- give the operator's job monitor (monitor/watch.py) READ-ONLY access
# to this box over one SSH key, with no password and no shell.
#
# Run as root ON THE BOX, with the monitor key's public half as the argument:
#     sudo sh install_box_reader.sh "ssh-ed25519 AAAA... bitport-monitor@macbookair"
#
# What it does:
#   1. /usr/local/sbin/bitport-monitor-read: four read-only subcommands (the incident feed from an
#      offset, one incident brief, sha256 of the Python files, the UI build time). Nothing writes.
#   2. /etc/sudoers.d/bitport-monitor: l3novo may run THAT script, and only it, without a password
#      (/opt/bitport is root-only). Checked with visudo before it is installed.
#   3. ~l3novo/.ssh/authorized_keys: the key, forced to that script -- no pty, no forwarding.
#
# Revoke: delete the key line, /etc/sudoers.d/bitport-monitor and /usr/local/sbin/bitport-monitor-read.
set -eu
PUB="${1:?usage: install_box_reader.sh '<ssh public key>'}"
USER_NAME="${BITPORT_MONITOR_USER:-l3novo}"
READER=/usr/local/sbin/bitport-monitor-read

cat > "$READER" <<'W'
#!/bin/sh
# Read-only access for the operator's job monitor; see monitor/install_box_reader.sh.
set -eu
cd /opt/bitport
case "${1:-}" in
  feed)
    off="${2:-0}"; case "$off" in ''|*[!0-9]*) exit 2;; esac
    f=logs/incidents/feed.log
    echo "SIZE=$(stat -c %s $f)"; tail -c +$((off + 1)) $f | head -c 200000 ;;
  brief)
    p="${2:-}"; n="${3:-2000}"
    case "$p" in /opt/bitport/logs/incidents/[0-9]*.md|logs/incidents/[0-9]*.md) ;; *) exit 2;; esac
    case "$p" in *..*|*' '*) exit 2;; esac
    case "$n" in ''|*[!0-9]*) exit 2;; esac
    head -c "$n" "$p" ;;
  pyhash) sha256sum *.py ;;
  uibuilt) stat -c %Y migration-webui/dist/index.html ;;
  *) echo "usage: feed <offset> | brief <path> [chars] | pyhash | uibuilt" >&2; exit 2 ;;
esac
W
chown root:root "$READER"; chmod 755 "$READER"

TMP=$(mktemp)
echo "$USER_NAME ALL=(root) NOPASSWD: $READER" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/bitport-monitor
rm -f "$TMP"
visudo -c >/dev/null

H=$(getent passwd "$USER_NAME" | cut -d: -f6)/.ssh
mkdir -p "$H"; touch "$H/authorized_keys"
KEYBODY=$(echo "$PUB" | awk '{print $2}')
grep -q "$KEYBODY" "$H/authorized_keys" || echo "command=\"set -f; sudo -n $READER \$SSH_ORIGINAL_COMMAND\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $PUB" >> "$H/authorized_keys"
chown -R "$USER_NAME:$USER_NAME" "$H"; chmod 700 "$H"; chmod 600 "$H/authorized_keys"
echo "installed: $READER, /etc/sudoers.d/bitport-monitor, key for $USER_NAME"
