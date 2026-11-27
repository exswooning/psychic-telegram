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
