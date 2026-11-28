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

# Codex implementation pieces

## Piece 1 — finding/run kind classification and migration scope

- **Files:** `known_issues.py`, `verification_repairs.py`, `parity_gate.py`, `parity_deep_audit.py`, `repair.py`, `verify_sample.py`, `api_server.py`, `tests/test_known_issues.py`, `tests/test_known_issues_run_kinds_dns.py`, `tests/test_repair_scope_services.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py -q` — passed (32 tests).
- **Result:** Added the requested Drive and Gmail finding kinds, corrected the nonconverging reset conflict to needs-person, and made Drive-only scope close Gmail queue rows as `OUT_OF_SCOPE` with the DMS reason. Reopening Gmail scope requeues those rows. Historical classification now processes the supplied live batch in one bounded pass.

## Piece 2 — target-only Drive finding repairs

- **Files:** `known_issues.py`, `finding_repairs.py`, `external_access.py`, `verification_repairs.py`, `tests/test_known_issues.py`, `tests/test_known_issues_run_kinds_dns.py`, `tests/test_government_finding_repairs.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_verification_repairs.py tests/test_known_issues_are_recorded.py -q` — passed (68 tests; fake APIs and ledger only).
- **Result:** A unique fresh content match is adopted into the ledger; multiple or zero matches remain for a person. External original shortcuts are created or moved on the target while fallback grants needing review are listed. Sheet repair removes only extra target protected-range editors after proving other structure matches; Form title repair writes only the target. Combined shortcut and fallback findings retain the accepted external-copy kind, while fallback-only findings need a person.

## Piece 3 — named Gmail run failures

- **Files:** `known_issues.py`, `gmail_engine.py`, `repair.py`, `tests/test_known_issues_run_kinds_dns.py`, `tests/test_government_run_failure_repairs.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_verification_repairs.py tests/test_known_issues_are_recorded.py -q` — passed (68 tests; fake APIs and ledger only).
- **Result:** Source raw-read 404s after listing are recorded as `SOURCE_GONE`, not failed, including reconciliation of existing matching failures. Ambiguous destination matches and different-content reset leftovers are named needs-person outcomes with concrete next steps, surfaced in the repair summary, and excluded from automatic retries.

## Piece 4 — reclassify nested not-copied failures and name unsupported Drive cases

- **Files:** `known_issues.py`, `finding_repairs.py`, `external_access.py`, `verification_repairs.py`, `tests/test_known_issues.py`, `tests/test_government_finding_repairs.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_verification_repairs.py tests/test_known_issues_are_recorded.py tests/test_retry_failed.py tests/test_retry_failed_drafts.py -q` — passed.
- **Result:** A `notCopied` row with an item `type` and `error` now uses the run-failure classifier, and classifier version 3 reprocesses older queue rows. Run kinds found in the one-to-one queue appear by name in its summary. Added person-directed kinds and repair output for Apps Script source differences, Forms file-upload questions, Sheet tab/gid conflicts, and external shortcut placement blocked on a missing parent mapping.

## Piece 5 — block the remaining automatic Gmail conflict retry

- **Files:** `repair.py`, `tests/test_government_run_failure_repairs.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_missing_mail_recovery.py tests/test_verification_repairs.py tests/test_government_run_failure_repairs.py tests/test_retry_failed.py tests/test_retry_failed_drafts.py -q` — passed.
- **Result:** The verification missing-mail recovery path now rejects `gmail.reset_leftover` rows as well as `retry_failed`; the fake-ledger check confirms no target message insert is attempted for the different-content conflict.

## Piece 6 — make stranded Gmail retries follow the failure classifier

- **Files:** `retry_failed.py`, `tests/test_retry_failed.py`, `tests/test_retry_failed_drafts.py`, `tests/test_government_run_failure_repairs.py`, `tests/test_missing_mail_recovery.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_retry_failed.py tests/test_retry_failed_drafts.py tests/test_government_run_failure_repairs.py tests/test_missing_mail_recovery.py -q` — passed.
- **Result:** Failed messages and drafts are filtered by the named needs-person kinds rather than exact error-string equality, so wrapped/extended ambiguous and different-content errors cannot slip back into automatic retry.

## Piece 7 — close campaign coverage and preserve regression contracts

- **Files:** `campaign_scenarios.py`, `known_issues.py`, `finding_repairs.py`, `verification_repairs.py`, `tests/test_campaign.py`, `tests/test_escalation_recovery.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_campaign.py tests/test_escalation_recovery.py tests/test_resource_keys.py tests/test_lint.py tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_missing_mail_recovery.py tests/test_verification_repairs.py tests/test_retry_failed.py tests/test_retry_failed_drafts.py -q -rA` — passed (exit 0).
- **Result:** Added a campaign scenario and covering test path for each new kind, kept native-structure’s existing user-facing title stable, retained bounded classifier batches in the batch regression, and removed unused locals flagged by lint. The run’s progress reached 100% with no failures.

## Piece 8 — verify queued findings close after target repairs

- **Files:** `tests/test_government_finding_repairs.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_known_issues.py tests/test_campaign.py -q` — passed (exit 0).
- **Result:** The native Sheet and Form fake repairs now enqueue their sanitized findings, verify target-only changes, then simulate a clean follow-up check and assert the queue closes. The combined external shortcut/fallback row is asserted as accepted `drive.external_copy`, while its unreviewed fallback grants remain open with the person-review reason.

# Codex work pieces for review

## Piece 1 — changed-after target cases (3/9)

- Implemented `gmail.labels_missing`, `drive.mtime` and `drive.missing_on_target`: each has an explicit baseline migration act, a guarded target-side change, and a verification/repair or gate judgment. The runner now performs baseline → change → verify in that order and checks listed outcomes as well as automatic fixes.
- Files: `campaign.py`, `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 3 — source-side changes (3/9)

- Implemented `gmail.unmapped_source`, `drive.content_changed` and `contacts.unmapped_source`. Each now has an explicit baseline migration, a domain-guarded write to a., and its verify/repair, delta, or listed-next-step judgment. All nine changed-after cases now have acts.
- Files: `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 4 — first seeded fixture acts (3 cases)

- Added explicit fixture registry names and product-path fixture preparation for `gmail.no_message_id`, `drive.embedded_images` and `tasks.untitled`. c04's new `mail_no_message_id_link` fixture is a Drive-link candidate, so it exercises the migration's split-mail path without moving bulk mail through the engine. The other two use the existing image Doc and empty-title task fixtures. Each scenario migrates and verifies its fixture user; the fake-driver test checks a PASS when handled and a FAIL when the finding is not handled.
- Files: `campaign.py`, `campaign_scenarios.py`, `campaign_evidence.py`, `data-generator/fixtures.py`, `data-generator/cohorts/fableplan2.json`, `data-generator/test_cohort.py`, `tests/test_campaign.py`, `tests/test_campaign_error_acts.py`, `tests/test_seed_one_service.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 106 tests**. `.venv/bin/python -m pytest data-generator/test_cohort.py -q` — **passed, 15 tests**.
- Tenant/live status: not run; offline fake-driver evidence only. Live fixture seeding uses the existing `/api/seed` product route with the source domain confirmation and `fixtures_only` cohort scope.

## Piece 2 — remaining target-side changes (3/9)

- Implemented `drive.grant_missing`, `contacts.missing_on_target` and `tasks.list_missing` with an explicit baseline migration, guarded target-side mutation, and the verify/repair or listed-next-step judge.
- Files: `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 5 — seeded Drive and comment acts (3 cases)

- Added explicit fixture preparation and scoped Drive migration/verification acts for `drive.shortcut_target`, `drive.comments` and `run.comment_differs`, using the existing c03 fixtures. The offline Driver test verifies each fixture is declared for its scenario owner, reaches PASS when handled and FAIL when not handled.
- Files: `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 107 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 6 — seeded sharing and read-limit acts (3 cases)

- Added fixture-backed Drive migration and verification acts for `drive.group_grant`, `drive.sheet_too_large` and `drive.comments_unreadable`. Tests tie each declared fixture to its cohort owner and exercise PASS plus unhandled FAIL through the fake Driver.
- Files: `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 107 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 7 — colleague shortcut and outside-owner boundary (2 cases)

- Added the fixture-backed `drive.item_absent` Drive act and its PASS/FAIL fake-Driver checks. Moved `drive.unmapped_source` to UNSEEDABLE: the exact outside-owned upload requires credentials outside the allowed a./b. write scope; its source probe remains covered by the named test.
- Files: `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `tests/test_campaign_evidence.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 107 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 8 — evidence-backed malware refusal (1 case)

- Added the `run.malware_flag` Drive act for c09. Its source probe now proves Google's exact `403 cannotDownloadAbusiveFile` response for the EICAR fixture; an existing file name alone cannot make this scenario pass. Added PASS/unhandled FAIL fake-Driver coverage.
- Files: `campaign_scenarios.py`, `campaign_evidence.py`, `tests/test_campaign_error_acts.py`, `tests/test_campaign_evidence.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 108 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 9 — Form image boundary (1 case)

- Moved `run.form_rebuild` to UNSEEDABLE. The current product fixture API can create text and choice items, but the known failure needs an image item already on the source Form; making that item requires a Forms-owner UI credential. The focused rebuild test remains its named coverage.
- Files: `campaign_scenarios.py`, `campaign_evidence.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 108 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 10 — old-domain SENT mail (1 case)

- Added the `mail_old_domain_sent` fixture on c05 and a Gmail-only engine migration/verification act for `gmail.labels_added`. Its source probe requires a SENT label and the exact source account in From, so another old-domain message cannot stand in for the case.
- Files: `data-generator/fixtures.py`, `data-generator/cohorts/fableplan2.json`, `data-generator/test_cohort.py`, `campaign_scenarios.py`, `campaign_evidence.py`, `tests/test_campaign_error_acts.py`, `tests/test_campaign_evidence.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 109 tests**. `.venv/bin/python -m pytest data-generator/test_cohort.py -q` — **passed, 16 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 11 — split-mail fixtures and DMS evidence (5 cases)

- Added a real same-domain peer-file link fixture for `gmail.link_owner_not_migrated` and `run.mail_owed_to_dms`; moved the no-Message-ID fixture to c06; removed fake missing/outside file IDs that blocked split scans. Added explicit split migration acts for `gmail.body_link_copy` and `gmail.link_scan_encrypted`. Duplicate and owed-to-DMS cases now use the product `/api/v2/dms/start` as a follow-up. The judge checks the relevant audit row and Gmail verdict; the fake Driver proves PASS with evidence and FAIL without handling.
- Files: `data-generator/fixtures.py`, `data-generator/cohorts/fableplan2.json`, `data-generator/test_cohort.py`, `campaign_scenarios.py`, `campaign_evidence.py`, `campaign.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 109 tests**. `.venv/bin/python -m pytest data-generator/test_cohort.py -q` — **passed, 17 tests**.
- Tenant/live status: not run; offline fake-driver evidence only. The DMS follow-up is only asserted through the fake Driver.

## Piece 12 — fixed-defect absence acts (4 cases)

- Added `gmail.link_scan_fixed`, `drive.sheets_scope`, `drive.office_compared_as_native` and `drive.comment_quote_escaped` acts. Each seeds its source case, runs the scoped migration and verification, and requires the finding to be absent while the source probe confirms the case exists. Added a missing-Drive-link fixture and a minimal DOCX fixture.
- Files: `campaign.py`, `campaign_scenarios.py`, `campaign_evidence.py`, `data-generator/fixtures.py`, `data-generator/cohorts/fableplan2.json`, `data-generator/test_cohort.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 109 tests**. `.venv/bin/python -m pytest data-generator/test_cohort.py -q` — **passed, 19 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 13 — Forms image rebuild act (1 case; replaces Piece 9 classification)

- Added `forms_with_image` using the Forms API's image-item `sourceUri`, a source probe that reads the named Form and requires its image item, and the scoped Drive migration/verification act. This provides an API fixture, so `run.form_rebuild` is seeded rather than UNSEEDABLE; Piece 9's earlier classification is superseded.
- Files: `data-generator/fixtures.py`, `data-generator/cohorts/fableplan2.json`, `data-generator/test_cohort.py`, `campaign_scenarios.py`, `campaign_evidence.py`, `tests/test_campaign_error_acts.py`, `tests/test_campaign_evidence.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py tests/test_campaign_evidence.py tests/test_seed_one_service.py -q` — **passed, 110 tests**. `.venv/bin/python -m pytest data-generator/test_cohort.py -q` — **passed, 20 tests**.
- Tenant/live status: not run; offline fake-driver evidence only. The Forms API image-item request is only checked against the fake API.
