# Bitport to 10/10 on synthetic tenants: a. → b.

The operator calls this plan **fableplan2**.

Operator, 16 Nov 2026: the old tenants are gone. dharan.gov.np can no longer be read, and s. and t.
lapsed. Two fresh tenants exist: a.sarafgloabalexim.com and b.sarafgloabalexim.com. The goal:
1. test every benchmark, error and failure with synthetic data;
2. improve the seeder until it simulates real user data for 300 users;
3. finish every open phase and reach 10/10.

Confirmed by the operator, 16 Nov: a. is the source, b. is the target, both are sandboxes on
Business Starter with 300 licences. The subscriptions end on 16 Dec 2026.

This plan replaces [dharan-to-a.md](dharan-to-a.md) (withdrawn) and the account 3 runs of
[parity-10-plan.md](parity-10-plan.md). That plan's meaning of 10/10 and its open code items are
carried over here; its history stays where it is.

## 1. How to work this plan (for the model executing it)

Model: Sonnet. Items marked **[opus]** need Opus. Do not use Fable for this plan.

Words used below:
- **Cohort**: the 30 test users of Phase 2 (`c01` to `c10`, `t01` to `t20`).
- **Twin**: one of 20 users seeded with identical content. `t01` is the **control**: it is migrated
  with nothing injected, and every failure test compares its twin against it.
- **Failpoint**: a named place in the code where a test can make the process die or lose a reply.
- **Campaign**: the runner (`campaign.py`) that executes scenarios and writes one result line each.

Session routine:
1. Read section 11 (State), then take the first unticked item that is not marked OPERATOR. While an
   OPERATOR item blocks a phase, work the items that need no tenant: Phase 1, then P2.1 to P2.4.
   Read other files only when the item needs them.
2. See what is running, in one read:
   `ssh -i ~/.ssh/bitport_lenovo root@100.79.158.38 'cd /opt/bitport && .venv/bin/python -c "import job_admission as j; print(j.list_active())"; tail -5 logs/incidents/feed.log'`
3. Do one item: the code, a test that fails without it, `./gate.sh`, commit, push. Tick the box and
   add one line to State in the same commit.
4. Stop at about 60% of the usage window.

Rules. They come from CLAUDE.md and are repeated because these are the ones a test run breaks:
1. Writes and deletes go to a. and b. only. Read the domain in the command or request before each one.
2. Wipes and resets: the operator allowed the campaign to reset or wipe b., and to reset seeded data
   on a., without a typed confirmation each time (P0.7(a), 16 Nov). That covers a. and b. only,
   through the product's own wipe and reset. Look at the target first, every time. Any other domain
   needs the operator's typed confirmation, and a refusal is never worked around.
3. A run that counts starts from the product: the UI button, or the same `/api/v2/...` request the
   button sends. Never `main.py migrate` by hand. Reading over SSH is fine.
4. Every error stops the run. Name it, fix it with a test, pass the gate, deploy on an idle box,
   restart from the UI. A new kind of error gets a `known_issues` entry.
5. Deploy only when nothing runs: `./gate.sh && ./sync_vps.sh root@100.79.158.38 /opt/bitport ~/.ssh/bitport_lenovo`.
   The script refuses a busy box or a tree the gate has not passed. Never force it.
6. No password, key or token in the repo, memory, logs or chat. Logins live on the box in
   `/etc/bitport/dwd.env`. Do not open or edit that file.
7. No real person gets mail from a test. The only outside address is the operator's from P0.6.
   Everything else uses `@seed.test` or `example.com`.
8. A check nobody could make is UNVERIFIED, never a pass. Report measured numbers, and say when a
   number is an estimate.
9. Approving a migration, teardown, billing, licences, SSH keys: the operator's only.
10. Never delete or reset account 3's ledger (`/opt/bitport/data/accounts/3/migration.db`). It holds
    the only surviving statistics of real user data (Phase 4 needs them).
11. Tokens: start jobs on the box and read their one-page result. Never tail a log in a loop.
12. Chat stays off. One opt-in Chat scenario runs outside the score.

Hand the item to Opus when: a cause is still unknown after two attempts based on evidence; the change
touches how `drive_engine.py` or `gmail_engine.py` copy an item, `resilience.py`, or the ledger's
schema; or the item is marked [opus].

Stop and ask the operator when: a step says OPERATOR; Google refuses something for a whole tenant
(billing, licences, policy); or a result contradicts this plan.

## 2. What 10/10 means here (the acceptance test)

| # | 10/10 needs | Measured by | Pass |
|---|---|---|---|
| 1 | Every data type exercised | `coverage_audit.py` on seeded a. | 0 ABSENT and 0 UNPROBED among supported rows; what cannot be seeded is listed with its reason |
| 2 | Every known error handled | campaign group `errors` | each `known_issues` id happens on purpose and ends as its entry says; 0 `unclassified` |
| 3 | Every failure recovered | campaign group `failures` | after the failure and a restart, counts and verdict equal the control's; 0 duplicates |
| 4 | Every benchmark can pass and can fail | campaign group `benchmarks` | each check in `benchmarks.py` reads PASS on the control and FAIL or UNVERIFIED when its fault is caused |
| 5 | Every watcher opens and closes | campaign group `watchers` | each `oversight.watch_*` check and each incident kind opens on its condition and closes on evidence |
| 6 | Parity at scale | `parity_gate.py --account-id ID` after the 300-user run | PARITY; the remainder holds accepted kinds only |
| 7 | Repeatable | three wipe-and-migrate runs of the cohort, in a row | same verdict, same counts per service, no failing kinds |
| 8 | Recoverable at scale | three failures caused during the 300-user run | the run still ends at row 6's verdict |
| 9 | Fast | P2.10 | each phase is bound by a Google quota or by the box's line, shown by measurement; never by idle Bitport workers |
| 10 | Unattended | operator actions between Start and the final report | none beyond the confirmations Phase 0 lists |
| 11 | No open code items | Phase 6 | every box ticked, or dropped by the operator in writing |

Row 7 uses the cohort, not 300 users, because three 300-user runs may not fit the tenants'
subscription window. The operator can ask for more at P5.1.

### The clock

The subscriptions end on 16 Dec 2026 (the operator's word, 16 Nov: Business Starter, 30 days). All
tenant work stops on 14 Dec. These are the latest dates, planned backwards from that. P3.6 replaces
the Phase 5 dates with measured ones.

| By | What |
|---|---|
| 20 Nov | Phase 0 done. Each day it is late moves every date below by a day. |
| 23 Nov | Phase 1, and the code of Phase 2 (runner, failpoints, fixtures) |
| 28 Nov | Phase 3: the cohort runs |
| 1 Dec | Phase 4: the seeder, with a dry plan for N users |
| 2 Dec | P5.1: go or no-go |
| 12 Dec | Phase 5 done: seeding, the run, the DMS, verification, the delta |
| 14 Dec | Stop. Two days of slack before the subscriptions end. |

A Phase 6 item that needs a tenant and is not done by 14 Dec waits for a renewal, or the operator
drops it. If the operator renews, change the dates here.

## 3. Phase 0: tenants and account (OPERATOR, then the executor verifies)

- [x] P0.1 (16 Nov, the operator's word; not read from the console) Both tenants are Business
  Starter and end on 16 Dec 2026. See The clock. On 16 Nov the last pair lapsed mid-run and left a
  half-finished wipe.
- [x] P0.2 (16 Nov, the operator's word; P0.8 reads the real count) 300 licences on a. and on b.
  Business Starter allows 300 users in a tenant, the admin included. So N is 300 with the admin's
  own account as one of them when both admins share a localpart, and 299 otherwise. Every report
  says N.
- [ ] P0.3 OPERATOR: on both tenants, Drive allows external sharing and link sharing, the Drive SDK
  is on, and Cloud project creation is allowed.
- [x] P0.4 (16 Nov, written on the operator's word in chat: `info@` on both tenants, one password; the old s./t. file is kept as `dwd.env.s-t.bak`) OPERATOR: put both admin logins on the box in `/etc/bitport/dwd.env`: the same four
  variable names it holds now, with the values of a. (source) and b. (target) in place of s. and t.
  Never in chat.
- [ ] P0.5 OPERATOR, in the Bitport UI: create the account "a.sarafgloabalexim.com →
  b.sarafgloabalexim.com", run Setup for both sides, declare both domains sandboxes, and switch
  seeding on for the account (Admin, Accounts).
- [x] P0.6 (17 Nov, the operator's word) The outside address is `learn.aryanpaul@gmail.com`. OPERATOR: one outside Google account you own, for external-share tests. The address only.
- [x] P0.7 (16 Nov) The operator said yes to all three:
  - (a) the campaign may reset or wipe b., and reset seeded data on a., without a typed confirmation
    each time (never any other domain);
  - (b) network tests: a firewall rule on the box blocks outgoing HTTPS for 3 and for 12 minutes and
    removes itself by a timer set before the rule;
  - (c) one reboot of the box in the middle of a run.
- [ ] P0.8 Executor, read-only: `readiness.py --account-id ID --side source --purpose migrate`,
  `--side source --purpose seed` and `--side target --purpose migrate` read Ready; Setup's capacity
  card passes; the licence counts are read (`seed_sandbox.seat_report`). Write the Bitport account
  id, both licence counts and both storage figures in State.
- [ ] P0.9 Smoke run, as soon as P0.3 to P0.8 are done, with the tools as they are: seed five users
  (`--scale tiny`), start a full run from the UI (all services, split mail, DMS after, server-side),
  let the follow-ons finish, read the parity gate. Its purpose is to find what a fresh Business
  Starter pair refuses while the code of Phase 2 is still being written: a staging shared drive on
  b., grants across the two tenants, the DMS approval, the sharing policy. Write each refusal in
  State and fix or route around it before Phase 3.

Open on 16 Nov, 16:40 UTC, read on the box: no Bitport account for a. and b., and `dwd.env`
unchanged since 31 Oct. P0.3 to P0.6 are the operator's, and they are on the critical path.

Phase 1 and the code of Phase 2 need no tenant. Start them while Phase 0 is open.

## 4. Phase 1: before any test run (code)

- [x] P1.0 (16 Nov) Retire account 3 from the watchers. Done: `bitport-autopilot-5fa` was a
  transient unit, so stopping it removed it (nothing to disable, no timer restarts it); the two
  failed `*-resume` units were cleared. #77 and #78 (tenant not ready) are NOT resolved by hand: the
  oversight check re-finds them every sweep while account 3 had a run event in the last 48 hours
  (its last was 16 Nov 06:00 UTC), so a manual resolve would only reopen them. They close on their
  own at the first sweep after 18 Nov 06:00 UTC; check then. Incidents #74, #70, #9 and #8 (older
  account 3 runs) stay open as history. The ledger is kept (rule 10).
- [x] P1.1 (18 Nov: full `tests/` green, exit 0, 7 skips, all "macOS OS-isolation"; `data-generator/` green) Green baseline. Two tests failed, both older than this plan. Fixed 16 Nov:
  - `test_interrupted_mail_is_recovered_inside_original_job` failed 4 runs in 5, not always: the
    verifier (`verify_sample.Verifier._failed`) counts a later copy as recovering a service-level
    FAILED row only when its timestamp is a later second, and the repair inside the test finishes in
    the same second. A tie kept the user FAILED. Now a row written after the failure row also counts
    (`id>`); a copy written before it, in the same second, still does not. Tests added in
    `tests/test_failed_items_since_copied.py`. 5 runs in 5 now pass.
  - `test_orphaned_verification_is_interrupted_only_when_disabled_and_owner_dead` claimed a repair
    while the policy was paused, which the operator's pause (11 Nov) correctly refuses. The test now
    enables, claims, then pauses.
  Both files and the new tests are in `tests/gate.txt`; the gate passed. Still open, so the box stays
  unticked: the full `tests/` and `data-generator/` runs, and the skip count in State.
- [x] P1.2 (16 Nov) Box scripts work for any account. `dms_delta_loop.py` and `dms_user_verify.py`
  take `--account-id` (and the delta loop `--identities`); their paths, ledger, logs and lock follow
  it; the install path is the file's own folder, not `/opt/bitport`. Units
  `systemd/bitport-autopilot@.service` and `systemd/bitport-parity@.service` (the instance is the
  account id; deploy installs them, enables none). `tests/test_account_scripts.py`, in the gate.
  The delta loop still waits on the unit `bitport-dms-fresh`, a name shared by all accounts.
- [x] P1.3 (16 Nov) `coverage_audit.py` takes `--account-id`. It was also launched by the API
  (`/api/v2/coverage/start`) with no account, so it read the legacy ledger whoever pressed the
  button; the API now passes the caller's account. Tested both ways (the API test fails without it).
  `sharing_report.py` is not a command: its numbers are read on the Sharing page.
- [x] P1.4 (16 Nov) Wipe safety, from the 16 Nov half-wipe. Done in `wipe_tenant.py`, `oversight.py`,
  `webui.py`, `api_server.py`:
  - the wipe refuses (exit 2, "NOT WIPED -- refused: nothing was deleted") unless readiness reads Ready
    for that side, every scope is granted, and a real read per service (Drive, Gmail, Calendar,
    Contacts, Tasks) succeeds as the admin (Drive) and as every user. Chat has no read under its
    delete-only scope and Groups is one admin call, so both stay token checks. `--allow-partial`
    wipes what answers;
  - the summary says per service how many users were emptied and how many items, and the ledger line
    says what it forgot ("Ledger reset: forgot drive for 44 of 45 user(s) ...");
  - seed, reset target, wipe source, wipe target, remove setup (wipe, remove, delete users) and "Run
    the DMS again" answer 409 while a `watch_tenant_ready` incident is open for that side
    (`oversight.tenant_not_ready`). An unreadable incident list lets the action through: the wipe's
    own preflight is the hard guard.
  Tests: `tests/test_wipe_preflight.py`, `tests/test_destructive_launchers_wait_for_a_ready_tenant.py`.
- [x] P1.5 (16 Nov) Preflight before Start: `start_checks.py` (not preflight.py: that name is the older source scan, sniff.py), called by `POST /api/v2/migrate/start`
  after the capacity check (a real run only). It answers 409 "Not started: <reason>" for: no route to
  Google, the disk 90% full, a pending reboot or running upgrade, the DMS login (the file must exist,
  name no other pair's domain, and hold a target admin password; read by names and domains, never by
  value), and either tenant not ready (readiness). A check nobody could make is logged and does not
  refuse. `BITPORT_PREFLIGHT=0` switches it off (the test suite does). Not covered: Google's quota
  (the capacity check beside it asks storage only) and the DMS's own authorisation on the source.
  `tests/test_start_checks.py`.
- [x] P1.6 (16 Nov) The seeder is repeatable. `corpus.stable_seed(user, salt)` (crc32 of the campaign
  seed, the content key and the salt) replaces every `hash(user)`; `--seed` and `--content-key`
  (env `SEED_CAMPAIGN`, `SEED_CONTENT_KEY`, the channel the seeder already uses for opt-ins). Users
  with one content key get the same mail; the default key is the user's own address, so users differ
  and a rerun repeats. Message-IDs stay uuid4 on purpose. `data-generator/test_seed_repeatable.py`
  runs the seed in four processes with different `PYTHONHASHSEED`, and fails on any `hash(` in the
  seeder.
- [~] P1.7 Mail under 5 MB goes up as raw bytes. Today it goes up as base64 text inside JSON, 33%
  bigger, on a line whose upload is the limit (0.9 MB/s).
  - [x] (16 Nov) Seeder: `seed_gmail` and `seed_drafts` upload `message/rfc822` bytes (the labels
    alone in the JSON body). Test in `test_seed_repeatable.py`. Not yet measured live: measure bytes
    on `wlp2s0` for 1,000 seeded messages before and after, in P0.9.
  - [ ] **[opus]** Engine: `gmail_engine._deliver`, which already uploads raw bytes above 5 MB. Measure
    upload bytes per message before and after on one mailbox (`/proc/net/dev`); expect about a
    quarter fewer.

## 5. Phase 2: the campaign

### The runner

- [~] P2.1 `campaign.py` and `campaign_scenarios.py` at the repo root, with `tests/test_campaign.py`.
  Done 16 Nov: the scenario registry (160 scenarios: 55 errors of `known_issues`, 21 failures, 42
  benchmark outcomes, 42 watcher opens and closes), the runner (`run_scenario`, `judge`, results in
  `logs/campaigns/<name>/results.jsonl`, `RESULTS.md`, `--dry-run`, `--redo`, `--only`, `--group`,
  `--allow net,reboot`), tested with a fake driver. The rules it keeps: a scenario with no `act` is
  UNVERIFIED, never a pass; a fact the driver cannot read is UNVERIFIED; a permission the operator
  did not give is SKIPPED; an unseedable one is COVERED only when its unit test is named.
  Still open, needs a tenant: **`LiveDriver`** (the box's `Driver`: sign in with
  `accounts_auth.create_session`, send the request, `job_admission` for idle, `parity_gate.py --json`,
  `run_watch.list_incidents`, ledger counts per twin), and the `act`, `inject` and `expect` of each
  scenario, refined when its group first runs (P3.4). `python campaign.py` without `--dry-run` exits
  2 until the live driver exists.
  18 Nov (Opus): `LiveDriver` written (session as the superadmin, requests to the API on 127.0.0.1:8090,
  twins t02-t20 handed out once each in `logs/campaigns/<name>/twins.json`, idle = no job of the account for two
  polls, facts from the ledger's findings and `classify_failure`, the latest report's benchmarks, incidents
  opened/closed since the scenario began, and `user_tally` against t01). A failpoint is armed by
  `logs/failpoint` (a launched run takes no env from its caller) and disarmed after each scenario. A cause
  the driver cannot make yet raises `Unautomated` -> UNVERIFIED. Not yet run live; each group's `act` and
  restart step are still P3.4's.
- [x] P2.2 (16 Nov) Completeness tests in `tests/test_campaign.py`, in the gate: every id of
  `known_issues.ISSUES` and `RUN_KINDS` has a scenario (and none is invented); every benchmark of
  `benchmarks.DEFAULTS` and `SEED_DEFAULTS` has a pass and a fail scenario and a named fault (read:
  13 migration + 7 seed, not 14 + 7); every `oversight.CHECKS` entry and incident kind opens and
  closes; the 21 failures exist; a kind that cannot be caused names an existing unit test. Left for
  P2.3: every failpoint name has a scenario.
- [x] P2.3 (18 Nov, Opus; code only, not yet caused live) `failpoints.hit(name, value)` returns the call's reply and
  sits INSIDE the retried lambda, so `:lost` (ConnectionResetError) is retried exactly as a dropped reply would be
  and the write is made twice -- what F-lost tests the engine's adoption against. Placed: copy (server-side and both
  upload paths), `db.record_mapping` (file/folder/shortcut -> drive.mapped, message -> gmail.mapped), ACL (single
  and batch), comment create, move hop 1, native link rewrite, gmail insert, calendar insert/import, contact,
  task, shared drive create, after each pass (main), after main returns (`job_exit.run`), and the API's
  `_follow_on` for repair and dms (kills the API: set BITPORT_FAULT on bitport-api only for that scenario).
  `campaign_scenarios._failpoints`: `fp:<name>:kill` for all 16, `fp:<name>:lost` for the 10 Google writes;
  `tests/test_failpoints.py` and `test_campaign` (every name has a scenario and is placed). Original spec:
  **[opus]** for where the calls go; Sonnet for the rest. `failpoints.py`: `hit(name)` does
  nothing unless `BITPORT_FAULT` is set and the run's target domain is a declared sandbox. With
  `BITPORT_FAULT=name@n` the process exits with code 137 at the n-th hit (`kill`). With
  `name@n:lost` the call has already been made and the code then raises a connection error, as if
  the reply was lost (`lost`). Names, each placed right after the call it names:
  `drive.copy.landed`, `drive.mapped`, `drive.acl.granted`, `drive.comment.created`,
  `drive.moved_out`, `drive.native_links`, `gmail.inserted`, `gmail.mapped`, `calendar.imported`,
  `contacts.created`, `tasks.created`, `shareddrive.created`, `run.pass_done`, `run.exit`,
  `followon.repair.start`, `followon.dms.start`.

### The cohort: 30 users on a.

- [ ] P2.4 Seed plan for the cohort, built on the seeder's existing shapes (`corpus.py`,
  `seed_sandbox.py`, `seed_shared_drives.py`, `seed_external_shares.py`). The seeder gives every
  user the same tree today and puts its edge cases on the first user. Add
  `data-generator/fixtures.py` (a registry: a fixture's name to the function that seeds it) and
  `seed_sandbox.py --cohort FILE` (JSON: each user's fixture names and content key). A scenario
  names the fixtures it needs, and a test fails when a named fixture does not exist.

| User | Holds |
|---|---|
| `c01` | Drive shapes: the corpus edge cases (deep tree, 250 files in one folder, hard names, a 12 MB Doc, a zero-byte file, a 6 MB file) |
| `c02` | Sharing: every grant shape, groups, the outside address, anyone-with-link, inherited grants, a share to a user outside the run |
| `c03` | Docs, Sheets and Slides with links to each other, images and comments; Forms with a responses Sheet; Apps Script; shortcuts |
| `c04` | Mail: labels, threads, read states, sizes on both sides of 5 MB, no Message-ID, a Message-ID past 8 KB of headers, non-ASCII headers, one Message-ID twice, an encrypted body, each kind of Drive-link mail |
| `c05` | Mail settings and drafts: filters, forwarding, delegates, signatures, vacation, IMAP and POP, language |
| `c06` | Calendar: recurrence with a moved instance, out-of-office, focus time, working location, Meet links, secondary calendars, calendar sharing, guests, Asia/Kathmandu and other time zones, all-day events |
| `c07` | Contacts with photos and groups; tasks with lists, subtasks and an empty title |
| `c08` | Three shared drives with every member role; one whose only Manager is outside the run |
| `c09` | What only a person can move: a Site, a My Map, an EICAR test file (Google flags it as malware), a Form with an image only the source can fetch |
| `c10` | Size in miniature: 5,000 small messages and 3,000 files, for paging and pacing |
| `t01`–`t20` | Twins: one mid-size user (about 150 files, 300 messages, 20 events, contacts, tasks), every service, same content key |

  OPERATOR, once: what no API can create is made by hand in `c09`'s account (a Site, a My Map, a
  Google Vids file). The seeder then only checks that they exist. If Google does not flag the EICAR
  file, `run.malware_flag` goes to `UNSEEDABLE`.

### Shapes (row 1)

- [x] P2.5 (18 Nov, Opus: audit on seeded a. reads 57 covered, 0 absent, 0 unprobed; Meet recordings UNSEEDABLE) `scope.py` has 59 supported rows (34 FULL, 25 PARTIAL) and `coverage_audit.PROBES` has
  24. For each supported row without a probe: add a probe that counts it from the inventory, and a
  seeded instance in the cohort. Done when the audit on seeded a. reads 0 ABSENT and 0 UNPROBED, or
  the row is in `UNSEEDABLE` with its reason (for example Jamboard, which Google has retired).

### Errors (row 2)

- [ ] P2.6 One scenario per id. Read the id's cause and fix in `known_issues.py` first. A scenario
  passes when the finding is classified with that id, ends as its entry says (automatic: a RESOLVED
  row in `issue_fix_log`; accepted: in the gate's remainder; a person's: listed with the next step),
  and the gate treats it that way. How to make each one happen:
  - **Seeded, happens in the run:** `gmail.labels_added`, `gmail.duplicate`, `gmail.no_message_id`,
    `gmail.body_link_copy`, `gmail.link_owner_not_migrated`, `gmail.link_scan_encrypted`,
    `run.mail_owed_to_dms`, `run.held_for_drive`, `drive.shortcut_target`, `drive.item_absent`,
    `drive.group_grant`, `drive.embedded_images`, `drive.form_relinked`,
    `drive.forms_response_sheet`, `run.form_rebuild`, `drive.comments`, `run.comment_differs`,
    `run.comment_missing`, `drive.comments_unreadable`, `drive.sheet_too_large`, `tasks.untitled`,
    `run.malware_flag`, `drive.unmapped_source` (the outside account puts a file in a user's folder).
  - **Seeded, must not happen** (fixed defects; the scenario fails if the finding returns):
    `gmail.link_scan_fixed`, `drive.sheets_scope`, `drive.office_compared_as_native`,
    `drive.comment_quote_escaped`.
  - **Download-and-upload arm only:** `drive.native_structure`, `drive.content_export`.
  - **Changed on purpose after the run:** `gmail.labels_missing` (change a label on b.),
    `gmail.unmapped_source` (new mail on a.), `drive.mtime` (touch a file on b.),
    `drive.content_changed` (edit on a. after the copy), `drive.missing_on_target`,
    `drive.grant_missing`, `contacts.unmapped_source`, `contacts.missing_on_target`,
    `tasks.list_missing`, `drive.ab_test_residue` (after an A/B run).
  - **Made before the run on b.:** `drive.parent_folder` (two folders, same name and creation time).
  - **Through a failpoint:** `drive.duplicate` (`drive.copy.landed` lost),
    `run.comment_prior_attempt` (`drive.comment.created` lost).
  - **Through a failure scenario:** `run.disk_full`, `run.db_rollback` (see F-disk).
  - **Needs a protected source:** `drive.source_changed`. Seed first, then lift a.'s sandbox
    declaration for this one scenario (`/api/v2/domain-guard/restore`), edit a file during the run,
    and declare the sandbox again after.
  - **Google decides when:** `drive.read_refused`, `drive.unverified_transient`,
    `gmail.rate_limited`. Run a full verify beside a migration of the same mailbox, three tries. If
    it never happens live, the existing unit test covers it; say so in the result.
  - **Likely unseedable:** `run.resource_key` (needs a file shared by link before Google's 2021
    change). Try one link-shared file reached through a folder shared from the outside account;
    otherwise `UNSEEDABLE`.
  - **Group `chat`, opt-in, not scored:** `chat.attributed`, `chat.space_type`, `chat.members`,
    `chat.sender`, `chat.text`, `chat.not_migrated`.

### Failures (row 3)

- [ ] P2.7 One scenario per row. Each takes an unused twin, causes the failure, restarts the way the
  row says, and compares the twin with `t01`: equal counts per service on b., equal verdict, no item
  twice. One twin may take several kill points one after another; use a new twin for each new kind.

| Id | Failure | How | Pass |
|---|---|---|---|
| F-kill | The process dies at each failpoint | `BITPORT_FAULT=name@n` | the next run finishes; nothing twice |
| F-lost | A reply is lost after the write landed | `name@n:lost` on each write failpoint | the copy is adopted, not written again |
| F-kill-random | SIGKILL at 5 random moments | Jobs page, second press (`/api/v2/jobs/{pid}/stop` with `force`) | recorded as stopped by the operator; the next run finishes |
| F-stop | Stop, then Start | one press; and, on another twin, two presses | recorded STOPPED, not crashed; resumes |
| F-api | The API restarts | `systemctl restart bitport-api` in the Drive pass, the mail pass and a follow-on | the run and its follow-ons go on or are picked up; no second start of a phase |
| F-webui | The web UI restarts | `systemctl restart bitport-webui` during a seed, then during a migration | the migration survives; the seed, started again, adds nothing twice |
| F-reboot | The box reboots mid-run | P0.7(c) | the run resumes after boot |
| F-net-3 | No network for 3 minutes | P0.7(b) | the run rides it out; 0 failed items |
| F-net-12 | No network for 12 minutes | P0.7(b) | a clean stop with a named incident; the resume ends equal to the control |
| F-disk | The scratch disk fills | a download-and-upload run with its scratch folder on a 64 MB tmpfs; never fill the real disk | no file lost; `disk_full` opens and closes |
| F-memory | Memory runs short | the watchdog's own threshold set low (`tests/test_memory_pause.py` shows the setting) | exit code 3; the autopilot resumes with one process fewer |
| F-rate | Google pushes back | `per_user_qps: 40`, `mail_workers: 16` | the limiter backs off and saves the ceiling; 0 failed; the next run starts at the saved ceiling |
| F-scope | A scope is gone | remove one scope from a.'s delegation between two runs | the next launch grants it again, or switches that pass off and names it |
| F-suspend | A target account is suspended mid-run | Directory API on b. | that user fails with the reason named; the rest finish; after unsuspending, the rerun finishes |
| F-source-delete | A source file and a source message vanish mid-run | delete on a. | named with the reason; not left FAILED |
| F-source-edit | A source file changes after its copy | edit on a. | `drive.content_changed`; the delta brings it over |
| F-tamper | b. is changed after the run | delete a copy, a contact and a task list; remove a grant; add a grant; change a label; touch a file | each one named; repair restores what it can; the matching benchmark fails |
| F-double | Start pressed twice | two requests | the second is refused |
| F-deploy | A deploy during a run | `./sync_vps.sh` with a harmless change | it refuses and restarts nothing |
| F-dms-held | The DMS with one user FAILED | leave one twin failed | the DMS does not start; `dms_not_started` opens, then closes after the fix |
| F-licence | More users than b. has licences | only if N leaves none spare | the blocked items are named; otherwise the unit test covers it |

  F-net uses one script, `netdrop.sh SECONDS`: it first schedules the rule's removal
  (`systemd-run --on-active`), then adds the rule, and it never touches the Tailscale interface.

  Not caused live, because they need a credential or a billing change: a wrong admin password, a
  missing DMS approval, a tenant that is not ready. Each keeps its unit test and is named in
  `UNSEEDABLE`.

### Benchmarks and speed (rows 4 and 9)

- [ ] P2.8 (a) The truth table. For each check in `benchmarks.py` (14 for a migration, 7 for a
  seed): PASS on the control, and FAIL or UNVERIFIED when its fault is caused. Most faults are the
  failure scenarios above: `users_failed` by F-suspend, `count_parity` and `checksum_failures` by
  F-tamper, `quota_pushbacks` by F-rate, `run_completed` by F-kill. A benchmark that cannot be made
  to fail is a defect: fix the benchmark.
- [ ] P2.9 (b) The arms, through the Benchmarks page (`ab_transfer.py`), on twins: Drive
  `server_side`, `download_upload` and `adaptive`; mail `split` with the DMS, `engine`, `dms`; link
  scan `search` and `exhaustive`; processes 1 and automatic; DMS proof light and full. `move` must
  be refused with "MOVE REFUSED" on this edition, and that refusal is its pass. Errors rank before
  speed. `/api/v2/benchmark/start` (`benchmark_run.py`) wipes the target tenant: use it only under
  P0.7(a).
- [ ] P2.10 (c) What binds each phase. Record for the Drive pass, the mail pass, the DMS import, the
  DMS check, verify, tally and seeding: items per minute, the limiter's rate, ceiling and
  rejections, CPU, bytes per second on `wlp2s0`, and the share of idle workers. Name the binding
  limit: a Google quota, the line, the CPU, or nothing. "Nothing" (idle workers, no pushback, a free
  line) is a Bitport defect: fix it. Baseline, 13 to 15 Nov, 45 users: Drive 38,513 files in
  1 h 43 min; mail pass 34 min; DMS import about 23 h; DMS check 830 messages a minute.

### Watchers (row 5)

- [ ] P2.11 For each `oversight.watch_*` check (12) and each incident kind in `run_watch.py`: one
  scenario that opens it and one that closes it. Most open inside a failure scenario; note which.
  The rest run the check against a crafted copy of the ledger (`BITPORT_LEDGER`). The teardown
  incident is not caused: teardown is the operator's.

## 6. Phase 3: cohort runs

- [ ] P3.1 Seed the cohort through the product's seed path. All seven seed benchmarks pass. Then
  map identities (Identities page, auto-map), so every seeded user has its address on b.
- [x] P3.2 (18 Nov, ~06:10 UTC: 57 covered, 0 absent, 0 unprobed, 21 not migrated, 1 unseedable; `logs/coverage-5.txt` on the box) `coverage_audit.py`: row 1.
- [x] P3.3 Control run from the UI: all services, split mail, DMS after, server-side, ordered. Let
  every follow-on finish. The gate must read PARITY. Fix until it does (rule 4).
- [ ] P3.4 First one scenario of each group (`--only`), to prove the runner itself. Then the
  `campaign.py` groups in this order: `errors`, `failures`, `benchmarks`, `watchers`,
  `chat`. A failed scenario is a bug: fix, test, deploy, `--redo` that scenario. After each group
  run `ui_check.py --account-id ID` once, so the UI's numbers are compared with the ledger.
- [ ] P3.5 Three wipe-and-migrate runs of the cohort in a row: row 7. Record phase times, verdict
  and counts for each.
- [ ] P3.6 From the measured rates, estimate the 300-user run: seeding hours, Drive hours, DMS days,
  verify hours. Write the numbers in State. They are the inputs of P4.3 and P5.1.

## 7. Phase 4: the seeder simulates real user data

The real data is gone, but its shape survives in account 3's ledger on the box (45 users copied
from a real organisation). Read on 16 Nov:

| Per user | p50 | p90 | max | total |
|---|---|---|---|---|
| Files | 364 | 2,189 | 5,657 | 38,513 |
| Folders | 9 | 158 | 691 | 2,388 |
| Google Docs, Sheets, Slides | 19 | 64 | 72 | 1,131 |
| Shortcuts | 0 | 2 | 7 | 31 |
| Folder depth | 2 | 5 | 10 | |
| Drive size | 1.9 GB | 13.4 GB | 18.4 GB | 179.5 GB |
| Largest file | 253 MB | 2.9 GB | 10.1 GB | |
| Messages | 1,275 | 5,949 | 40,191 | 141,434 |
| Threads | 1,147 | 5,235 | 28,033 | 114,639 |
| Own labels | 0 | 2 | 23 | 44 |

Files by type: .docx 31.1%, JPEG 23.5%, PDF 12.7%, folders 5.8%, .doc 5.7%, .xlsx 4.8%, TTF fonts
3.5%, unknown binary 3.2%, Google Docs 1.4%, Google Sheets 1.2%, BMP 0.9%, MP3 0.7%, HEIF 0.5%,
.pptx 0.5%. Mail: 245 GB in 141,416 messages (1.7 MB each on average); 4,991 messages (3.5%) carry
a Drive link.

- [x] P4.1 (19 Nov; `profiles/org45.json`: 45 users, files p50 364 / p90 2,189 / max 5,657, messages 141,434, link mail 4,991 at 2.1 files each; calendar/contacts/tasks per user are not in account 3's ledger shape and stay at the seeder's own defaults) `data-generator/profile_extract.py`, run on the box, read-only on account 3's ledger
  (tables `discovery`, `migration_source_manifest`, `audit_log`, `mail_link_messages`,
  `label_map`). It writes `data-generator/profiles/org45.json`: sorted per-user vectors and
  histograms. Numbers and file types only. No name, address, subject, file name or id leaves the
  box; a test asserts that. Check its output against the table above.
- [ ] P4.2 `seed_sandbox.py --profile PATH --users N --seed S`:
  - each user's totals come from the per-user vectors, interpolated by rank, so the tail survives:
    one mailbox near 40,000 messages, one Drive near 5,600 files, several users with almost nothing;
  - file types follow the histogram; names come from a name model with Latin and Devanagari names,
    long names, two files of one name in one folder, and characters that break paths;
  - folder depth and width follow the profile; creation and modified times spread over years (set
    when the file is created); mail carries old Date headers;
  - mail: threads average 1.23 messages with a long tail; most users have no label of their own and
    a few have many; 3.5% of messages carry a Drive link (to the user's own file, a colleague's
    file, a shared-drive file, a folder, a deleted file, a file of a user outside the run);
  - sharing: departments as groups, many similar light users, one shared mailbox, shared drives;
    outside sharing only to the P0.6 address;
  - calendars, contacts, tasks and mail settings at plausible rates;
  - the cohort keeps its fixtures; the profile content is added beside them.
- [ ] P4.3 A budget computed from inputs, never from constants. Before writing anything the seeder
  prints users, items, bytes that cross the box and hours, from a 60-second upload probe and the API
  rates measured in P3.6. It refuses when the hours exceed `--max-hours`. How the bytes fit:
  - Drive: the seeder uploads everything today and has no server-side copy. Add one. Upload a pool
    of unique files once (`--pool-gb`, default 5), then make volume with `files.copy` inside a., so
    no further byte crosses the box. No file is larger than the largest pool file; State records
    that gap against the profile's 10.1 GB;
  - mail: no server-side copy exists. Keep the message counts of the profile and shrink the sizes
    so the total fits the budget. At 0.9 MB/s, 12 hours carry about 38 GB, which is about 40 KB a
    message for 943,000 messages. If the DMS estimate of P3.6 does not fit the window either, scale
    the counts by one factor and write the factor in State. Early warning: at account 3's DMS
    import rate (141,977 messages in about 23 h), 943,000 messages take about six days, more than
    Phase 5's ten days can carry beside seeding and verification. Expect a factor below 1 unless
    the cohort shows the DMS is faster with more users.
- [ ] P4.4 A killed seed, started again, adds nothing twice. All seven seed benchmarks pass.
- [ ] P4.5 Tests in `data-generator/`: same seed, same plan; the p50 and p90 of files, folders and
  messages per user within 15% of the profile; file-type shares within 3 points; the budget
  refusal; the profile file holds numbers only; every cohort shape still present.

Done when a dry plan for N users passes P4.5 and its estimate fits the window of P0.1.

## 8. Phase 5: 300 users

- [ ] P5.1 OPERATOR: read the estimate (seeding hours, Drive hours, DMS days, verify hours, the end
  date of P0.1) and say go, or change N or the mail factor.
- [ ] P5.2 b. is emptied (the operator's press, or P0.7(a)) and is empty by counts.
- [ ] P5.3 Seed a. up to N users with the profile (`--create-users`), added to the cohort. Run
  `coverage_audit.py` again.
- [ ] P5.4 Start the full run from the UI by click: all services, split mail, DMS after,
  server-side, ordered. The autopilot and the parity loop run for this account.
- [ ] P5.5 Row 8: during the run cause a SIGKILL in the Drive pass, an API restart in the mail pass,
  and a 3-minute network drop during the DMS check (if P0.7(b)).
- [ ] P5.6 After the follow-ons: the parity gate, the tally, `fidelity_audit.py`,
  `form_deep_check.py`, `mail_dead_links.py`, `sharing_report.py` and `ui_check.py`. Every
  difference is explained or fixed (rule 4), then verified again. Row 6.
- [ ] P5.7 A delta. Change a. with a seeder mode `--mutate` (new mail, edited files, new shares,
  deleted items; `rehearsal.py`'s delta phase is the model). Run Delta from the UI, verify, gate.
- [ ] P5.8 The final report and section 10's scorecard, filled with measured numbers. Update
  CLAUDE.md's state. Approval is the operator's click.

## 9. Phase 6: the open code items

Carried from parity-10-plan.md. Do each when the campaign reaches the scenario that proves it, with a
measurement before and after.

- [ ] One run record that lists its phases for the UI; a restart resumes from the recorded phase.
  Proven by F-api and F-reboot.
- [ ] **[opus]** Bulk DMS check: list the target once with Message-ID headers, match in memory, read
  content only for link mail and mismatches. Proven by P2.10.
- [ ] **[opus]** One target write per Drive file: comments, then grants, while the file is in the
  staging drive, then the move. A/B on twins first.
- [ ] The mail verify reuses the bulk index for its extras check.
- [ ] End-of-pass retry by class: transient (retry), quota (wait for the reset), explained
  (accepted), unexplained (hold that user, go on, open an incident). Two rounds without progress
  stop and report.
- [ ] **[opus]** The DMS starts per user as that user's mail pass ends clean.
- [ ] The UI refuses a full verify while the DMS imports.
- [ ] One log file per run; DMS console readings appended to a history, never overwritten.
- [ ] A nightly cohort run with a morning report: the campaign on a systemd timer, one line in
  `monitor/INBOX.md`. Allowed by P0.7(a).
- [ ] Each known difference fixed or accepted. Open: why the DMS imported its own copy of 4 engine
  link mails (`gmail.duplicate`).
- [ ] `api_server.py` and `webui.py` split by concern. Last. Moves only, no behaviour change, the
  full suite green before and after.
- [ ] Gmail search-first link rewriting ([claude-gmail-search-first-dms.md](claude-gmail-search-first-dms.md)):
  only if P2.10 shows the link scan binds the mail pass.

OPERATOR, security. The score cannot reach 10 without these: rotate the SSH and sudo password, the
Bitport admin password and the tenant admin passwords; switch off SSH password login; remove the
old keys from root's `authorized_keys`; decide on rewriting the git history that holds a password.

## 10. Scorecard

| Area | 13 Nov | What lifts it to 10 |
|---|---|---|
| Drive | 8 | rows 1, 3 and 6; one write per file, measured |
| Mail | 5 | rows 2 and 6; the bulk DMS check; the DMS per user; the duplicates explained |
| Verification | 6 | rows 4 and 6; the index reused; nothing UNVERIFIED |
| Orchestration | 4 | the run record; box scripts for any account; row 10 |
| Operations | 4 | the preflight; one log per run; the nightly run; wipe safety |
| Security | 3 | the operator's items above; rule 6 kept |
| Tests | 6 | a green baseline; the campaign and its completeness tests; failpoints |

## 11. State (newest first)

- 20 Nov 2026 (box 19 Nov ~21:00 UTC) (Sonnet): **P3.5 run 1: the reset-leftover mail failures are fixed (0 FAILED rows), the DMS is running fresh.**
  Five product defects found by run 1 and fixed with tests: (1) `reset_target` listed Chat with Chat off (40 x 11 retries);
  (2) a reset moves migrated mail to Trash and the engine then stopped on the previous run's own REWRITTEN copy (1,431 blocked
  messages): a trashed copy with the same identity headers (message-id, from, to, cc, subject, date; NOT content-type or
  transfer-encoding, which a rewrite re-encodes) is the previous run's, a fresh copy is inserted beside it; (3) two trashed
  leftovers read "ambiguous": Gmail tokens a FULL page of two, so the ambiguity check now re-lists with room (ten) and counts only
  LIVE copies; the test fake now does what Gmail does; (4) the DMS after a reset stopped at "stale-map": `_dms_needs_fresh`
  (a finished `reset target` newer than the last DMS that began cleanly) makes the follow-on pass `--fresh-import`; (5) the dispatcher's own
  verify batches held the one job slot for an hour ahead of the retry (speed item a: stop re-checking users whose findings a queued
  job is about to change; `logs/deploy_pending` is the stop-gap). `known_issues.RUN_KINDS` names `gmail.reset_leftover`. Retry =
  `POST /api/v2/migrate/start {services:[gmail], mail_mode:split}` (takes only users whose Gmail is not done: 4 minutes for ~40).
  Run 1 timeline (box clock): reset 16:28-18:18 (c10's 5,000 messages at 46 a minute); migrate start 18:19; Drive pass and Gmail pass done 18:51.
  **Next: let the DMS pipeline finish (import, deltas to zero, labels, verify-after-dms, parity gate); read `parity-gate-latest.json`,
  record the phase times from run_events, then run 2 and 3 with `/tmp/p35_run_nochat.sh N`.**
- 20 Nov 2026 (box 19 Nov ~21:30 UTC) (Sonnet): **P3.5 run 1 hit its first real error; fixed at the cause.** After the reset and ledger reset the
  migrate pass ended with 1,480 failed rows: 1,431 `FAILED_SPLIT` "Existing destination Message-ID has different content" and
  6 "Ambiguous destination Message-ID" (alice). Cause: `reset_target` moves migrated mail to Trash; on the next run the engine found the
  previous run's own REWRITTEN copy there, judged it different from the source, and its Trash exemption only accepted an old
  UNREWRITTEN copy, so every link-mail message of every user blocked (and the DMS held 39 of 41 users: "mail items failed and are not on the
  target"). Fix (`gmail_engine`): a trashed copy with the same headers is the previous run's copy: insert a fresh one beside it, leave
  the old one in Trash; and Trash leftovers are not an "ambiguous" destination (only >1 LIVE copy is). Tests in
  `test_split_mail_duplicates.py` (one fails without the fix). Then the account's repair retries them (`POST /api/v2/repair/5`).
  Also open: the DMS follow-on read "stale-map" (rc 3): the console still holds the previous run's user map, so the DMS after a
  wipe needs `--fresh-import` (the pipeline's `fresh`), and the monitor pattern for incidents is `INCIDENT`, not `OPENED`.
- 19 Nov 2026, ~17:35 UTC (Sonnet): **P3.5 run 1 under way (reset phase); P4.1 done.** Run 1 = `/tmp/p35_run.sh 1` on the box: reset target
  (all six services), reset the ledger, `POST /api/v2/migrate/start` (defaults: all services, split mail, DMS after). The reset took
  ~45 min for 40 users and ~1 h for the last: (a) it listed Chat spaces for every user and retried a 403 eleven times each (40
  failure lines; fixed: `reset_target.default_services()` leaves Chat out unless MIGRATE_CHAT); (b) c10's 5,000 messages go at
  ~46 a minute (one delete each: a `batchDelete` of 1,000 ids is the speed fix, and it is what a 300-user reset needs). Runs 2 and 3:
  `/tmp/p35_run_nochat.sh N`. Record phase times for P3.6 from `run_events` / the PASS markers.
- 19 Nov 2026, ~17:30 UTC (Sonnet): **All nine `changed_after` scenarios PASS live; the last one found a fifth defect.**
  The label repair held EVERY message whose links had been rewritten: `repair_verified_gmail_labels` passed decoded BYTES to
  `link_rewrite.rewrite_raw`, which takes the base64 text and returned them unchanged, so the repair's own "fresh content" guard
  always read the rewritten message as different (the verifier never noticed: it trusts the engine's receipt). Fixed (base64 in,
  and the target's own genuine copy of a shared file counts, as in `verify_sample.copy_lookup`), with a test; `gmail.labels_missing`
  then PASSED (found, repaired, in the fix log). Errors group now: 28 PASS, 1 COVERED, rest UNVERIFIED (see the list in the entry
  above). Twins t02..t14/15 used; the failures group needs 21 and only the rest remain.
  **Next: P3.5, the three wipe-and-migrate runs (they give fresh twins, the phase times for P3.6 and the throughput benchmark), then the
  `failures` group (F-kill ... F-licence), then `watchers`.** Wiping b. needs no confirmation (operator, 16 Nov, a./b. only).
- 19 Nov 2026, ~16:00 UTC (Sonnet): **P2.6 `changed_after` kinds run live on twins; four product defects found by them and fixed.**
  `campaign_changes.py` (one change per kind: drive.mtime, missing_on_target, grant_missing, content_changed, gmail.labels_missing,
  gmail.unmapped_source, contacts.unmapped_source, contacts.missing_on_target, tasks.list_missing), then `driver.verify` (check AND repair,
  `services` filter) and the judge reads the fix log (`fixed`). First pass: 6 PASS (mtime, missing_on_target, gmail.unmapped_source, both
  contacts kinds, tasks.list_missing) and 3 FAIL, all of them real: (1) a finding with two causes was named after the wrong one
  (`drive.mtime` listed before `drive.content_changed` and `drive.grant_missing`: now after them); (2) the label repair held every message
  whose target copy has LF where the source has CRLF ("Fresh message content differs"; `verify_sample.normalise_message` now treats the
  line endings of a TEXT part as equal, binary stays exact); (3) an edit on the source is fixed by a delta, never by a repair, so the
  scenario runs verify, delta, verify (`campaign.FIXED_BY_DELTA`). Still open for this group: `drive.ab_test_residue` (after an A/B arm),
  `drive.parent_folder` (made on b. first), `drive.duplicate` and `run.comment_prior_attempt` (failpoints), `run.disk_full`,
  `run.db_rollback` (F-disk), `drive.source_changed` (protected source), the three Google-decided kinds, the two `arm` kinds,
  held_for_drive, item_absent, comment_differs/missing/unreadable, unmapped_source, and Chat (opt-in).
  Twins used so far: t02..t13; seven left (t14..t20) for the failures group, which needs 21: **wipe-and-migrate the cohort (P3.5) before it.**
- 19 Nov 2026, ~12:00 UTC (Sonnet): **P2.6 errors group: source probes and Drive fixtures.** `campaign_evidence.py` proves a seeded case from the
  source (`source_has`); `campaign.READS_THE_RUN` scenarios PASS when the finding is classified OR the case is on the source and
  every service of the user reads IDENTICAL, and a must-not-happen PASSES only when the case was made (`needs_source`).
  Probes: tasks.untitled, drive.comments, drive.shortcut_target, run.malware_flag, drive.comment_quote_escaped, gmail.no_message_id,
  gmail.body_link_copy, gmail.link_owner_not_migrated, drive.sheet_too_large, drive.embedded_images, run.form_rebuild, drive.group_grant.
  New fixtures (`data-generator/fixtures.py`, cohort file): `drive_comments`, `drive_shortcuts`, `big_sheet` (20,000 x 120),
  `doc_with_image`, `forms_basic`; seeded on c03/c09 through `/api/seed` (webui, port 8080) with `fixtures_only`, then a delta and a verify.
  Lesson: a Form's Drive modifiedTime moves a few seconds after its last write (the Forms API is async), so a copy made at once keeps
  the earlier time (c09 `drive.mtime`): a delta after it settles reads the final time. Still UNVERIFIED: forms_response_sheet and
  form_relinked (no API links a Form to its responses Sheet: a person), held_for_drive, item_absent, comment_differs/missing/unreadable,
  unmapped_source (needs the outside account to put a file in a folder), then the `changed_after` kinds (need an `act`).
- 19 Nov 2026, ~11:00 UTC (Sonnet): **P2.6/P3.4 `errors` group read on the control run: 8 PASS, 1 COVERED, 40 UNVERIFIED, 0 FAIL.**
  The seeded scenarios are reads of the fixture user (`campaign.READS_THE_RUN`: `check_only`, `seeded`, `must_not_happen`; no twin
  spent, `facts(user, since="")`). Raising a finding is not the same as coping with the case, so the judge has three outcomes:
  PASS = finding classified, OR (`campaign_scenarios.EVIDENCE`: the ledger row that shows the case was met) and the user reads
  IDENTICAL on every service; FAIL = met but not clean, or a finding of that kind never classified where no evidence is
  expected; UNVERIFIED = no finding and no evidence defined (never a pass). Passing now: `gmail.labels_added`, `gmail.duplicate`
  (c04: 2 `SUCCESS_SOURCE_DUPLICATE`), `gmail.link_scan_encrypted`, `run.mail_owed_to_dms`, and the four `must_not_happen`. I removed a false
  PASS: `run.malware_flag` had been credited on c09's My Map row, which says nothing about the EICAR file.
  **Next:** one EVIDENCE entry per remaining seeded kind (read what the ledger or the source shows for c02/c03/c04/c07/c09: shortcuts,
  comments, the Forms, the huge Sheet, the untitled task, the group grant, the outside-account file; start with `tasks.untitled`,
  `drive.shortcut_target`, `drive.comments`), then the `changed_after` kinds (they need an `act`: change on a./b., then verify), then
  `failures` and `watchers`. `ui_check.py --account-id 5` is still owed once.
- 19 Nov 2026, ~09:00 UTC (Sonnet): **Gate PARITY 41/41 again, with the draft duplicates cleaned.** The Gmail-only re-verify of the 40
  users (new `services` field on `POST /api/v2/one-to-one/run`) read IDENTICAL for all, after the audit rows carried the MESSAGE id
  (the backfill pattern first missed because a draft id is `s:123...`; the new rows `split_mail.trash_dms_drafts` writes were right).
  Rule kept: a verify after a change that touched one service uses `services`, and `logs/deploy_pending` keeps the dispatcher from
  taking the job slot while it runs. The tally's mail surplus (175) still counts the trash, so it cannot show this fix.
  **Next, in order:** P3.4 groups need their `act` steps (P2.6 first: errors seeded in c01-c09 are in the control run already, so a
  seeded scenario is a read of that user's findings and fix log, `facts()` already reads both); then P3.5 (three wipe-and-migrate
  runs; they will also judge `items_per_min_per_worker`), P3.6. Open: run `ui_check.py --account-id 5` once after the benchmarks group.
- 19 Nov 2026, ~07:30 UTC (Sonnet): **P3.4: report fixed, draft duplicates fixed, 7 of 8 benchmark reads PASS; gate re-verify pending.**
  Report: rebuilt through `POST /api/v2/reports/generate` (13 pass, 1 unknown); `run_watch.REGRADE_AFTER` now rebuilds it after a
  `tally`, `user-tally` or `dms` job, and the migrate run is found past 50 later jobs (the lookup limit was 50, verify jobs filled it).
  `items_per_min_per_worker` stays UNKNOWN (not required): the latest migrate job is c09's 72 s one-user delta; the P3.5 full runs judge it.
  Drafts: `split_mail.trash_dms_drafts` (called by `reconcile_dms`) trashes the DMS's copy of a draft only when it equals an engine
  draft that has no twin yet; 157 trashed on the 41 users, audit item `dms_draft_duplicate`, text carries the MESSAGE id (the
  verifier's `bitport_trashed` matches message ids; the first version logged draft ids and every trashed copy read as an extra:
  20 users DIFFERENCES until fixed; the 157 audit rows were backfilled). The tally's mail surplus (175) is unchanged because the
  tally counts the trash; it is not a test of this fix. **Next: re-verify the users whose gmail verdict is not fresh IDENTICAL
  (not all 41: the Drive/other verdicts are still valid), then `parity_gate.py --account-id 5`; PARITY again = back to P3.4.**
  Lesson: a verify with `limit 0` over 41 users takes ~1 h per 20 users; verify only the stale users.
- 19 Nov 2026, ~05:30 UTC (Sonnet): **P3.4 started; two defects found by the first campaign reads.**
  (1) `campaign.py --group benchmarks` (read-only, on the control run): 25 `check_only`/benchmark scenarios, 9 PASS, 27 have no
  `act` (UNVERIFIED, the P2.6-P2.11 work), 6 FAIL = migration fidelity benchmarks reading UNKNOWN. A whole-tenant tally was run
  (pid 526871, 04:59Z: count parity 1.0, ACL 1.0, timestamps 1.0, 0 checksum failures, 0 extra grants, 0 missing), but the
  benchmarks are computed when the report is BUILT, and the only report is `migration-20261118T164116Z.json`, older than
  the tally. **Next: rebuild the report after the tally (find the entry point in `run_report.py`; the API builds it when a
  migrate job ends) then `campaign.py --redo --only` those six.** Also: the migrate follow-on order should run the tally BEFORE
  the report is built, or the benchmarks read UNKNOWN on every run (fix at its cause).
  (2) **Drafts arrive twice** (not seen by the gate: 41/41 PARITY still holds). The engine's draft pass (`gmail_engine._migrate_drafts`,
  every mode, mapped, compared by `verify_sample._drafts`) creates each draft, and the DMS also imports them (original date,
  CATEGORY_PERSONAL, ids unmapped; `split_mail.inventory` leaves drafts out of the DMS-owed rows, and `verify_sample`
  excludes draft message ids from strays, so nothing flags them). c01: source 4 drafts, target 8; the tally's mail surplus (175
  over 41 users, +4 or +6 per user) is these plus the target's own Google welcome pair (+2, expected). A draft has no
  Message-ID, so `reconcile_dms` cannot match it. **Fix to build: after the DMS, trash (never delete) a target draft that is
  unmapped and equals a mapped engine draft by internalDate-independent content; count each in `issue_fix_log`; make the
  verify name an unmapped draft as an extra; test; then re-run c01's check.** Rule from account 3: find out what an extra is.
- 19 Nov 2026, ~04:00 UTC (Sonnet): **P3.3 DONE: the gate reads PARITY, 41/41.** The operator accepted that a Vids video
  and a Site (c09: "Ward Welcome Video", "Ward Notice Board") are copied but cannot be compared: no API reads their content.
  `verification_coverage.NO_READ_API` keeps them out of the native count and names them in `drive.native`'s note, and
  never counts them as compared. Deployed to the box; a full c09 verify (limit 0) read IDENTICAL and the gate PARITY.
  Next: P3.4 (one scenario of each group, `--only`), P3.6 estimate, then speed items (a) dispatcher stops re-checking an
  unchanged fingerprint, (b)-(h) above. Operator rule for later types: "copied, listed by name, never counted as compared".

- 18 Nov 2026, ~17:00 UTC (Opus): **P3.3 nearly done: 40/41 at parity; c09 waits on a Drive delta.** After
  the DMS (12,091 emails, 0 failed, check 0 blocked, 2,505 labels aligned) the gate read UNVERIFIED, 32/41.
  Fixed and deployed (9cf93d6): the gate placed `dms_duplicate`/`link_repair` audit rows under no service
  (every service went STALE; a test now reads the code for unplaced types); the 1:1 check counted a links
  redo's trashed copy as a duplicate (the redo now names it; alice's 6 rows backfilled from evidence); a room
  guest was not mapped; Apps Script was never compared (c03 INCOMPLETE); a My Maps reading canDownload=false
  was logged a download ban. `external_owner_share.py` now uses partner@b (made 18 Nov, no source twin), not
  the target admin. **Next, in a fresh session on Sonnet:** (1) when action 247 (c09 Drive delta, 7 days) has
  run and c09 is re-verified, run `parity_gate.py --account-id 5`; PARITY ticks P3.3. A migrate skips a DONE
  user, so only a delta reaches c09. (2) The speed items below, then P3.4.
  **Speed plan (measured 18 Nov):** (a) the repair dispatcher re-verified c09 5 times in 8 min on an
  unrepairable finding, holding the slot: stop re-checking an unchanged fingerprint, mark it for a person
  after 2 tries; (b) a skip whose rule changed is never retried (only a delta re-walks a DONE user): repair
  should retry it; (c) `sync_vps.sh` never sets `logs/deploy_pending`: set it while waiting for idle; (d) run
  the gate when the dispatcher closes the last finding; (e) before each full cohort run, run the fixture
  users first (time the DMS once to see what it saves); (f) P4.1 in a Sonnet worktree while the box runs
  Phase 3; (g) campaign groups overnight, one job at a time; (h) P3.6 right after P3.3. **Token budget:**
  Claude Pro; on 18 Nov the week was 72% used (resets Fri 20 Nov 09:44). Fresh session per task, Sonnet to
  execute, Opus only for [opus] items, no polling (one wait per run), agents only for real parallel work.

- 18 Nov 2026, ~13:05 UTC (Opus): **P3.3 control run: migrate pass rc=0, all 41 users DONE; the DMS
  imported 12,091 emails for 41 users, 0 failed; its reconciliation blocked 11 of 11,931.** Defects found
  in the run, each fixed, tested, deployed (live-run rule): (1) the Apps Script copy was stranded in staging
  (no server-side route for it now); (2) the calendar instances call named no calendarId (the fake now
  requires it); (3) a mail link to a Drive *shortcut* never resolved (shortcuts are in the link lookups);
  (4) the DMS map was named the same for every import, so a stale pilot map read as ours (maps are
  content-named, `fresh_import`); (5) one email held twice on the source (c04, one Message-ID) blocked the
  pipeline (`same_source_email`, both may share one target copy) and the blocked user was never released
  (a passing check releases it); (6) a shrinking ready list made a new map every delta (the console's own map
  is reused when it covers every ready user); (7) a map that left out users owing nothing went stale when
  they got mail (the map carries every user not held); (8) **Gmail sends a nextPageToken on a full page with
  nothing after it**: maxResults=2 over exactly two copies (engine's + the DMS's of the same thread mail,
  alice..erin, 10 emails) read as "more", so the DMS duplicate was never trashed (`split_mail.target_copies`,
  4ad4ffb). The 11th: alice's email linking to a shortcut was written by the engine before (3), so it still
  names the source file; a links-redo run for alice (mail only) replaces it. Then the DMS delta, then
  verify-after-dms and the gate.

- 18 Nov 2026, ~04:30 UTC (Opus, the operator asleep): **P2.3 done in code; P2.5 near done.** Failpoints at 16
  calls, 26 `fp:` scenarios (see P2.3). Coverage: the audit had 34 rows with no probe; every supported row now
  has one (`coverage_audit.DEEP`), or a reason in `UNSEEDABLE` (Meet recordings). Seeded on a. for it: Apps
  Script (c03), 3 shared drives (90 memberships), 4 groups (44 members, 1 nested), a file owned by info@b.
  shared into c02/c03 (`external_owner_share.py`; b. is the only other live domain -- re-run it after a wipe
  of b.), an expiring share, a group grant and an inherited grant (c02), filters and a signature (c05),
  extendedProperties, a Drive attachment, a room ("Ward Hall") and a followed calendar (c06), a conversation
  (c04: the seed's 11,554 messages held none), and a Doc Google refuses to export as .docx (c01: no size field
  shows it -- quotaBytesUsed reads 1 -- so the probe asks for the export). First audit after: 55 covered,
  2 absent (threading, big Doc: both since seeded), Chat now reads not migrated (off by default). Second:
  **57 covered, 0 absent, 0 unprobed** -- row 1 met (P2.5, P3.2).
  New: `seed_sandbox --fixtures-only` (and `users` now narrows a cohort), every fixture runs once per user.
  Defects found and fixed: (1) **the seed key's delegation probe used the migrate key** --
  `dataclasses.replace` re-runs `Settings.__post_init__`, which reloads the account's key; so
  `separate_credentials` always read the seed entry as not delegated and overwrote it on every run, each
  overwrite costing ~20 min of Google's propagation (also in `dwd_helper --key`). (2) a fixtures-only pass
  re-ran c06's calendar_shapes (6 duplicates deleted). (3) mail_settings used a client without
  gmail.settings.basic; the 403 "insufficient scopes" is retried as transient: 7 minutes. Focus time and
  working location cannot be seeded on Business Starter (Google: "Organizer is not an enterprise account, or
  does not have the ... feature enabled", malformedFocusTimeEvent / malformedWorkingLocationEvent);
  out-of-office covers the row. The seed key now also holds admin.directory.resource.calendar; the migrate entry was not rewritten.

- 18 Nov 2026, ~20:45 UTC (Sonnet): **the 30-user cohort is seeded, mapped and created on b.** Seed: 30 users in
  2,870 s, c10's bulk 8,000 items; fixtures ran: mail_shapes 15, calendar_shapes 8, tasks_shapes 6, eicar 1,
  person_only_check 3 (Site, My Map, Vids in c09). Gaps found and routed: c07 calendar was refused
  (`notACalendarUser`, a new user's Calendar not yet active) and re-seeded alone (10 events, 2 secondary
  calendars); c01, t05, t20 are a few events short and t08 one message short (Gmail `failedPrecondition`),
  t17's Gmail labels read 400 in the audit (mailbox not ready): re-seed those when convenient. Identity map 41/41;
  provisioning created all 41 on b., 0 failed, the second pass had nothing left (the 404 did not recur this time).
  **P1.1 ticked**: full `tests/` green. It found one real defect, fixed: P1.5 on 16 Nov had overwritten the older
  `preflight.py` (the source scan `sniff.py` uses); the Start checks now live in `start_checks.py`. NOT deployed
  yet (box still has the overwritten file); deploy on an idle box. `coverage_audit.py --account-id 5` (P3.2 row 1)
  is NOT met: ABSENT: Apps Script, shared drives (the cohort ran `shared_drives: 0`), external-owned shares,
  groups; many UNPROBED calendar/drive/gmail rows. Next: seed those four absent kinds, then the probes.
- 18 Nov 2026, ~19:55 UTC (Sonnet): **P2.4 cohort registry deployed (8609159); the 30-user cohort seed is running**
  on a. (c01..c10 specific, t01..t20 twins; fixtures mail/calendar/task shapes, EICAR, bulk). The three
  hand-made files no API can create now exist in c09's Drive, made through the browser: a Site ("Ward Notice
  Board"), a My Map ("Ward Boundary Map") and a Vids file ("Ward Welcome Video"); `person_only_check` will count
  3. Not yet: the seed's fixture results, c10's bulk (5,000 messages, 3,000 files), mapping and creating the 30
  on b. (a second provisioning pass is still needed: 404 right after creation), `coverage_audit.py` (P3.2 row 1).
- 17 Nov 2026, 19:10 UTC (Opus): **parity gate PARITY, 11/11 users** on account 5 (engine run + split run with the
  DMS). The run-1 users' 10 duplicates had two causes, both fixed: the engine mail default (bc9ac38 era) and
  the DMS importing users who owed it nothing (96c513a: `dms_handoff.nothing_owed`). The extra copies were
  trashed after a byte check; the check now treats a copy Bitport itself trashed as kept trash (6b70b62).
  Incident #86 resolved. Next: P2.4 (the 30-user cohort), P2.3 failpoints [opus], the live campaign driver.
- 17 Nov 2026, ~18:25 UTC (Opus): **the split path is proven end to end.** Five new users (frank..judy)
  seeded on a., mapped and created on b. (a second provisioning pass again cleared the Directory's 404
  right after creation), migrated with `mail_mode: split`; the DMS followed on its own, the source
  admin's approval was clicked by Bitport itself (17:40), Google imported 346/346 (0 failed, 11 users),
  the reconciliation and verify ran, and the gate reads those five AT PARITY. The six earlier users read
  STALE (the DMS also processed their mailboxes; their mail had been moved by the engine in run 1): a
  fresh verify is running. The DMS job exited 2 (incident #86) on 11 unresolved messages: 10 are on the
  run-1 users (2 each, "match absent or ambiguous"), 1 is alice's link to an unmapped file. Shipped
  8906c23: a per-user DMS progress bar on Migration detail (`dms_progress.py`,
  `GET /api/v2/dms/per-user`, `DmsUserProgress.tsx`). Box: auto power-on after a clean shutdown (RTC
  alarm), backlight off, power-off requests logged; the account 3 campaign timer disabled.
- 17 Nov 2026, ~16:05 UTC (Opus, deep check after the smoke run). Found and fixed, deployed 508ac39:
  1. **Mail moved by the engine, against the rule.** A full run with no `mail_mode` took the box-wide A/B
     winner ("server_side + engine", from account 3), so the smoke run moved every message by the engine
     and the DMS never followed. A full run is now always split (`_default_mail_mode`); tests updated.
     Report: the smoke run's mail on b. was moved by the engine (sandbox; nothing to undo).
  2. **Every rewritten Doc failed its check.** `replaceAllText` writes the new id with no style, Docs keeps
     a default-equal style as unset, so one run read as three. The check now merges adjacent runs of
     equal effective style (`native_fidelity._merge_runs`); the 5 live Docs read IDENTICAL. The rewrite
     also restyles inserted ids from their neighbours (needed when the link text is bold etc.).
  3. `dwd_helper` read a dialog still saving ("Progress indicator") as a duplicate client: now waits.
  4. Key creation retries a service account made moments ago (NOT_FOUND "does not exist").
  Re-verify with repair running; then the gate. OPEN, in order: (a) the repair dispatcher re-verified the
  same 5 users every ~3 min with new fingerprints (should stop at an unchanged finding): find why;
  (b) re-run the smoke migration as split to prove the DMS follow-on (P0.9 again, after a wipe of b. or
  on new users); (c) P1.7 engine half: a raw upload needs `resumable=True` for non-ASCII bytes, so measure
  the extra round trip against the 33% before choosing; (d) P1.1 full-suite rerun; (e) P2.3 failpoints
  [opus]; (f) P2.4 fixture registry; (g) resolve incident #84.
- 17 Nov 2026, 15:25 UTC (Sonnet): P0.3 to P0.8 done; **P0.9 smoke run ran**: 5 users seeded on a.
  (tiny scale, 498 s), identities mapped (6 with the admin), users created on b., migrate (Drive 10 min,
  mail 1 min, calendar/contacts/tasks 3 min) -> all 6 users DONE, tally finished, `parity_gate`
  UNVERIFIED (Drive checks STALE because later passes changed the files; Gmail NOT_VERIFIED).
  NO repair or DMS follow-on started after the run: find out why (the request left `mail_mode` unset;
  read `api_server._default_mail_mode` and the `then` list in `migrate_start`), then run verify with
  repair and the DMS from the UI and re-read the gate. Found and fixed by the smoke run (all deployed,
  bc9ac38): the seed key could not pass Google's first-use screens (notice channel + watcher); a
  non-ASCII message cannot go up as a raw multipart upload (seeder keeps the JSON route; **the engine
  half of P1.7 must use a resumable upload for the same reason**); my own preflight refused every start
  (it matched `unattended-upgrade-shutdown`). Open: erin's Drive acceptance found 1 difference (read
  it); `dwd_helper` reports "could not tick Overwrite" after a first Authorize that had worked; key
  creation straight after `seed-sa` is created needs a retry; incident #84 (the first provisioning
  pass's 404s, resolved by the second pass) is open and can be resolved; the idle browser tab group
  holds Google sign-ins for both admins.
- 17 Nov 2026, 13:40 UTC (Sonnet): Setup done for both tenants of **account 5**. Source a.: project
  `wsmig-src-83074`, delegation 18 scopes. Target b.: project `wsmig-tgt-94671`, delegation 15 scopes
  (a first attempt left an empty project `wsmig-tgt-36651` in b.: delete it at teardown). Readiness
  (read-only, on the box): source for migrate READY, target for migrate READY, source for SEED not
  ready (no seed key yet: the seeder creates it first, through gcloud and the Admin console).
  Used `approve_setup_notices.py` (committed) for the two Google first-use screens. Open: sandbox
  declarations for a. and b., seed switch on account 5 (account 4's flag was set by mistake), P0.3
  Drive sharing settings, P0.8 licence counts and storage, P0.9 smoke run, deploy of the pending
  commit when the gate passes.
- 17 Nov 2026, ~08:50 Nepal (Sonnet): P0.5 in progress. LESSONS: (1) the wizard creates the pair's
  Bitport account itself when its destination step is confirmed: **account 5**
  (`migration-2-4c22631c@migrations.bitport.local`, "migration of #2"). Account 4
  (`migration-a-b@...`, made by hand, password not kept) is an orphan: delete it from Admin > Accounts.
  (2) Setup acts on the OPERATOR's own account view ("Viewing account: My own account"), never on a
  viewed one ("read-only here"); the Migration picker lists only configured pairs. (3) The LAN UI
  `http://192.168.1.9:81/app/...` works only on the home network. (4) Setup's real button hides behind
  "Show advanced options" > untick "Dry run first"; it then asks for the word SETUP. Source setup for a.
  was started 08:5x (full_setup.py --side source, account 5, keeps the admin login root-only); watch
  `logs/5/full-setup-source.json.progress`. Then the same for b. (target), then sandbox declarations,
  seeding on for account 5 (account 4's flag was set by mistake, harmless), P0.3, P0.8, P0.9.
- 17 Nov 2026, ~00:50 Nepal (Sonnet): P0.5 started in the UI through BrowserOS at
  `http://192.168.1.9:81/app/...` (the Cloudflare Access address needs a one-time email code; this
  LAN address is already signed in as admin@bitport.local). Bitport **account 4**
  (`migration-a-b@migrations.bitport.local`, random password never kept: act on it through "Viewing
  account") is created. Its subscription was already active (a Deactivate dialog opened by mistake
  was cancelled). NOT yet done: "Seeding allowed" (state unread: the snapshot does not show checkbox
  state, so read it with a screenshot before clicking), Setup for source a. and target b., sandbox
  declarations, P0.3, P0.6, P0.8, P0.9. Resume at 2:55 AM Nepal when the session resets. Page id 3 in
  the BrowserOS session is on Admin > Accounts; open a new tab if it is gone.
- 16 Nov 2026, ~23:45 Nepal (Sonnet): P0.4 done: `/etc/bitport/dwd.env` now holds the a. (source) and b.
  (target) logins (names only checked, mode 600, backup kept). Still the operator's: P0.3, P0.5, P0.6.
- 16 Nov 2026, ~22:30 UTC (Sonnet): STOPPED at 93% of the session window (the operator's screenshot;
  weekly 27%, Fable 23%, session resets 2:55 AM Nepal). Done tonight, all on branch `fableplan2-work`
  merged into the main branch: P1.0 to P1.6, the seeder half of P1.7, P2.1 (no live driver), P2.2.
  P1.1's full-suite run found 14 failures, all test-side: stale expectations (tui.bar, a fake db
  without fetchall, gmail.duplicate now automatic), and one control-plane-db order leak in the
  follow-on tests; fixed in tests. The data-generator suite finished with the failures listed in
  this commit's message, if any. The full runs were NOT repeated after the fixes (one file group
  each was run). NEXT, in order: (1) run the full `tests/` and `data-generator/` once more and write
  the skip count (6 skips so far, all "macOS OS-isolation") here, then tick P1.1; (2) P2.3 failpoints
  **[opus]**; (3) P2.4 fixture registry and the cohort file; (4) P1.7 engine half **[opus]**. Nothing
  here is deployed to the box. Phase 0 (the operator's four items) still blocks every tenant step.
- 16 Nov 2026, ~21:00 UTC (Sonnet): P2.1 (registry and runner, no live driver) and P2.2 done in the
  worktree. Next: merge the branch into the main tree when the full run ends, gate, push; then P1.1's
  remaining full-suite failures.
- 16 Nov 2026, ~20:00 UTC (Sonnet): P1.2 to P1.6 and the seeder half of P1.7 done in the worktree and
  tested in isolation (P1.5: preflight.py; P1.6: stable seeds). Not gated or deployed. NOTE: some
  seeder and oversight tests fail in the worktree only because it has no `migration.db` (the control
  plane's database); judge them in the main tree.
- 16 Nov 2026, ~19:00 UTC (Sonnet: P1.4 done in the worktree too, tested in isolation) (earlier note follows) (Sonnet, working in the worktree `../calude-workspace-fp2`, branch
  `fableplan2-work`, while the full suite runs in the main tree): P1.2 and P1.3 done and tested in
  isolation; not yet gated or deployed. Merge into the main branch after the full run ends.
- 16 Nov 2026, 17:50 UTC (Sonnet): P1.1 fixed and gated; the two named tests pass (the first was a
  one-second timestamp tie, not a missing feature). The full-suite run is the one thing left on P1.1.
- 16 Nov 2026, 17:00 UTC (Sonnet): P1.0 done on the box (autopilot for account 3 stopped; no job
  running; API and web UI active). #77 and #78 close themselves after 18 Nov 06:00 UTC. Next: P1.1.
- 16 Nov 2026, 16:40 UTC: where we are. Phase 0 is open; nothing else has started (0 boxes ticked
  by an executor, no commit after the plan, the box unchanged). The operator answered P0.1, P0.2 and
  P0.7. Still the operator's: P0.3 (sharing settings), P0.4 (logins on the box), P0.5 (the Bitport
  account, Setup, sandbox declarations, the seed switch), P0.6 (the outside test address). An
  executor can start P1.0 to P1.7 and the code of Phase 2 now. The plan was checked against the
  code and the box and changed: N counts the admin (Business Starter's limit of 300 users); the
  runner needs no password file; only `coverage_audit.py` lacks `--account-id`; unit templates and
  P1.0 added; the cohort needs a fixture registry; P0.9, a five-user smoke run, added; The clock
  added; a DMS warning for the mail factor added. The three scheduled check-ins of the account 3
  run were cancelled.
- 16 Nov 2026: plan written. Nothing started. The box (l3novo) is idle and runs ed3d325: 8 cores,
  19 GB of memory, 52 GB of disk free, about 0.9 MB/s up. The Wi-Fi link reads 433 Mbit/s, so the
  limit is the internet line and a cable would not help. `/etc/bitport/dwd.env` holds the logins of
  s. and t. Account 3's ledger is kept for P4.1. No Bitport account exists for a. and b. yet.

## Quick reference

- Box: `ssh -i ~/.ssh/bitport_lenovo root@100.79.158.38`, code in `/opt/bitport`, clock UTC.
- UI: https://bitport.sarafgloabalexim.com. `ui_check.py` drives it and compares it with the ledger.
- Tests: `./gate.sh` (about 5 minutes, before every deploy); `.venv/bin/python -m pytest tests/ -q`
  (about 2 hours); `.venv/bin/python -m pytest data-generator/ -q`. One pytest at a time, and no
  source edits while it runs.
- Verdicts: `parity_gate.py --account-id ID`; `readiness.py --account-id ID --side source|target`.
- A test that looks stuck: `pytest -o faulthandler_timeout=45`.
