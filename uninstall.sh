#!/usr/bin/env bash
#
# uninstall.sh -- take Bitport off this machine.
#
# The inverse of install.sh, with one deliberate asymmetry: it removes the
# SOFTWARE and keeps the DATA unless you say otherwise.
#
# Two things in the install directory cannot be recreated by reinstalling.
# keys/ holds the service-account credentials for real tenants -- generating
# new ones means re-doing domain-wide delegation on both Google tenants by
# hand. migration.db and data/ hold the record of what has already been
# copied, and Drive's duplicate check reads it: lose the ledger and a resumed
# migration re-copies every file it already moved. Neither is worth losing to
# a flag you did not read.
#
#   ./uninstall.sh                 # say what would go. Changes nothing.
#   ./uninstall.sh --yes           # remove the software, keep keys and ledger
#   ./uninstall.sh --yes --purge   # remove those too (asks you to type it)
#   ./uninstall.sh --yes --node    # a worker node: no systemd, no /etc/bitport
#
set -uo pipefail

BOLD=$(tput bold 2>/dev/null || true); DIM=$(tput dim 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true); RED=$(tput setaf 1 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
step() { echo; echo "${BOLD}==> $*${RESET}"; }
ok()   { echo "  ${GREEN}✓${RESET} $*"; }
warn() { echo "  ${YELLOW}!${RESET} $*"; }
die()  { echo "  ${RED}✗ $*${RESET}" >&2; exit 1; }

YES=0 PURGE=0 NODE=0 FORCE=0 DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)  YES=1; shift ;;
    --purge)   PURGE=1; shift ;;
    --node)    NODE=1; shift ;;
    --force)   FORCE=1; shift ;;
    --dir)     DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

UNITS="bitport-webui.service bitport-api.service bitport-backup.timer
       bitport-backup.service xvfb.service x11vnc.service bitport-fleet.service"

# ---------------------------------------------------------------------------
# Where is it?
#
# Read from the unit rather than guessed: install.sh takes --dir and rewrites
# WorkingDirectory into it, so /root/migration is a default and not a fact.
# ---------------------------------------------------------------------------
if [ -z "$DIR" ]; then
  DIR=$(sed -n 's/^WorkingDirectory=//p' /etc/systemd/system/bitport-api.service 2>/dev/null | head -1)
fi
[ -z "$DIR" ] && [ -f "$HOME/bitport/node.env" ] && { DIR="$HOME/bitport"; NODE=1; }
[ -z "$DIR" ] && [ -d /root/migration ] && DIR="/root/migration"
[ -n "$DIR" ] || die "cannot find a Bitport install. Pass --dir /path/to/it."
case "$DIR" in
  /|/bin|/boot|/etc|/home|/lib|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "refusing to treat '$DIR' as a Bitport install directory" ;;
esac

step "Found"
echo "  install directory: $DIR"
[ "$NODE" = 1 ] && echo "  mode: worker node (no systemd, no /etc/bitport)"

# ---------------------------------------------------------------------------
# Is it busy?
#
# Removing the tree under a running migration leaves that migration writing
# into deleted files: it neither stops nor finishes, and the ledger it was
# updating is the thing you would need to resume from.
# ---------------------------------------------------------------------------
BUSY=$(pgrep -fa "main\.py.*(migrate|seed)" 2>/dev/null | grep -v uninstall | head -3)
if [ -n "$BUSY" ]; then
  echo
  warn "a migration looks like it is RUNNING on this machine:"
  echo "$BUSY" | sed 's/^/      /'
  [ "$FORCE" = 1 ] || die "refusing while it runs. Let it finish, or pass --force."
  warn "--force given: continuing anyway"
fi

# ---------------------------------------------------------------------------
# What would go
# ---------------------------------------------------------------------------
KEEPABLE="keys migration.db data identities.csv"
step "Would remove"
if [ "$NODE" != 1 ]; then
  for u in $UNITS; do
    [ -f "/etc/systemd/system/$u" ] && echo "  service   /etc/systemd/system/$u"
  done
fi
echo "  code      $DIR (excluding the items below unless --purge)"
if [ "$PURGE" = 1 ]; then
  for k in $KEEPABLE; do
    [ -e "$DIR/$k" ] && echo "  ${RED}DATA${RESET}      $DIR/$k"
  done
  [ "$NODE" != 1 ] && [ -d /etc/bitport ] && echo "  ${RED}SECRETS${RESET}   /etc/bitport (node token, admin password, UI creds)"
  [ "$NODE" = 1 ] && [ -f "$DIR/node.env" ] && echo "  ${RED}SECRETS${RESET}   $DIR/node.env (node token)"
else
  for k in $KEEPABLE; do
    [ -e "$DIR/$k" ] && echo "  ${DIM}keeping   $DIR/$k${RESET}"
  done
  [ "$NODE" != 1 ] && [ -d /etc/bitport ] && echo "  ${DIM}keeping   /etc/bitport${RESET}"
fi

if [ "$YES" != 1 ]; then
  echo
  echo "Nothing was changed. Re-run with --yes to do it${PURGE:+ (and --purge to include the data)}."
  exit 0
fi

# A typed confirmation for the irreversible half only. A --purge that took a
# bare -y would be one shell-history arrow-up away from destroying the
# credentials for somebody's tenant.
if [ "$PURGE" = 1 ]; then
  echo
  echo "${RED}${BOLD}--purge deletes the service-account keys and the migration ledger.${RESET}"
  echo "Re-doing domain-wide delegation on both tenants is the only way back."
  printf "Type PURGE to confirm: "
  read -r reply
  [ "$reply" = "PURGE" ] || die "not confirmed -- nothing was changed"
fi

# ---------------------------------------------------------------------------
# Do it
# ---------------------------------------------------------------------------
if [ "$NODE" != 1 ]; then
  step "Services"
  for u in $UNITS; do
    [ -f "/etc/systemd/system/$u" ] || continue
    systemctl stop "$u" >/dev/null 2>&1
    systemctl disable "$u" >/dev/null 2>&1
    rm -f "/etc/systemd/system/$u"
    ok "removed $u"
  done
  systemctl daemon-reload >/dev/null 2>&1
  systemctl reset-failed >/dev/null 2>&1

  step "Reverse proxy"
  # Only OUR config, and only if install.sh's own backup is there to restore.
  # Caddy may be fronting something else on this machine that has nothing to
  # do with Bitport -- asuswb already ran Nextcloud and SABnzbd.
  if [ -f /etc/caddy/Caddyfile ] && grep -q "127.0.0.1:8090" /etc/caddy/Caddyfile 2>/dev/null; then
    if [ -f /etc/caddy/Caddyfile.bitport-backup ]; then
      mv /etc/caddy/Caddyfile.bitport-backup /etc/caddy/Caddyfile
      ok "restored the Caddyfile that was there before Bitport"
    else
      rm -f /etc/caddy/Caddyfile
      ok "removed Bitport's Caddyfile"
    fi
    systemctl reload caddy >/dev/null 2>&1 || systemctl restart caddy >/dev/null 2>&1 || true
  else
    warn "the Caddyfile is not Bitport's -- left alone"
  fi
  # caddy itself is NOT removed: it is a general-purpose web server this
  # machine may be using for something else entirely.
fi

step "Files"
cd / || exit 1            # never delete the tree we are standing in
if [ "$PURGE" = 1 ]; then
  rm -rf "$DIR"
  ok "removed $DIR"
  if [ "$NODE" != 1 ]; then
    rm -rf /etc/bitport && ok "removed /etc/bitport"
  fi
else
  # Keep the unrecreatable things, remove everything else. Moving them aside
  # and back is simpler to get right than enumerating what to delete, and it
  # never leaves a half-deleted tree if something fails midway.
  SAVE=$(mktemp -d) || die "cannot make a temporary directory"
  for k in $KEEPABLE; do
    [ -e "$DIR/$k" ] && mv "$DIR/$k" "$SAVE/" 2>/dev/null
  done
  rm -rf "$DIR"
  mkdir -p "$DIR"
  moved=0
  for k in $KEEPABLE; do
    [ -e "$SAVE/$k" ] && { mv "$SAVE/$k" "$DIR/"; moved=$((moved + 1)); }
  done
  rmdir "$SAVE" 2>/dev/null
  ok "removed the code from $DIR, kept $moved data item(s)"
  [ "$NODE" != 1 ] && ok "kept /etc/bitport (delete it yourself, or re-run with --purge)"
fi

step "Done"
echo "  Bitport is off this machine."
if [ "$PURGE" != 1 ]; then
  echo "  ${DIM}Still there: $DIR (the keys and the ledger).${RESET}"
  echo "  ${DIM}Reinstalling over it picks the ledger and keys back up.${RESET}"
fi
