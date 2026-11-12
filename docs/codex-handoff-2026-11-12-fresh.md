# Fresh migration checkpoint — 12 November 2026

Account 3: s.sarafgloabalexim.com -> t.sarafgloabalexim.com, 45 users;
Chat excluded. Source is never a wipe subject. Never use the old VPS.

## Live state

Runtime d1b4651, all 193 installed Python hashes verified (190 release files plus
three preserved operator helpers); four services healthy; both UIs, 525 assets,
unchanged. Repo branch codex/google-configuration-ui; preserve .claude/settings.json.

State: /opt/bitport/logs/campaigns/fresh-20261112/state.json.
Current service bitport-fresh-copy-resume; phase migrate, attempt 2. Timer
bitport-fresh-campaign.timer enabled (five-minute eligible-phase ticks).
11:19 UTC: 3,898 file mappings, 307 folders, zero current failed audit items.
Source baseline: 45 users, 40,932 Drive items, taken 06:08:42 UTC.
All selected target data was emptied successfully at 10:14:17 UTC; keep accounts,
auth/configuration and real upload_ledger. DO NOT REPEAT THE WIPE.

## Resolved and qualified

Initial wipe failures: administration Gmail batch precondition; social/ward10/
ward16 Drive read timeouts. Reviewed bounded recovery emptied only those failed
services. No partial wipe was called complete.

Fresh copy hit a real missing-reply hold: reset omitted comment_reply mappings;
118 exact pre-wipe destination IDs and confirmed old-copy receipts remained.
Published fixes clear helper receipts on resets and invalidate the user's mapping
cache after commit. Reconcile can replace a missing reply mapping only with a
receipt proving a DIFFERENT target copy; same-copy edits, empty/unknown evidence,
ambiguity and current-copy uncertain delivery remain held.

Cooperative SIGINT drained the first copy; retained all file/message mappings.
Normal idle-gated d1b4651 deploy, rollback retained, no force/over-jobs override.
One-file live qualification: six replies created, complete supported-thread
readback matched, second pass zero creates, source comments/mtime unchanged.
This qualifies that concrete repair, not original native comment author/time
metadata or overall parity. Backups/proof: campaign/comment-canary/.

V3 metadata returned no ETag. V2 exposed an actual Google-issued ETag; a no-field
negative patch using another file's actual ETag was rejected with 412. A fresh
correct If-Match patch restored only the selected target timestamp and readback
matched. An earlier malformed artificial token produced 500, not a migration
failure; do not treat it as valid precondition evidence. Do NOT assume this file
ETag works for Docs/Sheets API writes. All other native repair guards remain.
The exact comment audit was marked successful only after live readback; old
failure remains in retry history. Global cases remain governed by full checks.

## Recovery and next steps

Controller stages: migrate -> fresh complete mail handoff inventory -> one fresh
DMS import -> full six-service user verification with admitted repairs -> tenant
verification -> deep ledger/source-after/readiness audits -> REVIEW_REQUIRED.
No samples establish parity. DMS ambiguity/interruption and unsupported errors
hold for review; never blindly start another fresh import. Inspect provider
failure IDs rather than assuming previous 17 failures recur or are fixed.

Google byte quota: preserve rolling hourly upload buckets; resume no earlier than
recorded expiry (conservative +25h) or 24h after refusal if unknown. Minute API
throttling is different. Four total copy attempts; do not reset counters/quotas.
--admit-release --retry-copy is explicit operator recovery, before DMS, idle blocked
copy only with a proven successful wipe. Attempt 2 started 11:09:56 UTC; timer
restored after admission. No further stop/restart without an actual actionable
reason. Check fresh counts, API activity and item errors, not badges.

Keep source/target edits and backups; publish/review/test before future rollout;
verify exact runtime baseline and all tenant/repair idleness; never override gates.
Only generated Gmail quota candidates have approved worker canaries; other generated
code is held. Current fix is independently reviewed/manual with live scoped proof.
The error observer uses managed-campaign observe-only mode; controller owns recovery
and typed repairs. Model advice is bounded/redacted and cannot deploy code.

## Access and incident notes

All temporary deployment credentials were removed remotely and locally. A new
privileged credential requires fresh explicit permission; do not bypass a rejection.
A broad manual admission cleanup was rejected; it was omitted. Use read-only
process/repair checks and normal gates. Existing SSH/sudo authorization is in chat;
never put the password or service secrets in docs.
Two local wait helpers overlapped earlier, interrupting the recovery rollout.
Both are terminal; finish_recovery.py.retired must never be run. RESTART_PENDING
protected the interrupted state; the release was completed normally before copying.

Scope fidelity remains UNPROVEN until the queued full checks complete. Do not call
source-wide unchanged from the single-file canary, or call samples/fixtures parity.
