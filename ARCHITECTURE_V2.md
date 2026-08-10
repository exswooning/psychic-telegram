# ARCHITECTURE_V2.md — Greenfield rebuild design (scoped, post-benchmark)

Status: proposal. Written 2026-08-09 after the B-series protocol and the
server_side-wins A/B verdict. It designs the *next* engine, not a rewrite of
the current one mid-benchmark. Nothing here is live; the current
`drive_engine.py`/`webui.py`/`main.py` stack remains the shipped product and
is exclusively what the B-series trials measure.

## 0. Why rebuild at all

The current system works and just won an A/B (server_side: 1.55x faster and
0 ACL leaks vs link_flip). `ARCHITECTURE_V2.md` exists because the current
engine accumulated its capabilities incrementally over many agent sessions:

- transfer modes galore (`download_upload`, `server_side`, `link_flip`),
  each with its own branch in the hot path
- per-service engines (`drive_engine.py`, `gmail_engine.py`, ...) that each
  re-implement retry/audit/resume scaffolding against a shared `db.py`
- a process webui with vendored JS/HTML and `/api/*` endpoints
- preflight/state logic in `webui.py` that overlaps with `phases.py`,
  `wizard.py`, `inventory.py`

A rebuild is warranted for **ergonomics, correctness-by-construction and
lock-step scalability**. The hard ceiling is Google's **3 writes/sec/account**
(not raiseable on request), and every speedup below is bounded by it.

> **Correction (supersedes the original draft of this section).** This
> document was first written carrying a "~2.7h aggregate write-floor". That
> number was wrong and is retracted — it charged the *whole corpus* against a
> *single account's* 3/sec ceiling. The ceiling is **per account**, and the
> engine runs up to `user_workers=8` accounts concurrently, so the aggregate
> write budget is ~24 writes/sec, not 3. See AGENT_COORDINATION.md, 11:05 UTC.
>
> **Corrected floor:** a batch cannot finish before its slowest single user.
> Here that is `alice` at 3,118 files × ~4.4 writes/file ÷ 3 writes/sec ≈
> **~76 min**, so the batch floor is ~1.3h, not 2.7h. Phase A took 13,284s
> (3h41m) — meaning the engine is at **~35% of its own achievable floor**, and
> the remaining win is real rather than imaginary.

## 1. Non-negotiable: the ceilings the design must respect

| Ceiling | Value | Consequence |
|---|---|---|
| Drive writes/sec/account | 3 (sustained), not raiseable | ~4.4 writes/file measured → per-account floor |
| Drive calls/100s | 20,000 per user *and* per project | ~200/s — two orders above current use; reads are effectively free |
| Cross-user concurrency | 8 accounts (`user_workers=8`) | Aggregate budget ≈ 8 × 3 = 24 writes/sec |
| per-user QPS limiter | `per_user_qps=3.0` (`resources.py`) | One shared token bucket per user |
| memory | sized via `resources.py` | fixed, bounded workers |

Design rule: **saturate each account's 3 writes/sec; never exceed it.**

> **Correction (supersedes this section's original rule).** The first draft
> said *"concurrency is across users, not across writes of one user … an
> async rewrite that raises in-file concurrency hits the 429 wall and is
> strictly slower."* The prohibition is wrong and is retracted; only the
> ceiling is real. The measured rate is **0.66 req/s per user against a
> 3/sec ceiling — 4.6x of that account's budget sits unused.** In-user
> concurrency is not what breaches the ceiling; it is the only thing that
> *reaches* it. What must never happen is in-user concurrency **without** a
> shared per-account limiter — that is the 429 storm the original rule was
> reaching for, and the guard is the limiter, not serialism.
>
> Why it matters more than cross-user scaling: a batch cannot finish before
> its slowest single user, and that user is one thread. Raising
> `user_workers` cannot shorten `alice`; only splitting `alice` can.
> Implemented as `drive_file_workers` (machine-derived, 4 on a healthy host)
> — see §5.1.

## 2. Target architecture (one module = one responsibility)

```
src/
  tenant.py        # credentials, scopes, discovery, per-tenant auth LRU
  config_schema.py # declarative YAML/TOML, validated at load (single source)
  ledger.py        # idempotency ledger + audit (write-ahead, batched)
  engines/
    drive.py       # walk / copy / move / acls — server_side ONLY
    gmail.py       # messages / labels / filters
    calendar.py    # events / settings
    chat.py        # spaces / members / messages
    contacts.py    # merged-insights model
    tasks.py       # lists / tasks
  pipeline.py      # async scheduler + cross-user concurrency + backoff
  observability/   # WebSocket progress, metrics, quota telemetry
  ui/              # single-page app, fetch to a small API shim
```

Decisions:
- **Server-side-only.** Drop `download_upload` and `link_flip` entirely.
  Removes dead branches and the link_flip public-exposure footgun out of the
  product. A source account that can only ever be `drive.readonly` is not a
  configuration to keep supporting once the decision is made.
- **One engine skeleton.** Every service implements the same interface
  (`plan()`, `pace()`, `apply()`, `persist()`), so new services stop
  re-implementing retry/audit/resume. Retry policies come from one
  place (see §4), not per-call `@retry` decoration.
- **Single validated config.** One config file (YAML) replaces env.sh +
  per-service flags. `validate_config` at a single boundary; no later branch
  re-checks a string.

## 3. Idempotency by construction

The current ledger keyed on `(source_user, source_id, type)` is the right
shape — a rebuild keeps it and makes every apply path *write the ledger in
the same transaction as the source-of-truth state*, then returns that row to
the caller instead of returning None and hoping.

Ledger row: `(source_user, source_id, type, target_id, parent_target_id,
state, modified_time, attempt, ctime)` with upsert-key on
`(source_user, source_id, type)`.

Concrete rules:
1. An apply that mutates a remote object must record intent in the ledger
   **before** the remote call.
2. The ledger write and the audit write are one SQLite transaction, batched
   (see §5). A crash loses neither partial state consistent in the same
   commit point.
3. Resume = "replay ledger, skip `state='DONE'`", never "walk everything and
   guess what's missing". The drive walk is only for items absent from the
   ledger for this user.
4. `get_target_id` reads the in-memory read-through cache, exactly as the
   current `MigrationDB.preload_mappings` does — that design decision is
   adopted wholesale.

## 4. Pacing & retry: one band, one scheduler

Both the current stack's `per_user_qps` and Google's guidance live in the
**pacer**, one component the engines consult, instead of each engine
interleaving sleeps:

```
pacer = TenantPacer(accounts=[...], write_qps=3.0, read_qps=4.0)
task  = pacer.acquire(account, kind="write")   # token, per-account
```

Retry policy centralised:
- budgets per call category (write vs read);
- exponential jitter back off, capped at the pacer's replenish window;
- a 429 raises `RateLimitError`, which the pacer turns INTO a degraded
  per-account rate — no exception, no storm.

Measured flavor: the A/B retried 1 call per ~70K (essentially zero). ML
retry scheduling is dropped as over-engineering for a near-empty problem.

## 5. Write batching (the two deferred items, now in one place)

These are the two items from the ranked plan intentionally deferred until
the B-series completes (they'd mutate engine code mid-benchmark). The rebuild
puts them in by design:

- **Folder (and apply-path) batching.** The current `_sync_folder` issues one
  `files.create` per folder. In V2 every engine's `apply` stage emits
  idempotent ops into a per-account batch; the pacer drains 20-100/request via
  `BatchHttpRequest` like the ACL chunker (`drive_engine.py:_create_permissions_chunk`)
  already does. Folders are the long pole for large corpora; this removes the
  per-environment round trip.
- **Ledger/audit write batching.** Current `log_audit`/`record_mapping` are
  synchronous single-row transactions under one global write lock. V2
  queues rows per worker and flushes `executemany` in one commit every N rows
  or `T` ms. SQLite is already WAL + `synchronous=NORMAL` + `busy_timeout`;
  only the frequency changes. (Crash-consistency margin: a lost tail re-migrates,
  which the ledger's exact-once-`id_mapping` rejection already handles.)

### 5.1 Intra-user pipelining — the largest remaining win, already shipped

Implemented ahead of the rebuild (it is ~40 lines in the current engine, and
V2 should inherit the shape rather than reinvent it):
`Settings.drive_file_workers`, consumed by `drive_engine._sync_files`.

- Folders stay strictly serial and depth-first — a child's copy needs its
  parent's target id, so parallelising the tree would race the ordering the
  mirror depends on. Only the *files* fan out.
- The pool spans the whole user walk, not one folder. A per-folder pool
  blocked until that folder drained, so the walk alternated N-wide bursts
  with serial folder creates, and a folder holding fewer files than there
  are workers never parallelised at all. A semaphore supplies backpressure
  so the queue cannot grow to the size of the corpus.
- Every call still passes through a per-account `RateLimiter`, so workers
  interleave into the same per-account bucket. Utilisation rises; the ceiling
  does not move. There are **two** write buckets, not one: the copy is issued
  as the source user and everything after it as the target user, and those
  are separate accounts with separate 3/sec allowances. Reads have a third,
  much looser budget (`drive_read_qps`), since they come from the
  20,000-per-100s pool rather than the write ceiling.
- `stats` moved behind `_bump()` + a lock. `d[k] += 1` is not atomic, and the
  lost update would have silently undercounted the failure counters a run is
  judged on.
- `QuotaExhausted` still aborts the whole user rather than degrading to a
  per-file failure, matching the serial path.
- **Default 1 = byte-identical to the serial path**, so deploying it cannot
  perturb an in-flight benchmark trial.

Expected: `alice` from ~164 min latency-bound toward her ~76 min write floor.
To be measured as **B5** against B4's numbers, once B4 Trial B is done.

## 6. Observability with WebSockets

Progress today is `webui.py`'s `/api/status` polling with a server-side cache.
V2: a WebSocket pushes `/progress`, `/ledger`, `/pacer` state deltas from the
(orchestrator) process. The list of 3 screens (`/ui-drive`, etc.) is kept
but the state rounds come from the socket, eliminating both the poll storm
and the `_refresh_snapshot`/preflight dance in `webui.py`.

Scope: UI-only. No migration speed impact, no ceiling collision. The history
screen (from the current UI) is a nice candidate to keep as a built-in, not a
re-implementation.

## 7. Porting horizon

Nothing in the current code turns into a throwaway where it is already right:

- Ledger & id_mapping cache → V2 ledger (copy semantics; the `identity_map`
  snapshot cache is carried over).
- Auth LRU service cache (auth.py) → V2 `tenant.py` near-verbatim.
- Retry/backoff constants + `_retry` bit → V2 scheduler budgets.
- `server_side` copy/move file path → V2 `drive.py` verbatim; the `verify_`
  and `fields=` behavior becomes defaults, not flags.
- Tests: the fake drive/API layer in `tests/fakes.py` is reusable and is
  worth keeping as the test double of record.

V2 explicitly does NOT auto-migrate in bulk; it implements engine-by-engine on
behalf of the existing ledger so a dual-run comparison is possible during
the transition.

## 10. Roadmap (post-Benchmark)

1. **B-series laning precedent set** — Trial B, then both trials validated
   and written to `BENCHMARKS.md`. Only after that, code changes.
2. **Tier-2 write batching** (item §5) onto the current engine — ~15-20% under
   high concurrency; with the A/B's server_side this is already the path to a
   new B5. Do it in the current stack first (fast, measurable), then bake the
   pattern into V2.
3. **V2 skeleton** — module layout + interfaces + one engine (drive) ported,
   running the SAME ledger so a side-by-side B-series run can be produced.
4. **Remaining services** ported one per column, each validated against the
   drive parity rig (`acl_audit.py` + metrics).
5. **UI WebSocket shim** — keep current UI, add the WS endpoint, slimming
   polling.

## 11. Honest accounting

The rebuild's wins are: removing the FUD of three transfer modes, one
retry/rate system, first-class batching in the write path, and real-time
operability.

**On the original pitch's "70-80% faster" claim** — this section previously
called it *"not achievable"*. That verdict rested on the retracted 2.7h
floor and is itself now corrected:

| | value | vs Phase A |
|---|---|---|
| Phase A measured | 13,284s (3h41m) | — |
| Corrected write-ceiling floor | ~4,600s (~76 min, `alice`-bound) | **~65% faster** |
| Original pitch | ~45 min | ~80% faster |

So ~65% is the honest theoretical ceiling for this corpus — the pitch's
70-80% band is *near* the floor rather than fantasy, but 45 min sits
**below** `alice`'s own 76-minute write floor and remains unreachable while
she is a single account. The reachable version of that number is
per-account parallelism (splitting one user's writes across accounts), which
Google's ceiling explicitly forbids.

Corrected position: **a ~2-2.5x speedup is real and mostly unclaimed**, and
§5.1 is the mechanism. V2 is still justified by the other four wins — but it
should no longer be sold as *"speed is impossible, buy ergonomics"*, which
is what this section previously argued.