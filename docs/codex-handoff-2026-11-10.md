# Codex continuation, 10 November 2026

The operator's pasted 16:50 UTC handoff governs this work. All 45 account-3
users are in scope; Chat is excluded. Repairs stay paused. No automatic resume,
approval or teardown. React to reports rather than periodically polling.

## Confirmed on the laptop

At 16:58 UTC DMS fresh import and its delta loop were active. The last valid
console reading in `logs/jobs/3/dms_fresh.log` was 115,719 imported of 115,735,
16 failed, 43 users processed. Its next reading was `Not started` without
counters; this does not establish completion or a stall.

`_post_dms.sh` was waiting for both DMS units to exit, then would explicitly
enable account-3 repairs through the API and run a full parity recheck. This
contradicted the operator's pause. Stopped **only** `bitport-post-dms.service`
and these timers: `bitport-autopilot-resume`, `bitport-parity-resume`,
`bitport-dms-user-verify-resume`, `bitport-verify-all-resume`. All five were
confirmed inactive; both DMS units remained active. No tenant job was launched,
no server code was deployed and no source/target items were changed.

A read-only ledger query confirmed `verification_repair_policy` row `(1, 0)`.
The obsolete hourly polling automation was paused; the operator later authorized a one-time continuation after the quota reset.
The script still exists and must not be manually restarted while repairs are
paused. At login the laptop reported 9% RAM and 51°C; that is an idle-era
measurement, not evidence the full verification memory problem is resolved.

## Reviewed source fixes, not deployed

Based on HEAD `e560f9d`; preserve the pre-existing `.claude/settings.json` edits
and Claude's uncommitted X-Forwarded-Encrypted comparison change and regression.

- `verify_sample.py`: unexplained extras now prevent IDENTICAL and appear in
  report details. This does not delete extras or establish whether they are DMS
  deliveries missing from the ledger. The prior policy intentionally ignored
  extras; tests were updated to the operator's stricter parity definition.
- The accepted storage-header change is retained. Equivalent-message evidence
  now names ignored header differences even when the header exists on both
  sides, without recording its value. `verification_cache.py` comparison
  version advances to 2 so old comparison-context cache entries are not reused.
- Heavy Drive content comparisons and native link reads share one process-wide
  slot across user workers, with cancellation and exception-safe release.
  Metadata remains parallel. This reduces simultaneous memory demand and can
  reduce throughput; it is not a machine-wide cap across separate processes.
- `native_fidelity.py`: remove a redundant deepcopy before rebuilding the
  canonical document. A 12,000-paragraph isolated fixture's traced peak fell
  from 25,070,795 to 12,866,531 bytes (about 49%). Input remains untouched and
  content/style/reference comparisons remain tested. This is not a live proof
  that the earlier 11 GB process growth is fully fixed.
- `oversight.py`: verdict-flip alerts preserve the incident but no longer claim
  an unproven cause. Missing tracked runs does not exclude DMS or user changes.

Regression tests reproduced failures before the patches: extras incorrectly
returned IDENTICAL, four heavy reads overlapped, normalisation exceeded the
fixture memory bound, and changed storage headers were not named. Isolated
tests passed afterward. The full verify-sample, native-image, pacing and initial
memory suite passed; a subsequent 80-test native/pipeline/oversight/memory suite
and final 71-test comparison/verdict/pipeline/memory suite passed (overlapping,
not 151 distinct tests). No Google tests or live repairs ran. Test sources and
ledgers were isolated under `/private/tmp/bitport-nov10-sskbu_td`.

## Evidence and remaining work

Saved Nov10 verification reports independently confirm false IDENTICAL verdicts:
audit 32 checked / 722 extras; buildingpermit 46 / 766; cao 28 / 3,772;
ward11 74 / 3,537. The audit and buildingpermit repeats retained those same
counts. The intermediate DIFFERENCES snapshots were not present in the saved
reports inspected, so incidents 50–56's precise cause remains unproven. Do not
label it fixed or clear those incidents from these local tests.

DMS completion, its deltas, per-mailbox reconciliation, the nine refused
it.department messages, real label/duplicate differences, admin/ward8 checks,
and full deep parity are outstanding. The operator subsequently asked to implement this plan, authorizing the reviewed source commit. None of these changes is deployed. Deployment also waits
for all migration/DMS/verification jobs to be idle, followed by current installed
hash checks and normal release gates. Do not resume repairs to obtain a canary.

The local `dms_user_verify.py` fix holds verification when reconciliation reports
an error or a blocker. A mailbox counts as finished after its worker returns a
complete Gmail check. Each worker reports its verdict separately and saves its
report in its own directory. Workers reconcile the ledger and verify Gmail with
repairs disabled. After a worker failure, the watcher stops scheduling new work
and lets checks already underway finish. Its scheduled resume remains cancelled.
The isolated DMS worker, autopilot, memory and first-pass suite passed 73 tests.

## Continuation at 11 November 04:00 Nepal

The operator requested implementation and better local-model use, then authorized
additional RAM/cores when a measured improvement justifies them. A one-time
continuation resumed after the reported usage reset; it is retired after delivery,
not converted back into hourly polling. See `docs/bitport-readiness-plan.md` for
acceptance gates, model evaluation, resource tuning and token-saving targets.

Additional reviewed changes:
- Forms comparison aligns items before comparing full canonical content, avoiding
  cascading false changes after one missing image. It retains duplicate, missing,
  reordered and genuinely changed item findings and does not broaden exceptions.
- DMS metrics retain older counters under a separate timestamped history field;
  empty current observations stay empty. The UI explicitly labels old counters.
  History cannot manufacture a completed import; cache writes are atomic.
- Local-model diagnosis now queues redacted evidence, uses grounded structured
  advisory output, and saves hypotheses, uncertainty, an investigation, proposed
  fix and regression test for review. No model output is executed or clears cases.
  Calls wait for idle jobs/repairs and available memory, have a daily bound and
  one attempt per cached case, and record usage/latency. Attempts persist before
  model calls; interrupted attempts do not silently retry. Requests cannot use
  remote endpoints, environment proxies or redirects. CPU thread count is tunable;
  model/service/resource limits were NOT changed on the live laptop.

Validation: 175 targeted isolated Python tests passed; 32 follow-up local-model
and autopilot tests passed after adding usage telemetry/thread tuning (overlapping,
not 207 distinct tests). The earlier verify-sample suite also passed. TypeScript
checking passed. Google behaviour, model diagnosis quality, full-memory stability
and overall 45-user parity are not established by these tests.

Fresh read-only server check at 10 November 22:16 UTC found DMS In progress:
138,839/138,856 emails imported, 17 failed, 44 users processed. Autopilot-resume,
DMS-user-verify-resume, verify-all-resume and verify-2 services were active despite
being inactive in the earlier checkpoint. Codex did not start them. Parity-resume
failed. Repair policy remains `(1,0)`; unfinished repair records 268 and 270 were
present. RAM was about 8.3/19.3 GiB. No server deployment or model benchmark is
allowed over this work. Do not infer the resumed services' initiator or turn
historical timestamps into current success. No credentials were written to files.
