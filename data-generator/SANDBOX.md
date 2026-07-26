# Verifying against two live tenants

You have two Workspace tenants. This is the runbook that turns them into a
rehearsal you can repeat, not a one-shot manual click-through.

Total time: about 90 minutes the first time, 20 minutes for each subsequent
iteration.

---

## 0. Before anything else: are these tenants disposable?

**If either tenant contains real users or real data, stop.** The seeder writes a
few hundred junk files, a dozen test messages and several calendar events into
the source, and the migration writes all of it into the target. Neither is
something you want in a tenant people work in.

Options, best first:

1. **Two Workspace trial domains.** Free for 14 days, full super-admin console,
   which is the part you actually need to rehearse. This is what I'd use.
2. **Dedicated test users in your existing tenants.** Workable. Put them in
   their own OU so you can apply sharing policy separately, and understand that
   the seeder will fill their Drive with junk.
3. **Your production tenants.** Don't.

The tooling enforces this. `tools/seed_sandbox.py` refuses to run unless
`SANDBOX_MODE=true` is set, `--confirm-domain` exactly matches `SOURCE_DOMAIN`,
and the domain is absent from `PROTECTED_DOMAINS`. Set that last one now, before
you forget:

```bash
export PROTECTED_DOMAINS=yourcompany.com,yourclient.com
```

---

## 1. Credentials (about 40 minutes, mostly waiting)

Follow README §1 for both tenants — two GCP projects, two service accounts, DWD
grants in each Admin Console. Do it properly rather than shortcutting; rehearsing
the setup is half the value of this exercise.

**Then a third grant, for seeding only.** The source service account is
read-only by design, which means it cannot create the test corpus. That friction
is deliberate: it is what makes it structurally impossible to seed a production
tenant by accident.

In the **sandbox source** Admin Console, grant a service account these write
scopes and point `SEED_SA_KEY` at its key:

```
https://www.googleapis.com/auth/drive,
https://www.googleapis.com/auth/gmail.insert,
https://www.googleapis.com/auth/gmail.labels,
https://www.googleapis.com/auth/gmail.modify,
https://www.googleapis.com/auth/calendar
```

> Never add these scopes to the service account you will use against a real
> source domain. If you reuse the same SA, remove them before going near
> production.

```bash
export SANDBOX_MODE=true
export SOURCE_DOMAIN=sandbox-src.example
export TARGET_DOMAIN=sandbox-dst.example
export SOURCE_SA_KEY=./keys/sandbox-src-sa.json
export TARGET_SA_KEY=./keys/sandbox-dst-sa.json
export SEED_SA_KEY=./keys/sandbox-seed-sa.json
export SOURCE_ADMIN=admin@sandbox-src.example
export TARGET_ADMIN=admin@sandbox-dst.example
```

Create at least two users in each tenant — `alice` and `bob`. Bob exists so
there is a real internal identity to translate in ACLs and attendee lists.

**DWD grants take time to propagate.** Usually two minutes, occasionally
thirty. If `preflight` returns `unauthorized_client`, wait and retry before
debugging anything else.

---

## 2. Check the target tenant's sharing policy

Do this *before* seeding, or you'll spend an hour blaming the engine for missing
permissions that the tenant refused.

Admin Console → Apps → Google Workspace → Drive and Docs → Sharing settings.
Newer tenants default to blocking external sharing and link sharing entirely.
For the rehearsal to be meaningful, allow both — then note that you did, because
your production target may not.

The seeder reports rejected ACL types in its manifest rather than swallowing
them, so if you skip this step you'll at least see it named.

---

## 3. Seed the source: a five-user organisation

Create five users in the **source** tenant and five matching accounts in the
**target** tenant: `alice`, `bob`, `carol`, `dave`, `erin`. The target accounts
must exist and be unsuspended before migration — this engine maps identities, it
does not provision them.

```bash
python tools/seed_sandbox.py \
  --confirm-domain sandbox-src.example \
  --scale medium \
  --external-email your.personal@gmail.com
```

Seeds all five users in parallel and writes both `sandbox_manifest.json` and a
ready-to-use `identities.csv`.

### Scale profiles

| `--scale` | Files | Folders | ACL grants | Roughly |
|---|---:|---:|---:|---:|
| `tiny` | 250 | 134 | 83 | under a minute |
| `small` | 460 | 165 | 113 | under a minute |
| `medium` | 1,340 | 196 | 200 | ~1 min |
| `large` | 4,450 | 252 | 455 | ~2 min |
| `huge` | 15,200 | 307 | 1,210 | ~8 min |

Start at `medium` for the first rehearsal; it exercises every shape in a couple
of minutes. Move to `large` or `huge` once the phases are green and you want to
see pagination, throughput and quota behaviour under real load.

### What gets built

Each user owns one department and one project, and shares outward:

```
MIGRATION-TEST/
├── Dept-Engineering/          <- alice owns; shared DOMAIN-WIDE (reader)
│   ├── Architecture/  Runbooks/  Postmortems/
│   └── Design Docs/  API Specs/  Onboarding/
├── PRJ-001-Apollo/            <- shared with 3 named peers (writer)
│   ├── Discovery/  Design/  Specs/
│   └── Meeting Notes/  Assets/  Status Reports/
├── Archive/2023..2025/Q1..Q4/
├── Personal/                  <- shared with NOBODY
└── 99-Edge-Cases/
```

| User | Department | Sharing |
|---|---|---|
| alice | Engineering | domain-wide reader |
| bob | Finance | **restricted** — two named colleagues only |
| carol | Sales | domain-wide, plus per-account fan-out |
| dave | Marketing | domain-wide reader |
| erin | People | **restricted** — two named colleagues only |

Files are Google Docs with real prose, Sheets with real CSV data, Slides decks,
PDFs, images, CSV/JSON/ZIP. Around 18% carry an individual grant on top of what
they inherit, some to an external address, some "anyone with link".

Mail is seeded between the five users — cross-addressed, CC'd, in every read
state, with nested labels and a three-message thread. Calendar meetings have
attendees drawn from the org, mixed RSVP states, recurrence, and external
guests.

Plus the corpus of things that have broken migrations before:

| Seeded | Why |
|---|---|
| 16-level folder chain | recursion limits, path-length assumptions |
| 250 files in one folder | pagination bugs that silently drop page 2 |
| `re/port "final" (v2).pdf`, emoji, 120-char names | naive path handling |
| A ~12 MB native Doc | the 10 MB `files.export` ceiling |
| Zero-byte file, 6 MB file | chunk-threshold boundaries |
| Every ACL shape: user, external, domain, anyone | identity translation, target policy |
| Folder-level grant with 3 children | inherited permissions not duplicated per file |
| Shortcut into the deep tree | two-pass ordering |
| Read / unread / starred / Spam / Trash mail | state preservation |
| Nested label `Clients/Acme/2024` | parent-first label creation |
| 3-message thread, 2 MB attachment | threading, media upload path |
| Weekly recurring meeting, external guest, all-day event | recurrence, attendee mapping |

### Why the sharing matters more than the volume

The five users collectively **see** far more than they collectively **own**. With
`OWNED_ONLY=true` (the default), each user migrates only what they own; files
shared *to* them arrive through the owner's migration.

So the corpus establishes an equality worth checking:

```
files across 5 TARGET users  ==  files OWNED across 5 SOURCE users
```

If the target comes out several times larger, the engine is copying every shared
file once per recipient — meaning you pay to store the same deck four times and
nobody can tell which copy is authoritative. Every individual file would still
look perfectly correct. The `duplication` phase asserts this ratio, and it is
the check that only becomes possible once files are shared across users.

**One thing to do by hand:** open the "Weekly team sync" series in the Calendar
UI and move a single instance. The API cannot cleanly seed a recurrence
exception, and exceptions are the fiddliest part of calendar migration. Also
create one Google Form, so you can confirm it is cleanly skipped rather than
crashing the run.

---

## 4. Run the rehearsal (10–30 minutes)

```bash
python main.py init-db --identities identities.csv   # written by the seeder
python tools/rehearsal.py
```

Ten phases across all five users, each with a pass/fail gate:

```
--- preflight ---     DWD works for all 5 users on both tenants
--- discover ---      counts match the seed manifest
--- dryrun ---        --dry-run wrote NOTHING to the target
--- migrate ---       bulk copy completed for all users
--- sharing ---       cross-user ACLs translated; Personal stayed private
--- duplication ---   target count == source OWNED count, not visible count
--- idempotency ---   second full run created ZERO new items
--- interrupt ---     SIGINT mid-run, resume, converges
--- delta ---         edited 3 files per user, exactly those moved
--- verify ---        reconciliation clean across all users
```

Watch it live in a second terminal:

```bash
python tui.py
```

### The phases that matter

**`duplication`** counts what the five source users actually own, subtracts
deliberate skips, and compares against the five target mailboxes. A ratio near
1.0 passes; a ratio near the number of users means shared files are being
duplicated per recipient.

**`sharing`** confirms that `bob@sandbox-src` became `bob@sandbox-dst` in the
ACL, that no source-domain address leaked through, that the external
collaborator survived, that the domain grant now points at the **target**
domain — and that the `Personal` folder still has zero grants. A migration that
leaks private files is worse than one that drops them.

**`idempotency`**. Everything else can be re-run and forgiven. A non-idempotent
engine silently duplicates a customer's entire Drive the first time anyone
restarts a failed job — and by the time you notice, you have two copies of
40,000 files and no clean way to tell them apart.

That phase counts the target through the API before and after a second full
`migrate`, and fails if a single item appears. It is not checking the audit log;
it is asking Google.

Worth knowing: writing exactly this test against the fakes found **two real bugs
in the engine I gave you** — shortcuts were re-created on every run (the
shortcut branch returned before the `id_mapping` lookup that protects ordinary
files), and permanently-skipped items were re-downloaded every night. Both are
fixed, and both now have regression tests. Assume there are more, and let the
rehearsal find them rather than a customer.

---

## 5. Look at it yourself

Reconciliation checks counts and checksums. It cannot tell you a Sheet's
conditional formatting survived. Sign in as `alice@sandbox-dst.example` and
spend ten minutes:

**Drive**
- [ ] Folder tree matches, at full depth
- [ ] The 250-file folder has all 250 (pagination)
- [ ] `re/port "final" (v2).pdf` opens, name intact
- [ ] Open the migrated Doc and Sheet — formatting intact?
- [ ] Sort by "Last modified" — do the dates look like 2023–24, or all today?
- [ ] Right-click the shared file → Share. Is `bob@sandbox-dst.example` there
      with the right role? Is the external address preserved? Is
      `bob@sandbox-src.example` **absent**?
- [ ] The three files under `inherited-acl` should have *no* direct grants —
      they inherit from the folder
- [ ] The shortcut resolves to a real file, not a dead pointer

**Gmail**
- [ ] Inbox sorts correctly by date, with 2019 mail at the bottom
- [ ] Unread count is 3-ish, not "everything"
- [ ] The Q2 numbers thread is one conversation, not three loose messages
- [ ] `Clients/Acme/2024` exists, nested correctly
- [ ] Spam and Trash contents are where they belong
- [ ] The 2 MB attachment downloads and opens

**Calendar**
- [ ] Weekly sync appears as a series, not 12 standalone events
- [ ] Your hand-moved instance is still moved
- [ ] Bob shows as accepted — he should **not** have been re-invited
- [ ] The all-day event is all-day, not 00:00–01:00

**Then sign in as bob in the TARGET tenant.** Alice's Engineering folder should appear in his "Shared with me" — not duplicated into his own Drive. Open Apollo: he should be a writer, and alice should be the owner.

**Then check bob's mailbox in the SOURCE tenant.** He should have received
nothing at all. If bob has 40 calendar invitations, `events.import` is not being
used and you have found the exact failure this design exists to prevent.

---

## 6. Reset and go again

```bash
python tools/seed_sandbox.py --confirm-domain sandbox-src.example --reset
rm -f migration.db sandbox_manifest.json identities.csv
```

Resets all five users in parallel. Reset the **target** too, or your second
rehearsal starts dirty — point the same command at the target domain with
`SOURCE_DOMAIN` temporarily set to it, or delete and recreate the five accounts.

You will iterate several times. That's the point — each cycle is 20 minutes once
the credentials are in place, and each one buys you a fix you'd otherwise make
in front of a customer.

---

## 7. Things that only show up with real tenants

Keep a note of these as you hit them; they're the reason this layer exists.

| Symptom | Cause |
|---|---|
| `unauthorized_client` | DWD scope string mismatch, or grant hasn't propagated. Paste scopes as one comma-separated line with no spaces. |
| `invalid_grant` | The impersonated user doesn't exist, is suspended, or the key was revoked. |
| External ACLs all fail with `domainPolicy` | Target tenant blocks external sharing. Admin Console, not a code bug. |
| `storageQuotaExceeded` | Target user is at their storage cap. Non-retryable by design. |
| Native Doc export returns nothing | Over 10 MB. Should log `SKIPPED_EXPORT_TOO_LARGE`. |
| Sustained `userRateLimitExceeded` | `PER_USER_QPS` is too generous. Lower it; backoff will mask the problem while destroying throughput. |
| Trial tenant stops accepting users | Trials cap at 10 users. |

---

## 8. Promotion checklist

Do not point this at production until:

- [ ] `pytest` — 114 tests green
- [ ] All 10 rehearsal phases green across all 5 users
- [ ] `idempotency` and `duplication` green **specifically**, run three times
- [ ] Manual review above completed with no surprises
- [ ] Bob's source mailbox received zero notifications
- [ ] `python main.py scope --status NONE` reviewed and signed off by whoever
      owns the data
- [ ] Sharing policy differences between sandbox and production target
      understood and written down
- [ ] Seeding write scopes removed from any SA that will touch production

Then Layer 5: three to five real volunteers, a week before their cutover.
