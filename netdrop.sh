#!/usr/bin/env bash
# netdrop.sh SECONDS -- campaign F-net-3 / F-net-12 (fableplan2 P2.7): cut this box's outgoing traffic on its
# uplink for SECONDS, and put it back by itself.
#
#   NETDROP_IF=wlp2s0           the uplink to cut (the box is on Wi-Fi; refuses lo and anything named tailscale*)
#   NETDROP_KEEP_UDP=41641      tailscaled's own UDP port, let through so the operator's tunnel can survive the cut
#
# Order matters and is the whole point: the rule's REMOVAL is scheduled first (systemd-run --on-active, a
# transient timer that runs without this shell), and only then is the rule added. If the schedule cannot be
# made, nothing is cut. It never adds, deletes or edits anything on the Tailscale interface. Root only, 1 to
# 1800 seconds. Run by the campaign for the operator-allowed cut ("net", P0.7(b)) and by nobody else.
set -euo pipefail

SECS="${1:-}"
IFACE="${NETDROP_IF:-wlp2s0}"
KEEP_UDP="${NETDROP_KEEP_UDP:-41641}"
TAG="bitport-netdrop"

[[ "$SECS" =~ ^[0-9]+$ ]] && (( SECS >= 1 && SECS <= 1800 )) || { echo "usage: netdrop.sh SECONDS (1-1800)" >&2; exit 64; }
[[ "$IFACE" =~ ^(lo|tailscale.*)$ ]] && { echo "refusing to touch $IFACE" >&2; exit 64; }
[[ "$IFACE" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "bad interface name" >&2; exit 64; }
(( EUID == 0 )) || { echo "netdrop.sh needs root" >&2; exit 77; }
command -v systemd-run >/dev/null || { echo "no systemd-run: will not cut the network without a timer to restore it" >&2; exit 69; }

FW=(iptables)
command -v ip6tables >/dev/null && FW+=(ip6tables)

# What puts it back: every rule this script ever added (one loop each, so a doubled rule goes too).
UNDO=""
for fw in "${FW[@]}"; do
  UNDO+="while $fw -w -D OUTPUT -o $IFACE -m comment --comment $TAG -j DROP 2>/dev/null; do :; done; "
  UNDO+="while $fw -w -D OUTPUT -o $IFACE -p udp --sport $KEEP_UDP -m comment --comment $TAG -j ACCEPT 2>/dev/null; do :; done; "
done

# 1. schedule the removal, FIRST
systemd-run --quiet --on-active="${SECS}s" --unit="$TAG-undo-$(date +%s)" \
  --description="restore the network cut by netdrop.sh" /bin/sh -c "$UNDO"

# 2. then cut: the ACCEPT goes in above the DROP, both at the head of OUTPUT
for fw in "${FW[@]}"; do
  "$fw" -w -I OUTPUT 1 -o "$IFACE" -m comment --comment "$TAG" -j DROP
  "$fw" -w -I OUTPUT 1 -o "$IFACE" -p udp --sport "$KEEP_UDP" -m comment --comment "$TAG" -j ACCEPT
done

echo "netdrop: outgoing traffic on $IFACE is cut for ${SECS}s; the timer removes the rule at $(date -d "+${SECS} seconds" '+%H:%M:%S %Z')"
