# Migration Benchmarks

Speed/fidelity measurements for the `c.anupam-poudel.com.np` →
`a.anupam-poudel.com.np` Google Workspace migration, taken on the live
tenant. Each row is tied to the improvement(s) it measures; a row's
"delta vs prior" tells whether that change moved the needle. Once a
change stops producing gains (plateau), we stop pursuing further tweaks
of that kind and record the plateau here.

## Environment (constant across all runs)

- Source: `c.anupam-poudel.com.np`, Target: `a.anupam-poudel.com.np`
- 9 healthy users (the `3@c`/`e@c` accounts are permanently broken and
  always FAIL instantly — excluded from throughput)
- `user_workers=8` (cross-user concurrency), `per_user_qps=3.0`
  (Google's uncapped-raiseable 3 writes/sec/account ceiling)
- Machine: VPS root@78.47.176.120, repo `.venv`, python 3.10
- Latency numbers are API-call p50/p95/p99, printed by the run's own
  metrics (metrics.METRICS.snapshot()).

## Run history

### R0 — Baseline: full migration, `download_upload`, no improvements
- Date: 2026-08-07, `mig_run2.log`
- Services: drive,gmail,calendar,chat (all), 9 users
- elapsed 21,252s (~5h 54m); API calls 125,158; 5.89 req/s;
  p50/p95/p99 585 / 3834 / 7435 ms; 3 retries
- Drive fidelity: 12,309 files, 10 failed files (alice 5, 1@ 3, erin 2)
- ACL fidelity: 21–23 acl_failed/user (all the dead `e@a` account)

### R1 — Improvement 1: transfer mode `server_side` (Drive-only)
- Date: 2026-08-08/09, Phase A, `phaseA_serverside_job.json`
- Services: drive only, 9 users; md5-strict (pre-relaxation)
- elapsed **13,284s** (~3h 41m); API calls 69,711; **5.25 req/s**;
  p50/p95/p99 557 / 1769 / 3401 ms; 1 retry
- Drive fidelity: 12,309 files, **0 failures**; ACL extra_grants **0**
- **Delta vs R0:** 1.6x faster wall-clock; ~44% fewer API calls
  (no get_media/download/upload); native files stay native
  (no OOXML round-trip). Drive mode decision: **server_side wins.**

### R2 — Improvement 1b: transfer mode `link_flip` (Drive-only, the losing arm)
- Date: 2026-08-09, Phase B, `phaseB_linkflip_job.json`
- Services: drive only, 9 users; link_flip = server_side + public-grant
  hack (later fixed to skip owner-role restore)
- elapsed **20,641s** (~5h 44m); API calls 69,988; 3.39 req/s;
  p50/p95/p99 552 / 1616 / 3289 ms; 0 retries
- Drive fidelity: 12,309 files, 0 failures; ACL **extra_grants 93**
  (`anyone:reader` leaks, cleaned post-run)
- **Delta vs R1:** 1.55x slower + leaked public ACLs → mode retired.
  `TRANSFER_MODE=server_side` locked in.

### R3 — Improvements 2–4 applied: ACL batching + MD5 relaxation + fields= trim (IN PROGRESS)
- Started: 2026-08-09, full remigration (drive,gmail,calendar,chat) at
  user request, `TRANSFER_MODE=server_side`
- Improvement 2 — ACL batching (`BatchHttpRequest`, `acl_batch_size=20`):
  target ~17K permissions.create round-trips → ~200
- Improvement 3 — server_side MD5 relaxed: mismatch is a warning, not a
  FAILED (`VERIFY_SERVER_SIDE_MD5=1` restores strict) → drops ~1 API
  call/file on the hot path
- Improvement 4 — `fields=` trim on files.list (drop owners/starred)
- Improvement 5 — connection reuse: **already present** (auth.py LRU),
  no code change, no measurable delta expected
- Improvement 6 — per-user concurrency: **not implemented by design**
  (would exceed the 3 writes/sec/account ceiling → 429 storm). The
  correct multiplier is cross-user concurrency, already at 8.
- Status: waiting for the current run to finish; will fill in elapsed,
  req/s, latency, failures below when it lands.

## Verdicts / plateaus

- **Transfer mode: PLATEAUED at `server_side`.** It's the structurally
  minimal call count for cross-tenant Drive copy (2 hops: copy into
  staging as source, move to My Drive as target, ownership transfers).
  link_flip adds 2+ calls/file and leaks — retired. No faster mode
  exists within the current Google API surface.
- **ACL batching: pending measurement in R3.** Expected to cut the
  dominant single-call cost (~17K creates) to ~200 round-trips; if it
  does not move req/s, the bottleneck is elsewhere (per-account write
  ceiling), and we stop there.
- **Per-user concurrency: PLATEAUED by external constraint.** Google's
  3 writes/sec/account ceiling is not raiseable by request, so
  in-file concurrency beyond the existing cross-user workers is a 429
  risk, not a speedup. Not pursuing further.
- **Broader-engine batching (gmail/calendar/chat/contacts/tasks):**
  not yet audited. If their per-item call counts dominate a full
  migration, same batching treatment may apply — deferred pending user
  prioritisation.
- **Quota-increase request (Cloud Console):** filed only at user's
  direction (account-level, billing-adjacent). Won't move the 3/sec
  ceiling regardless.

## Raw source files (on the VPS)

- `/root/mig_run2.log` (R0)
- `/root/phaseA_serverside_job.json`, `/root/phaseA_acl_audit_correct.json` (R1)
- `/root/phaseB_linkflip_job.json`, `/root/phaseB_acl_audit.json` (R2)
