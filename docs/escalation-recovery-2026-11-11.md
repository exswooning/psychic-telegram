# Escalation recovery, 11 November 2026

## Observed evidence and rollout hold

Read-only laptop checks at 18:03–18:09 UTC and the new One-to-one UI show
account 3 has not established full parity. The UI reports 14 DIFFERENCES,
3 INCOMPLETE and 28 IDENTICAL, but some IDENTICAL rows contain extras or lack
Gmail verification. Repairs remain paused (policy 0). DMS delta 2, the delta
loop and verify-13 remain active; the current verifier is checking info Gmail.
No deployment, tenant restart, source write or target repair was performed.

The current saved ledger has 45 users, 20 Gmail result rows and 25 users with
no saved Gmail result. Saved Gmail findings include 34,595 target-only entries,
12 duplicate findings, 46 copy/access findings and five comparison differences.
These are mixed-age results: extras are not proven unwanted mail and must not
be deleted. Some can be DMS deliveries not yet reconciled to source identities.
Single-message failures still overwrite mailbox summaries in the installed code.

IT Department's nine failed message records all say that an existing destination
Message-ID has different content. The latest saved Gmail result checks 982
messages and retains nine copy/access findings and two extras. Its receipt table
has 955 VERIFIED and three NEEDS_RECHECK entries. None of the nine failed IDs has
an item receipt. These counts measure different sets and cannot prove that 23
messages are missing. Preserve the existing destination copies; the next admitted
read-only comparison must distinguish storage-header differences from real body,
attachment or rewritten-link conflicts. The accepted storage-header correction
and safer item-versus-mailbox recording are already in reviewed source, not in
the installed verifier. No new live Google comparison was run this turn.

Drive still has eight users with differences and two with incomplete checks:
Sheet content, Form items, comment fidelity, inaccessible/time-limited reads and
Sheet read budgets remain unresolved. Saved findings are retained. Historical
count-parity alerts for Calendar/Contacts/Tasks were caused by using the overall
mail-influenced ratio; the installed parity_loop.py already matches the earlier
service-specific count fix. Do not treat the 113 old ratio alerts as 113 current
migration defects or erase them without their verification history.

## Confirmed code defects fixed locally

- Autopilot's report opened two SQLite connections each minute without closing
  them. The old process exited with `OSError: [Errno 24] Too many open files` on
  November 11 at 11:49:29 UTC. The replacement process has a larger descriptor
  limit, which is not a fix for the leak. Reports now use one explicitly closed
  read-only connection; repair-policy and local-diagnostic readers also close
  connections on exceptional/early-return paths.
- The report reused `bad` as a dictionary then sliced it as a list, after opening
  REPORT.md for writing. This reproduces the live empty report. The report now
  reads shared coverage-aware verdicts, preserves incomplete/missing-service
  evidence, renders first and replaces the file atomically. A render failure
  preserves the prior report.
- Old escalations no longer disappear solely after six hours. Paused/skipped
  repairs no longer count as fixes. The report states that a saved escalation
  does not confirm delivery to Claude.
- Every failed mail item previously reclassified all unknown queue findings.
  A live stack showed Health in known_issues.ensure/classify, with 50,645 unknown
  rows. Classification now carries a rule version, handles at most 200 old rows
  per transaction and never repeats an unchanged unknown result at that version.
  New findings are classified/versioned immediately. Bump CLASSIFIER_VERSION
  when changing classification rules. Existing findings, states and evidence are
  preserved; the additive issue_version column/index requires no data deletion.
- DMS reconciliation flushes progress even under a buffered `python -c` launcher,
  identifies the user and reports checked/total counts. Errors are item-scoped,
  not fabricated one-of-two mailbox samples. It still performs no Google writes.
- Parity stage counts require exact known counts; 99.99%, unknown counts and
  extra items do not pass. Outstanding DMS is WAITING_DMS even when counts happen
  to match. Form transport errors are incomplete reads, not zero-difference Form
  objects. Stage-only success no longer announces full parity.
- Local-model evidence explicitly identifies x9 as nine failed items and receipt
  gaps as unconfirmed, not missing messages. Output that calls x9 an error code
  is rejected. This is a narrow factual guard, not proof of general model quality.

## Validation and deployment

Thirteen new regression cases failed against the previous committed source and
passed after the patch. The expanded isolated backend suite passed 171 tests,
including autopilot, parity, classification, DMS safety, repairs, readiness and
local diagnostics. No tests ran against tenant ledgers or Google. Tests used
/private/tmp/bitport-escalation-hm52gwwo and temporary databases.

A synthetic 50,000-unknown-row benchmark, two ensure calls, reduced classifier
calls from 100,000 to 400 and measured 0.639s versus 0.008s on this Mac. This
measures the backfill path only, not end-to-end migration speed or Google latency.

Live hashes still differ from reviewed source for autopilot.py, verify_sample.py
and migration_checks.py. Current parity_loop.py matches the earlier committed
file. This is a partial comparison, not a complete release attestation. Preserve
independently installed changes; once all tenant/DMS/repair work is idle, establish
a full baseline and use unchanged sync_vps.sh gates, rollback and postchecks.
Do not release the new autopilot without its db.py coverage helpers and the
reviewed item-evidence fixes from 1f4eb07. Target repairs remain paused. Local tests
are not a live recheck and no migration finding is marked resolved by this work.
