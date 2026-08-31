#!/usr/bin/env bash
#
# make-tarball.sh -- package Bitport into a portable install tarball.
#
# Produces bitport.tar.gz: the runtime code, install.sh, and a PREBUILT SPA,
# with no deps, secrets, logs, or data. Copy it to a VPS by any means (flash
# drive, scp, whatever), then:
#
#   tar xzf bitport.tar.gz
#   cd bitport
#   sudo bash install.sh
#
# The bundled prebuilt SPA means the server needs no Node just to install.
#
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-bitport.tar.gz}"
STAGE="$(mktemp -d)"
DIR="$STAGE/bitport"
mkdir -p "$DIR"

echo "staging runtime tree..."
# Copy the tree, excluding everything the server rebuilds or must not carry.
# env.sh is gitignored but this rsync doesn't consult .gitignore -- it must
# be excluded explicitly. It is per-developer/per-tenant config (MIGRATION_DB,
# SOURCE/TARGET admin+domain, SA key paths); api_server.py's main() loads it
# into the process env unconditionally. Shipping the packager author's own
# env.sh once poisoned a fresh install with their local dev machine's
# MIGRATION_DB path, crash-looping bitport-api with "unable to open database
# file" from the moment it started.
rsync -a \
  --exclude='.git' --exclude='.venv' --exclude='node_modules' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
  --exclude='*.log' --exclude='*.db' --exclude='*.db-*' \
  --exclude='logs/' --exclude='data/' --exclude='keys/' --exclude='env.sh' \
  --exclude='*.png' --exclude='scratch/' --exclude='.claude/' \
  --exclude='bitport.tar.gz' --exclude='bitport-selfinstall.sh' \
  ./ "$DIR/"

# Keep the PREBUILT SPA (migration-webui/dist) -- that is the whole point of
# this bundle -- but drop node_modules if rsync pulled any in.
rm -rf "$DIR/migration-webui/node_modules"
[ -f "$DIR/migration-webui/dist/index.html" ] \
  && echo "  prebuilt SPA included" \
  || echo "  WARNING: no prebuilt SPA (run 'cd migration-webui && VITE_CP_BASE= npm run build' first)"

# A short readme at the archive root.
cat > "$DIR/INSTALL.txt" <<'TXT'
Bitport -- offline install bundle
=================================

  tar xzf bitport.tar.gz
  cd bitport
  sudo bash install.sh              # interactive

Unattended:

  BITPORT_DOMAIN=bitport.example.com BITPORT_ADMIN_EMAIL=you@example.com \
  BITPORT_ADMIN_PASSWORD='a-strong-password' NONINTERACTIVE=1 \
  sudo -E bash install.sh

Preview only (changes nothing):

  bash install.sh --dry-run

The server still needs internet for OS packages (apt: python venv, caddy,
chromium, xvfb). The SPA is prebuilt and bundled, so Node is not required.
Config, keys and data are NOT in this bundle -- the server starts clean and
you connect its tenants in the UI's Setup Wizard.
TXT

echo "packing $OUT ..."
# Strip macOS Apple-xattrs so a GNU-tar unpack on the target stays quiet.
export COPYFILE_DISABLE=1
TAR_MAC=(); [ "$(uname)" = Darwin ] && TAR_MAC=(--no-mac-metadata)
tar "${TAR_MAC[@]}" -C "$STAGE" -czf "$OUT" bitport
rm -rf "$STAGE"
SIZE=$(( $(wc -c < "$OUT") / 1024 ))
echo "wrote $OUT (${SIZE} KB)"
echo "copy it to the VPS, then: tar xzf $(basename "$OUT") && cd bitport && sudo bash install.sh"
