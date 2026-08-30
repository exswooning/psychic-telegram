#!/usr/bin/env bash
#
# install.sh -- Bitport server installer.
#
# One command turns a fresh Debian/Ubuntu server into a running Bitport
# host, the way cPanel's installer does: install dependencies, lay down the
# code and a Python venv, build the SPA, write the root-only config, install
# and start the systemd services, front them with Caddy (real HTTPS), and
# create the first superadmin account.
#
# Interactive by default; every prompt also reads an env var, so a fully
# unattended install is:
#
#   BITPORT_DOMAIN=bitport.example.com BITPORT_ADMIN_EMAIL=you@example.com \
#   BITPORT_ADMIN_PASSWORD=... NONINTERACTIVE=1 sudo -E bash install.sh
#
# Safe to re-run: every step checks before it acts.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# tiny UI helpers
# ---------------------------------------------------------------------------
BOLD=$(tput bold 2>/dev/null || true); DIM=$(tput dim 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true); RED=$(tput setaf 1 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
step() { echo; echo "${BOLD}==> $*${RESET}"; }
ok()   { echo "  ${GREEN}✓${RESET} $*"; }
warn() { echo "  ${YELLOW}!${RESET} $*"; }
die()  { echo "  ${RED}✗ $*${RESET}" >&2; exit 1; }

DRY_RUN=0
for a in "$@"; do case "$a" in
  --dry-run|--check) DRY_RUN=1 ;;
  -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac; done
run() { if [ "$DRY_RUN" = 1 ]; then echo "  ${DIM}[dry-run] $*${RESET}"; else eval "$@"; fi; }

# ---------------------------------------------------------------------------
# 0. preflight
# ---------------------------------------------------------------------------
step "Preflight"
if [ "$(id -u)" != 0 ]; then
  [ "$DRY_RUN" = 1 ] && warn "not root -- fine for --dry-run; a real install needs sudo" \
                     || die "run as root (sudo -E bash install.sh)"
fi
command -v apt-get >/dev/null || { [ "$DRY_RUN" = 1 ] && warn "no apt-get (dry-run on non-Debian is fine)" || die "this installer supports Debian/Ubuntu (apt) only"; }
PRETTY_NAME=""
[ -r /etc/os-release ] && . /etc/os-release
ok "OS: ${PRETTY_NAME:-unknown}"
ARCH=$(dpkg --print-architecture 2>/dev/null || echo unknown)
ok "arch: $ARCH"

# The repo is whatever directory this script lives in.
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC_DIR/webui.py" ] && [ -f "$SRC_DIR/api_server.py" ] \
  || die "run this from a Bitport checkout (webui.py/api_server.py not found next to install.sh)"
ok "source: $SRC_DIR"

# ---------------------------------------------------------------------------
# 1. gather settings (prompt, or take from env when NONINTERACTIVE)
# ---------------------------------------------------------------------------
step "Settings"
ask() {  # ask VAR "prompt" "default"
  local var="$1" prompt="$2" def="${3:-}" cur="${!1:-}"
  if [ -n "$cur" ]; then echo "  $prompt: $cur ${DIM}(from env)${RESET}"; return; fi
  if [ "${NONINTERACTIVE:-0}" = 1 ]; then printf -v "$var" '%s' "$def"; echo "  $prompt: ${def:-<empty>}"; return; fi
  local reply; read -r -p "  $prompt${def:+ [$def]}: " reply || true
  printf -v "$var" '%s' "${reply:-$def}"
}
ask_secret() {  # ask_secret VAR "prompt"
  local var="$1" prompt="$2" cur="${!1:-}"
  if [ -n "$cur" ]; then echo "  $prompt: ${DIM}(from env)${RESET}"; return; fi
  if [ "${NONINTERACTIVE:-0}" = 1 ]; then printf -v "$var" '%s' "$(openssl rand -base64 12)"; echo "  $prompt: ${DIM}(generated)${RESET}"; return; fi
  local reply; read -r -s -p "  $prompt (blank = generate): " reply || true; echo
  printf -v "$var" '%s' "${reply:-$(openssl rand -base64 12)}"
}

ask INSTALL_DIR         "Install directory"        "/root/migration"
ask BITPORT_DOMAIN      "Public domain for HTTPS"  ""
ask BITPORT_ADMIN_EMAIL "First superadmin email"   "admin@bitport.local"
ask_secret BITPORT_ADMIN_PASSWORD "Superadmin password"
ask ENABLE_BROWSER      "Enable browser automation for DWD/DMS (yes/no)" "yes"

# The install dir gets an rsync'd tree; a system path here would be
# catastrophic. Require an absolute, non-system, reasonably specific path.
INSTALL_DIR="${INSTALL_DIR%/}"                       # strip a trailing slash
case "$INSTALL_DIR" in
  "" | "/" ) die "refusing install dir '${INSTALL_DIR:-/}': pick a directory like /opt/bitport" ;;
  /bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var )
    die "refusing a system directory '$INSTALL_DIR' -- use something like /opt/bitport or /root/migration" ;;
esac
[ "${INSTALL_DIR#/}" != "$INSTALL_DIR" ] || die "install dir must be an absolute path (start with /)"
case "$INSTALL_DIR" in "$SRC_DIR"|"$SRC_DIR"/*) die "install dir must not be inside the bundle ($SRC_DIR)";; esac
ok "install directory: $INSTALL_DIR"

[ -n "$BITPORT_DOMAIN" ] || warn "no domain given -- Caddy will serve on :80 only (no HTTPS); set BITPORT_DOMAIN to enable TLS"
case "${#BITPORT_ADMIN_PASSWORD}" in [0-9]) die "password too short";; esac
[ "${#BITPORT_ADMIN_PASSWORD}" -ge 12 ] || die "superadmin password must be at least 12 characters"

# ---------------------------------------------------------------------------
# 2. system packages
# ---------------------------------------------------------------------------
step "System packages"
PKGS="python3 python3-venv python3-pip git rsync curl ca-certificates openssl"
[ "$ENABLE_BROWSER" = yes ] && PKGS="$PKGS xvfb"
run "apt-get update -qq"
run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS"
ok "base packages installed"

# Node.js (for the SPA build) -- only if npm is missing AND no prebuilt SPA
# was shipped in the archive. The flash-drive tarball bundles dist/, so a
# server with no internet for NodeSource still installs.
if [ -f "$SRC_DIR/migration-webui/dist/index.html" ]; then
  ok "prebuilt SPA present -- skipping Node"
elif ! command -v npm >/dev/null; then
  run "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs"
  ok "node: $(command -v node >/dev/null && node -v || echo 'MISSING')"
else
  ok "node: $(node -v 2>/dev/null || echo present)"
fi

# Caddy (official repo) -- only if missing.
if ! command -v caddy >/dev/null; then
  run "apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https"
  run "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  run "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list"
  run "apt-get update -qq && apt-get install -y -qq caddy"
fi
ok "caddy: $(command -v caddy >/dev/null && caddy version 2>/dev/null | head -1 || echo 'MISSING')"

# A Chromium for the DWD/DMS browser flows.
if [ "$ENABLE_BROWSER" = yes ] && ! command -v google-chrome >/dev/null \
   && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium-browser || true"
fi
[ "$ENABLE_BROWSER" = yes ] && ok "chromium: $(command -v chromium || command -v chromium-browser || command -v google-chrome || echo 'not found -- DWD/DMS will need one')"

# ---------------------------------------------------------------------------
# 3. code + venv
# ---------------------------------------------------------------------------
step "Application code"
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  run "mkdir -p '$INSTALL_DIR'"
  # No --delete: it is not needed for a copy and would remove anything the
  # operator already had in the target -- the whole reason a mistyped path
  # like '/' must never reach this line (see the guard in Settings).
  run "rsync -a --exclude '.venv' --exclude '.git' --exclude 'node_modules' '$SRC_DIR/' '$INSTALL_DIR/'"
  ok "copied to $INSTALL_DIR"
else
  ok "installing in place at $INSTALL_DIR"
fi

step "Python environment"
[ -x "$INSTALL_DIR/.venv/bin/python" ] || run "python3 -m venv '$INSTALL_DIR/.venv'"
run "'$INSTALL_DIR/.venv/bin/pip' install -q --upgrade pip"
run "'$INSTALL_DIR/.venv/bin/pip' install -q -r '$INSTALL_DIR/requirements.txt'"
[ -f "$INSTALL_DIR/requirements-control-plane.txt" ] && \
  run "'$INSTALL_DIR/.venv/bin/pip' install -q -r '$INSTALL_DIR/requirements-control-plane.txt'"
ok "venv ready"

step "Database"
# Create the control-plane schema (migrations/*.sql) up front. Without it the
# DB file does not exist, and the services -- plus the superadmin step, which
# calls authenticate() -> a read-only open -- fail with "unable to open
# database file". Run from INSTALL_DIR so Settings().db_path resolves here.
if [ "$DRY_RUN" = 1 ]; then
  echo "  ${DIM}[dry-run] apply_migrations() to create the control-plane schema${RESET}"
else
  ( cd "$INSTALL_DIR" && "$INSTALL_DIR/.venv/bin/python" -c \
      "import control_plane_db as c; print('  applied:', ', '.join(c.apply_migrations()) or 'none')" )
fi

step "Frontend"
if [ -f "$INSTALL_DIR/migration-webui/dist/index.html" ] \
   || { [ "$DRY_RUN" = 1 ] && [ -f "$SRC_DIR/migration-webui/dist/index.html" ]; }; then
  # A prebuilt SPA shipped in the archive (the offline/flash-drive path):
  # no Node needed on the server. It was built with a relative API base.
  ok "using bundled prebuilt SPA (no build needed)"
elif [ -d "$INSTALL_DIR/migration-webui" ] || { [ "$DRY_RUN" = 1 ] && [ -d "$SRC_DIR/migration-webui" ]; }; then
  # VITE_CP_BASE="" makes the SPA call the API on its own origin (Caddy
  # routes /api/v2 and /ws to :8090); a baked-in localhost:8090 would make
  # every browser hit its own machine and fail. This is a known-real bug.
  run "cd '$INSTALL_DIR/migration-webui' && (npm ci --silent || npm install --silent) && VITE_CP_BASE='' npm run build --silent"
  ok "SPA built (relative API base)"
else
  warn "no migration-webui/ -- skipping SPA"
fi

# ---------------------------------------------------------------------------
# 4. config (root-only, outside the checkout so a deploy never overwrites it)
# ---------------------------------------------------------------------------
step "Configuration"
run "install -d -m 700 /etc/bitport"
if [ ! -f /etc/bitport/node.env ]; then
  run "umask 077; printf 'BITPORT_NODE_TOKEN=%s\nBITPORT_PUBLIC_ORIGIN=%s\n' \
       '$(openssl rand -hex 24)' '${BITPORT_DOMAIN:+https://$BITPORT_DOMAIN}' > /etc/bitport/node.env"
  run "chmod 600 /etc/bitport/node.env"
  ok "wrote /etc/bitport/node.env (node token generated)"
else
  ok "/etc/bitport/node.env exists -- left as is"
fi

# ---------------------------------------------------------------------------
# 5. systemd services (path-patched to INSTALL_DIR)
# ---------------------------------------------------------------------------
step "Services"
SVC_SRC="$INSTALL_DIR/systemd"
UNITS="bitport-webui.service bitport-api.service"
[ "$ENABLE_BROWSER" = yes ] && UNITS="xvfb.service $UNITS"
for u in $UNITS bitport-backup.service bitport-backup.timer; do
  [ -f "$SVC_SRC/$u" ] || continue
  # WorkingDirectory and the venv ExecStart both hardcode /root/migration in
  # the repo; rewrite them for this install dir.
  run "sed 's#/root/migration#$INSTALL_DIR#g' '$SVC_SRC/$u' > '/etc/systemd/system/$u'"
done
run "systemctl daemon-reload"
[ "$ENABLE_BROWSER" = yes ] && run "systemctl enable --now xvfb.service"
run "systemctl enable --now bitport-webui.service bitport-api.service"
[ -f "$SVC_SRC/bitport-backup.timer" ] && run "systemctl enable --now bitport-backup.timer || true"
ok "systemd units installed and enabled"

# ---------------------------------------------------------------------------
# 6. Caddy reverse proxy (real HTTPS when a domain is set)
# ---------------------------------------------------------------------------
step "Reverse proxy"
if [ -n "$BITPORT_DOMAIN" ]; then
  run "sed 's/^everything\.nishantbohara\.com\.np/$BITPORT_DOMAIN/' '$INSTALL_DIR/Caddyfile' > /etc/caddy/Caddyfile"
else
  # No domain: serve HTTP on :80 so it is at least reachable.
  run "printf ':80 {\n\treverse_proxy /api/v2/* 127.0.0.1:8090\n\treverse_proxy /ws 127.0.0.1:8090\n\treverse_proxy /* 127.0.0.1:8080\n}\n' > /etc/caddy/Caddyfile"
fi
# enable + (re)start, not just reload: on a fresh box caddy is installed but
# may not be running yet, so a bare reload fails with "not active".
run "systemctl enable caddy >/dev/null 2>&1 || true"
run "systemctl restart caddy || systemctl reload caddy || true"
ok "caddy configured for ${BITPORT_DOMAIN:-:80}"
[ -n "$BITPORT_DOMAIN" ] && warn "if $BITPORT_DOMAIN does not resolve to this server's public IP, Caddy cannot get a TLS cert -- leave the domain blank to serve HTTP on :80"

# ---------------------------------------------------------------------------
# 7. first superadmin account
# ---------------------------------------------------------------------------
step "Superadmin account"
if [ "$DRY_RUN" = 1 ]; then
  echo "  ${DIM}[dry-run] would create/promote $BITPORT_ADMIN_EMAIL${RESET}"
else
  cd "$INSTALL_DIR"
  BP_EMAIL="$BITPORT_ADMIN_EMAIL" BP_PASS="$BITPORT_ADMIN_PASSWORD" \
  "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())   # cwd is INSTALL_DIR, so the control-plane DB resolves here
import control_plane_db as cpdb
cpdb.apply_migrations()           # ensure the schema exists before any read
import accounts_auth as a
email = os.environ["BP_EMAIL"].strip().lower()
pw = os.environ["BP_PASS"]
try:
    a.bootstrap_legacy_account()
except Exception:
    pass
existing = a.authenticate(email, pw)
if existing is None:
    try:
        a.create_account(email, pw, "Administrator", plan="internal")
        print(f"  created account {email}")
    except Exception as e:
        # already exists with a different password: reset it
        import manage_account
        manage_account.set_password(email, pw)
        print(f"  reset password for existing account {email}")
a.promote_to_superadmin(email)
print(f"  {email} is superadmin, login verified={a.authenticate(email, pw) is not None}")
PY
fi

# ---------------------------------------------------------------------------
# 8. health check + summary
# ---------------------------------------------------------------------------
step "Health check"
if [ "$DRY_RUN" = 1 ]; then
  ok "dry-run: skipped"
else
  sleep 3
  for p in 8080 8090; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/" 2>/dev/null || echo 000)
    [ "$code" != 000 ] && ok "port $p responding (HTTP $code)" || warn "port $p not responding yet"
  done
fi

echo
echo "${BOLD}${GREEN}Bitport installed.${RESET}"
if [ -n "$BITPORT_DOMAIN" ]; then
  echo "  URL:      https://$BITPORT_DOMAIN/app"
else
  echo "  URL:      http://<server-ip>/app"
fi
echo "  Login:    $BITPORT_ADMIN_EMAIL"
[ "${NONINTERACTIVE:-0}" = 1 ] && echo "  Password: ${DIM}(as supplied / generated -- check your env or the prompt output)${RESET}"
echo
echo "  Next: open the URL, sign in, and use the ${BOLD}Setup Wizard${RESET} in the"
echo "  sidebar to connect your source and target Google Workspace tenants."
