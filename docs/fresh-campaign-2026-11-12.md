# Fresh account-3 rehearsal

Operator request: wipe target data, run a full migration, manage recoverable errors,
schedule quota resumes, then deep-check and fix remaining parity blockers.

Scope: 45 current source/target user mappings, s.sarafgloabalexim.com to
t.sarafgloabalexim.com; Chat excluded. Keep accounts, credentials, delegation and
domain protection. Drive uses server-side copy; changed link mail precedes a fresh
DMS import. Source reads only; source Calendar ACL migration disabled.

`sandbox_campaign.py --directory <private campaign directory> --start` admits ONE
fresh wipe. Subsequent timer calls omit `--start`. The durable phase marker is
written before destructive work. Interruption, partial wipe, mapping/code drift,
unavailable authorization, non-quota copy failures or unknown DMS state hold for
review. Never clear the marker to repeat a wipe. Before deletion, all five target
service grants and group grant, actual DMS idleness, all-account process/repair
idleness and a complete 45-user source baseline must be established. A consistent
SQLite backup preserves old findings, receipts and quota usage.

Reset only successfully wiped service/user evidence, including receipts, cache,
reconciliation and queued findings. Other users/services and real upload quota
remain. Delete owned Drive Trash items explicitly; never blanket-empty foreign
Trash. List task/calendar/group pages before mutation.

Normal deployment gates are strengthened to recognize the campaign itself; no
force/over-jobs override. The installed Python manifest is pinned at campaign
start and checked on each phase. Autopilot can use isolated state and observe-only
mode so it cannot race the campaign's durable recovery. Typed target repairs run
only after copying, through the existing admitted repair implementations.

Copy retries: at most four attempts; only PAUSED_QUOTA gets an automatic delayed
resume. Wait until the affected users' recorded upload buckets have expired
(bucket start +25h, conservative), or 24h from observed refusal if unknown. Do not
interpret minute-level API throttling as a daily wait. DMS is started fresh once;
an interrupted or failed DMS phase requires actual status/error review, not another
fresh import. A server systemd timer provides persistence without overlapping
phases or an agent being online.

Post-copy: full six-service user checks, supported typed target repairs/rechecks,
whole-tenant read-only check, offline deep ledger audit, source-after guard and
strict readiness evidence audit. Retain findings and stop in REVIEW_REQUIRED;
never equate process completion with parity. Unsupported features, source changes,
provider access failures and remaining differences need explicit investigation.
Generated code still requires review, meaningful tests, publication and idle-gated
deployment; this campaign does not automatically deploy model suggestions.

## First live run: observed failure and recovery

The 45-user baseline completed at 2026-11-12T06:08:42Z; deletion began at
06:12:52Z. Administration Gmail returned HTTP 400 Precondition check failed for
batchDelete. Its target inventory/metadata remained readable, no Chat messages
or drafts were listed, and a five-message delete using the existing full-mail
wipe credential succeeded. An initial diagnostic through the ordinary migration
credential returned 403 insufficient scopes; that is a different credential and
must not be confused with the original 400. The underlying Google precondition
is not yet identified. No message bodies were exported for this investigation.

The pending fix splits only an explicit failedPrecondition batch into bounded
sub-batches, then tries the documented single-message delete. Any retained IDs
remain a failed wipe; auth/quota/malformed-request errors do not become success.
Regression tests cover deletable and permanently blocked items and refusal to
split unrelated errors. This is fixture evidence until the failed mailbox is
successfully recovered live.

The scheduled next phase was paused while the initial wipe continues. After it
is terminal, deploy the reviewed correction through idle gates. Use
--admit-release only after checking installed hashes; the admission records the
previous code hash. --recover-wipe is an explicit operator continuation of ONLY
failed user/service pairs recorded in wipe.json, at most twice, and refuses once
any fresh copy or import started. The timer cannot choose wipe recovery. Never
clear wipeStartedAt or restart a complete target wipe.

Copying excludes deliberate Gmail deferral from the engine's unfinished work;
a separate handoff phase freshly inventories every source and mapped target
before admitting DMS. Later ordered passes can overwrite AWAITING_DMS with DONE,
so neither the status badge nor a missing gmail services_done flag alone decides
that handoff. Real daily byte expenditure can still establish a quota pause
when a later pass overwrote the original pause status.

Additional live evidence: social and ward10 Drive wipes raised TimeoutError:
The read operation timed out. These were recorded as failed services while other
users continued. Timeout/connection errors now use the existing bounded four
idempotent wipe attempts; authentication and permanent errors remain held.

Deployment process recognition now includes Python -u/-B options and all campaign,
DMS, parity and full verification controllers. Real filter tests cover the exact
commands used here; a running DMS now refuses copying/restart under the pinned
campaign baseline. This strengthens the gate rather than overriding it. The
previous DMS-allowed test was updated for the current all-phases idle policy.

## Recovery rollout checkpoint

The initial wipe ended at 07:10:35Z with three Drive timeouts (social, ward10,
ward16) and administration Gmail incomplete. Two local one-time release-wait
helpers accidentally overlapped; the first detected drift caused by the other,
then removed their shared transport key while the second was finishing. New
files reached disk but the restart did not complete. No migration/import had
started; RESTART_PENDING remained and the campaign timer was still disabled.
Both helpers are terminal and the helper script was retired. The reviewed
469e441 baseline (193 installed Python files including three preserved operator
helpers) was verified and the normal deployment was completed at the next
operator continuation, with four services healthy and 525 UI files unchanged.

This exposed an independent deployment-reporting defect: a failed SSH stamp
could still print stamped DEPLOYED_COMMIT and return success. Both frontend-only
and backend paths now fail closed if the stamp cannot be recorded. Two tests
reproduced the false success before the patch; all 66 targeted deployment tests
passed afterward. This script-only correction is pushed separately; it is not a
claim that the current migration has been rechecked or that remote Google
preconditions were repaired.

Scoped wipe recovery was explicitly admitted against the verified 469e441
baseline, with the quota timer restored. All three Drive failures recovered;
administration Gmail was still being emptied at the last check (1,569 messages
remaining). Do not claim the wipe or migration complete from this checkpoint.

## Copying started; confirmed reset residue

Scoped wipe recovery completed successfully at 10:14:17Z. All recorded wipe
failures are empty. The timer launched the fresh copy at 10:15Z; the Drive pass
and link-mail inventory started at 10:16:39Z. No new audit failures were present
at the initial checks (118, then 634, then 957 fresh file mappings). Source data
has not been a wipe subject. The final temporary deployment key was removed.

A read-only comparison with the pre-wipe database found 118 comment_reply
mappings with the exact pre-wipe destination IDs, all with CONFIRMED prior
comment_delivery_attempts receipts. Drive reset omitted that helper-owned type.
This is observed bookkeeping residue, not a claim that the new migration has
already thrown a reply exception. A failing-before regression reproduces the
subsequent Mapped comment/reply is missing hold on a newly mapped file.

The pending correction adds reply mappings/optional delivery receipts to the
user's Drive reset and invalidates that user's in-process mapping cache after
commit. It preserves other users, Gmail and real quota. Comment reconciliation
may ignore a missing old mapped reply ONLY when an existing nonempty receipt
proves it belongs to a DIFFERENT target file, while the current exact file
mapping is checked. The same-file case, missing/empty evidence, target edits,
ambiguity and uncertain delivery on the current file remain held. Retrying an
already recovered reply reuses the confirmed copy without another write.

79 affected reset/wipe/campaign/comment tests passed using isolated fake Google
services. These are not a live qualification of changed comment writes. Keep the
running 469e441 copy intact. Publish this reviewed correction, await global
idleness before deployment, independently qualify its concrete sandbox scope,
then recheck affected comments with preserved target edits and backups. Never
repeat the full wipe or clear current file/message mappings to fix these reply
records. Full user/tenant checks, source-after and readiness remain pending.
