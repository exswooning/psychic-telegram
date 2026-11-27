# Incremental verification inventories

Implemented 2026-11-27. `VERIFY_INCREMENTAL` defaults to `1`; `0` restores the prior full-list inventory paths.

Gmail now stores each source and target mailbox's message metadata in the account ledger, keyed by message ID, with a mailbox `historyId` checkpoint. Inventory rows include Message-ID, sender, label IDs, internalDate, threadId and size. Re-verification applies `users.history.list` changes and fetches metadata for changed messages only. A missing or version-mismatched inventory, expired history (404), malformed history window, or unavailable checkpoint causes a full inventory read. If the required full read or changed-message metadata cannot be read, the verifier records an error and keeps the service `INCOMPLETE`.

Drive's target stray inventory is also cached per user with a saved start page token. `changes.list` applies added, changed, removed, trashed and no-longer-owned files. A missing/version-mismatched inventory or unusable token triggers a new start token and full file listing. A failed fallback listing remains an error and cannot pass the Drive check.

## Gmail fake benchmark

Run with `.venv/bin/python tests/benchmark_verify_incremental.py`. It seeds 40,000 messages in each in-memory fake mailbox and measures the inventory work only. The legacy comparison assumes all 40,000 target messages are unmapped DMS deliveries, so the old stray path requests metadata for each. It does not include the separate mapped-message content cache. The fake records each inner `messages.get` operation; a live client batches these operations over HTTP. Fake Gmail emits an extra empty page after an exactly full final page, giving 81 pages per mailbox (162 list calls) in the legacy/full pass.

| Verify pass | Legacy wall time | Legacy API calls: list / get | Incremental wall time | Incremental API calls: profile / history / list / get |
|---|---:|---:|---:|---:|
| First, no inventory | 1.205 s | 162 / 40,000 | 2.581 s | 2 / 0 / 162 / 80,000 |
| Re-verify, 0 changes | 0.978 s | 162 / 40,000 | 0.290 s | 2 / 0 / 0 / 0 |
| Re-verify, 10 relabels per side | 0.990 s | 162 / 40,000 | 0.253 s | 2 / 2 / 0 / 20 |
| Re-verify, 1,000 relabels per side | 0.948 s | 162 / 40,000 | 0.367 s | 2 / 4 / 0 / 2,000 |

The first inventory pass pays the one-time cost of reading metadata for both complete mailboxes (80,000 reads here, versus the old target-only 40,000). Subsequent reads scale with mailbox changes: metadata reads fell from 40,000 to 0, 20, and 2,000 in the three re-verification cases. The cache checks its stored row count and required field shape before use; missing or malformed rows trigger a full read. Fake wall times measure local code and SQLite work, not Google's network latency or quota pacing.

## Tests

`.venv/bin/python -m pytest tests/test_verify_incremental.py tests/test_verify_sample.py tests/test_verify_mail_speed.py tests/test_verify_drive_batches.py tests/test_verify_gmail_quota.py tests/test_gmail_check_reads_ahead.py -q -p no:cacheprovider` — exit code 0.

The regression tests run a second full Gmail verification after a message is added, another is deleted on the target, and a mapped message is relabelled; all three are reported. They also verify expired history and inventory-version changes force a full read, Drive additions/deletions are found through its change feed, expired Drive tokens rescan, and `VERIFY_INCREMENTAL=0` restores the full inventory paths.

The full `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` run exited 1 with 22 failures and 4 setup errors. Failures were in control-plane/process probing, deploy-guard, and localhost-server tests: the sandbox denied `ps` subprocesses and localhost TCP binds, and the macOS resource probe returned 0 total RAM. No verifier test appeared in the failure list. These failures did not affect the focused verifier test command above.
