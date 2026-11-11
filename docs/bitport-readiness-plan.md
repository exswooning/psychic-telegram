# Bitport readiness and efficient diagnosis — 11 November 2026

## Current assessment

Engineering assessment: **5/10 against unattended, verified migration**, not a
statistical benchmark. Large transfers work, but incomplete verification,
contradictory verdicts, unresolved content differences and mixed releases prevent
production confidence. A local test pass cannot raise the live parity score.

10/10 means meeting an explicit supported-feature contract and the gates below.
It cannot mean a guarantee that Google never fails or that unavailable API
features can be reproduced. Unsupported features and operator-accepted differences
must remain visible; they cannot be silently counted as full fidelity.

Scope: all 45 account-3 users; Chat excluded. Preserve source and target data,
repair backups and concurrent Claude work. Repairs remain paused. No unattended
tenant job resumes, destructive actions, approval or teardown. The operator
allows more RAM/cores when a measured improvement justifies them. That permission
does not authorize overriding active-job or repair-pause gates.

## Ordered release gates

1. **Trust the measurements.** Commit/push the reviewed fixes, then verify the
   actual installed baseline and globally idle jobs before normal deployment.
   No extras/missing/duplicates/unknown reads may produce an identical verdict.
   Every result must identify user, service, source version, scope, timestamps,
   comparison rule version, run and evidence. Historical results stay historical.
   Reproduce verdict flips against identical captured inputs and separate real
   DMS/user changes from comparer errors.
2. **Close mail delivery.** DMS must reach a terminal outcome, with its failure
   report reconciled to exact messages. Reconcile each mailbox before classifying
   extras. Inspect all nine it.department refusals with the header fix; three
   prior samples do not establish all nine. Distinguish info's 13 missing drafts
   from ordinary DMS mail. Investigate labels/duplicates without silently changing
   or deleting target data. Prioritize admin and ward8 incomplete checks.
3. **Close Drive gaps.** Diagnose six saved Sheet mismatches by exact cells,
   formulas, formatting and other supported structures. Preserve retained
   backups. Recheck native read timeouts with bounded reads, inspect comment
   ambiguity/404s and Form differences with item alignment. Narrow accepted Form
   exceptions to their actual approved item and form. No blanket exceptions.
4. **Bound resources and recovery.** Measure RSS throughout representative large
   documents, sheets, mailboxes and repeated user checks. Initial operational
   target: no OOM, no sustained swapping, preserve 4 GiB host headroom, no
   unbounded growth over three comparable passes. These are acceptance targets,
   not measured live achievements. Tune concurrency from evidence. Exercise
   quota, 503, auth expiry, disconnect and interrupted-job recovery in fixtures;
   resumptions must not duplicate items or bypass operator holds.
5. **Prove coverage.** After an authorized affected-scope recheck, run an explicitly
   authorized full 45-user check at a recorded source cutoff. Six in-scope
   services need complete inventories and item/feature evidence, zero unexplained
   missing/extra/duplicate/different items and zero incomplete comparisons.
   Counts are necessary, insufficient for content fidelity. Recheck an unchanged
   snapshot and verify stable results. Maintain a capability matrix for Forms
   responses, revision histories and every other non-reproducible feature.
6. **Qualify unattended operation.** Exercise fresh and resumed migrations on
   separate authorized disposable fixtures, at least three clean end-to-end
   runs, then a seven-day observation window. Require zero source mutations,
   zero lost/duplicate items, all injected failures detected, bounded recovery,
   complete incident provenance, tested rollback and no false success reports.
   Only then reassess 10/10 for the agreed contract. Dates and score are outcomes,
   not promises.

## Local model and token budget

- Deterministic code handles known error classification, grouping, counts,
  recurrence and readiness. No model for status polling or unchanged errors.
- The local model receives a redacted evidence packet, never raw mail/body or
  credentials. It returns hypothesis, uncertainty, read-only investigation,
  conditional fix and regression test with evidence references. Output remains
  untrusted advisory text; no model verdict closes an incident or executes code.
- Current implementation queues at most 50 cases, caches evidence identity,
  preserves unknown run/version as unknown, limits 12 attempts/day, processes
  one case at a time only when idle and memory is available, and persists an
  attempt before calling the model. Interrupted attempts do not silently retry.
  Endpoint is loopback-only, with proxies and redirects disabled. Context 4096,
  output 450 tokens, 60-second request timeout; model unloads after each request.
- Record provider token counts when supplied, elapsed time, model and prompt
  version. Measure prompt size, repeated-case savings, invalid answers,
  unsupported claims and cloud escalations. Target at least 70% fewer cloud
  input tokens versus the current whole-log/history workflow; this is a target,
  not a claimed saving.
- Benchmark installed qwen2.5-coder:7b and llama3.2:3b first. Candidate replacement:
  qwen3:4b-instruct, explicitly non-thinking (official model card below). Do not
  switch merely because a model is newer. Use at least 50 redacted labelled
  cases, stratified by quota/auth/404/timeouts/content/reporting bugs, plus
  adversarial log instructions. Hold out cases from prompt development.
- Measure semantic diagnosis usefulness, false confident causes, evidence
  accuracy, valid JSON, wall time, RSS, CPU temperature and joules if available.
  Try 2, 4 and 8 threads on the laptop when idle; compare identical prompts and
  model digests. Adopt more cores/RAM only for >=20% latency/throughput gain
  without lower accuracy, host instability or unacceptable thermal throttling.
  AUTOPILOT_MODEL_THREADS is configurable; service quotas need a separate,
  measured update. Current live cap remains 7 GiB / 2 CPU equivalents.
- Send cloud coding agents only a new actionable case: exact traceback, relevant
  source lines, run/version, prior attempts, expected behaviour and one failing
  test. Aim for <2,000 input tokens per routine case. Fetch more evidence on
  demand. One agent owns the fix; avoid duplicate Codex/Claude investigations.
  Use stronger cloud reasoning for difficult defects/review, not routine polling.
- Keep a compact current checkpoint and durable issue records. Do not replay
  long hourly prompts or re-send the full chat. Preserve evidence outside prompt
  context and cite only relevant artifacts. Daily/attempt allowances remain real
  limits; do not reset them to manufacture progress.

Model references: https://ollama.com/library/qwen3:4b-instruct and
https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507.
OpenAI usage guidance: https://learn.chatgpt.com/docs/pricing (concise prompts,
relevant source scope, smaller AGENTS context and only necessary tool servers).

## Current blockers and next work

Fresh observation 10 November 22:16 UTC: DMS remains In progress, 138,839/138,856
emails imported, 17 failed, 44 users processed. Several resumed verification
services and unfinished repair records 268 and 270 exist. Repair policy is still
(1,0). These jobs were not started by Codex in this continuation. No deployment,
model load benchmark or tenant-wide verification is admissible while they run.

Finish local review/tests/commit first. Next idle rollout must re-read current
runtime hashes and account-wide job/repair states, preserve concurrent changes,
use unchanged sync_vps.sh gates, retain rollback and verify both UIs/services.
Do not overwrite a mixed runtime with an older staged tree. Ask only for an
actual missing authorization, credentials or a vendor action; carry out all
independent code work while blocked.


## Implementation checkpoint — 11 November, next release

Additional source implementation (not yet installed):
- `verification_repairs.finished_user` now checks the stored pause both before
  inspecting repair candidates and again before claiming a repair, with another check inside the claim transaction. A regression
  failed on the old code: a paused callback still entered target inspection.
  The policy flag itself already respected a pause; the callback was the gap.
- `verification_provenance.py` records run IDs, actual comparison-file hashes,
  mail comparison version, user/service identity, limits and start/end times.
  These fields reach incremental saved results, including failed reads. Claimed
  deployment revision is separate from file evidence. File hashes do not attest
  already-loaded modules or an atomic source snapshot.
- Verification history filenames are unique even within one second. Atomic
  latest-file updates use unique temporary files, preventing concurrent writers
  from removing each other's temporary file. The two latest formats remain
  separate files; use the uniquely named JSON as the authoritative artifact.
- `readiness_report.py REPORT.json --users EXPECTED_USERS.json` checks full
  saved evidence against an explicit tenant inventory. It rejects samples,
  omitted users/services, unexplained findings, unknown provenance and differing
  code manifests. Passing this command is only verification-evidence readiness;
  it does not certify source stability, deployment, recovery or full fidelity.
- Sheet comparisons name bounded cell addresses/field paths without logging
  cell contents. Missing/extra structures still fail. Another redundant full
  Sheet deepcopy was removed; source input remains immutable.
- `benchmark_local_diagnostics.py CASES.json --model MODEL --threads 2|4|8`
  validates a labelled dataset. `--run` performs idle-gated local inference and
  emits latency/token use and answers for grounding review. It never switches a
  model, changes service resource quotas or treats valid JSON as correctness.
  No live model benchmark has run while the laptop is busy.

Validation: 158 verification/repair/native/readiness tests passed, then 85
pipeline/readiness/repair/benchmark tests and a final 38 focused tests after the
last additions (overlapping suites), followed by 103 focused regression tests. All use isolated fake services and temporary
ledgers, not live Google migration qualification.

Latest fresh DMS log: 11 November 02:27 UTC (08:12 Nepal), complete for 45 users;
141,952/141,969 emails imported, 17 failed. This proves the console's reported
import outcome, not delivery parity or that retries resolved the failures.
DMS wrapper/delta, per-user checks and verification units remained active when
inspected. Deployment and live model benchmarking remain held. No tenant jobs
were launched, stopped or restarted by this implementation turn.

Read-only partial runtime checks (not a complete release attestation):
verification_repairs.py dfe2453348f496729fa201e19879b35fdc8c94052f9c73d81dd45d5a6f097463;
verify_sample.py b11c37a43faf94beceb99fe9d54bf237bbad8e54af23fcf2be6460102e836feb;
native_repair.py 9eb9e1f7fc4e1367ac555b6cd67527a237d1d94f1c06bde4b985fcc9602d80e1;
autopilot.py 3e668e3914a2be26c58a0cd873b8d3fb4c6063dab5656a9cf2536f7c22110fd3;
dms_user_verify.py 55d672751bc47ddf23ec9a96b2083baafeda263106c37ee99ab4dd6bdb557cd8.
The previous b0a1911 verifier is not installed; compare exact files again once
idle before deploying the new reviewed commit. Preserve unexpected drift.

At the next inspection three live account-3 verification processes were running
with `--repair-after-user` while the stored policy was paused (0). The installed
callback lacks the new pause checks. Codex attempted a normal stop of only the
three verification services, leaving DMS intact, but automatic approval review
rejected the action as a disruptive production-side mutation not clearly
covered by authorization. No stop was executed and no workaround was attempted.
The operator must explicitly authorize stopping those verification jobs if they
are to be halted; even with authorization, observe active repair records and
allow in-flight calls to finish. The new source safeguards prevent entry/claim
while paused but do not cancel a target request already in flight. They remain
uninstalled until the normal release gates permit deployment.
