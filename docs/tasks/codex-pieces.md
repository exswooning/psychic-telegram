# Codex work pieces

## P4.2 — realistic profile service and link data

- Closed the open profile gaps for calendar, contact, and task rates; named their assumptions and source in code.
- Made the link-mail kind distribution a validated plan input with a named default, and linked outside-kind mail to a pool file owned by the pool account.
- Kept the default pool file cap at 50 MiB, allowed larger profile files up to the configured cap, streamed large uploads, and included pool bytes in the budget.
- Files: `data-generator/profile_plan.py`, `data-generator/profile_seed.py`, `data-generator/seed_sandbox.py`, `data-generator/test_profile_seed.py`, `tests/fakes.py`.
- Tests: `.venv/bin/python -m pytest data-generator/test_profile_seed.py -q --tb=short` — **34 passed**.

## P4.3 — complete offline budget and seat accounting

- Budget output includes per-service items (including the cohort's five idempotent Gmail settings writes and two existence reads per mailbox), bytes crossing the box, sequential phase hours, API-rate sources, and a refusal gate. Account provisioning includes serial user lookups plus creates and the new-account count; the 300-seat calculation reserves the admin and all 30 cohort accounts, including twins, and reports 269 new accounts.
- Files: `data-generator/profile_plan.py`, `data-generator/seed_sandbox.py`, `data-generator/test_profile_seed.py`.
- Tests: `.venv/bin/python -m pytest data-generator/test_profile_seed.py -q --tb=short` — **35 passed**. The corrected offline plan command was also exercised by this suite; it reported 300/300 seats and 269 new accounts, then refused on its default 12-hour budget.

## P4.4 — hard-stop resume coverage for every profile service

- Fake services can now raise a hard-stop `BaseException` after a chosen number of successful calls. Resume tests kill and restart profile seeds during Drive, Gmail, Calendar, Chat, Contacts, Tasks, and Gmail settings; they verify stable IDs/markers and no duplicate resources. Gmail settings checks forwarding addresses and delegates before creating them on replay, and Chat resumes by reusing its named spaces and messages.
- Fixed secondary-calendar planning so replay skips cannot shift later calendar names and duplicate calendar events.
- Files: `tests/fakes.py`, `data-generator/test_profile_seed.py`, `data-generator/seed_sandbox.py`.
- Tests: `.venv/bin/python -m pytest data-generator/test_profile_seed.py -q --tb=short` — **35 passed**.

## P4 integration — generator suite and guard fixture

- Aligned the generator suite's sandbox fixture with the repository suite by isolating the cross-account source registry from the unavailable shared control-plane database. Updated the outside-link regression to require an explicitly outside-owned target.
- Files: `data-generator/conftest.py`, `data-generator/test_profile_sharing.py`.
- Tests: focused guard/attachment/link regressions passed. `.venv/bin/python -m pytest data-generator/ -q --tb=short` completed at 100% with no reported failures.

## P5.2–P5.3 — 300-user VM runbook

- Added the ordered VM preflight, sandbox wipe, offline plan review, live seed, seven benchmark/fact checks, identity initialization, and coverage audit. Recorded the exact user-corrected plan-only command and expected default-budget refusal, along with the expanded 10 GiB-tail budget command and the required benchmark evidence.
- Files: `docs/tasks/seed-300-runbook.md`.
- Tests: the runbook's local offline plan commands were exercised under P4.3. VM execution is intentionally left to Claude on the owned VM.

## P4 integration follow-up — service-only seeding compatibility

- Made secondary-calendar metric aggregation tolerate absent optional counters returned by a minimal test double. This keeps service-only and full-seed reporting compatible.
- Files: `data-generator/seed_sandbox.py`.
- Tests: `.venv/bin/python -m pytest tests/test_seed_one_service.py -q --tb=short` — **19 passed**; `.venv/bin/python -m pytest data-generator/ -q --tb=short` completed at 100% with no reported failures.

## P4/P5 integration follow-up — profile CLI cohort and settings budget

- Profile `--users` is a total seat count, so it no longer filters the selected cohort as if `300` were a cohort local-part. The complete budget includes Gmail settings across all 299 non-admin mailboxes (writes and existence reads). The live runbook explicitly leaves Chat off, as the plan requires.
- Files: `data-generator/profile_plan.py`, `data-generator/seed_sandbox.py`, `data-generator/test_profile_seed.py`, `data-generator/test_seed_sandbox.py`, `tests/fakes.py`, `data-generator/test_seeder_coverage.py`, `docs/tasks/seed-300-runbook.md`.
- Tests: pending focused and full generator reruns after this follow-up.

## Repository-suite verification

- Ran `.venv/bin/python -m pytest tests/ -q --tb=short` separately from `data-generator/`. It reached 100% but reported **24 failures and 4 setup errors**. The report includes host sandbox `PermissionError` for loopback binds and `ps` subprocess calls, plus the host RAM probe returning 0.0 GB; the two service-only seeder failures from that run pass in the focused 19-test rerun above.
- The full repository suite was not rerun after that narrow fix. VM commands and benchmarks remain unverified on Claude's VM by design.
