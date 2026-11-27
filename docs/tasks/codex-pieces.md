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
