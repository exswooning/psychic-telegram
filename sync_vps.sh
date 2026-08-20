#!/usr/bin/env bash
# sync_vps.sh
# ===========
# Push the current working tree to an existing deployment and RESTART its UI.
#
# The restart is the point. An rsync alone updates the files while the running
# server keeps serving the page it already has in memory -- so the browser
# shows old code and every "I deployed the fix" is wrong. That happened: a
# render fix was rsynced to the VPS, the process was left running, and the bug
# appeared entirely unfixed through the tunnel.
#
#   ./sync_vps.sh root@203.0.113.10 /root/workspace-migrator [ssh-key] [port]

set -uo pipefail
TARGET="${1:?usage: ./sync_vps.sh user@host /remote/dir [key] [port]}"
DEST="${2:?missing remote directory}"
KEY="${3:-}"; PORT="${4:-22}"

SSH=(ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH+=(-i "$KEY")

# Same exclusions as deploy_remote.py: never overwrite the remote's own
# credentials or its resume ledger with whatever happens to be local.
rsync -az -e "${SSH[*]}" \
  --exclude '.git/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.venv/' --exclude 'scratch/' --exclude 'migration.db*' \
  --exclude '*.log' --exclude 'sandbox_manifest*.json' --exclude 'identities*.csv' \
  --exclude 'keys/' --exclude 'oauth/' --exclude 'env.sh' \
  "$(cd "$(dirname "$0")" && pwd)/" "$TARGET:$DEST/" || exit 1
echo "  synced to $TARGET:$DEST"

# Second pass, scoped to the built frontend, WITH --delete.
#
# Vite fingerprints every bundle (index-<hash>.js), so each deploy writes a
# new filename and the main rsync -- which has no --delete, deliberately,
# because the remote owns its keys, ledger and logs -- leaves every previous
# one behind forever. Measured on this VPS after ~70 deploys: 70 bundles,
# 50 MB of dist/assets on a box with 3.7 GB of RAM and a small disk, none of
# it reachable.
#
# --delete is safe HERE and nowhere else in this script: `npm run build`
# regenerates dist/ wholesale, so the local copy is the complete and
# authoritative set. Scoping it to one directory is what makes that true --
# a blanket --delete would take the remote's own state with it.
#
# Skipped entirely when there is no local build, so a deploy from a checkout
# that has not run `npm run build` cannot wipe the frontend the remote is
# currently serving.
LOCAL_DIST="$(cd "$(dirname "$0")" && pwd)/migration-webui/dist"
if [[ -f "$LOCAL_DIST/index.html" ]]; then
  rsync -az --delete -e "${SSH[*]}" \
    "$LOCAL_DIST/" "$TARGET:$DEST/migration-webui/dist/" || exit 1
  echo "  frontend dist synced (stale bundles pruned)"
else
  echo "  no local frontend build -- left the remote's dist/ untouched"
fi

# The remote is an rsync of the tree, not a git checkout, so anything there
# that asks git what it is running gets no answer -- benchmark_run.py
# recorded every VPS run as commit "unknown", which makes those runs
# incomparable to each other. Stamp what we just shipped.
# Syntax-check with the TARGET's interpreter before anything restarts.
#
# The dev machine and the VPS do not run the same Python (3.14 vs 3.10 here),
# and newer syntax parses locally while being a SyntaxError there -- nested
# same-type quotes in an f-string (PEP 701, 3.12+) did exactly this: the file
# imported fine in every local test and the VPS refused to parse it at all.
# A restart on top of that gives you a service that is down for a reason
# nothing in the deploy output mentions.
if ! "${SSH[@]}" "$TARGET" "cd $DEST && .venv/bin/python3 -m compileall -q . \
        -x '(\.venv|node_modules|migration-webui)'"; then
  echo "  DEPLOY ABORTED: the synced code does not compile under the target's Python." >&2
  echo "  Nothing was restarted; the previous version is still running." >&2
  exit 1
fi
echo "  syntax ok under the target's Python"

COMMIT="$(cd "$(dirname "$0")" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(cd "$(dirname "$0")" && git status --porcelain 2>/dev/null)" ]]; then
  COMMIT="$COMMIT-dirty"
fi
"${SSH[@]}" "$TARGET" "printf '%s\n' '$COMMIT' > $DEST/DEPLOYED_COMMIT"
echo "  stamped DEPLOYED_COMMIT=$COMMIT"

# Restart both servers, then PROVE each restart happened.
#
# Prefers systemd (see systemd/README.md) when bitport-webui.service is
# installed -- real supervision (auto-restart on crash, starts on boot)
# beats the nohup+pid-file+kill-by-port dance this replaces, which stays
# below only as a fallback for a box that hasn't had systemd/*.service
# copied onto it yet.
if "${SSH[@]}" "$TARGET" "systemctl list-unit-files bitport-webui.service >/dev/null 2>&1"; then
  "${SSH[@]}" "$TARGET" "systemctl restart bitport-webui bitport-api; sleep 2; \
    if systemctl is-active --quiet bitport-webui && systemctl is-active --quiet bitport-api; then \
      echo '  restarted via systemd: bitport-webui, bitport-api both active'; \
    else \
      echo '  RESTART DID NOT TAKE: one or both services failed to start' >&2; \
      systemctl status bitport-webui bitport-api --no-pager -l | tail -30; exit 1; \
    fi"
  exit $?
fi

# Fallback: no systemd units on this box. Two failure modes the PID-file
# approach alone misses, both of which report success:
#
#  * Killing by PID file misses an instance started some other way. The new
#    process then cannot bind the port and dies, while curl still gets 200
#    from the old one -- so a stale server looks like a fresh deploy. Kill
#    whatever holds the port instead; that is the thing serving the browser.
#  * `pkill -f webui` cannot be used at all here: ssh passes this whole string
#    as one argument, so the remote shell's own command line contains the
#    pattern and pkill kills the command running it.
#
# The final check compares the listening PID against the one just started. If
# they differ, something else owns the port and the deploy did not take.
PORT_UI=8080
"${SSH[@]}" "$TARGET" "set -u; cd $DEST; \
  OLD=\$(ss -ltnp 'sport = :$PORT_UI' 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); \
  if [ -n \"\${OLD:-}\" ]; then kill \"\$OLD\" 2>/dev/null; fi; \
  for i in 1 2 3 4 5 6 7 8 9 10; do \
    ss -ltn 'sport = :$PORT_UI' 2>/dev/null | grep -q LISTEN || break; sleep 1; done; \
  nohup .venv/bin/python webui.py --port $PORT_UI > webui.log 2>&1 & \
  NEW=\$!; echo \$NEW > webui.pid; sleep 4; \
  LIVE=\$(ss -ltnp 'sport = :$PORT_UI' 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); \
  CODE=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_UI/); \
  if [ \"\${LIVE:-}\" = \"\$NEW\" ] && { [ \"\$CODE\" = 200 ] || [ \"\$CODE\" = 302 ]; }; then \
    echo \"  restarted: pid \$NEW serving HTTP \$CODE\"; \
  else \
    echo \"  RESTART DID NOT TAKE: started \$NEW but pid \${LIVE:-none} owns the port (HTTP \$CODE)\"; \
    tail -5 webui.log; exit 1; \
  fi"
