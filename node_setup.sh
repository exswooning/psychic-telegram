#!/usr/bin/env bash
# node_setup.sh -- turn a fresh Ubuntu box into a Bitport worker node.
#
# A worker node runs the same migration code as the coordinator; the only
# difference is that it asks the coordinator who owns each user before
# touching anything (see user_claims.py). It needs four things:
#
#   1. the code and its dependencies
#   2. the tenant's service-account keys        (copied from the coordinator)
#   3. the tenant config + identity map          (copied from the coordinator)
#   4. BITPORT_COORDINATOR and BITPORT_NODE_TOKEN
#
# (2) and (3) are copied rather than fetched over the API deliberately: the
# service-account keys are the credentials for the whole tenant, and an
# endpoint that serves them to anything holding a node token would make that
# token equivalent to the keys themselves.
#
# Usage, from your workstation (which can reach both machines):
#
#   ./node_setup.sh <node-ssh-target> <coordinator-ssh-target> <coordinator-url> <node-token>
#
# e.g.
#   ./node_setup.sh ubuntu@100.x.y.z root@78.47.176.120 \
#       http://100.a.b.c:8090 "$TOKEN"
#
# The coordinator URL is whatever the NODE can reach -- a Tailscale address
# if both are on your tailnet, or the public HTTPS host.
set -euo pipefail

NODE="${1:?usage: node_setup.sh <node-ssh> <coordinator-ssh> <coordinator-url> <token>}"
COORD="${2:?missing coordinator ssh target}"
COORD_URL="${3:?missing coordinator url}"
TOKEN="${4:?missing node token}"

REPO="${BITPORT_REPO:-https://github.com/exswooning/psychic-telegram}"
BRANCH="${BITPORT_BRANCH:-workspace-migrator}"
DIR="${BITPORT_DIR:-/opt/bitport}"
ACCOUNT="${BITPORT_ACCOUNT:-7}"

say() { printf '\n== %s\n' "$*"; }

say "1/5  installing prerequisites on $NODE"
ssh "$NODE" "sudo apt-get update -qq && sudo apt-get install -y -qq \
    python3-venv python3-pip git rsync >/dev/null"

say "2/5  fetching the code into $DIR"
ssh "$NODE" "sudo mkdir -p $DIR && sudo chown \$(id -u):\$(id -g) $DIR && \
    if [ -d $DIR/.git ]; then cd $DIR && git fetch --quiet && \
         git checkout --quiet $BRANCH && git pull --quiet; \
    else git clone --quiet -b $BRANCH $REPO $DIR; fi && \
    cd $DIR && python3 -m venv .venv 2>/dev/null || true && \
    ./.venv/bin/pip install -q --upgrade pip && \
    ./.venv/bin/pip install -q -r requirements.txt"

say "3/5  copying tenant keys and ledger from $COORD"
# Staged through the workstation: the node and the coordinator need no
# trust relationship with each other, only with you.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
scp -q -r "$COORD:/root/migration/keys/$ACCOUNT" "$TMP/keys-$ACCOUNT"
scp -q "$COORD:/root/migration/migration.db" "$TMP/migration.db"
scp -q "$COORD:/root/migration/env.sh" "$TMP/env.sh" 2>/dev/null || true

ssh "$NODE" "mkdir -p $DIR/keys"
scp -q -r "$TMP/keys-$ACCOUNT" "$NODE:$DIR/keys/$ACCOUNT"
scp -q "$TMP/migration.db" "$NODE:$DIR/migration.db"
[ -f "$TMP/env.sh" ] && scp -q "$TMP/env.sh" "$NODE:$DIR/env.sh"
ssh "$NODE" "chmod 600 $DIR/keys/$ACCOUNT/*.json"

say "4/5  writing node configuration"
# The token never reaches a command line -- an env file, mode 600. A command
# line is readable by any process on the box via ps.
ssh "$NODE" "umask 077 && cat > $DIR/node.env <<EOF
BITPORT_COORDINATOR=$COORD_URL
BITPORT_NODE_TOKEN=$TOKEN
BITPORT_NODE_ID=\$(hostname)
EOF
chmod 600 $DIR/node.env"

say "5/5  verifying the node can reach the coordinator"
ssh "$NODE" "cd $DIR && set -a && . ./node.env && set +a && \
  ./.venv/bin/python -c \"
import user_claims as uc
print('node id      :', uc.node_id())
print('coordinator  :', uc.coordinator_url())
ok, why = uc.acquire($ACCOUNT, '__preflight__@invalid', node=uc.node_id())
print('reachable    :', ok or why)
uc.release($ACCOUNT, '__preflight__@invalid', node=uc.node_id())
\""

cat <<EOF

Node is ready. To run a migration on it:

  ssh $NODE
  cd $DIR && set -a && . ./node.env && set +a
  ./.venv/bin/python main.py --account-id $ACCOUNT migrate --services gmail

Start the same command on the coordinator at the same time. Each machine
claims users the other has not taken; neither can start a user the other
owns. Watch who is doing what with:

  GET $COORD_URL/api/v2/claims        (signed in as the account)
EOF
