# Migration Benchmarks

Standardized, reproducible benchmarks for the
`c.anupam-poudel.com.np` → `a.anupam-poudel.com.np` Google Workspace
migration. Governed by the benchmark protocol adopted 2026-08-09 (see
AGENT_COORDINATION.md entry). No optimization is declared successful on
elapsed time alone.

## 1. Benchmark principle

Every benchmark compares the **same workload**:

- same 9 source users
- same source dataset
- same source tenant (`c.anupam-poudel.com.np`)
- same target tenant (`a.anupam-poudel.com.np`)
- same enabled services
- same VPS / machine (root@78.47.176.120)
- same credentials
- same migration configuration except for the optimization under test
- clean target state before each benchmark
- clean/resolved migration ledger before each benchmark

Full all-services migrations are **not directly comparable** to
Drive-only benchmarks. The historical R0 (full, `download_upload`) is
retained below as a historical baseline only, labelled **not directly
comparable**.

`link_flip` is excluded from production benchmarking (already showed
security + performance disadvantages).

## 2. Environment (constant across all runs)

- Source: `c.anupam-poudel.com.np`, Target: `a.anupam-poudel.com.np`
- 9 healthy users (the `3@c`/`e@c` accounts are permanently broken and
  always FAIL instantly — excluded from throughput)
- `user_workers=8` (cross-user), `per_user_qps=3.0` (RateLimiter token
  bucket, resilience.py:305)
- `TRANSFER_MODE=server_side`
- Machine: VPS root@78.47.176.120, repo `.venv`, python 3.10
- Latency = API-call p50/p95/p99 from metrics.METRICS.snapshot()
- config hash + commit sha recorded per run (section 4)

## 3. Benchmark series

```text
B0 = current server_side baseline          (pre-improvement code)
B1 = B0 + ACL batching                     (drive_engine.py _sync_acls)
B2 = B1 + fields= trimming                 (files.list response mask)
B3 = B2 + next approved optimization       (TBD — pending user approval)
B4 = final combined configuration          (current deployed code)
```

**Known constraint on isolation:** the three deployed improvements
(ACL batching, MD5 relaxation, fields= trim) were committed together in
one commit `b499b45`. Strict one-change-per-stage isolation is therefore
only possible by running B0 against the parent commit's code
(`b499b45~1`) and B4 against current HEAD, treating B1/B2 as bundled
into B4. Options: (a) accept B0 vs B4 as the meaningful comparison and
label B1/B2 as "bundled"; (b) re-checkout intermediate commits and
selectively revert components to truly isolate — flagged for user
decision before B1/B2 runs begin.

Two trials (A, B) per stage. Record both; do not silently average.
An improvement is reproducible only when it exceeds run-to-run
variance (`mean_elapsed`, `run_to_run_variance`).

## 4. Performance metrics (recorded per run)

```
commit_sha, config_hash, start_time, end_time, elapsed_seconds
total_api_calls, aggregate_requests_per_second
p50_latency_ms, p95_latency_ms, p99_latency_ms
retry_count, 429_count, failure_count
peak_concurrent_users, average_concurrent_users, peak_inflight_requests
drive_elapsed, gmail_elapsed, calendar_elapsed, chat_elapsed
objects_per_second, api_calls_per_object
```

## 5. Quota / throttling metrics

```
read_requests_per_second, write_requests_per_second
429_count, retry_count
per-user throughput, peak per-user write rate
```

`concurrency` and `QPS` are NOT interchangeable. A semaphore limiting
in-flight requests is not proof the app stayed under a requests/sec
limit. Only measured write req/s vs the 3/sec/account ceiling counts.

## 6. Drive validation (per benchmark)

```
source_files, target_files, missing_files, extra_files, failed_files
source_folders, target_folders, missing_folders
source_shortcuts, target_shortcuts
```

Required production result:
`missing_files = 0`, `extra_files = 0`, `failed_files = 0` unless a
discrepancy has a documented and approved explanation.

## 7. ACL validation (per benchmark — independent audit)

```
source_grants, target_grants, matched_grants, missing_grants, extra_grants
unresolved_source_identities, unexplained_missing_grants
unexpected_public_grants, unexpected_anyone_grants
```

Known dead-target identities (e.g. `e@a.anupam-poudel.com.np`) are
reported separately from unexplained discrepancies.

Security gate (must all be 0):
`unexpected_public_grants = 0`, `unexpected_anyone_grants = 0`,
`unexpected_extra_grants = 0`, `unexplained_missing_grants = 0`.

## 8. Full-service validation

For the currently enabled scope, validate each service independently:

- **Gmail:** source/target/missing/extra messages, drafts, labels,
  filters, attachments, failures
- **Calendar:** source/target calendars, source/target events,
  missing/extra events, attendee discrepancies, recurrence
  discrepancies, failures
- **Chat:** source/target spaces, source/target messages, source/target
  members, missing/extra objects, failures
- **Drive:** per section 6

If Contacts/Tasks are not in the actual run, call the run **"full
migration of currently enabled services"**, never "all services".

## 9. Integrity / verification (separate from migration status)

```text
migration_status:   SUCCESS | FAILED
verification_status: VERIFIED | UNVERIFIED | MISMATCH | NOT_AVAILABLE
verified_objects, unverified_objects, verification_mismatches
verification_coverage_percent
```

A verification warning must not silently disappear into a generic
SUCCESS.

## 10. Pass / fail

An optimization is successful only if ALL of:

1. elapsed improves ≥ **5%** vs the immediately preceding comparable
   benchmark
2. improvement is reproducible across both trials
3. no unexplained data loss
4. no unexplained ACL discrepancy
5. `unexpected_public_grants = 0`
6. `unexpected_anyone_grants = 0`
7. failures do not materially increase
8. verification quality does not materially regress

If performance improves but correctness/security regresses:
`RESULT = FAIL`; not promoted to production.

## 11. Required benchmark table (main)

| Run | Change            | Trial | Elapsed | API calls | Req/s | 429s | Retries | Failures | Missing | ACL unexplained | Public grants | Verification |
| --- | ----------------- | ----- | ------: | --------: | ----: | ---: | ------: | -------: | ------: | --------------: | ------------: | ------------ |
| B0  | Baseline (parent of b499b45) | A | | | | | | | | | | |
| B0  | Baseline              | B     |         |           |       |      |         |          |         |                 |               |              |
| B1  | ACL batching          | A     |         |           |       |      |         |          |         |                 |               |              |
| B1  | ACL batching          | B     |         |           |       |      |         |          |         |                 |               |              |
| B2  | fields trim           | A     |         |           |       |      |         |          |         |                 |               |              |
| B2  | fields trim           | B     |         |           |       |      |         |          |         |                 |               |              |
| B3  | Next optimization     | A     |         |           |       |      |         |          |         |                 |               |              |
| B3  | Next optimization     | B     |         |           |       |      |         |          |         |                 |               |              |
| B4  | Final combined        | A     |         |           |       |      |         |          |         |                 |               |              |
| B4  | Final combined        | B     |         |           |       |      |         |          |         |                 |               |              |

## 12. Required benchmark table (service-level)

| Run | Trial | drive_elapsed | gmail_elapsed | calendar_elapsed | chat_elapsed | drive objs/s | api_calls/obj | drive missing | drive extra | drive failed |
| --- | ----- | ------------: | ------------: | ---------------: | -----------: | -----------: | ------------: | ------------: | ----------: | -----------: |
| B0  | A     |               |               |                  |              |              |               |               |             |              |
| B0  | B     |               |               |                  |              |              |               |               |             |              |
| B4  | A     |               |               |                  |              |              |               |               |             |              |
| B4  | B     |               |               |                  |              |              |               |               |             |              |

## 13. Final benchmark report (published at B4)

```text
Baseline elapsed:            Final elapsed:            Time saved:
Percentage improvement:
Baseline API calls:          Final API calls:          API reduction:
Baseline req/s:              Final req/s:
429 change:                  Retry change:             Failure change:
Drive completeness:          Gmail completeness:       Calendar completeness:
Chat completeness:
ACL unexplained:             Unexpected public grants: Unexpected anyone grants:
Verification coverage:       Verification mismatches:
baseline_commit_sha:         final_commit_sha:
baseline_config_hash:        final_config_hash:
```

## 14. Stop rule

Stop further optimization when ANY of:

- latest optimization produces <5% reproducible improvement
- blocked by an external quota/architectural ceiling
- further optimization adds complexity without measurable improvement

When stopping, record the plateau and reason. Do not chase a number.

## 15. Agent execution rule (before each benchmark)

1. sync local repository with GitHub
2. record commit SHA
3. verify VPS code matches the intended commit
4. record configuration hash
5. verify target reset completed
6. verify migration ledger reset correctly
7. verify source dataset / user count
8. start benchmark
9. collect performance metrics
10. run independent correctness/ACL/security validation
11. write results to BENCHMARKS.md
12. append result to the coordination log

No benchmark is complete until BOTH performance and validation results
are recorded.

## Final decision rule

The objective is **not** the highest possible requests/sec. It is:

> The fastest reproducible migration that maintains complete data
> fidelity, zero unexplained ACL discrepancies, zero unexpected public
> permissions, and transparent verification status.

Performance improvements that violate those conditions are rejected.

---

## Historical runs (not part of the controlled B-series)

### R0 — FULL migration, `download_upload`, no improvements — NOT DIRECTLY COMPARABLE (different workload)
- Date: 2026-08-07, `mig_run2.log`
- Services: drive,gmail,calendar,chat (full); elapsed 21,252s (~5h54m)
- 125,158 API calls, 5.89 req/s, p50/p95/p99 585/3834/7435ms, 3 retries
- Drive: 12,309 files, 10 failed (alice 5, 1@ 3, erin 2)
- ACL: 21–23 acl_failed/user (dead `e@a` account)

### R1 — Drive-only `server_side`, NO improvements — closest available to B0
- Date: 2026-08-08/09, Phase A, `phaseA_serverside_job.json`
- Drive only; elapsed **13,284s** (~3h41m); 69,711 calls; **5.25 req/s**
- p50/p95/p99 557/1769/3401ms; 1 retry; 12,309 files, **0 failures**
- ACL extra_grants **0**; md5-strict (pre-relaxation)
- This is the last run before the improvement commit b499b45.

### R2 — Drive-only `link_flip` — EXCLUDED from production benchmarking
- Date: 2026-08-09, Phase B, `phaseB_linkflip_job.json`
- elapsed 20,641s; 69,988 calls; 3.39 req/s; **93 leaked `anyone:reader`**
- Mode retired on security + performance grounds.

### R3 — (aborted) full remigration with improvements — never ran
- The full wipe completed (target clean, 2026-08-09 10:40); the
  remigration was **held by explicit user instruction** awaiting
  direction. Superseded by this benchmark protocol.

## Ceiling floor analysis (context for the 5% bar)

Google's **3 writes/sec/account** ceiling is per account, not aggregate.
With `user_workers=8` the aggregate write budget ≈ 24 writes/sec.

- Copy path ≈ 2 writes/file (copy+move) + ~1.6 ACL creates ≈
  **~3.6 writes/file** (batching cuts round-trips; each create still
  counts toward quota).
- Batch floor is bounded by the slowest user: alice 3,118 × 3.6 / 3 ≈
  **~62 min**; whole corpus ≈ **~1h floor**.
- Phase A ran at only **~22% of the achievable ceiling** (5.25 req/s vs
  ~24/sec) — latency/serialization-bound, not quota-bound. Headroom ≈
  4x.
- Greenfield ~60-70 min is achievable via rate-limiter-guarded per-user
  pipelining toward 3/sec/account; "45 min" is below alice's per-account
  floor and not reachable for her corpus.

## Raw source files (on the VPS)

- `/root/mig_run2.log` (R0)
- `/root/phaseA_serverside_job.json`, `/root/phaseA_acl_audit_correct.json` (R1)
- `/root/phaseB_linkflip_job.json`, `/root/phaseB_acl_audit.json` (R2)
