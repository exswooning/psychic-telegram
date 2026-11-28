# Dharan migration finding and failure kinds

Counts below are the sanitized 27 Nov 2026 snapshot supplied for account 7. Identifiers, names, addresses, and message contents are intentionally omitted.

## Read-only classifier replay

On the first replay, finding `unclassified` counts fell from 1,067 to 981 and run-failure `unclassified` counts fell from 25 to 0. Of the remaining 981 findings, 948 were Gmail `notCopied` rows with the same sanitized different-content Message-ID error. `classify()` wrapped these items as `{"notCopied": item}`; the run-failure classifier was not given the nested `type` (`message`) and `error`. `classify()` now sends that nested pair directly to `classify_failure()`, and classifier version 3 reclassifies historical rows in a bounded batch of up to 5,000. The exact sanitized row has an enqueue-and-ledger regression test. A new live replay was not run because network access is prohibited for this task.

## One-to-one findings

| Family | Count | Kind | Automatic handling / person’s next step |
|---|---:|---|---|
| Not copied, but one target copy has matching content | ~490 | `drive.copy_unrecorded` | Compare fresh source and target content. Adopt and record the mapping only when exactly one candidate matches; zero or multiple candidates stay open with a reason for a person. A successful mapping closes the queue row. |
| External original shortcut missing or misplaced | ~500 | `drive.external_copy` | Create the marked shortcut on the target or move it to the mapped target folder. A combined fallback-sharing difference keeps this accepted family kind; fallback review is still reported separately. |
| External fallback has grants that need review | Included in the external family; 66 retained-fallback repair errors | `drive.external_copy_review` | Leave original and fallback unchanged. A person reviews the target fallback’s grants; shortcut repair can still complete independently. |
| Native Sheet structures differ / extra target protected-range editors | 22 + 21 | `drive.native_structure` | Remove only target-only protected-range editors with a Sheets API update on the target, after confirming the remaining structure matches. Other structure differences stay listed for a person. |
| Form title differs | 11 | `drive.form_title` | Set the target Form title from a fresh source read, then verify it. The source is read-only. |
| Gmail `notCopied` queue row carrying the different-content Message-ID error | 948 | `gmail.reset_leftover` | Classify the nested item by its item type and error text; needs a person to compare the raw messages and reconcile the mapping. |
| Apps Script project files or code differ | Part of 16 Drive differences | `drive.script_project` | Listed for a person. The current target grant has no Apps Script project-write scope; compare and reconcile the project source on the target. |
| Form file-upload question was not carried | Part of 16 Drive differences | `drive.form_upload_question` | Listed for a person. Recreate and verify it in the target Form because the Forms API cannot create that question type. |
| Sheet tabs differ and `gid` references must survive | Part of 12 Drive repair errors | `drive.sheet_tabs` | Listed for a person. Compare tabs and preserve existing `gid` references while reconciling them. |
| External original’s visible source parent has no target mapping | Part of 12 Drive repair errors | `drive.external_copy` | Retry shortcut placement when the parent mapping is available. If it is still absent, list the source parent mapping as the person’s next step. |

## Run failures

| Failure text family | Count | Kind | Handling / person’s next step |
|---|---:|---|---|
| Gmail raw read returned 404 after the message was listed | 22 | `gmail.source_gone` | Record `SOURCE_GONE`; it is no longer a failed item because there is no source content to migrate. Existing matching failed rows are reconciled the same way. |
| More than one unclaimed destination message could match | 3 | `gmail.destination_ambiguous` | Do not choose or insert automatically. A person inspects target messages with that Message-ID, resolves the duplicate or mapping, then retries. |
| Destination has the same Message-ID but different content | 923 | `gmail.reset_leftover` | Reclassified from automatic to needs-person. Automatic retry was filtered out because retrying cannot safely resolve an existing content conflict. A person compares source and destination raw messages and reconciles the mapping before retrying. |

## Migration scope

| Finding scope | Outcome |
|---|---|
| Service absent from the account’s recorded migration scope | Close as `OUT_OF_SCOPE` with a durable reason. For Gmail on a Drive-only account, the reason says Google’s DMS handles the mailbox. If a later run adds Gmail to scope, requeue those rows. |

## Additional sanitized evidence still needed

The replay after the first classifier changes left 2 Drive `notCopied` findings, 1 Calendar error, 1 Directory difference, and 1 Gmail difference without their text. These five rows still need their sanitized `error`/`diff` strings before a specific kind can be named safely. No IDs, names, addresses, or error wording have been invented.

## Verification

The finding handlers and Gmail repairs were exercised against isolated API fakes and ledger rows; no live tenant, network, SSH, or deployment was used. Focused checks passed, including campaign completeness, classifier batching, lint, scope reconciliation, repair fakes, and retry regressions. A prior full-suite attempt exposed sandbox limitations in process inspection, local socket binding, and the RAM probe; it was not rerun after the focused fixes.

```text
.venv/bin/python -m pytest tests/test_campaign.py tests/test_escalation_recovery.py tests/test_resource_keys.py tests/test_lint.py tests/test_known_issues.py tests/test_known_issues_run_kinds_dns.py tests/test_repair_scope_services.py tests/test_government_finding_repairs.py tests/test_government_run_failure_repairs.py tests/test_missing_mail_recovery.py tests/test_verification_repairs.py tests/test_retry_failed.py tests/test_retry_failed_drafts.py -q -rA
```
