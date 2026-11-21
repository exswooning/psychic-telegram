# Benchmark truth table (P2.8 a)

Every check in `benchmarks.py` has a row in `tests/test_benchmark_truth_table.py`; a check with no
row fails `test_every_check_has_a_row`. Each row is proven offline through a temp ledger
(`run_report.collect_facts`; a seed through `collect_seed_facts` on a transcript):

- **Control**: every check reads PASS and the verdict is PASS.
- **Fault**: the check reads the status below; a FAIL makes the verdict FAIL, a WARN leaves it PASS.
- **Missing evidence**: the check reads UNKNOWN. A required UNKNOWN makes the verdict UNVERIFIED
  (never PASS); an optional one is listed as unknown and does not block.

`WARN` is the highest a warn-only check (`bad = None`) can read. That is by design: a quota
pushback, a licence block or a noisy seed is information, not a failed run (the engine retries; a
re-run collects the blocked items).

## Migration (14)

| Check | Reads | Fault proven (crafted input) | Live fault | Missing evidence |
|---|---|---|---|---|
| `run_completed` (req) | `run.nonzeroExit` (job return code) | return code -6 -> FAIL | F-kill | no return code -> UNVERIFIED |
| `migrated_something` (req) | `ledger.succeeded` | 0 SUCCESS rows -> FAIL | crafted: a run that copies nothing (not caused live) | ledger unreadable -> UNVERIFIED |
| `item_failure_rate` (req) | `ledger.failureRate` (FAILED+BLOCKED over attempts) | 100 failed of 700 -> FAIL | F-suspend, F-net-12 (failed items) | ledger unreadable -> UNVERIFIED |
| `users_failed` (req) | `users.failedShare` (identity_map FAILED) | 1 of 3 users FAILED -> FAIL | F-suspend | no users -> UNVERIFIED |
| `blocked_items` | `ledger.blocked` | 5 BLOCKED -> WARN | F-licence (unit test if N leaves no spare) | ledger unreadable -> unknown |
| `retry_rate` | `metrics.retryRate` (latest `run_metrics`) | 200/1000 retries -> FAIL | F-rate, F-net-3 | no `retries` -> unknown |
| `quota_pushbacks` | `metrics.pushbacks` (sum of limiter backoffs) | 7 backoffs -> WARN | F-rate (WARN by design) | no limiters -> unknown |
| `p95_latency` | `metrics.p95` | 12 s -> FAIL | F-rate, F-net-3 | no p95 -> unknown |
| `items_per_min_per_worker` | `perf.itemsPerMinPerWorker` (this run's items over its own duration, per busy worker) | 600 items in 10 h -> FAIL | crafted: a throttled run (F-rate may reach it) | no job timing -> unknown |
| `count_parity` (req) | `fidelity.countParity` (latest tally, taken after the run began) | 0.95 -> FAIL | F-tamper (delete a copy, a contact, a task list) | key absent, no tally, or a tally from before the run -> UNVERIFIED |
| `checksum_failures` (req) | `fidelity.checksumFailures` | 1 -> FAIL (no grace) | F-tamper (a changed copy) | UNVERIFIED |
| `acl_fidelity` (req) | `fidelity.aclFidelity` | 0.90 -> FAIL | F-tamper (remove a grant) | UNVERIFIED |
| `extra_grants` (req) | `fidelity.extraGrants` | 1 -> FAIL (no grace) | F-tamper (add a grant) | UNVERIFIED |
| `timestamps_preserved` | `fidelity.timestampsPreserved` | 0.50 -> FAIL | F-tamper (touch a file) | key absent -> unknown |

`(req)` = required. With no tally at all the four required fidelity checks are the exact
`unverified` list.

## Seed (7)

| Check | Reads | Fault proven (crafted transcript) | Live fault | Missing evidence |
|---|---|---|---|---|
| `run_completed` (req) | `run.nonzeroExit` | return code -9 -> FAIL | F-kill, F-webui (a seed restarted under it) | no return code -> UNVERIFIED |
| `users_finished` (req) | `seed.finishedShare` | 2 of 4 users done -> FAIL | F-webui | no users in the transcript -> UNVERIFIED |
| `never_finished` (req) | `seed.neverFinished` (only once the run has ended) | 2 never reported -> FAIL | F-webui | run not ended -> UNVERIFIED |
| `service_failures` | `seed.failedServiceShare` | 2 of 4 users with `chat failed` -> FAIL | crafted: chat 404 | no finished users -> unknown |
| `fill_users_failed` | `seed.fillFailedShare` | one top-up refused with storageQuotaExceeded -> FAIL | crafted: pool exhaustion | no top-ups -> unknown |
| `warnings_per_user` | `seed.warningsPerUser` | 6 warnings over 4 users -> WARN | crafted | no finished users -> unknown |
| `fill_reached` | `seed.fillReached` (only once the run has ended) | 100 of 383 GB -> FAIL | crafted: pool exhaustion | no fill beat -> unknown |

## Findings

No benchmark needed fixing: every check can be made to fail or warn, and none reads PASS on
missing evidence. Three checks cannot FAIL by design (`blocked_items`, `quota_pushbacks`,
`warnings_per_user`: `bad = None`); P2.8 reads WARN as their caused result.
