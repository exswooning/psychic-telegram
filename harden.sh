#!/usr/bin/env bash
#
# harden.sh -- lock down the files that are worth locking down.
#
# What this DOES do: makes the secrets readable only by the account that
# runs Bitport, and owned by it. That matters for backups, for an rsync to
# somewhere less careful, for a container bind-mount, and for any future
# non-root user on the box.
#
# What it CANNOT do, stated plainly because the request that prompted it
# asked for exactly this: protect anything from a co-administrator who has
# root. Root can read another process's memory, its environment through
# /proc, and any key it holds in order to decrypt anything. File modes and
# at-rest encryption are both bypassed by root, not weakened by it. If the
# other people on this machine have root, they have the service-account
# keys for every tenant, the DWD admin password, and every customer's
# password hash -- and the only real fix is a machine where they do not.
#
# Safe to re-run.
set -uo pipefail
DIR="${1:-/root/migration}"
OWNER="${2:-root}"
fixed=0

note() { printf '  %s\n' "$*"; }

# 1. The credentials themselves.
for target in "$DIR/keys" /etc/bitport; do
  [ -d "$target" ] || continue
  chown -R "$OWNER:$OWNER" "$target" 2>/dev/null
  chmod 700 "$target"
  find "$target" -type f -exec chmod 600 {} + 2>/dev/null
  note "600/700 on $target"
  fixed=$((fixed + 1))
done

# 2. The ledgers. migration.db carries the accounts table -- password_hash
#    for every customer -- plus live session tokens and the audit log. It
#    was world-readable.
for f in "$DIR"/*.db "$DIR"/env.sh "$DIR"/identities.csv; do
  [ -f "$f" ] || continue
  chown "$OWNER:$OWNER" "$f" 2>/dev/null
  chmod 600 "$f"
  fixed=$((fixed + 1))
done
note "600 on the ledgers, env.sh and identities.csv"

# 3. Per-account data. data/accounts/<id>/migration.db is one tenant's
#    ledger; the inventories name every user in a tenant.
if [ -d "$DIR/data" ]; then
  chown -R "$OWNER:$OWNER" "$DIR/data" 2>/dev/null
  find "$DIR/data" -type f -exec chmod 600 {} + 2>/dev/null
  find "$DIR/data" -type d -exec chmod 700 {} + 2>/dev/null
  note "600/700 on $DIR/data"
fi
# Every top-level .json, not a list of the ones thought of at the time.
# The first version named inventories and manifests and left run_state.json,
# dms_metrics.json and staging_drives_before_sigkill.json world-readable --
# all three name real users or real Drive ids. An allowlist of secrets is
# only ever as good as the last person to extend it.
for f in "$DIR"/*.json; do
  [ -f "$f" ] && chmod 600 "$f" 2>/dev/null
done
note "600 on every top-level .json"

# 4. The tree itself. An rsync from a developer's laptop leaves files owned
#    by a UID that does not exist here, which shows as UNKNOWN and makes
#    every later permission question harder to answer than it should be.
chown -R "$OWNER:$OWNER" "$DIR" 2>/dev/null
chmod 700 "$DIR"
note "$DIR owned by $OWNER, mode 700"

echo
echo "  Done. This protects against backups, stray copies and future"
echo "  unprivileged users. It does NOT protect against anyone who has"
echo "  root on this machine -- see the header."
