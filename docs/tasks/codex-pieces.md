# Codex pieces, 27 Nov 2026

## A — create/record recovery

- **Commit:** `79fa6d0` (Claude committed the completed piece).
- **Files:** `tasks_engine.py`, `calendar_engine.py`, `contacts_engine.py`, `groups_engine.py`, `calendar_resources.py`, `drive_engine.py`, `failpoints.py`, `campaign_scenarios.py`, `campaign_failures.py`, and the corresponding engine, fake, and campaign tests.
- **Tests:** `tests/test_contacts_tasks.py`, `tests/test_calendar_fidelity.py`, `tests/test_groups_engine.py`, `tests/test_rooms_and_groups_in_run.py`, `tests/test_drive_engine.py`, and `tests/test_campaign.py` completed at 100% with no failures. The campaign module was also rerun independently and passed.
- **Result:** task-list recovery excludes the default list and follows pagination; secondary calendars and already-landed group members are adopted after a missing mapping; new create-boundary failpoints are registered as kill-only. Claude’s two follow-up test adjustments are included in the commit.

## V — verification at scale

- **Files:** `native_repair.py`, `native_fidelity.py`, `verify_sample.py`, `tests/conftest.py`, `tests/test_sheet_window_growth.py`, `tests/test_native_images_compared.py`, `tests/test_image_fetch_deadline.py`, `tests/test_verify_streamed_binary.py`, and `benchmarks/verify_at_scale_test.py`; measurements and method are in `docs/tasks/verify-at-scale.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_sheet_window_growth.py tests/test_native_images_compared.py tests/test_image_fetch_deadline.py tests/test_verify_streamed_binary.py tests/test_verify_sample.py tests/test_verify_unverified.py tests/test_verify_content_memory.py benchmarks/verify_at_scale_test.py -q` passed (177 passed, no failures). The performance harness separately passed all five measured workloads.
- **Results:** Sheet checks overlap source/target reads and adapt to an 8 MiB response budget, empty ranges expand across the remaining allocated tail, and the default 10M-cell refusal is removed. Binary verification now hashes one-MiB chunks even above 25 MiB. Doc image bodies stream to hashes through a shared eight-worker cap, and source/target native reads overlap. The 10M-cell fake took 12.001 s/146 calls/4.57 MiB peak; the empty 20,000×120 Sheet took 0.002 s/6 calls/0.02 MiB; the 200 MiB fake found its changed byte using 400 chunks/2.00 MiB peak. Full before/after numbers are recorded in the scale report.
- **Limit:** Gmail request count stayed 200,000 for both 100k-message modes; receipts reduced modeled payload from 819.2 MB to 51.2 MB. Fake workload timings do not predict Google API latency. The first full-scale Sheet run took 120.582 s while re-encoding every response to size it; that attempt was stopped, the estimator was changed, and the completed benchmark passed at 12.001 s.

## B — scoped F-reboot qualification

- **Source:** integrated local commit `db48518` with the current branch's timestamp-precision and API follow-on behavior. No cherry-pick or Git metadata write was performed.
- **Files:** `campaign_reboot.py`, `campaign.py`, `campaign_failures.py`, `tests/test_campaign_reboot.py`, `tests/test_campaign.py`, `tests/test_campaign_executor.py`, `tests/gate.txt`, and `docs/tasks/reboot-qualification.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_campaign_reboot.py tests/test_campaign.py tests/test_campaign_executor.py -q` passed with no failures; `py_compile` passed for the reboot module.
- **Result:** F-reboot now requires the exact account 3 tenant pair, pinned VM ID, clean activity/repair state, an unarmed failpoint set, an exact pre-boot Drive pass, and a durable checkpoint. It issues one real reboot after that pass and requires a unique audited recovery Start, interrupted exit receipt, terminal result, and read-only full verification before judging PASS. No live VM action was run.
- **Integration fix:** the imported reboot test expected the old API to schedule `repair` with the migration. The current branch schedules only a scoped tally; its test now asserts that later contract.

## D — benchmark credential scope and Gmail benchmark

- **Source:** `dc44e8f` (`Require account-scoped credentials for named benchmarks`). The exact source diff was applied by hand because this sandbox exposes `.git` read-only; no cherry-pick or other Git metadata write was attempted. `3076e0b` remains unmerged.
- **Files:** `benchmark_run.py`, `tests/test_benchmark_run.py`, and `docs/tasks/gmail-perf-benchmark.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_benchmark_run.py -q` passed (17 passed, no failures); `.venv/bin/ruff check --no-cache benchmark_run.py tests/test_benchmark_run.py` passed.
- **Result:** a named account benchmark now refuses OAuth or impersonation credentials before starting any child process; account 1's legacy mode remains supported. The Gmail live benchmark specifies account 5, the `p002` and `c10` mailboxes, identical full-read baseline/candidate commands, the required target-only inventory precondition, and a pass bar that preserves completeness and fidelity.

## E — final handoff

- **File:** `docs/tasks/codex-final-handoff-2026-11-27.md`.
- **Tests:** documentation-only; no additional tests run.
- **Result:** records V and D outcomes, the earlier B result, the redirected C full-suite state, the current Git restrictions, and the division of remaining B/C/S work across the operator's separate sessions.
