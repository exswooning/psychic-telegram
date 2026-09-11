#!/usr/bin/env bash
# install_node.sh -- join this machine to a Bitport coordinator. macOS + Linux.
#
# Run it ON the machine that is joining, unlike node_setup.sh, which drives a
# fresh Ubuntu box over SSH from a third machine. A laptop is not something
# you SSH into: it moves networks, it sleeps, and it is the machine you are
# already sitting at.
#
#   ./install_node.sh --coordinator http://100.x.y.z --token "$TOKEN"
#
# The coordinator URL is whatever THIS machine can reach. On a tailnet that
# is the coordinator's Tailscale address -- no port forwarding, no public
# exposure, and the token still required on every call.
#
# Use the CADDY port (80 by default, or whatever install.sh picked when 80
# was taken), NOT 8090. api_server.py binds 127.0.0.1 only -- deliberately,
# see its own --host warning -- so 8090 is unreachable from another machine
# and a node aimed at it just gets connection refused. Caddy proxies
# /api/v2/* through to it, which is exactly the path a node calls.
#
# It deliberately does NOT fetch the tenant's service-account keys. Those are
# the credentials for the whole tenant, and an endpoint that served them to
# anything holding a node token would make the token equivalent to the keys.
# Copy them yourself; the last step prints the exact command.
set -euo pipefail

# Flags OR the environment. The env form is what makes a piped one-liner
# possible -- `curl ... | bash` has no way to pass arguments, and that is
# the shape the Nodes page hands out.
COORD="${BITPORT_COORDINATOR:-}" TOKEN="${BITPORT_NODE_TOKEN:-}"
DIR="${BITPORT_DIR:-$HOME/bitport}" ACCOUNT="${BITPORT_ACCOUNT:-7}"
REPO="${BITPORT_REPO:-https://github.com/exswooning/psychic-telegram}"
BRANCH="${BITPORT_BRANCH:-workspace-migrator}"

while [ $# -gt 0 ]; do
  case "$1" in
    --coordinator) COORD="$2"; shift 2 ;;
    --token)       TOKEN="$2"; shift 2 ;;
    --dir)         DIR="$2"; shift 2 ;;
    --account)     ACCOUNT="$2"; shift 2 ;;
    -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$COORD" ] || { echo "no coordinator: pass --coordinator or set BITPORT_COORDINATOR" >&2; exit 2; }
[ -n "$TOKEN" ] || { echo "no node token: pass --token or set BITPORT_NODE_TOKEN" >&2; exit 2; }
# api_server.py binds 127.0.0.1, so a node aimed at 8090 is unreachable from
# any other machine. Caught here rather than in the reachability probe at the
# end, where it presents as a network fault on a correctly configured box.
case "$COORD" in
  *:8090|*:8090/) echo "8090 is api_server's LOOPBACK port and is not reachable from this machine. Use the Caddy port the installer reported (80, or 81 if 80 was taken)." >&2; exit 2 ;;
esac

say() { printf '\n== %s\n' "$*"; }

say "1/5  checking prerequisites"
OS="$(uname -s)"
command -v git >/dev/null || { echo "git is not installed" >&2; exit 1; }
PY=""
for c in python3.12 python3.11 python3 python; do
  command -v "$c" >/dev/null || continue
  # 3.10 is the floor: the code uses `X | None` annotations at runtime.
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || {
  echo "no Python 3.10+ found." >&2
  case "$OS" in
    Darwin) echo "  install one with:  brew install python@3.12" >&2 ;;
    *)      echo "  install one with:  sudo apt-get install -y python3-venv python3-pip" >&2 ;;
  esac
  exit 1; }
echo "  python: $($PY --version) at $(command -v $PY)"
echo "  os    : $OS"

say "2/5  fetching the code into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet && git -C "$DIR" checkout --quiet "$BRANCH" \
    && git -C "$DIR" pull --quiet
else
  git clone --quiet -b "$BRANCH" "$REPO" "$DIR"
fi

say "3/5  creating the virtualenv"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

say "4/5  writing node configuration"
# Never a command line: argv is readable by every process on the box.
umask 077
cat > "$DIR/node.env" <<ENVEOF
BITPORT_COORDINATOR=$COORD
BITPORT_NODE_TOKEN=$TOKEN
BITPORT_NODE_ID=$(hostname)
ENVEOF
chmod 600 "$DIR/node.env"
echo "  wrote $DIR/node.env (mode 600)"

say "5/5  can this machine reach the coordinator?"
set +e
( cd "$DIR" && set -a && . ./node.env && set +a && \
  ./.venv/bin/python -c "
import user_claims as uc
print('  node id     :', uc.node_id())
print('  coordinator :', uc.coordinator_url())
try:
    ok, why = uc.acquire($ACCOUNT, '__preflight__@invalid', node=uc.node_id())
    print('  reachable   :', 'yes' if ok else why)
    uc.release($ACCOUNT, '__preflight__@invalid', node=uc.node_id())
except Exception as exc:
    print('  reachable   : NO --', str(exc)[:160])
    raise SystemExit(1)
" )
RC=$?
set -e

cat <<DONEEOF

$( [ $RC -eq 0 ] && echo "Node is ready." || echo "Node installed, but it could NOT reach the coordinator." )

Still needed -- the tenant credentials, which this script will not fetch.
Run the first line ON THE COORDINATOR, in whatever directory Bitport is
installed in there (/root/migration and /opt/bitport are both common):

  ./export_node_config.py --account-id $ACCOUNT --out node-config.db

Then copy both to this machine:

  scp    <coordinator>:<bitport-dir>/node-config.db   $DIR/migration.db
  scp -r <coordinator>:<bitport-dir>/keys/$ACCOUNT    $DIR/keys/$ACCOUNT
  chmod 600 $DIR/keys/$ACCOUNT/*.json

node-config.db, not the coordinator's own migration.db. That file also
holds every customer's password hash, live sessions and the audit log, and
a node reads exactly five columns of one table out of it.

Then, to take part in a migration:

  cd $DIR && set -a && . ./node.env && set +a
  ./.venv/bin/python main.py --account-id $ACCOUNT migrate --services gmail

Read BEFORE you run Drive across two machines: each node keeps its OWN
ledger, so Drive's duplicate check does not see the other node's work.
Gmail's does -- it asks the target for the Message-ID. See MULTINODE.md.
DONEEOF
