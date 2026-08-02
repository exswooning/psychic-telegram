#!/usr/bin/env bash
# setup.sh
# ========
# Takes a tenant pair from "two Workspace domains exist" to "ready to migrate",
# automating everything that can be automated and stopping precisely where a
# human is genuinely required.
#
# There is exactly one unavoidable pause: Domain-Wide Delegation authorisation.
# Google provides no API for it, deliberately -- "let this credential act as
# every user in the organisation" is browser-only, for anyone, forever. This
# script prints the exact Client ID and scope string to paste, then *polls
# until the grant propagates* and continues on its own, so the pause costs you
# a paste rather than a re-run.
#
# What it handles that doing this by hand does not:
#   * service-account creation is eventually consistent -- it waits rather than
#     failing with NOT_FOUND
#   * the disableServiceAccountKeyCreation org policy that Google now enforces
#     by default on new organisations -- detected, explained, and offered a
#     keyless alternative rather than a dead end
#   * DWD propagation takes 2-30 minutes -- polled, not guessed at
#
# Usage:
#   ./setup.sh --source-domain c.example.com --target-domain a.example.com \
#              --source-admin info@c.example.com --target-admin info@a.example.com \
#              [--source-project ID] [--target-project ID] [--keyless]

set -uo pipefail

SOURCE_DOMAIN=""; TARGET_DOMAIN=""
SOURCE_ADMIN=""; TARGET_ADMIN=""
SOURCE_PROJECT=""; TARGET_PROJECT=""
KEYLESS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-domain) SOURCE_DOMAIN="$2"; shift 2 ;;
        --target-domain) TARGET_DOMAIN="$2"; shift 2 ;;
        --source-admin)  SOURCE_ADMIN="$2"; shift 2 ;;
        --target-admin)  TARGET_ADMIN="$2"; shift 2 ;;
        --source-project) SOURCE_PROJECT="$2"; shift 2 ;;
        --target-project) TARGET_PROJECT="$2"; shift 2 ;;
        --keyless) KEYLESS=1; shift ;;
        -h|--help) awk '/^#!/{next} /^#/{print; next} {exit}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# NB: no ${v,,} here -- that is bash 4, and macOS still ships bash 3.2.
for v in SOURCE_DOMAIN TARGET_DOMAIN SOURCE_ADMIN TARGET_ADMIN; do
    if [[ -z "${!v}" ]]; then
        flag="--$(echo "$v" | tr 'A-Z_' 'a-z-')"
        echo "missing $flag (use --help)" >&2
        exit 2
    fi
done

: "${SOURCE_PROJECT:=mig-src-$RANDOM}"
: "${TARGET_PROJECT:=mig-tgt-$RANDOM}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m   !! %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m   ok %s\033[0m\n' "$*"; }

command -v gcloud >/dev/null || { echo "gcloud not installed"; exit 1; }
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -1)"
[[ -n "$ACCOUNT" ]] || { echo "run: gcloud auth login"; exit 1; }
say "Running as $ACCOUNT"

# ======================================================================
# 1. Projects, APIs, service accounts
# ======================================================================
declare -A CLIENT_ID SA_EMAIL
for side in source target; do
    if [[ "$side" == source ]]; then PROJ="$SOURCE_PROJECT"; SA=source-sa
    else PROJ="$TARGET_PROJECT"; SA=target-sa; fi

    say "$side: project $PROJ"
    gcloud projects describe "$PROJ" >/dev/null 2>&1 \
        || gcloud projects create "$PROJ" --name="$PROJ" >/dev/null 2>&1 \
        || warn "project create failed (may already exist globally)"

    gcloud services enable drive.googleapis.com gmail.googleapis.com \
        calendar-json.googleapis.com admin.googleapis.com \
        iamcredentials.googleapis.com --project="$PROJ" >/dev/null 2>&1 \
        && ok "APIs enabled" || warn "could not enable all APIs (billing linked?)"

    gcloud iam service-accounts describe "$SA@$PROJ.iam.gserviceaccount.com" \
        --project="$PROJ" >/dev/null 2>&1 \
        || gcloud iam service-accounts create "$SA" --project="$PROJ" \
             --display-name="workspace migration" >/dev/null 2>&1

    # Service-account creation is eventually consistent. Waiting here is the
    # difference between this working and a confusing NOT_FOUND.
    for _ in $(seq 1 12); do
        gcloud iam service-accounts describe "$SA@$PROJ.iam.gserviceaccount.com" \
            --project="$PROJ" >/dev/null 2>&1 && break
        sleep 5
    done

    SA_EMAIL[$side]="$SA@$PROJ.iam.gserviceaccount.com"
    CLIENT_ID[$side]="$(gcloud iam service-accounts describe "${SA_EMAIL[$side]}" \
        --project="$PROJ" --format='value(uniqueId)' 2>/dev/null)"
    [[ -n "${CLIENT_ID[$side]}" ]] && ok "client id ${CLIENT_ID[$side]}" \
        || { echo "could not read client id for $side"; exit 1; }
done

# ======================================================================
# 2. Credentials: a key, or keyless impersonation
# ======================================================================
mkdir -p keys && chmod 700 keys
AUTH_MODE=key

if [[ "$KEYLESS" == 1 ]]; then
    AUTH_MODE=impersonate
else
    for side in source target; do
        PROJ=$([[ "$side" == source ]] && echo "$SOURCE_PROJECT" || echo "$TARGET_PROJECT")
        OUT="keys/$side-sa.json"
        if gcloud iam service-accounts keys create "$OUT" \
                --iam-account="${SA_EMAIL[$side]}" --project="$PROJ" 2>/tmp/keyerr; then
            chmod 600 "$OUT"; ok "$side key written to $OUT"
        else
            [[ -s "$OUT" ]] || rm -f "$OUT"
            if grep -qi disableServiceAccountKeyCreation /tmp/keyerr; then
                warn "key creation blocked by org policy on $PROJ"
                AUTH_MODE=impersonate
            else
                cat /tmp/keyerr >&2; exit 1
            fi
        fi
    done
fi

if [[ "$AUTH_MODE" == impersonate ]]; then
    say "Using keyless impersonation (no key files)"
    echo "   Google now enforces disableServiceAccountKeyCreation by default on"
    echo "   new organisations. Rather than fight it, this grants your own"
    echo "   account permission to mint tokens for each service account."
    for side in source target; do
        PROJ=$([[ "$side" == source ]] && echo "$SOURCE_PROJECT" || echo "$TARGET_PROJECT")
        gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL[$side]}" \
            --member="user:$ACCOUNT" \
            --role="roles/iam.serviceAccountTokenCreator" \
            --project="$PROJ" >/dev/null 2>&1 \
            && ok "$side: tokenCreator granted to $ACCOUNT" \
            || warn "$side: could not grant tokenCreator"
    done
fi

# ======================================================================
# 3. Write the environment file
# ======================================================================
cat > env.sh <<EOF
export SOURCE_DOMAIN=$SOURCE_DOMAIN
export TARGET_DOMAIN=$TARGET_DOMAIN
export SOURCE_ADMIN=$SOURCE_ADMIN
export TARGET_ADMIN=$TARGET_ADMIN
export AUTH_MODE=$AUTH_MODE
export SOURCE_SA_EMAIL=${SA_EMAIL[source]}
export TARGET_SA_EMAIL=${SA_EMAIL[target]}
export SOURCE_SA_KEY=\$(pwd)/keys/source-sa.json
export TARGET_SA_KEY=\$(pwd)/keys/target-sa.json
export MIGRATION_DB=\$(pwd)/migration.db
export SCRATCH_DIR=\$(pwd)/scratch
export PER_USER_QPS=8
export USER_WORKERS=5
EOF
ok "wrote env.sh"

# ======================================================================
# 4. The one step with no API
# ======================================================================
SRC_SCOPES="https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.insert,https://www.googleapis.com/auth/gmail.labels,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/admin.directory.user,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.directory.group.readonly"
TGT_SCOPES="https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/gmail.insert,https://www.googleapis.com/auth/gmail.labels,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/admin.directory.user,https://www.googleapis.com/auth/admin.directory.user.readonly"

cat <<EOF

================================================================
 MANUAL STEP — this is the only one, and it cannot be scripted
================================================================
Domain-Wide Delegation has no API. A super admin must authorise each
Client ID in the Admin Console of its own domain.

For BOTH: admin.google.com -> Security -> Access and data control
          -> API controls -> MANAGE DOMAIN WIDE DELEGATION -> Add new

--- $SOURCE_DOMAIN (sign in as $SOURCE_ADMIN) ---
Client ID:
  ${CLIENT_ID[source]}
OAuth scopes (paste the WHOLE line -- the editor replaces, it does not append):
  $SRC_SCOPES

--- $TARGET_DOMAIN (sign in as $TARGET_ADMIN) ---
Client ID:
  ${CLIENT_ID[target]}
OAuth scopes:
  $TGT_SCOPES

Leave this running -- it will detect the grants itself and continue.
================================================================
EOF

# ======================================================================
# 5. Poll until the grant lands, then verify
# ======================================================================
say "Waiting for delegation to propagate (usually ~2 min, up to 30)"
source ./env.sh
DEADLINE=$(( $(date +%s) + 1800 ))
while (( $(date +%s) < DEADLINE )); do
    if python3 main.py preflight >/tmp/preflight.out 2>&1; then
        ok "preflight passed — delegation is live"
        cat /tmp/preflight.out | grep -E '^\[' || true
        break
    fi
    printf '.'
    sleep 20
done

if (( $(date +%s) >= DEADLINE )); then
    warn "still not authorised after 30 minutes"
    tail -5 /tmp/preflight.out
    echo "   Re-check the scope lines -- a partial paste is the usual cause."
    exit 1
fi

cat <<EOF

================================================================
 Setup complete. From here everything is automated:

   source ./env.sh
   python3 main.py provision-users --tenant source --dry-run
   python3 main.py provision-users --tenant target
   python3 main.py init-db --identities identities.csv
   python3 main.py discover
   python3 main.py --dry-run migrate
   python3 main.py migrate
   python3 verify.py
================================================================
EOF
