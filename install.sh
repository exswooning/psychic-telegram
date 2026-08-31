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
# Network-sensitive commands, retried. A constrained/flaky box (asuswb hit
# "Error: Timeout was reached" on the first apt-get update, contacting several
# repos) must not fail the whole install on one transient download timeout.
# Three tries with backoff; a genuine, persistent failure still aborts loudly.
runnet() {
  [ "$DRY_RUN" = 1 ] && { echo "  ${DIM}[dry-run] $*${RESET}"; return 0; }
  local n=1
  until eval "$@"; do
    [ "$n" -ge 3 ] && { warn "still failing after $n attempts: $*"; return 1; }
    warn "network step failed (attempt $n) -- retrying in $((n*8))s"
    sleep $((n * 8)); n=$((n + 1))
  done
}
# Whether THIS invocation was already unattended -- captured before the
# Settings section can flip NONINTERACTIVE for its own internal use. A
# caller that already ran fully unattended (CI, Ansible, an already-detached
# process) controls its own supervision and gets the old synchronous
# behaviour; an interactive human over SSH gets auto-backgrounded below so a
# dropped connection can't take the install down with it.
ORIG_NONINTERACTIVE="${NONINTERACTIVE:-0}"

# Is TCP port $1 free on this host right now?
port_free() { ! ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[.:]$1\$"; }
# First free port at-or-after $1 (checked up to +50). Ports get squatted by
# whatever else runs on a shared box -- asuswb had SABnzbd already sitting on
# 8080 and Nextcloud's own Apache already on 80, silently breaking Bitport's
# own services until someone noticed and hand-patched the units. Picking a
# free port up front means a busy box just works instead of needing that.
pick_port() {
  local p="$1" tries=0
  while [ "$tries" -lt 50 ]; do
    port_free "$p" && { echo "$p"; return 0; }
    p=$((p + 1)); tries=$((tries + 1))
  done
  die "no free port found starting at $1"
}

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
# 1a. detach from the terminal for everything from here on
#
# Everything below this line is the long, network- and CPU-heavy part of the
# install (packages, a possible SPA build, services). Run over SSH by a human
# typing answers to the prompts above, that part used to die the moment the
# SSH session dropped -- "Killed" or a broken pipe partway through, with no
# way to resume short of knowing to re-run it inside tmux. Once every answer
# above is settled, re-exec this same script fully unattended (the answers
# just given become its env) under setsid+nohup, detached from this
# terminal's session entirely, and hand control back to the human
# immediately with a log to follow. A caller that was ALREADY unattended
# (ORIG_NONINTERACTIVE=1 -- CI, Ansible, a process that already detached
# itself) is left running exactly as invoked: it controls its own
# supervision and expects this process's real exit code.
# ---------------------------------------------------------------------------
if [ "$ORIG_NONINTERACTIVE" != 1 ] && [ "$DRY_RUN" != 1 ] && [ "${BITPORT_DAEMONIZED:-0}" != 1 ]; then
  LOG="/var/log/bitport-install.log"
  install -d -m 755 "$(dirname "$LOG")" 2>/dev/null || LOG="/tmp/bitport-install.log"
  echo
  echo "${BOLD}Settings collected.${RESET} Continuing in the background so a dropped"
  echo "connection can't interrupt the install -- this terminal is free to close."
  echo "  Log:    $LOG"
  echo "  Follow: tail -f $LOG"
  export INSTALL_DIR BITPORT_DOMAIN BITPORT_ADMIN_EMAIL BITPORT_ADMIN_PASSWORD ENABLE_BROWSER
  export NONINTERACTIVE=1 BITPORT_DAEMONIZED=1
  setsid nohup bash "$0" >>"$LOG" 2>&1 </dev/null &
  disown
  echo "  PID:    $!"
  echo "  Done when the log ends with \"Bitport installed.\""
  exit 0
fi

# ---------------------------------------------------------------------------
# 1b. scan for a prior or BROKEN install and clean it up
#
# A half-finished run (a killed installer, a crash-looping service with no
# schema, a wedged apt) must not poison this one. This ran the hard way: a
# mistyped install left several installers fighting over apt and a service
# restarting against a database that did not exist.
# ---------------------------------------------------------------------------
step "Scan for prior/broken installs"
if [ "$DRY_RUN" = 1 ]; then
  ok "dry-run: would kill stray installers, repair apt, stop existing services"
else
  # This whole step is best-effort cleanup: repairing a broken PRIOR install
  # must never abort THIS one. Run it with -e off so a fussy `ps`, a missing
  # `pgrep`, or a hidden non-zero (all redirected to /dev/null) can't silently
  # kill the installer via pipefail. Restored to -e before the next step.
  set +e

  # 1. Stray installers from earlier attempts -- identified by running in a
  #    DIFFERENT process group than this one, so we never kill ourselves.
  MYPGID=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
  killed=0
  # Only hunt strays if we actually know our own group; without it we cannot
  # tell "someone else's installer" from "us" and must not guess.
  if [ -n "$MYPGID" ]; then
    # The leading [ /] is load-bearing: a bare "install.sh" is a SUBSTRING of
    # this run's own `sudo bash .../bitport-SELFINSTALL.sh` parent. sudo runs
    # us in a fresh process group, so the pgid test below would see that
    # parent as a stray in another group and SIGKILL it -- killing the whole
    # run ("Killed" at this step). Requiring a space or slash before it
    # matches a real `bash install.sh` / `/path/install.sh` but never
    # `selfinstall.sh`. And we never signal our own pid or our parent (sudo).
    for pid in $(pgrep -f "[ /]install\.sh" 2>/dev/null); do
      { [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ]; } && continue
      pg=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
      [ -n "$pg" ] && [ "$pg" != "$MYPGID" ] && { kill -9 "$pid" 2>/dev/null && killed=$((killed+1)); }
    done
  fi
  # apt/dpkg children a dead installer left mid-package
  pkill -9 -f "apt-get install" 2>/dev/null && killed=$((killed+1)) || true
  [ "$killed" -gt 0 ] && warn "cleared $killed stray installer/apt process(es) from a previous run" \
                      || ok "no stray installer processes"

  # 2. A wedged package system (dpkg interrupted by a kill).
  if dpkg --audit 2>/dev/null | grep -q . || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
    warn "package system was interrupted -- repairing (dpkg --configure -a)"
    dpkg --configure -a >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get -f install -y -qq >/dev/null 2>&1 || true
  fi

  # 3. An existing/broken Bitport: stop its services so files and DB can be
  #    refreshed cleanly. A unit that is enabled but inactive is exactly the
  #    crash-loop-without-a-schema case this whole fix is about.
  found_prev=0
  for svc in bitport-webui bitport-api bitport-fleet xvfb; do
    if systemctl list-unit-files "$svc.service" >/dev/null 2>&1 \
       && systemctl cat "$svc.service" >/dev/null 2>&1; then
      st=$(systemctl is-active "$svc" 2>/dev/null || true)
      systemctl stop "$svc" >/dev/null 2>&1 || true
      [ "$svc" = xvfb ] || { warn "stopped existing $svc (was: ${st:-unknown})"; found_prev=1; }
    fi
  done
  [ "$found_prev" = 0 ] && ok "no existing Bitport services" \
                       || warn "existing install found -- it will be refreshed (config, keys and data kept)"

  # 4. Report the install dir's state without touching config/keys/data.
  if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/webui.py" ]; then
    warn "code already present at $INSTALL_DIR -- refreshing it"
  fi

  set -e   # cleanup done -- real steps below must fail loudly again
fi

# ---------------------------------------------------------------------------
# 2. system packages
# ---------------------------------------------------------------------------
step "System packages"
PKGS="python3 python3-venv python3-pip git rsync curl ca-certificates openssl"
[ "$ENABLE_BROWSER" = yes ] && PKGS="$PKGS xvfb"
runnet "apt-get update -qq"
runnet "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS"
ok "base packages installed"

# Node.js (for the SPA build) -- only if npm is missing AND no prebuilt SPA
# was shipped in the archive. The flash-drive tarball bundles dist/, so a
# server with no internet for NodeSource still installs.
if [ -f "$SRC_DIR/migration-webui/dist/index.html" ]; then
  ok "prebuilt SPA present -- skipping Node"
elif ! command -v npm >/dev/null; then
  runnet "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
  runnet "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs"
  ok "node: $(command -v node >/dev/null && node -v || echo 'MISSING')"
else
  ok "node: $(node -v 2>/dev/null || echo present)"
fi

# Caddy (official repo) -- only if missing.
if ! command -v caddy >/dev/null; then
  runnet "apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https"
  runnet "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  runnet "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list"
  runnet "apt-get update -qq && apt-get install -y -qq caddy"
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
  # Migrations are pure shipped code that apply_migrations() executes blindly
  # over the WHOLE dir. A refresh rsync (below, no --delete) would leave a
  # stale .sql from an older install in place, and one non-UTF-8 junk file
  # there aborted a whole install at the Database step. Clear the shipped
  # migrations first so only this bundle's set remains; rsync repopulates it.
  # ponytail: only migrations/ is cleaned, not stale .py generally -- add a
  # scoped `rsync --delete` (data/keys/db excluded) if stale code bites again.
  run "rm -f '$INSTALL_DIR/migrations/'*.sql"
  # No blanket --delete: it would remove anything the operator already had in
  # the target -- the whole reason a mistyped path like '/' must never reach
  # this line (see the guard in Settings).
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
# webui.py's default port (8080) is a common one for other software to have
# grabbed first on a shared box -- asuswb had SABnzbd already sitting on it,
# and Bitport's own webui crash-looped forever until someone noticed and
# hand-patched the unit. Pick a free one up front instead; harmless no-op
# when 8080 is actually free (the overwhelmingly common case).
if [ "$DRY_RUN" = 1 ]; then
  WEBUI_PORT=8080
else
  WEBUI_PORT=$(pick_port 8080)
  [ "$WEBUI_PORT" != 8080 ] && warn "port 8080 is already in use -- Bitport's web UI will use $WEBUI_PORT internally instead"
fi
SVC_SRC="$INSTALL_DIR/systemd"
UNITS="bitport-webui.service bitport-api.service"
[ "$ENABLE_BROWSER" = yes ] && UNITS="xvfb.service $UNITS"
for u in $UNITS bitport-backup.service bitport-backup.timer; do
  [ -f "$SVC_SRC/$u" ] || continue
  # WorkingDirectory and the venv ExecStart both hardcode /root/migration in
  # the repo; rewrite them for this install dir.
  PATCH="s#/root/migration#$INSTALL_DIR#g"
  if [ "$u" = "bitport-webui.service" ]; then
    PATCH="$PATCH; s/--port 8080/--port $WEBUI_PORT/"
  fi
  if [ "$u" = "bitport-api.service" ] && [ -z "$BITPORT_DOMAIN" ]; then
    # The shipped unit hardcodes BITPORT_COOKIE_SECURE=1, correct only when
    # Caddy terminates real HTTPS (the $BITPORT_DOMAIN branch below). With no
    # domain, install.sh's own Caddy step serves plain HTTP -- a browser
    # silently refuses to send a Secure cookie back over HTTP, so login
    # succeeds server-side but every following request looks signed-out and
    # the SPA bounces back to /login forever. Confirmed live: login returned
    # 200 with a Set-Cookie, curl's cookie jar showed it, and it never came
    # back on the next request because of exactly this flag.
    PATCH="$PATCH; s/BITPORT_COOKIE_SECURE=1/BITPORT_COOKIE_SECURE=0/"
  fi
  run "sed '$PATCH' '$SVC_SRC/$u' > '/etc/systemd/system/$u'"
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
  # ACME (Let's Encrypt) needs the standard 80/443 for the HTTP-01 challenge
  # and to serve; those can't be freely relocated without a different
  # challenge type, so a domain install still requires them free. The
  # webui target port, though, is the same one picked above regardless of
  # domain -- rewrite it here too, not just the hostname.
  run "sed 's/^everything\.nishantbohara\.com\.np/$BITPORT_DOMAIN/; s/127\.0\.0\.1:8080/127.0.0.1:$WEBUI_PORT/' '$INSTALL_DIR/Caddyfile' > /etc/caddy/Caddyfile"
  PUBLIC_PORT=""
else
  # No domain: serve plain HTTP. :80 is the nicest default when free, but a
  # shared box may already have something else on it (asuswb had Nextcloud's
  # bundled Apache there) -- fall back to another free port rather than
  # leaving Caddy failed and the install silently unreachable.
  if [ "$DRY_RUN" = 1 ]; then
    PUBLIC_PORT=80
  else
    PUBLIC_PORT=$(pick_port 80)
    [ "$PUBLIC_PORT" != 80 ] && warn "port 80 is already in use -- Bitport will be reachable on port $PUBLIC_PORT instead"
  fi
  run "printf ':$PUBLIC_PORT {\n\treverse_proxy /api/v2/* 127.0.0.1:8090\n\treverse_proxy /ws 127.0.0.1:8090\n\treverse_proxy /* 127.0.0.1:$WEBUI_PORT\n}\n' > /etc/caddy/Caddyfile"
fi
# enable + (re)start, not just reload: on a fresh box caddy is installed but
# may not be running yet, so a bare reload fails with "not active".
run "systemctl enable caddy >/dev/null 2>&1 || true"
run "systemctl restart caddy || systemctl reload caddy || true"
ok "caddy configured for ${BITPORT_DOMAIN:-:$PUBLIC_PORT}"
[ -n "$BITPORT_DOMAIN" ] && warn "if $BITPORT_DOMAIN does not resolve to this server's public IP, Caddy cannot get a TLS cert -- leave the domain blank to serve HTTP instead"

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
  for p in "$WEBUI_PORT" 8090; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/" 2>/dev/null || echo 000)
    [ "$code" != 000 ] && ok "port $p responding (HTTP $code)" || warn "port $p not responding yet"
  done
fi

echo
echo "${BOLD}${GREEN}Bitport installed.${RESET}"
if [ -n "$BITPORT_DOMAIN" ]; then
  echo "  URL:      https://$BITPORT_DOMAIN/app"
elif [ "${PUBLIC_PORT:-80}" = 80 ]; then
  echo "  URL:      http://<server-ip>/app"
else
  echo "  URL:      http://<server-ip>:$PUBLIC_PORT/app"
fi
echo "  Login:    $BITPORT_ADMIN_EMAIL"
[ "${ORIG_NONINTERACTIVE:-0}" = 1 ] && echo "  Password: ${DIM}(as supplied / generated -- check your env or the prompt output)${RESET}"
echo
echo "  Next: open the URL, sign in, and use the ${BOLD}Setup Wizard${RESET} in the"
echo "  sidebar to connect your source and target Google Workspace tenants."

# The self-extracting installer's own EXIT trap never fires (see
# make-selfinstall.sh's comment: `exec` replaces that process, so it never
# reaches its own exit) -- clean up its temp extraction dir here instead, as
# the last thing this script does. Matched against the known mktemp pattern
# before deleting anything, and a no-op for the plain-tarball path where this
# var was never set.
case "${BITPORT_SRC_DEST:-}" in
  /tmp/bitport-src.*) rm -rf "$BITPORT_SRC_DEST" ;;
esac
