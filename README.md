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

## 0. The guided wizard (start here)

```bash
python3 webui.py          # then open http://127.0.0.1:8080
```

A nine-step wizard that shows **one step at a time** and works out where you
already are. Every step detects its own state rather than trusting a checklist,
so closing the tab mid-migration and reopening lands you back in the right
place. Step 5 turns green when a token mint actually succeeds — not when you
click something.

It exists mainly to remove two failure modes that have nothing to do with
migration:

* **Typing commands with placeholders.** `./setup.sh --source-domain <SRC>`
  fails with `zsh: parse error near '\n'`, because `<SRC>` is redirection
  syntax and the shell rejects the line before the script ever runs. Step 2 is
  a form instead. It also checks the things that otherwise fail hours later —
  that each admin is in the domain it administers, and that the two domains
  differ.
* **Not knowing where to run it.** Step 2 offers "run on a VPS", which copies
  the tool there, installs it and starts its UI on the remote host. See §0.1.

The terminal equivalent is `python3 wizard.py --watch`.

> The server binds to **127.0.0.1** only and runs commands on its host. Reach a
> remote one through an SSH tunnel, never by binding it to a public interface.

### 0.1 Running it on a VPS

A migration runs for hours. A laptop that sleeps or changes networks interrupts
it — recoverably, since the engine resumes, but a run needing six resumes is a
run that will be got wrong.

```bash
python3 deploy_remote.py --host 1.2.3.4 --user root
python3 deploy_remote.py --host 1.2.3.4 --user root --include-credentials
```

This copies the tool to the host and runs the UI **there**, rather than keeping
the UI local and sending each command over SSH. One execution path instead of
two, and the migration no longer dies with the laptop's SSH session.

`--include-credentials` is opt-in and asks for a typed confirmation in the UI.
Without it, `keys/`, `oauth/` and `env.sh` are not copied — those files can read
every mailbox in both tenants, so sending them to a host should be a decision
rather than a side effect. `migration.db` is never copied either: overwriting
the remote's resume ledger with a laptop's would make a half-finished migration
look complete.

When it finishes it prints the tunnel command:

```bash
ssh -p 22 -L 8080:localhost:8080 root@1.2.3.4    # then open http://localhost:8080
```

---

## 1. Prerequisites

### 1.0 Choosing an authentication mode

Set `AUTH_MODE`. The three modes differ in *who has to do setup work*, which in
practice matters more than the technical differences.

| `AUTH_MODE` | Setup per tenant | Can migrate | Use when |
|---|---|---|---|
| `key` (default) | GCP project, service account, JSON key, DWD scope paste | **every user** | you run this yourself and are comfortable in the Cloud console |
| `impersonate` | same, minus the JSON key | **every user** | key creation is blocked by `disableServiceAccountKeyCreation` |
| `oauth` | admin clicks **Allow** in a browser | **only the admin who consented** | a non-technical customer is doing the setup |

The honest trade-off: `oauth` is the only mode a non-technical person can
complete unaided, and it is also the only mode that *cannot migrate other
people's mailboxes*. An OAuth grant acts as the person who consented — there is
no `subject` to switch into another user's account. `auth.py` refuses outright
rather than silently migrating the wrong mailbox:

```
OAuth for the source tenant was granted by info@src.com, so it cannot act
as alice@src.com. Migrating other users needs either domain-wide delegation
(AUTH_MODE=key) or a Marketplace domain-install.
```

To get both — browser consent *and* all users — the app has to be published to
the Google Workspace Marketplace and domain-installed by the admin. That grants
delegation proper. It is the right end state for a product; it needs a verified
listing, so it is not something you can turn on this afternoon. See §1.4.

### 1.0.1 Setting up `oauth` mode

This is **one-time work for you, the tool's operator** — not per tenant, and not
something the customer ever sees.

1. In any GCP project: **APIs & Services → Credentials → Create credentials →
   OAuth client ID → Desktop app**.
2. Download the JSON to `oauth/client_secret.json`.
3. Add the redirect URI `http://localhost:8080/oauth/callback` to the client
   (Web application type) if you are running the web UI on a remote host.

Then, per tenant, the admin does this and nothing else:

```bash
AUTH_MODE=oauth python3 webui.py     # open the page, click "Connect source tenant"
```

The page shows two buttons. Each opens Google's own sign-in, the admin approves,
and the token lands in `oauth/{source,target}-token.json` (mode `0600`). Treat
that file exactly as you would have treated a service-account key — a refresh
token is long-lived and carries every scope consented to. `Disconnect` forgets
it locally; it does **not** revoke access, which is done from the domain's admin
console.

> **Verification.** Drive and Gmail scopes are *restricted*. Using them against
> domains other than your own requires Google app verification plus an annual
> CASA security assessment; until that is granted, the consent screen shows an
> unverified-app warning and is capped at 100 users. An app marked **Internal**
> to a single organisation is exempt from all of it — which is what makes this
> testable immediately against your own domains.

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

Steps 1–4 (everything except the Admin Console authorisation in §1.2) can be
scripted with `bootstrap_gcp.sh`, using whatever `gcloud` identity you're
already authenticated as:

```bash
./bootstrap_gcp.sh --project your-source-project --sa-name source-sa \
    --role source --key-out keys/source-sa.json --domain tenantA.com
./bootstrap_gcp.sh --project your-target-project --sa-name target-sa \
    --role target --key-out keys/target-sa.json --domain tenantB.com
```

It prints the exact Client ID and scope string §1.2 needs. It does not, and
cannot, do §1.2 itself — Domain-Wide Delegation authorisation has no API;
Google requires a super admin to grant it by hand in the browser, on purpose.

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

### 1.4 Getting browser consent *and* all users (Marketplace)

`oauth` mode as shipped covers one account per tenant. The path to browser-only
setup that still reaches every user is a Google Workspace Marketplace listing,
domain-installed by the admin — a domain-install grants delegation, so the same
OAuth client can then impersonate any user in the domain.

What that requires, in order:

1. A published Marketplace listing (private listings are allowed, and are the
   usual choice for a migration tool).
2. OAuth app verification for the restricted Drive/Gmail scopes.
3. An annual CASA Tier-2 security assessment — a real audit with a real cost,
   renewed yearly.

Once installed, the admin's flow is: install from the Marketplace → done. No
Client ID, no scope string, no JSON key. Until then, `key` mode remains the only
way to migrate a whole tenant, and the setup burden it carries is the price.

---

## 2. Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AUTH_MODE=key          # key | impersonate | oauth  (see 1.0)
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

### 2.1 Transfer mode

```bash
export TRANSFER_MODE=download_upload   # default
export TRANSFER_MODE=server_side       # faster, higher fidelity, wider grant
```

**`download_upload`** streams every file through this host. It works with a
strictly read-only source service account, which is why it is the default.

**`server_side`** never moves bytes through this host at all. It creates a
staging Shared Drive in the *target* organisation, adds the source user as an
organizer, has Google copy each file straight into it (`files.copy`), then has
the target user move the copy into their My Drive — which is what makes them
the real owner. Measured against live tenants, this is dramatically faster
than streaming, and it fixes three fidelity problems at once:

| | `download_upload` | `server_side` |
|---|---|---|
| Bytes through this host | all of them | none |
| Native Docs/Sheets/Slides | OOXML round trip, formatting drift | stay native, no round trip |
| Native files over 10 MB | `SKIPPED_EXPORT_TOO_LARGE` | migrate fine — no export involved |
| Forms / Sites / Jamboard | `SKIPPED_UNEXPORTABLE` | copy across (verified for Forms) |
| Source service account | `drive.readonly` | needs full `drive` |

That last row is the real cost, and it is not small: `files.copy` is a
*create* call made as the source user, so the source credential can no longer
be read-only. You give up the structural guarantee that a source key is
incapable of writing to the source tenant. Decide that deliberately — the
scopes it needs are printed by `python main.py scope`.

Both modes are equally idempotent and resumable. If a file is copied into the
staging drive but the move out fails, the copy is deliberately left there and
the staging drive is **not** deleted; the next run finishes the move. A
staging drive that still contains files is never cleaned up, because losing
bytes is worse than leaving a drive behind.

> What `server_side` does **not** do is move the original file object. Drive
> refuses to move a file across an organisation boundary in either direction
> (verified: `403 insufficientFilePermissions`), so target files are new
> objects with new IDs, and any saved link to an old file ID still points at
> the source tenant. There is no API that changes this.

### 2.2 Optional passes

Four things are migrated only when asked for. Each is off by default because
it either widens the OAuth grant or carries a fidelity caveat worth agreeing
to explicitly.

```bash
export MIGRATE_COMMENTS=true              # Drive comments + replies
export MIGRATE_SECONDARY_CALENDARS=true   # calendars beyond 'primary'
export MIGRATE_CALENDAR_ACLS=true         # calendar sharing rules
export MIGRATE_GMAIL_SETTINGS=true        # Gmail filters
```

| Flag | What it adds | What it costs |
|---|---|---|
| `MIGRATE_COMMENTS` | Comment threads and replies on every migrated file | No extra scope. The API cannot author a comment *as* someone else, so each migrated comment is written by the target user with the original author and date prefixed into the text — visible, but honest |
| `MIGRATE_SECONDARY_CALENDARS` | Every calendar the user **owns** beyond `primary`, recreated on the target with its events | No extra scope. Subscribed/shared calendars are deliberately skipped — they belong to someone else, and copying one forks it instead of re-subscribing |
| `MIGRATE_CALENDAR_ACLS` | Calendar sharing rules, identity-mapped like Drive ACLs | **Upgrades the source grant** from `calendar.readonly` to full `calendar` — `acl.list` is rejected under read-only (verified: 403) |
| `MIGRATE_GMAIL_SETTINGS` | Gmail filters (label actions remapped) **and signatures** (addresses inside rewritten via `identity_map`) | Adds `gmail.settings.basic` to **both** tenants. No read-only variant of that scope exists |

Gmail **drafts** need no flag and no extra scope — they migrate with messages.

Signatures get one specific piece of care: an address inside a signature that
has an `identity_map` entry is rewritten to its target equivalent, so a
migrated signature stops advertising a mailbox on the tenant you are about to
switch off. Addresses *without* a mapping — a vendor's support address, a URL
that happens to contain the domain — are left exactly as written rather than
guessed at. Send-as aliases themselves cannot be migrated at all: Google makes
the owner confirm an alias by email first. Recreate those by hand, then re-run
to attach their signatures.

If one of these flags is on but the matching scope was never authorised, the
pass logs a warning naming the exact missing scope and the migration carries
on — a missing grant costs you that one feature, not the whole run.

Adding a scope the Admin Console has not authorised makes *every* call fail
with `unauthorized_client`, so none of these widen the baseline grant unless
you turn them on. `python main.py scope` prints the exact scope list for your
current configuration.

Two Calendar constraints worth knowing, both found by testing rather than
documentation:

- Importing into a **secondary** calendar is refused unless that calendar is
  the organizer or an attendee. The engine adds it as an *attendee*, which
  satisfies the rule while keeping the original organizer — making it the
  organizer instead works too, but silently rewrites who owned every meeting.
- `acl.list` needs the full `calendar` scope. If the ACL pass is enabled
  without it, the engine logs a loud warning naming the missing scope rather
  than skipping silently, because "no sharing rules" and "couldn't read the
  sharing rules" look identical otherwise.

---

## 2.3 Repair tools

Two one-off scripts for cleaning up after a run. Both are safe to re-run and
read-only against the source.

```bash
python repair_modified_times.py --dry-run    # report, change nothing
python repair_modified_times.py              # apply
```

Fixes `modifiedTime` on already-migrated items. Granting a Drive permission
bumps a file's `modifiedTime` to now, and ACLs are applied after the copy, so
data migrated before that was accounted for shows the migration date on every
*shared* file — which quietly breaks "sort by last modified" for exactly the
files people collaborate on. The engine now re-asserts the timestamp after
ACLs, but neither a re-run nor a delta pass repairs existing data: the item is
already in `id_mapping` so a full run skips it, and the source has not changed
so a delta skips it too. Hence this.

```bash
python resolve_failures.py --dry-run
python resolve_failures.py
```

Re-attempts everything still marked `FAILED` and records what actually
happened. Files are retried under whatever `TRANSFER_MODE` is set — a big
native Doc that fails under `download_upload` (export/re-upload tripping a
Google 500) generally succeeds under `server_side`, which never exports. ACL
rows are re-evaluated with the current identity handling, so a grant whose
grantee no longer exists is reclassified `SKIPPED_UNMAPPED_IDENTITY` rather
than left as a `FAILED` row implying something is still recoverable.

---

## 2.4 Creating accounts

The engine **maps** identities; it does not create them as part of migrating.
Account creation lives in its own command so that copying files can never be
the thing that provisions a licensed user:

```bash
python main.py provision-users --tenant target --dry-run   # report only
python main.py provision-users --tenant target             # create
```

It only ever **creates**. An address that already exists is left exactly as it
is — never renamed, never given a new password — because overwriting a real
account because a CSV disagreed with it is not recoverable. The set is bounded
by `identity_map`, and it refuses outright if any address is not in the tenant
you named, so a typo cannot create an account in a domain nobody meant to
touch. Needs `https://www.googleapis.com/auth/admin.directory.user` and
`SOURCE_ADMIN`/`TARGET_ADMIN` set to a super admin.

Accounts are created with `changePasswordAtNextLogin=False`, and that is
load-bearing rather than a convenience: **a pending password change silently
breaks domain-wide delegation**. Two accounts in testing failed impersonation
with `Active session is invalid` for exactly this reason, with nothing in the
API response pointing at the cause. An account provisioned for a migration it
then cannot participate in is worse than no account.

Generated passwords are printed once and stored nowhere — the migration
reaches these accounts through delegation and never needs them.

The seeder can do the same for a sandbox source domain:

```bash
python seed_sandbox.py --confirm-domain sandbox-src.example --create-users --scale medium
```

Still behind all three of the seeder's guard rails (`SANDBOX_MODE=true`, a
matching `--confirm-domain`, and absence from `PROTECTED_DOMAINS`).

> Worth saying plainly: in a real migration, provisioning usually belongs to
> GCDS, your IdP, or an HR system, where names, org units, licences and
> lifecycle already live. This command exists to make test tenants quick to
> stand up, not to replace that.

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
