#!/bin/sh
# One READ on the box for monitor/watch.py, over the monitor's own SSH key. The box forces that key to
# /usr/local/sbin/bitport-monitor-read (monitor/install_box_reader.sh), so the only things this can ask
# are its four subcommands: feed <offset> | brief <path> [chars] | pyhash | uibuilt.
exec ssh -i "${BITPORT_MONITOR_KEY:-$HOME/.ssh/bitport_monitor}" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o ConnectTimeout=20 "${BITPORT_BOX_HOST:-l3novo@100.79.158.38}" "$@"
