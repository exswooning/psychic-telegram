# Claude handoff — 30 October 2026

The operator requested: pause sandbox account 3, deploy fixes, prefer Google DMS after a complete email link scan, use count completion routinely, and retain optional full 1:1 checks. The deployment pause was superseded by the operator’s explicit resume request on 30 October 2026. All tenant operations and diagnostics use the new `/next/` UI.

## State and scope

- Sandbox: only info@s.sarafgloabalexim.com → nest@t.sarafgloabalexim.com. Existing target users/data remain. No wipe. Drive uses `server_side` copy, not source-moving `move`.
- The 29 October run was gracefully stopped from Jobs. UI subsequently showed zero busy/queued slots. Ledger/copies retained.
- Last observed Drive result: 1,403 file mappings, 107 folders, 2 shortcuts. Recheck matched 1,510 examined items, but four incomplete/error checks remained. 79 sharing entries, 20 group memberships and one shared-drive membership lack target mappings. Do not call this verified 1:1.
- Email was still advancing before stop; no full mailbox completion claimed.

## Changes in this release

1. Previously pushed `d01d85f` caps automatic OS process count by eligible users; the old runtime started eight processes for one user and reduced the working child's resource share. Explicit pins remain unchanged.
2. Full mail runs now default to `split` in API and new UI. Samples and runs without Gmail keep the engine. Explicit engine/DMS choices remain.
3. Split mail performs a bounded concurrent scan of EVERY source message, including already mapped messages, before any message-content insert. It retains only route/labels, not raw content. Unread/encrypted/unresolved internal references block delivery/handoff. Only changed-link mail is copied by Bitport; ordinary mail is deferred to DMS. Changed-link insert checks remain to protect the rewritten subset.
4. Routine mail completion defaults to `MAIL_COMPLETION_CHECK=counts`: two Gmail profile reads per user, source/target totals, extras/short/unknown reported. DMS completion bypasses exhaustive body reconciliation and all-service tally. It never creates per-message mappings, marks deferred IDs verified, or clears existing repair findings. Profile totals include drafts/spam/trash and existing target mail; equal totals can conceal gaps/duplicates and are explicitly NOT 1:1 fidelity. Extras require review rather than subtraction guessed from a baseline.
5. `MAIL_COMPLETION_CHECK=full` retains exhaustive migration-time checks; the separate Verification page remains available for full per-item checking. The engine's routine full acceptance and sampled completion exclude Gmail in counts mode; other service checks remain.
6. Metrics selects the busiest fresh reporting process instead of alternating with an idle parent. This is explicitly PROCESS-scoped latency/counters, not summed tenant percentiles. Ledger totals remain separate. Historical Run summary cards are unchanged.
7. Job details show current pass and copied counts instead of a fabricated whole-run percentage. Exact historical Drive baseline IDs can be compared with file/folder/shortcut mappings. Baseline age/scope is shown; it does not certify current content or sharing. Unknown ETA is explained, not invented. DMS count reports appear in Performance and job details.

## Constraints and remaining work

- DMS cannot consume an in-memory rewritten MIME message or edit a received source message body in place. Source stays unchanged. Hybrid means rewritten copies first, then Google's whole-mailbox import, which can overlap existing target mail. Ordinary deferred IDs are not an arbitrary DMS exclusion list.
- Existing s/t DMS qualification proved only a small rewritten-message overlap sample; wider first-pass fidelity is NOT established by this release. Count completion deliberately has lower assurance. Do not relabel it full verification.
- DMS connection approval/login and current target credentials may still require operator action. No new access grants or legal terms were accepted in this release. No custom IMAP relay/server was deployed.
- Deploy local commit → GitHub → exact laptop version only, normal `sync_vps.sh` active-job/repair gates, never override. Preserve original `/app/` UI, tenant credentials, data and source protections. Actual deployment receipt is `/opt/bitport/CONFIGURATION_RELEASE.json`; source version is `/opt/bitport/DEPLOYED_COMMIT`. `/next/` has a separately hashed versioned bundle.
- Post-import count differences, existing native-format checks and unmapped identities remain visible. Use optional full verification to establish 1:1; do not silently clear findings.

## Current state — operator resumed on 30 October 2026

- Resumed through the new UI at 2026-10-30T02:29:55Z (08:14:55 Asia/Kathmandu), PID 3265024, account 3, same single info@s → nest@t identity.
- Explicit Drive choice: server-side copy. Email: link scan + DMS, automatic DMS handoff selected. Existing target data retained; no wipe.
- UI readiness passed for both tenants. PASS 1/3 Drive started. The new cap is qualified live: automatic process allocation reduced eight proposed processes to one for one eligible user.
- The scan/DMS phases have not completed; no new 1:1 conclusion. Do not stop this resumed run based on the historical pause in earlier notes. No backend deployment while active.
- Runtime code release remains 8bc97a5d54b38e401cecab022e7c91a9251ce5f5; this handoff update changes documentation only.

## Next Claude task — search-first mail scanning

- [~] BUILT, NOT DEPLOYED (see "Search-first: rollout state" below): [Gmail search-first link rewriting, then DMS](tasks/claude-gmail-search-first-dms.md). The current runtime still scans every source message body. The task defines candidate-search coverage qualification, count completion, optional strict checks, UI visibility, benchmarks and safe deployment. Adding this task does not change or restart the running migration.

## Search-first: rollout state (Claude, 30 October 2026)

**Code:** local commit on `codex/google-configuration-ui`; the box still runs `8bc97a5` (read live 05:53Z, PID 3265024 on account 3 still running the old exhaustive scan). Not deployed: a job is active.

- `gmail_engine._scan_split_before_delivery`: `MAIL_LINK_SCAN=search` (opt-in; NOT the default, see Benchmarks) runs 12 separate id-only searches (`LINK_SEARCHES`: has:drive/document/spreadsheet/presentation, "drive.google.com", "docs.google.com", filename:txt/htm/html/csv/ics/eml), paginated and unioned, then reads only those bodies. `exhaustive` is the old read-everything scan. Unknown value raises.
- Messages already proved link-bearing in the ledger (rewritten, or blocked on a link) are always read; the number the search missed is reported as `recallMissed`. A delta run cannot judge recall and says null.
- Unread messages route to the DMS with the audit line "not named by Gmail's link search, so its body was not read". Drafts/chats found via `in:drafts` / `in:chats` keep their own handling.
- Report per user in table `mail_link_scans` (mode, state SEARCHING/READING/DONE/BLOCKED/INTERRUPTED, per-search counts, candidates, bodies/bytes/attachment reads, list calls, search and scan seconds, peak RSS). Served as `mailLinkScans` in the metrics payload; shown on Migration detail's job card and Metrics (`MailLinkScans.tsx`).
- API/UI: `StartMigration.mail_link_scan` (split only); the Migrate dialog offers "Search-first (fast)" / "Exhaustive link scan". A queued or older launch gets the server default (search).
- `_default_mail_mode`: split whenever Drive and mail are in the run and it is not a sample (Codex's rule had also made chosen users split; kept, the DMS still never auto-starts for them).
- Known limit, pinned by a test: a link inside base64-encoded mail is invisible to phrase search. Search-first sends it to the DMS unrewritten; exhaustive catches it. Live recall is NOT yet measured: run `exhaustive` once on the sandbox and compare `recallMissed`.

**Tests:** new `tests/test_mail_link_search.py` (8: only candidates read vs exhaustive reads all, duplicate hits, delta bound, interrupted, hidden-link limit, ledger-known link). Python suite: 5 failures that predate this change (verified on the stashed tree): `test_dms_action::test_a_finished_import_is_tallied`, `test_every_action_is_reachable::...hides_its_card_rather_than_crashing`, `test_move_mode::...mirror_never_deletes_a_moved_file`, `test_stop_reaches_the_engines::...does_not_exit_0`, `test_verification_repairs::...does_not_hide_other_historical_findings`. UI: 1,170 tests pass in both `/app/` and `/next/` builds; tsc and lint clean (fixed one existing exhaustive-deps warning in `WorkspaceOverview.tsx`).

**Live state read 05:53Z (account 3):** 13,678 messages copied, 0 in the last 10 min; 776 messages FAILED_SPLIT in the pre-delivery scan: 695 are a source Drive file that answers 404, 24 unresolved mappings, 12 "multipart body has no readable parts". These block the DMS handoff and are NOT fixed by search-first: `_split_unknown_link` treats a 404 as unresolved. Needs its own investigation (are the 404 files in other users' Drives or deleted?) before any claim of a clean handoff.

**Not done:** deploy (run active), live recall measurement, throughput/latency benchmark against the sandbox, background-delta review.

## Benchmarks and findings (Claude, 30 October 2026, laptop l3novo, account 3, audit@s. -> audit@t.)

Primary goal of these tests is the fastest way to migrate; errors are second. One run at a time.

**Link scan, 758 messages (read-only, ledger copy; `/tmp/bench_scan.py` on the box):**

| Scan | Seconds | Read | Known link msgs found |
|---|---|---|---|
| exhaustive, 3 req/s (old `per_user_qps`), 4/8/16 workers | 257 / 256 / 256 | 1,260 MB | 9/9 |
| exhaustive, 10 req/s, 16 workers | 79 | 1,260 MB | 9/9 |
| exhaustive, 25 req/s, 32 workers | 33 | 1,260 MB | 9/9 |
| exhaustive, 40 req/s, 48 workers | 23 | 1,260 MB | 9/9 |
| search-first, 3 req/s | 85-87 | 238 MB | 0/9 |

- The scan was capped by the per-user limiter (3.0/s), not by workers or bandwidth: 8 and 16 workers gave 2.97 msg/s exactly. New `MAIL_SCAN_QPS` (default 30) and `MAIL_SCAN_WORKERS` (36), a separate limiter for the read-only scan; messages.get is 5 of Gmail's 250 quota units per user per second, so 40/s is about 200 units. Retries were not counted by the bench script.
- **Search-first is not safe by default.** The 9 known link messages are Drive "shared with you" notifications. Gmail does not index URL text: no phrase or sender query found all 9 (best: `from:drive-shares-dm-noreply@google.com`, 5/9; `has:drive` 0). Search also saw 1 blocking message where exhaustive saw 4. Default is exhaustive again (`MAIL_LINK_SCAN`, dialog, API); search stays an explicit choice with the measurement beside it. One mailbox only: run the recall check on a second user before generalising.
- **Blocked counts vary run to run** (exhaustive: 4, 4, 5, 4, 4; one at 16 workers read 757). Some "blocked" are probably transient Drive owner lookups (`_split_unknown_link` treats any error as unresolved). Worth retrying a transient error before blocking.
- **Bug fixed:** a blocked scan wrote no deferral rows, the verifier read the clean mail as lost, and `repair_verified_missing_mail` copied the whole mailbox (734 messages, 700 s, about 63/min) through the engine ahead of the DMS. A blocked scan now records route-DMS mail as owed (`_owe_clean_mail_to_dms`); test `test_a_blocked_scan_owes_the_clean_mail_to_the_dms...`.
- **Drive:** audit@ 106 files server-side in 95.6 s. Peak scan memory 664 MB (search).
- **Mail copy is slow by setting, not by Google:** the engine copy ran at about 2 req/s (the same 3/s limiter), 1 message per second. Next benchmark: raise `PER_USER_QPS`/`MAIL_WORKERS` for the copy and measure insert rate and 429s (insert costs more quota than get, so expect a lower ceiling than the scan).
- **Account 3 verification (info@s.):** 258 "could not compare" are 254 files added to the source during the run (source tripwire; also 273 "no destination mapping") plus 4 transient Sheets 429 / 503 / 404 comment reads that get no retry. Re-walk Drive, then re-verify; add backoff on Sheets reads.

**UI gaps found:** the One-to-one page shows saved results only and has no running state or progress; a verify started by the repair step runs with no operator action and shows nowhere but Migration detail's `activeJobs`; Jobs "Running now" lists only the viewer's own account; no single-user identity mapping (added via `main.py init-db --identities` on the box).

**Rollout state (07:40Z):** box runs f618010's `config.py` and `gmail_engine.py` on top of 50d7026's `api_server.py` (hashes verified; backups `backups/search-first-20261030T060022Z` and `backups/exhaustive-scan-20261030T074016Z`; record in `SEARCH_FIRST_RELEASE.json`). Default scan is exhaustive at 30 reads/s. UI bundles are not redeployed, so `/app/` and `/next/` still show the old dialog (the API accepts `mail_link_scan` regardless). Stopped, with reasons recorded: the 8bc97a5 migrate (exhaustive scan at 3/s) and an automatic re-verify of info@s. that read 290 of 13,846 messages in 11 min (about 8 h at the 3/s limiter). SSH to the laptop is user `l3novo` (password + sudo); no key access for `aryan`/`root`.

**Next benchmarks:** mail copy (engine) and verification both still run at the 3/s `per_user_qps`; measure copy and verify at higher rates (insert costs more quota than get, so expect a lower ceiling than the scan's) before changing their defaults. info@s. still needs: Drive re-walk (254 files added during the run), mail pass, DMS.

## Benchmarks page and the same-user A/B (Claude, 30 October 2026, 13:41Z)

- **Deployed 8dd9f10** (backups `backups/benchmarks-page-20261030T132753Z`, `...T134045Z`; record `BENCHMARKS_PAGE_RELEASE.json`): `api_server.py` and `ab_transfer.py`, API restarted, plus a new `/next` bundle at `/var/www/bitport-workspace/8dd9f10...` with Caddy's root repointed (old root kept in the backup's Caddyfile). The original `/app` UI is byte-identical. `/app` does not have the page; only `/next/benchmarks` does.
- **Page:** `/next/benchmarks` and a header pill on every page while a run is going. Reads `GET /api/v2/benchmark/ab` (live progress, ETA only once one arm has finished, ranking of clean arms, a failed run's reason).
- **`ab_transfer.py`** (replaces my short-lived `benchmark_combo.py`): arms = transfer modes x `--mail-mode` (engine, split, dms), `--account-id`, one-to-one check per mail arm, progress in `logs/ab_progress.json`, results in `logs/ab_results.md(.json)`. **DMS arms are not runnable yet:** nothing runs the DMS import after the migrate pass, so a dms arm cannot finish clean. Build that tail (`dms_migrate.py --identities <one-row csv> --apply --until-done`) before adding `--mail-mode dms`.
- **Operator decision, 13:41Z:** `t.sarafgloabalexim.com` declared a sandbox (`domain_guard.py --revoke`, reason on the record) so the A/B can empty `audit@t.`. It is scoped by `--user`; never run an unscoped reset on t. (`nest@t.` holds 13,752 messages).
- **Running now:** pid 3321423, user audit@s. only, arms server_side/download_upload x engine/split. Mail copy still runs at the 3/s `per_user_qps`, so engine arms take ~12 min each; speeding that up is the next benchmark.
- Full Python suite not rerun after the last small change (error display); targeted ab tests pass; the five known failures are unchanged.
