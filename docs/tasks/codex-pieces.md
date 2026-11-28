# Codex sweep pieces — 2026-11-27

## Mailbox completion requires a successful draft inventory

- **Piece:** Treat a failed target draft inventory as incomplete mailbox coverage instead of logging and continuing.
- **Files:** `migration_checks.py`, `tests/test_migration_checks.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_migration_checks.py::test_target_draft_inventory_failure_keeps_mailbox_completion_incomplete -q` (failed before the fix; passed after).
- **Result:** A target draft listing failure is now included in `errors`, so `mailbox_completion()` cannot mark Gmail complete without checking mapped drafts.

## Bound Gmail conversation-lock memory

- **Piece:** Replace the per-conversation lock dictionary with 256 lock stripes so lock storage stays fixed-size on large mailboxes.
- **Files:** `gmail_engine.py`, `tests/test_gmail_threads.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_gmail_threads.py::test_conversation_lock_storage_is_bounded -q` (failed before the fix at 10,000 retained locks); `.venv/bin/python -m pytest tests/test_gmail_threads.py -q` (passed).
- **Result:** Messages in one source conversation still share a lock; unrelated conversations can collide on a stripe and serialize briefly.

## Fail closed when source protection cannot be read

- **Piece:** Make the `AuthorizedHttp` fallback guard refuse writes when the protected-domain registry is unavailable.
- **Files:** `auth.py`, `tests/test_source_transport_guard.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_source_transport_guard.py::test_registry_failure_does_not_disable_the_direct_client_source_guard -q` (failed before the fix); `.venv/bin/python -m pytest tests/test_source_transport_guard.py -q` (passed).
- **Result:** A direct delegated client can no longer write as a non-government source subject during a registry failure; explicitly marked target and sandbox writer clients retain their existing bypasses.

## Stop tally scans on repeated page tokens

- **Piece:** Detect repeated pagination tokens in Gmail, Calendar, Contacts, Tasks, and draft count/list loops so a broken API page cannot spin indefinitely.
- **Files:** `tally.py`, `tests/test_tally.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_tally.py::TestCounting::test_repeated_mail_page_token_stops_instead_of_counting_forever -q` (failed before the fix); `.venv/bin/python -m pytest tests/test_tally.py -q` (passed).
- **Result:** The count now raises a visible runtime error on a repeated token; `count_side()` records it as unknown rather than reporting a completed count.

## Stop repeated tokens in migration inventories

- **Piece:** Detect repeated pagination tokens in Gmail message listing, Drive folder listing, and Drive discovery; also guard the Gmail search/draft and other Drive inventories that share those loops.
- **Files:** `pagination.py`, `gmail_engine.py`, `drive_engine.py`, `discovery.py`, `tests/test_repeated_pagination.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_repeated_pagination.py -q` (all three cases failed before the fix; passed after).
- **Result:** A repeated token now raises a visible runtime error instead of driving an unbounded API loop. Gmail/Drive inventory failures remain migration failures, never clean completion.

## Bound Gmail split-mail preflight memory

- **Piece:** Replace the full-mailbox reference list and classification dictionary with a second paginated read and batched SQLite scratch rows.
- **Files:** `gmail_engine.py`, `tests/test_gmail_split_scan_memory.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_gmail_split_scan_memory.py -q` (failed before the fix with 1,200 retained classifications; passed after).
- **Result:** The entire mailbox still completes classification before the first delivery, while retained preflight state is limited to a 500-row write batch and bounded worker queues.

## Keep failed service inventories incomplete

- **Piece:** Propagate failed target Contacts, Contact Groups, Tasks, Chat, and secondary Calendar/ACL inventories so the orchestrator records a service failure; reject repeated pages before yielding their contents.
- **Files:** `contacts_engine.py`, `tasks_engine.py`, `chat_engine.py`, `calendar_engine.py`, `gmail_engine.py`, `drive_engine.py`, `discovery.py`, `migration_checks.py`, `tests/test_service_failure_completion.py`, `tests/test_repeated_pagination.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py -q` (six cases failed before the fix); `.venv/bin/python -m pytest tests/test_repeated_pagination.py -q` (four new pager cases failed before the fix); `.venv/bin/python -m pytest tests/test_service_failure_completion.py tests/test_repeated_pagination.py -q` (13 passed after).
- **Result:** An unreadable source or target inventory can no longer become empty success, a missing mapped contact is not silently skipped when target inventory is unavailable, and duplicate page contents are not handed to migration writers.

## Keep failed Tasks recopy checks open

- **Piece:** Record a failed source-task inventory as a failed list when the mapped target task list is gone, instead of returning with zero failed counts.
- **Files:** `tasks_engine.py`, `tests/test_service_failure_completion.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py::test_unreadable_tasks_before_recopy_is_counted_as_failure -q` (failed before the fix with `failed == 0`; passed after).
- **Result:** The Tasks service remains due when a missing target list cannot be safely rebuilt from source evidence.

## Read prior Gmail link evidence without loading it all

- **Piece:** Replace the pre-delivery scanner's whole-inventory `dict` with indexed SQLite lookups and a count query.
- **Files:** `mail_dead_links.py`, `gmail_engine.py`, `tests/test_gmail_split_scan_memory.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_gmail_split_scan_memory.py::test_split_preflight_does_not_materialize_a_completed_link_inventory -q` (failed before the fix); `.venv/bin/python -m pytest tests/test_gmail_split_scan_memory.py tests/test_mail_link_search.py -q` (15 passed after).
- **Result:** A completed prior link inventory no longer adds mailbox-sized Python state to the split-mail preflight.

## Spool Gmail search candidates to SQLite

- **Piece:** Stream search result pages into a per-run candidate table; calculate recall and membership with SQL instead of mailbox-sized sets.
- **Files:** `gmail_engine.py`, `tests/test_gmail_split_scan_memory.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_gmail_split_scan_memory.py::test_search_mode_spools_candidate_ids_instead_of_building_mailbox_sets -q` (failed before the fix); `.venv/bin/python -m pytest tests/test_gmail_split_scan_memory.py::test_search_mode_spools_candidate_ids_instead_of_building_mailbox_sets tests/test_mail_link_search.py -q` (14 passed after).
- **Result:** Search mode now keeps candidates, drafts, chats, and known-link recall on disk, while preserving the existing candidate totals and recall figures.

## Stop loops in group and subscription inventories

- **Piece:** Detect repeated tokens in directory group/member inventories and stop Calendar subscription inventories from swallowing a failed read as zero subscriptions.
- **Files:** `groups_engine.py`, `calendar_engine.py`, `tests/test_repeated_pagination.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_repeated_pagination.py::test_calendar_subscription_inventory_stops_on_a_repeated_token tests/test_repeated_pagination.py::test_directory_group_inventory_stops_on_a_repeated_token -q` (both failed before the fix); `.venv/bin/python -m pytest tests/test_repeated_pagination.py tests/test_groups_engine.py tests/test_calendar_fidelity.py -q` (30 passed after).
- **Result:** These tenant-level scans stop on a repeated page token; subscription inventory failures reach the caller as failures instead of reporting no subscriptions.

## Do not create Tasks without target stray evidence

- **Piece:** Stop task creation when the target task-list inventory cannot be read, rather than treating the list as having no unledgered copies.
- **Files:** `tasks_engine.py`, `tests/test_service_failure_completion.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py::test_unreadable_target_task_inventory_does_not_create_duplicate_tasks -q` (failed before the fix; passed after).
- **Result:** A transient target read failure cannot turn a prior create-before-ledger interruption into a duplicate task.

## Do not duplicate Drive copies when target inventory fails

- **Piece:** Propagate failure from Drive's target inventory used to adopt an unledgered copy instead of continuing with an empty index.
- **Files:** `drive_engine.py`, `tests/test_service_failure_completion.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py::test_unreadable_drive_target_inventory_does_not_copy_an_unledgered_file -q` (failed before the fix; passed after).
- **Result:** A failed target read now stops the adoption path before it copies a possible existing target file again.

## Stop Drive moves when staging inventory is unknown

- **Piece:** Propagate staging-drive inventory failures so a stranded target copy is never treated as absent before the next copy/move.
- **Files:** `drive_engine.py`, `tests/test_service_failure_completion.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py::test_unreadable_staging_inventory_does_not_ignore_a_stranded_copy -q` (failed before the fix; passed after).
- **Result:** Move recovery now stops when it cannot inspect the staging drive and safely adopt a prior copy.

## Cache Drive adoption inventory failures

- **Piece:** Memoize a failed Drive target-adoption inventory so concurrent file workers do not retry the mailbox-wide listing once per file.
- **Files:** `drive_engine.py`, `tests/test_service_failure_completion.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_service_failure_completion.py::test_unreadable_drive_target_inventory_does_not_copy_an_unledgered_file -q` (failed before the cache: two failed target listings; passed after with one).
- **Result:** The failure remains visible for each item, with one bounded inventory attempt per user.

## Complete and guard read-only inventory pages

- **Piece:** Stop repeated tokens in tenant-wide and pre-migration inventories, and page through every Calendar list instead of counting only its first page.
- **Files:** `tenant_verify.py`, `inventory.py`, `tests/test_tenant_verify.py`, `tests/test_inventory.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_tenant_verify.py::test_tenant_inventory_stops_on_repeated_page_token tests/test_inventory.py::TestScanDrive::test_repeated_inventory_page_token_is_an_error tests/test_inventory.py::TestScanDrive::test_calendar_inventory_follows_every_calendar_page tests/test_inventory.py::TestDraftCounting::test_repeated_draft_page_token_is_an_error -q` (all four failed before the fix); `.venv/bin/python -m pytest tests/test_tenant_verify.py tests/test_inventory.py -q` (31 passed after).
- **Result:** Inventory counts either finish across all pages or raise on a repeated token; they no longer silently omit Calendar pages or spin forever.

## Avoid quadratic tenant profile comparisons

- **Piece:** Build the source-to-target identity translation once per tenant user check instead of rebuilding the full map for every source user.
- **Files:** `tenant_verify.py`, `tests/test_tenant_verify.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_tenant_verify.py::test_tenant_user_verification_builds_identity_translation_once -q` (failed before because each user received a different rebuilt mapping); `.venv/bin/python -m pytest tests/test_tenant_verify.py -q` (passed after).
- **Result:** Profile comparison setup scales linearly with mapped user count rather than repeatedly traversing the whole identity map.

## Keep uncertain tally evidence out of COMPLETE

- **Piece:** Record draft inventory failures and require successful per-item evidence plus known counts before a per-user tally can report `COMPLETE`.
- **Files:** `tally.py`, `db.py`, `tests/test_tally.py`, `tests/test_tally_items.py`, `tests/test_tally_exact_copy.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_tally_items.py::test_unreadable_draft_inventory_cannot_roll_up_as_complete -q` (failed before as `COMPLETE`); `.venv/bin/python -m pytest tests/test_tally.py::TestCounting::test_draft_inventory_failure_is_recorded_not_discarded -q` (failed before because the error was discarded); `.venv/bin/python -m pytest tests/test_tally_exact_copy.py::test_an_uncounted_required_service_keeps_the_user_unknown -q` (failed before as `COMPLETE`); `.venv/bin/python -m pytest tests/test_tally_exact_copy.py tests/test_tally_items.py tests/test_tally.py -q` (passed after).
- **Result:** A draft inventory failure or required-service count error produces `UNKNOWN` when the other measured counts happen to match; known shortages and differences retain their existing verdicts.

## Stream the Drive stray inventory

- **Piece:** Yield Drive target stray pages one at a time instead of retaining every target metadata row before comparison.
- **Files:** `verify_sample.py`, `tests/test_verify_drive_batches.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_verify_drive_batches.py::test_target_stray_inventory_yields_one_page_at_a_time -q` (failed before because both pages were loaded before the first yield); `.venv/bin/python -m pytest tests/test_verify_drive_batches.py -q` (passed after).
- **Result:** Stray detection preserves full coverage and repeated-token errors while limiting this inventory's retained page data to one API page.

## Preserve MIME-part identity in mail verification

- **Piece:** Compare the canonical MIME tree, part headers, and decoded bodies so attachment metadata and multipart changes cannot be called `equivalent`; omit Gmail-regenerated envelope headers only in the draft comparator, which still compares person-authored fields and MIME content.
- **Files:** `verify_sample.py`, `tests/test_verify_sample.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_verify_sample.py::TestCompareMessage::test_attachment_metadata_is_part_of_message_identity -q` (failed before: attachment filename change returned `equivalent`); the four draft and DMS duplicate regressions reported by Claude all failed after the initial MIME change; `.venv/bin/python -m pytest tests/test_verify_sample.py tests/test_split_mail_duplicates.py -q` (passed, exit 0, including the new strict message and draft attachment tests).
- **Result:** Ordinary mail verification remains strict about attachment filenames and MIME structure. Draft verification ignores regenerated Message-ID, Date, and From in the MIME envelope while still checking mapped From, encoded/decoded Subject, recipients, body, and attachments, restoring DMS duplicate reconciliation.
