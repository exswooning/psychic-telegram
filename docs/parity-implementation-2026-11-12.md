# Parity implementation and deep findings — 12 November 2026

## Implemented source changes

Reconciliation now inventories every current source message, including mail not
previously recorded by the engine. Draft backing-message IDs are explicitly
inventoried and excluded from ordinary mail adoption; existing draft checks
remain responsible for their fidelity. Inventory API/pagination failures fail
closed rather than becoming empty mailboxes.

Unique destination matches with proven equivalent content can acquire a ledger
mapping even when labels differ. Label differences remain NEEDS_RECHECK and do
not mark DMS delivery complete. Source labels with no destination mapping also
block acceptance. Ambiguous identities, actual body conflicts and changed
mappings remain held. Adoption is transactional and prevents multiple sources
from claiming one destination. No Google modification/insert/delete is added.

Per-item outcomes are durable in mail_reconciliation_items. Successful comparisons
can be reused only with identical source/target history anchors, comparison-file
hashes, target identity, label maps, rewrite setting and Drive mappings, plus a
still-valid mapping and VERIFIED receipt. Failed outcomes never reuse. Missing
history/code evidence means fresh reads. Source or target changes during a run
invalidate that run's checkpoint on the next invocation. This is conservative
checkpointing, not an atomic snapshot, a full metadata index or a proof that all
source references work. Discovery transport is isolated per worker with the
mailbox limiter shared across those workers.

Native Sheet verification reads all cells in bounded windows and compares
structures independently. Source/destination IDs are normalized, expected Drive
references are rewritten only in memory, mismatches name ranges without logging
cell values, and traversal continues after mismatches. Window responses missing
sheet identity fail. A qualified 10-million-cell ceiling and the existing file
retry/time budget still hold; exceeding either remains INCOMPLETE. Repair readers
retain their existing conservative limits. This does not itself repair the six
saved Sheet mismatches or prove live large-sheet performance.

DMS delta launches use unique service names, a singleton scheduler lock and an
unknown-state hold. Existing attempt limits are unchanged; this does not reset
or repair the 17 provider failures. Per-loop attempt counts are not yet a durable
cross-restart retry budget, so do not schedule repeated fresh loops to evade it.

The running-job progress parser starts a new phase at detached verify dispatch.
A completed phase cannot show overall 100% while a verify job remains live.
Unmapped destination messages are no longer silently exempted merely because
they are in Trash or have a Google sender. They remain visible for reconciliation;
no automatic removal is introduced. Previously retained reset copies require
explicit reviewed accounting, rather than an invisible blanket exemption.

parity_deep_audit.py opens a ledger read-only and reports failed items, finding
backlog, pending receipts, mapping collisions, invalid receipt mappings,
unfinished repairs, missing/zero-item/unversioned service results and saved
findings. It never calls Google, writes the ledger, closes findings or certifies
parity. Example: python parity_deep_audit.py --db PATH --out REPORT.json.

## Deep live checks (read-only, approximately 10:44–10:55 Nepal)

SQLite quick_check: ok. No duplicate target message mappings, no VERIFIED receipt
with an invalid mapping, and no unfinished repair records were found. Repairs
remain paused. Pending receipts changed from 27,885 to 27,890 during concurrent
DMS work; these are item states, not a count of missing mail.

Open finding rows at the first snapshot:
- Drive: 18 differences and 12 errors.
- Gmail: 10,398 differences, 12 duplicates, 7,617 errors, 26 missing and nine
  notCopied rows. These are historical/mixed-age findings and can overlap.
- Nine failed audit messages all report existing-destination content conflicts
  (eight FAILED, one FAILED_SPLIT).

A bounded scan of the latest one megabyte of each selected log found six
tracebacks in dms_user_verify.log, including three database-is-locked failures
while queuing findings (Administration, Education and Finance). It also contained
Google HTTP500 failures and comparison mismatches. Exact observation timestamps
of every old stack were not established; do not claim they all recurred after
34a25c1. The earlier reduced classification work should shorten write pressure,
but database contention is not proven resolved. Scheduler admission and durable
retry handling still need qualification under real concurrent workloads.

The autopilot log retains its old too-many-open-files traceback; no new crash was
established. The DMS delta log retains a service-launch CalledProcessError with
a reused unit name; the unique-name change addresses that collision path.

DMS wrappers/reconciliation were still active at 10:52 Nepal. Administration had
12,350/14,558 checked, 6,443 verified, 5,907 unresolved. Deployment and overlapping
provider verification remain held by the existing release gates. No tenant job
was stopped, restarted or launched and no source/target data was changed.

## Validation and rollout limitations

Isolated suites passed: 190 initial reconciliation/verifier tests, 149 follow-up
reconciliation/repair/readiness/mapping tests, 129 verifier/deep-check tests and
24 progress/deep-check tests. These suites overlap. Tests use fake transports and
temporary ledgers under /private/tmp/bitport-deep-kdi6fiy7, not tenant data. They
exercise stable/changed-history checkpoints, no-write label differences, body
conflict holds, atomic adoption, inventories, bounded Sheet reads, retry launch
names, unknown DMS state and read-only audit behavior.

Only .claude/settings.json was pre-existing dirty and is preserved. All reviewed
source must be committed/pushed before an exact-version idle-gated rollout with
baseline hashes, rollback and both UIs preserved. The full 45-user/270-service
provider verification and whole-tenant checks are still required after affected
scope checks, at a stable source cutoff. The 17 DMS failures, actual Sheet/Form/
comment differences, inaccessible source references, draft gaps and supported
mailbox-setting coverage are NOT declared fixed by this implementation. Existing
typed repair handlers stay behind the paused policy; live repairs need admission.
