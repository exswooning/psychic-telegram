# Bitport to 10/10: plan and progress

Operator's goal (13 Nov 2026): parity at every run; the most efficient, fast and reliable migration tool.
Work one item at a time: code, a test, commit, push. Deploy to l3novo only when no migration, DMS,
reconcile or verify job is live. Update the checkboxes here in the same commit. Audit behind this plan:
the 13 Nov session (score 6/10: Drive 8, mail 5, verification 6, orchestration 4, operations 4, security 3,
tests 6).

Operator, 13 Nov 2026: both tenants of account 3 (s. and t.sarafgloabalexim.com) may be used for live
tests and verification, including test writes to the source. Still ask first: an SSH key on the box
(the permission check refused it on the general grant), a wipe, a git history rewrite, password rotation.

## What 10/10 means (the acceptance test)

A run scores 10 when all hold with nobody involved between Start and the final report:
- Parity: every migratable item is on the target and verified identical, every user, every service.
- Explained remainder: anything not migrated is named with its reason (Sites, Chat, unfetchable Form
  images, orphaned grants); nothing unexplained.
- Repeatable: three consecutive wipe-and-migrate runs on the sandbox (account 3) reach the same result.
- Recoverable: a kill at any point or a network drop, then a restart, ends in the same result.
- Fast: run time is bound by Google's quotas, not Bitport. Baseline 13 Nov: Drive 1 h 43 min
  (38,513 files), mail pass 34 min, DMS ~23 h, reconcile ~20 h projected.

## Phase 1: safe and correct
- [x] Lint green on HEAD (repair.py unused target read, split_mail/verify_sample dead names).
- [x] Link inventory stops with the run (Codex's change, committed).
- [x] A later pass no longer overwrites an earlier pass's failure or a mail hand-off still owed
      (`main._standing_failures`; tested on its own and end to end through `migrate_user`).
- [x] SSH password entries removed from `.claude/settings.json`. OPERATOR: rotate the SSH/sudo password,
      the Bitport admin password and the source admin password; decide on rewriting git history
      (the password is in pushed history); remove the temporary key from root's authorized_keys.
- [x] Key login for deploys: the operator installed `~/.ssh/bitport_lenovo.pub` for l3novo and root
      (13 Nov); `ssh -i ~/.ssh/bitport_lenovo root@100.79.158.38` works, so
      `./sync_vps.sh root@100.79.158.38 /opt/bitport ~/.ssh/bitport_lenovo` can be used again.
      OPERATOR, still open: password login is still enabled (switch it off after rotating the password);
      root's authorized_keys also holds l3novo's older key and the temporary key `bitport-5fa-qualified-20261112`.
- [x] Label race: Gmail re-files inserted sent mail moments after the insert. The inline check now waits,
      aligns once more and looks again (`LABEL_SETTLE_SEC`, 2 tries); labels still settling are recorded
      `LABELS_PENDING` and queued for the full verify, and no longer fail the user. Not yet proven live.

## Phase 2: one orchestrator
- [ ] Box scripts become admitted jobs with `--account-id` (autopilot.py, parity_loop.py,
      dms_user_verify.py, dms_delta_loop.py: hard-coded account 3 and /opt/bitport). The post-migration
      chain (/var/lib/bitport-sched/post_migration_chain2.py on the box, a copy in the 13 Nov scratchpad)
      is superseded by `dms_pipeline.py` + the `verify-after-dms` follow-on (deployed b0adcb8, not yet
      proven live): prove it on the next run, then delete the box script.
- [ ] One run record with phases (wipe, drive, mail, dms, reconcile, verify, review); a restart resumes
      from the recorded phase; nothing else can start the same phase twice.
- [x] Upgrades never restart Bitport units. The 06:54 UTC restart on 13 Nov was needrestart after an
      unattended upgrade: it restarted every bitport-* unit (the API too) and the DMS import died.
      `systemd/needrestart-bitport.conf`, installed on l3novo and by every deploy.
- [x] Console navigation retries on DNS/connection errors with backoff (`dwd_helper.retry_navigation`;
      one ERR_NAME_NOT_RESOLVED ended the DMS job). Not yet deployed.
- [x] Explained: the verify with repair that ran beside the DMS from 07:02 UTC on 13 Nov was a
      One-to-one run pressed in the web UI (`POST /api/v2/one-to-one/run` from 192.168.1.5), not a bug.
      Open question for the run record: should the UI refuse a full verify while the DMS is importing?

## Phase 3: speed (measure before and after)
- [ ] Read each mailbox once: the mail pass takes candidates from the link inventory
      (`mail_link_messages`) and reads only messages newer than it (129,800 reads were repeated).
- [ ] Bulk DMS reconciliation: list the target once with Message-ID headers, match in memory, read content
      only for link mail and mismatches (today ~3 calls per message).
- [ ] One target write per file: merge the move out of staging with the modifiedTime restore. Inferred
      from ward14@/ward16@ at 1.5 files/s under the fixed 3 writes/s limit; test on one large user first.
- [ ] Mail verify reuses the bulk index for the extras check (ward14@: 45 min for 53 mapped messages).

## Phase 4: operations
- [ ] `sync_vps.sh` builds the frontend and publishes the `/next` bundle (box still serves the 9 Nov build).
- [ ] One log file per run; DMS console readings appended to a history, never overwritten.
- [ ] Every verdict carries an "as of" time; a defined final delta and freeze for cutover (the source is live).

## Beyond the fixes (8 -> 10)
- [ ] Parity gate: one command gives the final verdict from a full verify; any unexplained difference fails the run.
- [ ] Fault-injection tests: kill mid-copy, DNS drop, rate limits, full disk; the rerun converges.
- [ ] Nightly end-to-end run on the sandbox with a morning report.
- [ ] Each known difference fixed or formally accepted (sent-mail label, orphaned grants, Form image).
- [x] Quick test gate: `./gate.sh` runs the files in `tests/gate.txt` (lint, status, split mail, DMS,
      verify, Drive, deploy, safety guards) in about 5 minutes. Run it before every deploy.
      TODO: have `sync_vps.sh` refuse without a passing gate; the full suite (~21,000 tests, ~2 h) nightly.
- [ ] Split api_server.py (9,700 lines) and webui.py (6,900 lines) by concern.
- [ ] Confirm throughput is at Google's quota ceiling; wired network or a hosted box if the box is the limit.

## State of the live run (13 Nov, 09:20 UTC)
- Engine run finished 06:07 UTC: Drive 38,513/38,513, 0 failed items. DMS relaunched 09:00 UTC by the box
  chain for all 45 users (decision in logs/jobs/3/dms_decision.json); then delta, deep check, review file
  logs/jobs/3/REVIEW-post-migration.md. Repairs paused. Check `grep chain2: logs/jobs/3/scheduled.log`.
- Not deployed yet: the Phase 1 commit after b0adcb8 (deploy when the box is idle).

## Priority order from 13 Nov evening (operator: faster, parity first time, failures handled)
Work these first, one or two per usage window, each with a test and a before/after measurement on one
sandbox mailbox or user. Stop a session at about 60% of the usage window.
1. Bulk DMS reconciliation by a Message-ID index (Phase 3). Biggest saving: ~20 h projected today.
2. Verify streams results to the ledger instead of holding them (the 11 GB growth), reuses the index for
   extras, and re-verifies only what changed since its last "as of".
3. Read each mailbox once (shared classifier between the link inventory and the mail pass).
4. End-of-pass retry of failed items by class: transient (retry), quota (wait for the reset), explained
   (accepted list), unexplained (hold that user, continue the rest, incident). Two rounds with no
   progress stops and reports.
5. Preflight gate before Start: both tenants ready, quota, network, DMS login and authorisation, disk,
   no pending upgrade. Refuse with the reason.
6. One target write per Drive file; DMS started per user as its mail pass ends clean.
7. Run record with phases; fault-injection tests; UI refuses a full verify while the DMS imports.

## Usage log (operator's Claude Pro limits; keep each session small)
Weekly limits reset Fri 20 Nov, 9:45 AM Nepal. The Fable weekly limit is the binding one.
| When (Nepal) | Session (5 h) | Weekly | Weekly Fable | Note |
|---|---|---|---|---|
| 13 Nov ~3:00 PM | 72% | 23% | 16% | before the audit fixes |
| 13 Nov ~9:00 PM | 92% | - | - | plan written, stopped |
| 14 Nov 12:40 AM | 0% | 41% | 45% | one evening of Fable cost about 29% of the week |
Rule from 14 Nov: about 55% of the Fable week is left for 6 days, so spend at most about 8% a session:
one or two plan items, targeted reads, the gate, then stop and schedule the next run. Claude cannot read
the meter itself; the operator's screenshots are the only measurements.

## State, 14 Nov 00:55 Nepal (13 Nov 19:10 UTC)
- Deployed a84d9fd through `sync_vps.sh` (first gated deploy with the key). The deploy first failed its
  compile check on 739 macOS `._*` files an earlier hand deploy (tar) had left on the box; removed.
- Priority item 1, first half done: the DMS reconciliation has its own 8 threads and 20 reads/s per
  mailbox (was 2 threads at 4/s). Not yet measured live. Second half (a Message-ID index instead of one
  search per message) is still open.
- The box chain is retired: it read the watcher's death (DNS, 18:32 UTC) as "DMS finished" and started
  the deep check early. Incident #72 (`dms_not_started`) is open so the API's hourly retry starts the
  default pipeline (`dms_pipeline.py`, then `verify-after-dms` with repair). First live proof of that path.
- One-to-one verify from the UI (finished 15:00 UTC, mid-DMS): 44,505 of 44,576 identical, 0 missing;
  60 Gmail label differences (the label race), 6 Sheet cell differences, 4 comment differences, the
  accepted Form; 100,990 target-only messages are DMS mail awaiting reconciliation; 6 read errors.
- Google's import at 18:30 UTC: 133,743 of 133,760 imported, 17 failed, 44 of 45 users; info@ (to
  nest@t) had 34,964 of 40,193.
