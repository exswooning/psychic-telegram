#!/usr/bin/env bash
#
# make-selfinstall.sh -- package Bitport into ONE self-extracting installer.
#
# Produces `bitport-selfinstall.sh`: a single file you scp to any fresh
# Debian/Ubuntu VPS and run (`sudo bash bitport-selfinstall.sh`). It carries
# the whole runtime tree embedded as a base64 gzip archive, unpacks it, and
# runs install.sh -- no git, no repo access, no token needed.
#
# Run this on a checkout to (re)generate the installer:
#   bash make-selfinstall.sh [output-file]
#
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-bitport-selfinstall.sh}"
[ -f install.sh ] || { echo "install.sh not found -- run from a checkout" >&2; exit 1; }

echo "packaging runtime tree..."
PAYLOAD="$(mktemp)"
# On macOS, bsdtar embeds Apple xattrs (com.apple.quarantine/provenance) as
# pax headers; GNU tar on the target then prints a wall of "Ignoring unknown
# extended header keyword" warnings on unpack. Strip them at the source.
export COPYFILE_DISABLE=1
TAR_MAC=(); [ "$(uname)" = Darwin ] && TAR_MAC=(--no-mac-metadata)
# Runtime code only: no deps, no build output, no logs/data/secrets, and not
# the installer we are writing. install.sh rebuilds the venv and the SPA.
tar "${TAR_MAC[@]}" \
  --exclude=.git --exclude=.venv --exclude=node_modules \
  --exclude='migration-webui/dist' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='*.pyc' --exclude='*.log' \
  --exclude='*.db' --exclude='*.db-*' --exclude=logs --exclude=data \
  --exclude=keys --exclude='*.png' --exclude=scratch --exclude='.claude' \
  --exclude="$OUT" --exclude=bitport-selfinstall.sh \
  -cf - . | gzip -9 | base64 > "$PAYLOAD"

SIZE=$(( $(wc -c < "$PAYLOAD") / 1024 ))
echo "  embedded payload: ${SIZE} KB (base64)"

# The self-extracting header: find the marker, stream everything after it
# through base64 -d | tar xz into a temp dir, then hand off to install.sh
# with whatever args/env the operator passed.
{
cat <<'HEADER'
#!/usr/bin/env bash
#
# bitport-selfinstall.sh -- self-contained Bitport installer (generated).
#
# Run on a fresh Debian/Ubuntu VPS:
#   sudo bash bitport-selfinstall.sh
#
# Unattended:
#   BITPORT_DOMAIN=bitport.example.com BITPORT_ADMIN_EMAIL=you@example.com \
#   BITPORT_ADMIN_PASSWORD=... NONINTERACTIVE=1 sudo -E bash bitport-selfinstall.sh
#
# Preview without changing anything:
#   bash bitport-selfinstall.sh --dry-run
#
set -euo pipefail

SELF="$0"
MARKER="__BITPORT_ARCHIVE_BELOW__"
LINE=$(awk "/^${MARKER}\$/{print NR+1; exit}" "$SELF")
[ -n "$LINE" ] || { echo "archive marker not found -- corrupt installer" >&2; exit 1; }

command -v base64 >/dev/null || { echo "base64 missing (install coreutils)" >&2; exit 1; }
DEST="$(mktemp -d /tmp/bitport-src.XXXXXX)"
trap 'rm -rf "$DEST"' EXIT
echo "unpacking Bitport into $DEST ..."
tail -n +"$LINE" "$SELF" | base64 -d | tar xz -C "$DEST"
[ -f "$DEST/install.sh" ] || { echo "install.sh missing after unpack -- corrupt installer" >&2; exit 1; }

cd "$DEST"
exec bash install.sh "$@"
HEADER
echo "__BITPORT_ARCHIVE_BELOW__"
cat "$PAYLOAD"
} > "$OUT"

rm -f "$PAYLOAD"
chmod +x "$OUT"
TOTAL=$(( $(wc -c < "$OUT") / 1024 ))
echo "wrote $OUT (${TOTAL} KB)"
echo "scp it to a VPS and run:  sudo bash $(basename "$OUT")"
