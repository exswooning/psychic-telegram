#!/usr/bin/env bash
# harden_ssh.sh -- key-only SSH, and fail2ban banning whoever keeps guessing.
#
# Live: the box took 12,924 password guesses at root in one day, with password
# login on and nothing banning anyone; every real login in the week before was a key.
#
# Refuses to switch passwords off while root has no key on file -- on a fresh box
# that is the only way in. Idempotent: running it twice changes nothing.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "harden_ssh: run as root" >&2; exit 1; }

KEYS=/root/.ssh/authorized_keys
CONF=/etc/ssh/sshd_config.d/00-bitport-keys-only.conf
JAIL=/etc/fail2ban/jail.d/bitport-sshd.conf

if ! grep -qE '^(ssh-|ecdsa-|sk-)' "$KEYS" 2>/dev/null; then
  echo "harden_ssh: no key in $KEYS -- password login left on, it is the only way in" >&2
  exit 0
fi
if ! grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
  echo "harden_ssh: sshd_config does not read sshd_config.d -- nothing changed" >&2
  exit 1
fi

# 00- so it is read first: sshd keeps the FIRST value it meets for a keyword, and the
# Include sits above sshd_config's own "PasswordAuthentication yes".
cat > "$CONF" <<'EOF'
# Written by Bitport's harden_ssh.sh: key-only SSH. Remove this file and
# `systemctl reload ssh` to allow passwords again.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
eff="$(sshd -T)"
for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin without-password"; do
  grep -qx "$want" <<<"$eff" || { echo "harden_ssh: sshd does not report '$want'" >&2; exit 1; }
done
echo "harden_ssh: SSH is key-only"

if ! command -v fail2ban-client >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q fail2ban >/dev/null
fi
# A key login never counts as a failure, so the operator is never the one banned.
cat > "$JAIL" <<'EOF'
# Written by Bitport's harden_ssh.sh.
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
EOF
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
for _ in $(seq 1 15); do fail2ban-client ping >/dev/null 2>&1 && break; sleep 1; done
fail2ban-client status sshd
