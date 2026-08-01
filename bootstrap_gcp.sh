#!/usr/bin/env bash
# bootstrap_gcp.sh
# =================
# Automates the GCP-console side of README.md section 1: selecting/creating
# a project, enabling the required APIs, creating a service account, and
# generating its key.
#
# What this deliberately does NOT do: authorize the service account for
# Domain-Wide Delegation in the Google Workspace Admin Console. That step has
# no API -- Google requires a logged-in super admin to paste the client ID
# and scopes into the browser by hand, specifically so that "let this
# credential impersonate every user in the domain" can never be a silent or
# scripted action. This script gets you to the point of having everything
# that one manual step needs, and prints it out plainly.
#
# Requires: the gcloud CLI, already authenticated (`gcloud auth login`) as
# a principal with permission to create projects/service accounts/keys in
# the target project (or an org that allows it).
#
# Usage:
#   ./bootstrap_gcp.sh --project PROJECT_ID --sa-name NAME --role ROLE --key-out PATH [--domain DOMAIN]
#
#   --project   GCP project ID. Created if it doesn't exist yet.
#   --sa-name   Service account short name, e.g. source-sa, target-sa, seed-sa.
#   --role      One of: source | target | seed. Selects the OAuth scope list
#               printed at the end (source is read-only, target/seed write).
#   --key-out   Where to write the downloaded JSON key. Parent dir is
#               created with mode 700; the key itself is written mode 600.
#   --domain    Optional, just used to print the exact Admin Console URL
#               for that Workspace customer.
#
# Example:
#   ./bootstrap_gcp.sh --project workspace-migrator-503709 \
#       --sa-name source-sa --role source --key-out ./keys/source-sa.json \
#       --domain sandbox-src.example.com

set -u

# ======================================================================
# Argument parsing
# ======================================================================
PROJECT=""
SA_NAME=""
ROLE=""
KEY_OUT=""
DOMAIN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --sa-name) SA_NAME="$2"; shift 2 ;;
        --role) ROLE="$2"; shift 2 ;;
        --key-out) KEY_OUT="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        -h|--help)
            awk '/^#!/{next} /^#/{print; next} {exit}' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

fail() {
    echo "" >&2
    echo "REFUSING / STOPPING: $1" >&2
    [[ -n "${2:-}" ]] && echo "" >&2 && echo "What to do: $2" >&2
    exit 1
}

[[ -n "$PROJECT" ]] || fail "missing --project"
[[ -n "$SA_NAME" ]] || fail "missing --sa-name"
[[ -n "$KEY_OUT" ]] || fail "missing --key-out"
case "$ROLE" in
    source|target|seed) ;;
    *) fail "missing or invalid --role (must be source, target, or seed)" ;;
esac

# ======================================================================
# Scope lists per role -- must match config.py's SOURCE_SCOPES/TARGET_SCOPES
# ======================================================================
case "$ROLE" in
    source)
        SCOPES="https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.directory.group.readonly"
        ;;
    target)
        SCOPES="https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/gmail.insert,https://www.googleapis.com/auth/gmail.labels,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/admin.directory.user.readonly"
        ;;
    seed)
        SCOPES="https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/gmail.insert,https://www.googleapis.com/auth/gmail.labels,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar"
        ;;
esac

# ======================================================================
# Preflight: gcloud present and authenticated
# ======================================================================
command -v gcloud >/dev/null 2>&1 || fail \
    "the gcloud CLI is not installed" \
    "install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
[[ -n "$ACTIVE_ACCOUNT" ]] || fail \
    "gcloud is not authenticated" \
    "run: gcloud auth login"

echo "Using gcloud identity: $ACTIVE_ACCOUNT"

# ======================================================================
# Project: use if it exists, create if it doesn't
# ======================================================================
if gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
    echo "Project $PROJECT already exists -- using it."
else
    echo "Creating project $PROJECT ..."
    if ! gcloud projects create "$PROJECT" --name="$PROJECT" 2>/tmp/bootstrap_gcp_err; then
        cat /tmp/bootstrap_gcp_err >&2
        fail "project creation failed" \
             "the project ID may already be taken globally (they're unique across all of Google Cloud, not just your account) -- try a different --project value. If the error mentions an organization policy, ask whoever administers your GCP org to grant you 'roles/resourcemanager.projectCreator', or have them create the project for you."
    fi
fi

# Billing must be linked before most APIs will enable. Check, don't fix --
# which billing account to attach is not something to guess at.
BILLING_ENABLED="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null)"
if [[ "$BILLING_ENABLED" != "True" ]]; then
    echo "" >&2
    echo "WARNING: billing does not appear to be linked to $PROJECT." >&2
    echo "The API-enable step below will fail if that's the case. If it does," >&2
    echo "link a billing account first: https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT" >&2
    echo "" >&2
fi

# ======================================================================
# Enable the APIs this engine needs
# ======================================================================
echo "Enabling required APIs (this can take a minute the first time) ..."
if ! gcloud services enable \
    drive.googleapis.com \
    gmail.googleapis.com \
    calendar-json.googleapis.com \
    admin.googleapis.com \
    --project="$PROJECT" 2>/tmp/bootstrap_gcp_err; then
    cat /tmp/bootstrap_gcp_err >&2
    fail "enabling APIs failed" \
         "most commonly this means billing isn't linked yet (see the warning above), or your account lacks 'roles/serviceusage.serviceUsageAdmin' on this project -- ask your GCP project owner to grant it, or to run 'gcloud services enable drive.googleapis.com gmail.googleapis.com calendar-json.googleapis.com admin.googleapis.com --project=$PROJECT' themselves."
fi

# ======================================================================
# Service account: create if missing
# ======================================================================
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
    echo "Service account $SA_EMAIL already exists -- using it."
else
    echo "Creating service account $SA_EMAIL ..."
    if ! gcloud iam service-accounts create "$SA_NAME" \
        --project="$PROJECT" \
        --display-name="workspace-migrator ($ROLE)" 2>/tmp/bootstrap_gcp_err; then
        cat /tmp/bootstrap_gcp_err >&2
        fail "service account creation failed" \
             "your account likely lacks 'roles/iam.serviceAccountAdmin' on $PROJECT -- ask your GCP project owner to grant it, or to create the service account for you."
    fi
fi

# Service account creation is eventually consistent: a keys.create issued
# immediately after an accounts.create fails with NOT_FOUND even though the
# account exists. Wait for it to become visible rather than making the
# operator re-run the whole script.
echo "Waiting for the service account to propagate ..."
for attempt in $(seq 1 12); do
    if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" \
            >/dev/null 2>&1; then
        break
    fi
    sleep 5
    if [[ "$attempt" == "12" ]]; then
        fail "service account $SA_EMAIL still not visible after 60s" \
             "this is usually transient -- re-run the script and it should find the existing account"
    fi
done

# The numeric ID gcloud calls 'uniqueId' is the same value Workspace Admin
# Console calls the service account's 'Client ID' for DWD purposes.
CLIENT_ID="$(gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" --format='value(uniqueId)' 2>/dev/null)"
[[ -n "$CLIENT_ID" ]] || fail "could not read the service account's client ID after creating it -- this is unexpected; check 'gcloud iam service-accounts describe $SA_EMAIL --project=$PROJECT'"

# ======================================================================
# Key generation
# ======================================================================
mkdir -p "$(dirname "$KEY_OUT")"
chmod 700 "$(dirname "$KEY_OUT")" 2>/dev/null || true

if ! gcloud iam service-accounts keys create "$KEY_OUT" \
    --iam-account="$SA_EMAIL" --project="$PROJECT" 2>/tmp/bootstrap_gcp_err; then
    cat /tmp/bootstrap_gcp_err >&2
    # A failed attempt leaves a zero-byte file behind, which looks like a
    # usable key until something tries to parse it.
    [[ -s "$KEY_OUT" ]] || rm -f "$KEY_OUT"

    if grep -qi "disableServiceAccountKeyCreation" /tmp/bootstrap_gcp_err; then
        cat >&2 <<EOF

Key creation is blocked by an organization policy. Both the classic
(iam.disableServiceAccountKeyCreation) and managed
(constraints/iam.managed.disableServiceAccountKeyCreation) forms produce this.
README.md section 1.1 flags it.

Two ways forward:

  1. Override the policy for this project. Needs the Organization Policy
     Administrator role:

       cat > /tmp/pol.yaml <<'YAML'
       name: projects/$PROJECT/policies/iam.managed.disableServiceAccountKeyCreation
       spec:
         rules:
         - enforce: false
       YAML
       gcloud org-policies set-policy /tmp/pol.yaml

  2. Create the project inside the organisation that owns the *tenant this
     credential is for*, and run this script there. Policies are per-org, so
     the tenant's own organisation may not enforce this constraint at all --
     and putting each credential in its own org is the separation README
     section 1.1 asks for anyway.

EOF
        fail "key creation blocked by org policy on $PROJECT" \
             "see the two options printed above"
    fi
    fail "key creation failed" \
         "your account likely lacks 'roles/iam.serviceAccountKeyAdmin' (or the broader 'roles/iam.serviceAccountAdmin') on $PROJECT."
fi
chmod 600 "$KEY_OUT"

# ======================================================================
# What's left -- the one step this script cannot do
# ======================================================================
echo ""
echo "=================================================================="
echo "GCP-side setup done."
echo "  Project:      $PROJECT"
echo "  Service acct: $SA_EMAIL"
echo "  Client ID:    $CLIENT_ID"
echo "  Key file:     $KEY_OUT (mode 600)"
echo "=================================================================="
echo ""
echo "ONE MANUAL STEP REMAINS -- this has no API, a super admin must do it"
echo "by hand in the browser:"
echo ""
if [[ -n "$DOMAIN" ]]; then
    echo "  1. Sign in to https://admin.google.com as a super admin of $DOMAIN"
else
    echo "  1. Sign in to https://admin.google.com as a super admin of the target Workspace domain"
fi
echo "  2. Security -> Access and data control -> API controls ->"
echo "     Domain-wide delegation -> Add new"
echo "  3. Client ID:"
echo "       $CLIENT_ID"
echo "  4. OAuth scopes (paste as one comma-separated line, no spaces):"
echo "       $SCOPES"
echo "  5. Authorize. Propagation is usually ~2 minutes, occasionally up to 30."
echo ""
echo "After that, this key is ready to use as the '$ROLE' credential."
