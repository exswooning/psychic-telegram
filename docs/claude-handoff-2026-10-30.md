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

- [ ] Implement [Gmail search-first link rewriting, then DMS](tasks/claude-gmail-search-first-dms.md). The current runtime still scans every source message body. The task defines candidate-search coverage qualification, count completion, optional strict checks, UI visibility, benchmarks and safe deployment. Adding this task does not change or restart the running migration.
