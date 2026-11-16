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
- [ ] P0.4 OPERATOR: put both admin logins on the box in `/etc/bitport/dwd.env`: the same four
  variable names it holds now, with the values of a. (source) and b. (target) in place of s. and t.
  Never in chat.
- [ ] P0.5 OPERATOR, in the Bitport UI: create the account "a.sarafgloabalexim.com →
  b.sarafgloabalexim.com", run Setup for both sides, declare both domains sandboxes, and switch
  seeding on for the account (Admin, Accounts).
- [ ] P0.6 OPERATOR: one outside Google account you own, for external-share tests. The address only.
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
- [~] P1.1 Green baseline. Two tests failed, both older than this plan. Fixed 16 Nov:
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
- [ ] P1.2 Box scripts work for any account. `dms_delta_loop.py` and `dms_user_verify.py` hard-code
  account 3 and `/opt/bitport`; `autopilot.py` and `parity_loop.py` take `--account-id` already.
  Add unit templates `systemd/bitport-autopilot@.service` and `systemd/bitport-parity@.service`
  (the instance is the account id): the repo has none, and the box runs a hand-made unit for
  account 3. Done when a grep for `accounts/3` and `account_id=3` in the four files finds nothing,
  with a test.
- [ ] P1.3 `coverage_audit.py` takes `--account-id`. It is the only check tool without it (read
  16 Nov: `fidelity_audit.py`, `form_deep_check.py`, `mail_dead_links.py`, `tally.py`,
  `parity_gate.py` and `readiness.py` have it). `sharing_report.py` is not a command: its numbers
  are read on the Sharing page.
- [ ] P1.4 Wipe safety, from the 16 Nov half-wipe:
  - a wipe refuses to start unless readiness reads Ready for that side and one real read per
    requested service succeeds as the admin and as every user it will touch. All or nothing; each
    refusal named; `--allow-partial` for a deliberate partial wipe;
  - its summary says what was removed and what was not, per service with user counts, and what the
    ledger forgot and kept;
  - wipe, reset, seed and "Run the DMS again" refuse while a `watch_tenant_ready` incident is open
    for that side.
- [ ] P1.5 Preflight before Start. Refuse with the reason when any of these fails: both tenants
  ready, quota, network, the DMS login and authorisation, disk, a pending upgrade.
- [ ] P1.6 The seeder is repeatable. It seeds its random numbers from `hash(user)`, which Python
  changes in every process (`seed_sandbox.py`, `corpus.py`; measured 16 Nov: 8543 and 9792 for one
  address in two processes). Use `zlib.crc32` over a campaign seed
  and a content key. Add `--seed` and `--content-key`; two users with the same content key get the
  same content. Done when two separate processes print the same plan for the same seed (test).
- [ ] P1.7 Mail under 5 MB goes up as raw bytes. Today it goes up as base64 text inside JSON, 33%
  bigger, on a line whose upload is the limit (0.9 MB/s). Seeder: `seed_gmail` and `seed_drafts`.
  Engine **[opus]**: `gmail_engine._deliver`, which already uploads raw bytes above 5 MB. Measure
  upload bytes per message before and after on one mailbox (`/proc/net/dev`); expect about a quarter fewer.

## 5. Phase 2: the campaign

### The runner

- [ ] P2.1 `campaign.py` and `campaign_scenarios.py` at the repo root, with `tests/test_campaign.py`.
  - A scenario is data: `id`, `group` (`errors`, `failures`, `benchmarks`, `watchers`, `chat`),
    `prepare` (what to seed), `act` (the request to send), `inject` (optional), `expect`.
  - The runner is root on the box. It makes its own session with `accounts_auth.create_session`
    for the superadmin account, as the tests do, so no password file is needed.
    (`/etc/bitport/ui.env`, which `ui_check.py` reads, does not exist on the box; give `ui_check.py`
    the same option.) Every request carries `reason: "campaign <name> <id>"`.
  - The runner prepares, sends the request, injects, waits until
    `job_admission` shows no job and no follow-on for the account, judges from the ledger, the
    parity gate and the tally, and appends one JSON line to
    `logs/campaigns/<name>/results.jsonl`. It rewrites `RESULTS.md` (one table row per scenario).
  - `python campaign.py --account-id ID --name NAME --group G [--only ids] [--redo] [--dry-run]`.
    A scenario already in the results is skipped unless `--redo`. It runs on the box as a unit, so
    no model waits on it.
  - It never wipes a tenant. A scenario that needs an untouched user takes the next unused twin.
    When none is left it resets one used twin on b. (P0.7(a)) and says so in the result.
- [ ] P2.2 Completeness tests in `tests/test_campaign.py`. These are what make "every possible"
  checkable:
  - every id in `known_issues.ISSUES` and `RUN_KINDS` has a scenario, or a line in `UNSEEDABLE`
    with its reason and the unit test that covers it;
  - every check in `benchmarks.py` has a scenario for its passing and its failing outcome;
  - every `oversight.watch_*` function and every incident kind has a scenario;
  - every failpoint name has a scenario.

  Add `tests/test_campaign.py` to `tests/gate.txt`.
- [ ] P2.3 **[opus]** for where the calls go; Sonnet for the rest. `failpoints.py`: `hit(name)` does
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

- [ ] P2.5 `scope.py` has 59 supported rows (34 FULL, 25 PARTIAL) and `coverage_audit.PROBES` has
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
- [ ] P3.2 `coverage_audit.py`: row 1.
- [ ] P3.3 Control run from the UI: all services, split mail, DMS after, server-side, ordered. Let
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

- [ ] P4.1 `data-generator/profile_extract.py`, run on the box, read-only on account 3's ledger
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
