#!/usr/bin/env bash
# Make (or refresh) a source tenant's seed key from this Mac and install it on a Bitport box.
#
# Why from the Mac: Google refuses an automated sign-in from a headless or bundled Chromium ("This browser or app may
# not be secure" -- the fableplan2 VM, 28 Nov 2026), and an ARM VM has no real Chrome. Here the one sign-in happens
# in a real browser: the operator's, or BrowserOS driven by Claude. Nothing else needs a person.
#
# usage: seed_key_from_mac.sh ADMIN_EMAIL PROJECT_ID SSH_TARGET ACCOUNT_ID [SSH_CONFIG]
#   e.g. seed_key_from_mac.sh info@a.example.com wsmig-src-83074 lima-fableplan2 3 ~/.../ssh.config
# Steps: gcloud signs in as the admin (prints the sign-in URL and waits) -> seed-sa is created if missing -> a new key
# is made and installed root-only as keys/<account>/seed-sa.json on the box -> every seed scope is minted as the
# admin to prove the delegation -> if it is missing, the exact Admin console line is printed -> the gcloud sign-in is
# revoked and its config deleted. The key never stays on this Mac.
set -euo pipefail
ADMIN="$1"; PROJECT="$2"; TARGET="$3"; ACCOUNT="$4"; SSHCFG="${5:-}"
G="${GCLOUD:-$(command -v gcloud || echo "/Users/aryan/Repos/google-cloud-sdk 2/bin/gcloud")}"
SSH=(ssh -o ConnectTimeout=10); [ -n "$SSHCFG" ] && SSH+=(-F "$SSHCFG")
export CLOUDSDK_CONFIG; CLOUDSDK_CONFIG="$(mktemp -d)"
KEY="$CLOUDSDK_CONFIG/seed-sa.json"
cleanup() { "$G" auth revoke "$ADMIN" >/dev/null 2>&1 || true; rm -rf "$CLOUDSDK_CONFIG"; }
trap cleanup EXIT

echo "1/5 sign in as $ADMIN in a real browser (the URL is printed below; it waits for you)"
BROWSER=/usr/bin/true "$G" auth login "$ADMIN" --brief
"$G" config set project "$PROJECT" --quiet >/dev/null 2>&1

SA="seed-sa@$PROJECT.iam.gserviceaccount.com"
echo "2/5 service account $SA"
"$G" iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  "$G" iam service-accounts create seed-sa --display-name "Bitport seed (writes; sandbox only)" >/dev/null
COUNT=$("$G" iam service-accounts keys list --iam-account "$SA" --managed-by=user --format="value(name)" | wc -l)
[ "$COUNT" -ge 10 ] && { echo "refused: $SA already has $COUNT user keys (Google allows 10); delete old ones first"; exit 2; }

echo "3/5 new key -> $TARGET:keys/$ACCOUNT/seed-sa.json (root, 600)"
umask 077; "$G" iam service-accounts keys create "$KEY" --iam-account "$SA" >/dev/null 2>&1
CLIENT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['client_id'])" "$KEY")
"${SSH[@]}" "$TARGET" "sudo bash -c 'umask 077; cat > /opt/bitport/keys/$ACCOUNT/seed-sa.json; chmod 600 /opt/bitport/keys/$ACCOUNT/seed-sa.json'" < "$KEY"
rm -f "$KEY"

echo "4/5 proving the delegation (client $CLIENT)"
"${SSH[@]}" "$TARGET" sudo bash -s -- "$ACCOUNT" <<'R'
cd /opt/bitport && .venv/bin/python - "$1" <<'P'
import sys
import google.auth.transport.requests as r
from google.oauth2 import service_account
import separate_credentials as sc
from config import Settings
acct = int(sys.argv[1]); subject = Settings(account_id=acct).source_admin
missing = []
for scope in sc.seed_scopes():
    try:
        service_account.Credentials.from_service_account_file(
            f"keys/{acct}/seed-sa.json", scopes=[scope]).with_subject(subject).refresh(r.Request())
    except Exception:      # noqa: BLE001 - an unmintable scope is the answer
        missing.append(scope)
print(f"delegated {len(sc.seed_scopes()) - len(missing)} of {len(sc.seed_scopes())}")
if missing:
    print("ADD IN ADMIN CONSOLE (Security > API controls > Domain-wide delegation), client id above, scopes:")
    print(",".join(sc.seed_scopes()))
    sys.exit(3)
P
R
echo "5/5 done; the gcloud sign-in is revoked on exit"
