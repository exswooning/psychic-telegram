# Tenant-to-Tenant Migration Engine

A modular Google Workspace migration engine (tenantA.com → tenantB.com) covering
Drive with ACL translation, Gmail, and Calendar.

```
migration_engine/
├── config.py           # scopes, tunables, MIME export map
├── db.py               # SQLite schema + thread-safe persistence
├── auth.py             # Domain-Wide Delegation for both tenants
├── resilience.py       # backoff decorator, token bucket, 750 GB/day guard
├── discovery.py        # Module 1: pre-migration scan
├── drive_engine.py     # Module 3 + 6: recursive mirror, transfer, ACL mapper
├── gmail_engine.py     # Module 4a: messages.insert ingestion
├── calendar_engine.py  # Module 4b: events.import ingestion
├── main.py             # Module 5: CLI + ThreadPoolExecutor orchestration
├── scope.py            # declarative manifest of what does and does not migrate
├── tui.py              # curses operator dashboard (reads migration.db read-only)
└── requirements.txt
```

---

## 1. Prerequisites

### 1.1 Two GCP projects, two service accounts

Create **one project per tenant**. Using a single service account for both sides
means the source tenant's super-admin has to authorise a key that can also write
into the target — an unnecessary trust relationship across an organisational
boundary.

For each project:

1. **Enable APIs** — APIs & Services → Library:
   - Google Drive API
   - Gmail API
   - Google Calendar API
   - Admin SDK API
2. **Create a service account** — IAM & Admin → Service Accounts.
3. **Enable Domain-Wide Delegation** on it and note the numeric **Client ID**
   (the 21-digit `client_id`, not the email).
4. **Create a JSON key** and download it. Store the two keys as
   `keys/source-sa.json` and `keys/target-sa.json`, mode `0600`.

> Note: on newer projects, `iam.disableServiceAccountKeyCreation` is enforced by
> default. If key creation is blocked, exempt the two projects via an org-policy
> override before proceeding.

### 1.2 Authorise the scopes in each Admin Console

Admin Console → **Security → Access and data control → API controls →
Domain-wide delegation → Add new**. Paste the Client ID, then the scopes as a
single comma-separated line.

**Source tenant (tenantA.com) — read-only:**

```
https://www.googleapis.com/auth/drive.readonly,
https://www.googleapis.com/auth/gmail.readonly,
https://www.googleapis.com/auth/calendar.readonly,
https://www.googleapis.com/auth/admin.directory.user.readonly,
https://www.googleapis.com/auth/admin.directory.group.readonly
```

**Target tenant (tenantB.com) — write:**

```
https://www.googleapis.com/auth/drive,
https://www.googleapis.com/auth/gmail.insert,
https://www.googleapis.com/auth/gmail.labels,
https://www.googleapis.com/auth/gmail.modify,
https://www.googleapis.com/auth/calendar,
https://www.googleapis.com/auth/admin.directory.user.readonly
```

The scope strings must match `config.py` **exactly**. A single missing or extra
scope produces `unauthorized_client` at token-mint time, and DWD grants can take
up to ~30 minutes to propagate.

### 1.3 Target-tenant settings to check first

| Setting | Why |
|---|---|
| Drive external sharing | If the target restricts external sharing, ACLs for outside collaborators fail with `403 domainPolicy`. |
| Storage quota per user | A user at their pooled-storage cap fails with `storageQuotaExceeded` — non-retryable by design. |
| Gmail routing / compliance rules | These do not fire on `messages.insert`, but confirm before a large run. |
| Accounts provisioned | Every `target_email` in `identity_map` must exist and be unsuspended. |

---

## 2. Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SOURCE_DOMAIN=tenantA.com
export TARGET_DOMAIN=tenantB.com
export SOURCE_SA_KEY=./keys/source-sa.json
export TARGET_SA_KEY=./keys/target-sa.json
export SOURCE_ADMIN=admin@tenantA.com
export TARGET_ADMIN=admin@tenantB.com
export USER_WORKERS=6
```

Scratch space needs to hold the largest single file per concurrent worker —
budget roughly `USER_WORKERS × largest_file_size`.

---

## 3. Runbook

### Step 1 — Initialise the database and identity map

```bash
# From a reviewed CSV (recommended):
python main.py init-db --identities identities.csv

# Or derive mappings by matching localparts across both directories:
python main.py init-db --auto-map
```

`identities.csv`:

```csv
source_email,target_email,entity_type
j.smith@tenantA.com,john.smith@tenantB.com,user
sales@tenantA.com,sales@tenantB.com,group
```

This creates `migration.db` with `identity_map`, `id_mapping`, `audit_log`,
`discovery`, `upload_ledger`, and `label_map`.

### Step 2 — Preflight

```bash
python main.py preflight
```

Mints a token and makes one trivial call per user on both tenants. Exits
non-zero on any failure. Run this *before* every large batch — it turns a
four-hour failure into a four-second one.

### Step 3 — Discovery

```bash
python main.py discover --include-mail
```

Read-only. Reports per-user file/folder counts, tree depth, total bytes, MIME
distribution, and a duration estimate, then prints a tenant-wide floor on
wall-clock time derived from the 750 GB/user/day cap.

### Step 4 — Dry run

```bash
python main.py migrate --dry-run --user j.smith@tenantA.com
```

Logs every intended write without performing it.

### Step 5 — Bulk migration

```bash
python main.py migrate --services drive,gmail,calendar
```

Runs `USER_WORKERS` users concurrently. Safe to interrupt with Ctrl-C: in-flight
items finish, state is committed, and a re-run resumes from `id_mapping`.

### Step 6 — Delta passes

```bash
python main.py delta --services drive,gmail,calendar --days 2
```

Run nightly between bulk copy and cutover, and once more after the cutover
window closes.

- **Drive** compares source `modifiedTime` against `audit_log.modified_time` and
  re-uploads changed files in place via `files.update`, so target file IDs and
  their ACLs survive.
- **Gmail** narrows the query with `newer_than:Nd`; `id_mapping` prevents any
  double-insert.
- **Calendar** uses `updatedMin` on `events.list`.

### Step 7 — Watch it

```bash
python tui.py        # attach the dashboard from any session
```

### Step 8 — Report

```bash
python main.py report --max-failures 50
```

---

## 4. The operator dashboard (TUI)

```bash
python tui.py                    # or: python main.py ui
python tui.py --db /srv/migration.db --refresh 1.0
```

The dashboard is a **separate process that reads `migration.db`**. It is not
coupled to the runner. Because all state already lives in SQLite under WAL,
readers never block the writer, which means you can:

- run the migration under systemd/tmux/nohup on a jump box and attach the UI
  from any SSH session — or three sessions at once;
- kill the UI without touching a 40-hour migration;
- survive the UI crashing mid-run.

The connection sets `PRAGMA query_only=ON`, so the UI is structurally incapable
of writing to the ledger that makes the migration idempotent.

### Screens

| Key | Screen | Shows |
|---|---|---|
| `1` | Dashboard | Overall bar, per-service progress, active users, recent failures |
| `2` | Users | Per-user table: drive/mail/cal counts, failures, bytes, progress |
| `3` | Failures | Every `FAILED` row with timestamp, item ID, and error text |
| `4` | Scope | The full scope matrix plus your discovered volume |
| `5` | Logs | Live tail of the job log; `End` resumes following |

### Keys

| Key | Action |
|---|---|
| `d` | Run discovery (`discover --include-mail`) |
| `p` | Run preflight (DWD verification) |
| `m` | Start full migration — **asks for confirmation** |
| `x` | Start delta pass |
| `k` | Stop the running job (sends SIGINT; in-flight items finish and commit) |
| `t` | Toggle dry-run for the next launch |
| `s` / `g` / `c` | Toggle Drive / Gmail / Calendar in the service set |
| `e` | Export the scope matrix to `SCOPE.md` |
| `r` | Force refresh · `↑↓ PgUp PgDn Home End` scroll · `q` quit |

Quitting the UI while a job runs prompts for confirmation and **leaves the job
running** — it is spawned into its own process group.

### Reading the header

```
 tenantA.com  ->  tenantB.com                RUNNING migrate --services ... 02:14:07
 Overall  [##################.....................]  43.2%
 Items 1,204,331 / 2,787,001   Skipped 8,204   Moved 1.8TB        Failures 87
 Users  12 done / 6 running / 1 failed / 2 quota-paused of 40
                                    24h 1,440/4,275GB [###.....] peak 55%
```

The `24h` figure is aggregate usage against aggregate capacity, but the colour
and `peak` reflect the **worst individual user**. The 750 GB ceiling is
per-user, so one account pinned at its cap stalls even when the fleet-wide
average looks comfortable.

Where a bar reads `no baseline`, discovery has not sized that service yet. Run
`d` first — otherwise a percentage is unknowable, and the UI says so rather
than inventing one.

---

## 5. What actually migrates

```bash
python main.py scope                          # full matrix
python main.py scope --status NONE            # only what is left behind
python main.py scope --service drive
python main.py scope --format markdown --out SCOPE.md
python main.py scope --format json
```

`scope.py` holds this as a declarative table rather than prose, so it renders
identically in the CLI, the TUI, and the Markdown export you attach to the
change-approval ticket.

**Summary — 70 tracked data elements:**

| Service | Full | Partial | Not migrated |
|---|---:|---:|---:|
| Drive | 9 | 3 | 12 |
| Gmail | 8 | 1 | 6 |
| Calendar | 8 | 3 | 7 |
| Identity | 1 | 0 | 4 |
| Other Workspace apps | 0 | 1 | 7 |

A 100% tenant-to-tenant migration does not exist for Google Workspace.
Commercial tools carry substantially the same "not migrated" list. What
separates a good migration from a bad one is that the list was agreed **before**
cutover. Run `scope --status NONE` and walk the output with your stakeholders.

The items people are most often surprised by: Drive revision history and
comments, native Docs over 10 MB, Shared Drives, Gmail drafts and filters,
secondary calendars, room-resource bookings, and Contacts/Tasks/Keep/Chat
entirely. User accounts are **mapped, not provisioned** — every target address
must exist and be unsuspended before you start.

---

## 6. Design decisions worth knowing

**`messages.insert`, not `messages.import`.** `import` runs the message through
the delivery pipeline: spam classification, filters, forwarding rules. Legitimate
old mail lands in Spam and user filters fire thousands of times. `insert` writes
directly to the mailbox. Combined with `internalDateSource='dateHeader'`,
original timestamps are preserved instead of every message appearing to arrive on
migration day. Read/unread state needs no special handling — Gmail models unread
as the `UNREAD` label, so copying the label set carries it across.

**`events.import`, not `events.insert`.** `insert` notifies attendees. Migrating
five years of calendar with `insert` sends invitations for every past meeting to
everyone who attended it. `import` preserves the original `iCalUID` and
`organizer` and has no notification path at all.

**403 is not one error.** `rateLimitExceeded` is transient and must be retried;
`insufficientPermissions` and `storageQuotaExceeded` never succeed on retry.
`resilience.py` branches on the `reason` field rather than the status code —
retrying permanent 403s burns quota and hides real bugs.

**Full jitter, not partial.** Delay is `random.uniform(0, base × 2ⁿ)`. When
threads collide on the same quota bucket, partial jitter leaves them synchronised
enough to re-collide.

**Concurrency across users, not within.** Every binding Google quota is
per-user. Ten threads on one mailbox mostly sleep in backoff; ten threads on ten
mailboxes run at nearly ten times the throughput.

**The 750 GB/day cap is persisted.** `upload_ledger` survives process restarts.
Blowing the cap locks the account out of uploads for 24 hours — far more
expensive than pausing. On exhaustion the user is marked `PAUSED_QUOTA` and the
batch continues; the next day's delta pass skips everything already copied.

---

## 7. Known limitations

These are inherent to the Google APIs and apply to commercial tools in this
category too. Set expectations with stakeholders before cutover.

| Limitation | Detail |
|---|---|
| Native Doc export ceiling | `files.export` hard-fails at 10 MB. Logged as `SKIPPED_EXPORT_TOO_LARGE`; needs a PDF fallback or manual handling. |
| Revision history | Not transferable. Target files start with a single revision. |
| Doc comments & suggestions | Lost in the export/import round trip. The Drive Comments API can migrate them as a separate pass if required. |
| Forms, Sites, Jamboards | No export representation. Logged `SKIPPED_UNEXPORTABLE`. |
| File ownership | Everything is owned by the impersonated target user. Cross-domain ownership transfer is blocked by policy in most tenants. |
| Shared Drives | Out of scope here — different traversal (`corpora='drive'`, `driveId`) and a membership model rather than per-file ACLs. |
| Meet links | Source-tenant conference data does not resolve for target users; stripped deliberately (`conferenceDataVersion=0`). |
| Room resources | Dropped from attendee lists; migrate calendar resources separately and re-book. |
| Vault / retention | Not covered. Legal hold obligations need a separate export path. |

---

## 8. Operating notes

- **Migrate Drive before Calendar.** Calendar attachments are remapped through
  `id_mapping`; attachments whose files have not migrated yet are dropped.
- **Rerunning is free.** Every mutating call checks `id_mapping` first.
- **Tune `PER_USER_QPS` down, not up.** Sustained `userRateLimitExceeded` means
  the token bucket is too generous; backoff will mask it while destroying
  throughput.
- **Watch `SKIPPED_UNMAPPED_IDENTITY` in `audit_log`.** Each row is a permission
  silently dropped because the grantee has no `identity_map` entry. A large count
  usually means an incomplete identity map, not a real access change.
- Consider `PRAGMA optimize` / `VACUUM` on `migration.db` between phases on
  multi-million-item tenants.

### Useful queries

```sql
-- Progress by type
SELECT item_type, status, COUNT(*), SUM(bytes_moved)/1024/1024/1024 AS gb
FROM audit_log GROUP BY item_type, status;

-- Everything that needs a human
SELECT source_user, item_type, item_id, error_message
FROM audit_log WHERE status LIKE 'FAILED%' OR status LIKE 'SKIPPED_%';

-- Today's upload consumption against the cap
SELECT target_user, bytes_sent/1024.0/1024/1024 AS gb_today FROM upload_ledger
WHERE day_utc = strftime('%Y-%m-%d','now');
```
