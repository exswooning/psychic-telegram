# Codex pieces — 2026-11-27

## Prevent the seeder from shadowing the guarded verifier

- **Files:** `seed_loader.py`, `reset_target.py`, `campaign_changes.py`, `separate_credentials.py`, `verify_scopes.py`, `tests/test_seed_loader.py`, `tests/test_reset_target.py`
- **Tests:** `.venv/bin/python -m pytest tests/test_seed_loader.py tests/test_reset_target.py tests/test_separate_credentials.py tests/test_verify_scopes.py tests/test_campaign_executor.py -q -rs`
- **Result:** Passed. Seeder imports now use one cached file-path loader that removes `data-generator/` from `sys.path` after module execution; scope and campaign callers no longer leave the shadowing directory behind.

# Codex pieces

## 2026-11-27 — Source write guard red team

- **Piece:** Added a fake-transport attack matrix over 831 discovery write-method/route combinations, transport and upload bypass probes, strict expected failures for current leaks, and the credential-backed `source_attack_probe.py` dry-run/live CLI.
- **Files:** `tests/test_source_write_attacks.py`, `source_attack_probe.py`, `docs/tasks/source-guard-redteam.md`, `docs/tasks/codex-pieces.md`.
- **Tests and result:**
  - `.venv/bin/python -m pytest -o addopts=--tb=short tests/test_source_write_attacks.py -q` — **281 passed, 567 xfailed** in 8.85s. The strict xfails record the currently observed leak routes, including the incomplete move update; the document lists each route and reason.
  - `.venv/bin/python -m py_compile source_attack_probe.py tests/test_source_write_attacks.py` — passed.
  - `.venv/bin/python source_attack_probe.py --help` — passed.
  - Invoked live mode with `dharan.gov.np` — refused with exit code 2 before credential construction, as required. The fake-transport dry-run was tested; no credential-backed dry-run, live writes, or network requests were run.

## 2026-11-27 — Align source attack regressions to the transport guard

- **Piece:** Removed all xfails from the 831 discovery cases; accepted the three read-only POST endpoints in the leak classifier; counted `MediaUpload` refusal at request construction; removed bare `auth._http()` because it carries no source credentials. Updated the route findings to reflect the merged guard.
- **Files:** `tests/test_source_write_attacks.py`, `source_attack_probe.py`, `docs/tasks/source-guard-redteam.md`, `docs/tasks/codex-pieces.md`.
- **Tests and result:**
  - `.venv/bin/python -m pytest --runxfail -o addopts=--tb=short tests/test_source_write_attacks.py -q` — **847 passed** in 5.40s.
  - `.venv/bin/python -m pytest -o addopts=--tb=short tests/test_source_write_attacks.py tests/test_source_transport_guard.py -q` — **878 passed** in 6.92s.
- No credential-backed dry-run or live probe was run; no network requests were made.


# Codex worktree pieces

## Phase binding analyzer and telemetry

- **Piece:** Read-only, per-account phase analysis with measured limiter, CPU, line, and worker-slot inputs. Missing signals stay `UNKNOWN`; they do not produce a `NOTHING` verdict.
- **Files:** `phase_binding.py`, `metrics.py`, `main.py`, `db.py`, `verify_sample.py`, `tally.py`, `split_mail.py`, `dms_light_proof.py`, `data-generator/seed_sandbox.py`, `tests/test_phase_binding.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**. `.venv/bin/python -m py_compile phase_binding.py sampler.py metrics.py main.py tally.py split_mail.py dms_light_proof.py verify_sample.py db.py data-generator/seed_sandbox.py tests/test_phase_binding.py tests/test_sampler.py` — **passed**.
- **Result:** Crafted ledger and metric/sampler series cover Google quota, line, CPU, NOTHING, and incomplete evidence. Worker occupancy now records busy user/task slots in migration, verification, tally, DMS reconciliation, and seeding pools. Seed metrics are persisted as bounded snapshots and retain a per-run summary.

## Sampler and live runbook

- **Piece:** Add the standalone Linux JSONL CPU/interface sampler and document how to capture/analyze one account run, the verdict thresholds, evidence limits, historical baseline rates, and the inline test metric sample.
- **Files:** `sampler.py`, `tests/test_sampler.py`, `data-generator/test_seed_sandbox.py`, `docs/tasks/phase-binding.md`, `docs/tasks/synthetic-10-plan.md`, `docs/tasks/codex-pieces.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**; `.venv/bin/python -m pytest data-generator/test_seed_sandbox.py::test_seed_metrics_persist_the_sample_series_and_worker_slots -q` — **1 passed**. The existing regression selection across metrics, verifier, tally, DMS light proof, and split-mail tests also exited **0**.
- **Result:** Baseline rates are 373.9 Drive files/min, 102.9 DMS messages/min, and 830 DMS-check messages/min. Historical and inline fixture metrics lack enough evidence for a binding verdict, so the analyzer reports UNKNOWN.

## Limiter evidence attribution

- **Piece:** Keep cumulative limiter rejections phase-local; an unbased counter or empty event list cannot claim a quota result or a clean NOTHING verdict.
- **Files:** `phase_binding.py`, `tests/test_phase_binding.py`, `docs/tasks/phase-binding.md`, `docs/tasks/codex-pieces.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**.
- **Result:** Exact rejection counts require a counter delta across a phase boundary. An observed limiter backoff remains positive quota evidence when its exact rejection count is unavailable. Missing baseline evidence stays UNKNOWN.

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
- Tests: `.venv/bin/python -m pytest data-generator/test_profile_seed.py -q --tb=short` — **35 passed** at the P4.3 checkpoint. Final offline plan command reported 300/300 seats and 269 new accounts, priced all Gmail settings on 299 mailboxes, and refused at 384.3 h against the default 12-hour cap. The 10 GiB-tail plan with `--max-hours 360` exited successfully at 360.0 h.

## P4.4 — hard-stop resume coverage for every profile service

- Fake services can now raise a hard-stop `BaseException` after a chosen number of successful calls. Resume tests kill and restart profile seeds during Drive, Gmail, Calendar, Chat, Contacts, Tasks, and Gmail settings; they verify stable IDs/markers and no duplicate resources. Gmail settings checks forwarding addresses and delegates before creating them on replay, and Chat resumes by reusing its named spaces and messages.
- Fixed secondary-calendar planning so replay skips cannot shift later calendar names and duplicate calendar events.
- Files: `tests/fakes.py`, `data-generator/test_profile_seed.py`, `data-generator/seed_sandbox.py`.
- Tests: `.venv/bin/python -m pytest data-generator/test_profile_seed.py -q --tb=short` — **35 passed** at the P4.4 checkpoint. The final full generator suite, including added Chat and Gmail-settings kill/restart cases, completed at 100% without reported failures.

## P4 integration — generator suite and guard fixture

- Aligned the generator suite's sandbox fixture with the repository suite by isolating the cross-account source registry from the unavailable shared control-plane database. Updated the outside-link regression to require an explicitly outside-owned target.
- Files: `data-generator/conftest.py`, `data-generator/test_profile_sharing.py`.
- Tests: focused guard/attachment/link regressions passed. `.venv/bin/python -m pytest data-generator/ -q --tb=short` completed at 100% with no reported failures.

## P5.2–P5.3 — 300-user VM runbook

- Added the ordered VM preflight, sandbox wipe, offline plan review, live seed, seven benchmark/fact checks, identity initialization, and coverage audit. Recorded the exact user-corrected plan-only command and expected default-budget refusal, along with the expanded 10 GiB-tail budget command and the required benchmark evidence.
- Files: `docs/tasks/seed-300-runbook.md`.
- Tests: both local offline plan commands were exercised after the final budget changes (default command: expected budget refusal; expanded 10 GiB-tail command: exit 0 at 360.0 h). VM execution is intentionally left to Claude on the owned VM.

## P4 integration follow-up — service-only seeding compatibility

- Made secondary-calendar metric aggregation tolerate absent optional counters returned by a minimal test double. This keeps service-only and full-seed reporting compatible.
- Files: `data-generator/seed_sandbox.py`.
- Tests: `.venv/bin/python -m pytest tests/test_seed_one_service.py -q --tb=short` — **19 passed**; `.venv/bin/python -m pytest data-generator/ -q --tb=short` completed at 100% with no reported failures.

## P4/P5 integration follow-up — profile CLI cohort and settings budget

- Profile `--users` is a total seat count, so it no longer filters the selected cohort as if `300` were a cohort local-part. The complete budget includes Gmail settings across all 299 non-admin mailboxes (writes and existence reads). The live runbook explicitly leaves Chat off, as the plan requires.
- Files: `data-generator/profile_plan.py`, `data-generator/seed_sandbox.py`, `data-generator/test_profile_seed.py`, `data-generator/test_seed_sandbox.py`, `tests/fakes.py`, `data-generator/test_seeder_coverage.py`, `docs/tasks/seed-300-runbook.md`.
- Tests: focused profile/sharing/coverage and cohort-selection regressions completed at 100% without reported failures; `.venv/bin/python -m pytest data-generator/ -q --tb=short` completed at 100% without reported failures. `.venv/bin/python -m pytest tests/test_seed_one_service.py -q --tb=short` — **19 passed**.

## Repository-suite verification

- Ran `.venv/bin/python -m pytest tests/ -q --tb=short` separately from `data-generator/`; the completed run reported **24 failures and 4 setup errors**. It included host sandbox `PermissionError` for loopback binds and `ps` subprocess calls, a host RAM probe returning 0.0 GB, and two service-only seeder failures. Those two seeder failures pass in the focused 19-test rerun above.
- A second full `tests/` run on the final code state was interrupted when the usage limit reset the active session at 51%; it produced no final report. VM commands and benchmarks remain unverified on Claude's VM by design.

## P5 runbook follow-up — live seat preflight

- Added a read-only source Directory check before the wipe/seed steps. It verifies the admin plus 30 cohort/twin accounts exist, that `p001`–`p269` are unused, and that the planned union is exactly 300 seats; it reports only counts on mismatch.
- Files: `docs/tasks/seed-300-runbook.md`.
- Tests: offline plan commands remain passing as recorded above. The Directory preflight itself requires Claude's VM credentials and has not been run locally.
