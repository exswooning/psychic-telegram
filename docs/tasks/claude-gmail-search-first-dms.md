# Claude task: Gmail search-first link rewriting, then DMS

Status: TODO — queued for Claude; not implemented or deployed by adding this task.
Priority: High
Requested by the operator on 30 October 2026.

## Outcome

Replace the routine full-mailbox content scan with Gmail server-side candidate search. Inspect/rewrite only candidate messages, then hand off to Google DMS. Keep count-based routine completion and a separately selectable exhaustive scan/full 1:1 verification.

The existing full scanner reads every message's MIME payload. This is avoidable work when only a subset has Drive references. Do not interpret “scan every email for links” as a requirement to fetch every body when Google's search can preselect candidates.

## Required flow

- [ ] Finish Drive mapping first, preserving cross-user link lookup and the selected server-side copy method.
- [ ] Use `users.messages.list(q=..., includeSpamTrash=True)` for a broad UNION of supported candidate searches. Start by qualifying `{has:drive has:document has:spreadsheet has:presentation "drive.google.com" "docs.google.com"}` against every URL shape supported by `link_rewrite.py`. This is a candidate query, not a guarantee of exhaustive URL detection.
- [ ] Paginate every result page, deduplicate message IDs across searches, preserve user/account/run scope and delta bounds. `resultSizeEstimate` is not an exact count. Search support differs from Gmail's thread-oriented UI.
- [ ] Fetch MIME/text only for the resulting candidates. Reuse the existing decoded MIME classifier and mapped-link rewriter; do not download ordinary non-candidate message bodies or binary attachments merely to classify them.
- [ ] Classify and prepare the selected candidate set before content delivery. Copy only messages whose mapped links actually change. Preserve source messages, headers/date handling, labels, target data and duplicate protections.
- [ ] Keep unresolved, unreadable and encrypted candidates visible. Do not treat source Drive 403/404 as proof that a link is external, or resolve blockers simply because Gmail search reduced the candidate set. Existing missing-file/mapping issues need explicit resolutions.
- [ ] Start the existing DMS handoff only when its admission requirements are met. DMS imports the mailbox, including potential overlap with existing/rewritten copies; it does not accept an arbitrary list of locally rewritten MIME messages or excluded source IDs. Test overlap rather than assume deduplication. No source-body editing and no source label writes as an indexing shortcut.
- [ ] Use mailbox counts for routine completion, with clear source/target totals, existing target mail, drafts/spam/trash, rejected items and count differences. Matching counts must never be labelled full 1:1, adopted as per-message mappings, or used to clear unresolved content/access findings.
- [ ] Expose “Search-first (fast)” and “Exhaustive link scan” choices in the new UI/API. Keep standalone full per-item verification. No hidden fallback to full body scanning for every ordinary run; show any coverage limitation explicitly.

## Qualification and acceptance

- [ ] Validate candidate search recall against independently known link-bearing IDs from the existing strict scan or an isolated read-only sandbox census. Document any missed forms. `has:drive` alone is not a universal “contains URL” label.
- [ ] Add isolated fixtures for plain/HTML links, hidden anchor text, quoted-printable/base64 parts, attachments containing text, legacy URL shapes, folders, forms, external/unknown ownership, no matches, duplicate hits, pagination, interrupted scans, already mapped mail and malformed/unread results.
- [ ] Prove with transport call counts that a mailbox of M messages and N candidates reads only the N candidate bodies in fast mode (plus explicitly documented targeted overlap checks), while strict mode still examines M.
- [ ] Keep source protection, sample limits, existing target users/data, DMS admission and repair finding state intact. Do not mark untouched mail as individually verified.
- [ ] Measure search duration, exact candidate count, candidates examined/rewritten/blocked, body reads and bytes avoided, API calls, latency, retries, memory and count-check duration. Display phase progress and meaningful ETA availability/scope in the UI. Do not calculate total-job ETA from a partial candidate stage.
- [ ] Test the new UI choices and compatibility of queued/older launches. A sample remains bounded and must not expand into a full mailbox import.
- [ ] Use the new `/next/` UI for all live tenant diagnostics and operations; add missing operational visibility there. Terminal work is for local development and deployment infrastructure.
- [ ] Build and test locally, push the reviewed commit, then deploy the exact hash only when globally idle through unchanged `sync_vps.sh` safety gates. Retain rollback, original `/app/` UI, secrets/data and source protections. Never use `DEPLOY_OVER_JOBS` or `FORCE_RESTART`.
- [ ] Qualify live only on the authorized s. → t. sandbox and report the precise scope. Do not claim an IMAP relay exists or that a partial DMS overlap sample proves full fidelity.

## Current run and ownership

Runtime code release: `8bc97a5d54b38e401cecab022e7c91a9251ce5f5`.
The operator resumed account 3 as PID 3265024 on 30 October. Last UI observation: Gmail phase 2/3, 8,000 messages examined and 87 blocked (63 file-not-found errors, 24 unresolved-mapping errors; these are message findings, not unique file counts). This is historical context, not current status. Obtain fresh UI state. Do not stop, patch or deploy over that run merely to implement this task.

Implement without broadening beyond info@s.sarafgloabalexim.com → nest@t.sarafgloabalexim.com for sandbox qualification. No target wipe or extra user provisioning. See `docs/claude-handoff-2026-10-30.md` and root `AGENTS.md`.

## Entry points

- `gmail_engine.py`: `_iter_messages`, `_split_classify`, `_scan_split_before_delivery`, `_migrate_one_message`, `run`
- `split_mail.py`, `link_rewrite.py`, `mail_completion.py`, `migration_checks.py`
- `api_server.py`: `StartMigration`, `_mail_plan`, `_default_mail_mode`, `_start_dms`
- `dms_migrate.py`: completion policy
- `migration-webui/src/pages/MigrationDetail.tsx`, `Metrics.tsx`, `components/RunningJobDetail.tsx`, `api/controlPlane.ts`
- Existing split safety, DMS handoff, count-completion and UI regression tests.

## Primary references

- Gmail search/filter API: https://developers.google.com/workspace/gmail/api/guides/filtering
- Message listing and pagination: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- Supported Gmail search operators: https://support.google.com/mail/answer/7190
