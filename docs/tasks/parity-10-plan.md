# Bitport to 10/10: plan and progress

Operator's goal (13 Nov 2026): parity at every run; the most efficient, fast and reliable migration tool.
Work one item at a time: code, a test, commit, push. Deploy to l3novo only when no migration, DMS,
reconcile or verify job is live. Update the checkboxes here in the same commit. Audit behind this plan:
the 13 Nov session (score 6/10: Drive 8, mail 5, verification 6, orchestration 4, operations 4, security 3,
tests 6).

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
      (`main._standing_failures`; helper tested). TODO: one end-to-end test through `migrate_user`.
- [x] SSH password entries removed from `.claude/settings.json`. OPERATOR: rotate the SSH/sudo password,
      the Bitport admin password and the source admin password; decide on rewriting git history
      (the password is in pushed history); remove the temporary key from root's authorized_keys.
- [ ] Key-only SSH for deploys (install `~/.ssh/bitport_lenovo.pub` on the box: needs the operator's word),
      then `sync_vps.sh` works again.
- [ ] Label race: Gmail re-files inserted sent mail moments after the insert; re-check after a settle
      delay instead of failing the user (`migration_checks.verify_written_mail`; 47 on administration@).

## Phase 2: one orchestrator
- [ ] Box scripts become admitted jobs with `--account-id` (autopilot.py, parity_loop.py,
      dms_user_verify.py, dms_delta_loop.py: hard-coded account 3 and /opt/bitport). The post-migration
      chain (/var/lib/bitport-sched/post_migration_chain2.py on the box, a copy in the 13 Nov scratchpad)
      is superseded by `dms_pipeline.py` + the `verify-after-dms` follow-on (deployed b0adcb8, not yet
      proven live): prove it on the next run, then delete the box script.
- [ ] One run record with phases (wipe, drive, mail, dms, reconcile, verify, review); a restart resumes
      from the recorded phase; nothing else can start the same phase twice (an unidentified actor restarted
      bitport-dms-fresh at 06:54 UTC on 13 Nov during a Wi-Fi drop).
- [ ] DMS driver retries navigation on DNS/connection errors with backoff (one ERR_NAME_NOT_RESOLVED ended it).

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
- [ ] Quick test gate under 10 minutes before deploy; the full suite (about 21,000 tests, ~2 h) nightly.
- [ ] Split api_server.py (9,700 lines) and webui.py (6,900 lines) by concern.
- [ ] Confirm throughput is at Google's quota ceiling; wired network or a hosted box if the box is the limit.

## State of the live run (13 Nov, 09:20 UTC)
- Engine run finished 06:07 UTC: Drive 38,513/38,513, 0 failed items. DMS relaunched 09:00 UTC by the box
  chain for all 45 users (decision in logs/jobs/3/dms_decision.json); then delta, deep check, review file
  logs/jobs/3/REVIEW-post-migration.md. Repairs paused. Check `grep chain2: logs/jobs/3/scheduled.log`.
- Not deployed yet: the Phase 1 commit after b0adcb8 (deploy when the box is idle).
