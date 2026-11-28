# 300-user seed runbook (P5.2–P5.3)

Run these commands in a root Bash shell on the Bitport box, from `/opt/bitport`. The source is `a.sarafgloabalexim.com`; the target is `b.sarafgloabalexim.com`. Use the account ID shown for this pair in Bitport. The seed writes only to the source. The target wipe removes target data but retains its users and setup.

The operator authorized this sandbox run. Keep the sandbox and typed-domain guards enabled. Do not use `--allow-partial`; do not continue after a readiness failure, active job, incomplete wipe, a refused budget, or a failed seed benchmark.

## 1. Select and verify the account

```bash
cd /opt/bitport
set -euo pipefail
read -r -p 'Bitport account ID for a. -> b.: ' ACCOUNT_ID
[[ "$ACCOUNT_ID" =~ ^[0-9]+$ ]]
export ACCOUNT_ID
export SEED_LOCALPART_PREFIX=

# Load only this account's saved pair into the CLI environment. No key contents or passwords are printed.
eval "$(
.venv/bin/python - "$ACCOUNT_ID" <<'PY'
import shlex, sys
from config import Settings
s = Settings(account_id=int(sys.argv[1]))
for name, value in {
    "SOURCE_DOMAIN": s.source_domain,
    "SOURCE_ADMIN": s.source_admin,
    "SOURCE_SA_KEY": s.source_sa_key,
    "TARGET_DOMAIN": s.target_domain,
    "TARGET_ADMIN": s.target_admin,
    "TARGET_SA_KEY": s.target_sa_key,
    "MIGRATION_DB": s.db_path,
}.items():
    print(f"export {name}={shlex.quote(str(value or ''))}")
print("export SANDBOX_MODE=true")
PY
)"

.venv/bin/python - "$ACCOUNT_ID" <<'PY'
import sys
from config import Settings
s = Settings(account_id=int(sys.argv[1]))
assert (s.source_domain, s.target_domain) == (
    "a.sarafgloabalexim.com", "b.sarafgloabalexim.com")
print(f"Account {sys.argv[1]}: {s.source_domain} -> {s.target_domain}")
PY

# Confirm the offline plan's 31 existing seats and 269 unused generated
# addresses against the live source Directory before any target wipe or seed.
.venv/bin/python - "$ACCOUNT_ID" <<'PY'
import sys
from auth import AuthManager
from config import Settings
sys.path.insert(0, "data-generator")
import fixtures

s = Settings(account_id=int(sys.argv[1]))
directory = AuthManager(s).directory("source")
existing, token = set(), None
while True:
    page = directory.users().list(domain=s.source_domain, maxResults=500,
                                  pageToken=token,
                                  fields="nextPageToken,users(primaryEmail)").execute()
    existing.update(u["primaryEmail"].lower() for u in page.get("users", []))
    token = page.get("nextPageToken")
    if not token:
        break
cohort = fixtures.load_cohort("data-generator/cohorts/fableplan2.json")["users"]
cohort_emails = {f"{u['localpart'].lower()}@{s.source_domain.lower()}" for u in cohort}
admin = s.source_admin.lower()
profile = {f"p{i:03d}@{s.source_domain.lower()}" for i in range(1, 270)}
expected_existing = cohort_emails | {admin}
missing = expected_existing - existing
occupied_profile = profile & existing
unexpected = existing - expected_existing
planned_total = len(existing | expected_existing | profile)
new_accounts = len(profile - existing)
print(f"Source Directory seats now: {len(existing)}; cohort users including twins: {len(cohort_emails)}")
print(f"Planned total seats: {planned_total}/300; new profile accounts to create: {new_accounts}")
if len(cohort_emails) != 30 or missing or occupied_profile or unexpected or planned_total != 300:
    print(f"STOP: missing required users={len(missing)}, occupied pNNN={len(occupied_profile)}, "
          f"other existing users={len(unexpected)}")
    raise SystemExit("Directory inventory differs from the offline 300-seat plan")
PY

.venv/bin/python - "$ACCOUNT_ID" <<'PY'
import sys, job_admission
aid = int(sys.argv[1])
active = [j for j in job_admission.list_active()
          if int(j.get("account_id") or 0) == aid and job_admission.is_live(j)]
print(f"active jobs for account {aid}: {active}")
if active:
    raise SystemExit("stop: this account has an active job")
PY

.venv/bin/python readiness.py --account-id "$ACCOUNT_ID" --side source --purpose seed
.venv/bin/python readiness.py --account-id "$ACCOUNT_ID" --side target --purpose migrate
```

Both readiness commands must say `READY`; the active-job check must print `[]`. `readiness.py` failures are a stop. The account ID must resolve to the stated pair before any wipe or seed command.

## 2. P5.2 — empty target b. and check the result

`wipe_tenant.py` is all-or-nothing by default. It preflights every service before deleting anything, retains all target accounts, prints the number of users and items emptied for each service, and exits nonzero if anything remains or could not be reached.

```bash
.venv/bin/python wipe_tenant.py --side target \
  --confirm-domain b.sarafgloabalexim.com --yes
```

Require exit code `0`, a `Removed ... emptied for N of N user(s)` line for every available service (`drive`, `gmail`, `calendar`, `contacts`, `tasks`, `chat`), and successful shared-drive and group removal lines. The per-service item counts are the number deleted; the `emptied for` denominator must cover every target user. `Ledger reset` must name the emptied services. Any `NOT WIPED`, missing scope, failed user, nonzero exit, or incomplete denominator means stop and resolve it before P5.3. Do not switch to `--allow-partial`.

Recheck that no job started during the wipe:

```bash
.venv/bin/python - "$ACCOUNT_ID" <<'PY'
import sys, job_admission
aid = int(sys.argv[1])
active = [j for j in job_admission.list_active()
          if int(j.get("account_id") or 0) == aid and job_admission.is_live(j)]
print(f"active jobs for account {aid}: {active}")
if active:
    raise SystemExit("stop: this account has an active job")
PY
```

The target accounts remain in place. Do not use `wipe_target.py` here; that command deletes target users.

## 3. P5.3 — price the exact seed, including the real file-size tail

First run the requested offline plan exactly. It reserves all 300 seats, reports 269 planned new accounts, and with the default `--max-hours 12` is expected to exit 1 with `REFUSED`; that is a budget refusal and writes nothing. The shell has `set -e`, so capture and verify that expected nonzero result explicitly:

```bash
if PLAN_OUTPUT="$(.venv/bin/python data-generator/seed_sandbox.py \
  --profile data-generator/profiles/org45.json --users 300 --plan-only 2>&1)"; then
  PLAN_RC=0
else
  PLAN_RC=$?
fi
printf '%s\n' "$PLAN_OUTPUT"
[[ "$PLAN_RC" -eq 1 ]]
grep -q '^REFUSED:' <<<"$PLAN_OUTPUT"
```

For the seed plan, include the profile's 2–10 GB file tail. The default file cap remains 50 MiB; these explicit settings raise it to the observed 10.1 GB maximum and give the pool enough bytes to contain that tail. `--max-hours 360` is the run cap used for this plan. This plan-only command uses the recorded 1.84 MB/s upload rate, not a live probe.

```bash
.venv/bin/python data-generator/seed_sandbox.py \
  --profile data-generator/profiles/org45.json --users 300 --plan-only \
  --pool-gb 100 --pool-max-file-mb 10240 --max-hours 360
```

Check that the plan says `300 of 300 tenant seats = admin 1 + existing cohort 30 (including twins) + 269 new profile users`, names `269 new accounts`, prints a largest pool file in the 2–10 GB range (the local plan is 9,666 MiB), and includes every service, payload bytes, and phase hours. The estimated hours must be at or below the explicit 360-hour cap. If it says `REFUSED`, stop; do not raise the cap to force the seed through.

Before the live write, ensure the scratch filesystem has at least 10 GiB free for the largest streamed pool file. Large files use Drive's resumable-upload protocol and are streamed from scratch; once an upload is complete its named pool file is reused on replay, while an interrupted upload can be recreated without duplicating a completed file. The temp file is removed after each upload attempt.

```bash
SCRATCH_DIR="$(.venv/bin/python -c 'from config import Settings; print(Settings().scratch_dir)')"
mkdir -p "$SCRATCH_DIR"
df -h "$SCRATCH_DIR"
```

## 4. Seed the source

The live command uses the same seat count and pool options as the accepted dry plan. It seeds Drive, Gmail, Calendar, Contacts, and Tasks plus idempotent Gmail settings. Chat remains off per the plan. Without `--plan-only`, the seeder measures the box's upload rate before it writes and still refuses if its complete estimate exceeds `--max-hours`. `--yes` skips only the interactive pause; the exact `--confirm-domain`, sandbox check, and protected-domain guard still apply. The cohort file defaults to `data-generator/cohorts/fableplan2.json` (30 accounts, twins included).

Save a root-only, password-redacted transcript. The CLI prints generated account passwords once; the `sed` filter redacts those lines before either the terminal or transcript sees them.

```bash
umask 077
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="/tmp/seed-300-$RUN_ID"
install -d -m 700 "$RUN_DIR"
SEED_LOG="$RUN_DIR/seed.log"
SEED_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
.venv/bin/python data-generator/seed_sandbox.py \
  --confirm-domain a.sarafgloabalexim.com \
  --profile data-generator/profiles/org45.json --users 300 --create-users --yes \
  --only drive,gmail,calendar,contacts,tasks,gmail_settings \
  --pool-gb 100 --pool-max-file-mb 10240 --max-hours 360 \
  --manifest "$RUN_DIR/sandbox_manifest.json" --identities-out identities.csv \
  2>&1 | sed -E 's/(password: ).*/\1[redacted]/I' | tee "$SEED_LOG"
SEED_RC=${PIPESTATUS[0]}
set -e
SEED_FINISHED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'seed exit code: %s\n' "$SEED_RC"
```

Check the transcript's seat line, the account provisioning result, all service summaries (including Gmail settings), and that there are no `FAILED` users or failed services. The tenant-seat line counts the admin and 30 cohort accounts; the seeder itself processes 299 non-admin entries (30 existing cohort users plus 269 profile users). Stop on a nonzero `SEED_RC`; rerun the same command after fixing the cause. The service markers and stable item IDs make that replay safe.

## 5. Read the seven seed benchmarks

A direct CLI run is not automatically observed by the job watcher. Build the same saved seed report from its full redacted transcript and the actual exit code:

```bash
.venv/bin/python - "$ACCOUNT_ID" "$SEED_LOG" "$SEED_RC" \
  "$SEED_STARTED" "$SEED_FINISHED" <<'PY'
import json, pathlib, sys
import run_report
from config import Settings
aid, log_path, rc = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
run = {"name": "seed-300", "returnCode": rc,
       "startedAt": sys.argv[4], "finishedAt": sys.argv[5]}
meta = run_report.generate(None, Settings(account_id=aid), aid, kind="seed",
                           run=run,
                           transcript=pathlib.Path(log_path).read_text(
                               encoding="utf-8", errors="replace").splitlines())
report = run_report.load_report(aid, meta["id"])
print(json.dumps({"report": meta["id"], "verdict": report["verdict"],
                  "benchmarks": report["benchmarks"]["results"],
                  "seed": report["facts"]["seed"]}, indent=2))
if report["verdict"] != "PASS":
    raise SystemExit("seed benchmark report is not PASS")
PY
```

Check each benchmark against its own report metric:

| Benchmark | Must read | P5.3 check |
|---|---|---|
| `run_completed` | `run.nonzeroExit` | `0` (the saved run code is zero) |
| `users_finished` | `seed.finishedShare` | `1.0` for all 299 seed entries |
| `never_finished` | `seed.neverFinished` | `0` after the run ended |
| `service_failures` | `seed.failedServiceShare` and `seed.failedServices` | `0` for P5.3; inspect and fix any nonzero failed service even if the optional benchmark only warns |
| `fill_users_failed` | `seed.fillFailedShare` | This seed does no storage top-up, so it is `unknown`/not applicable; if a fill is added, require `0` |
| `warnings_per_user` | `seed.warningsPerUser` and `facts.failures` | Read and resolve every warning family; values above 1/user are WARN, not a hidden pass |
| `fill_reached` | `seed.fillReached` | This seed does no storage top-up, so it is `unknown`/not applicable; for a fill, require at least 98% |

Only the two fill benchmarks are inapplicable to this normal profile seed; they are optional and must remain visibly `unknown`, never be described as passes. The overall saved seed report must say `PASS`, and all applicable required health checks above must pass.

## 6. Load mappings and run the P5.3 coverage check

The seeder writes `identities.csv` for the 30 cohort and 269 profile users. Load that exact output into this account's ledger, then read coverage for the same account:

```bash
.venv/bin/python main.py --account-id "$ACCOUNT_ID" init-db --identities identities.csv
.venv/bin/python coverage_audit.py --account-id "$ACCOUNT_ID"
```

`init-db` must report 299 mappings for the configured domains. `coverage_audit.py` must complete with `0 ABSENT` and `0 UNPROBED` among supported rows, with its unseedable reasons listed. Any mismatch, read error, or supported gap is a stop and fix before P5.4.
