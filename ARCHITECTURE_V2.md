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
lock-step scalability**, NOT for a raw speed fantasy: the hard ceiling is
Google's **3 writes/sec/account** (not raiseable on request). For the current
corpus the measured write-floor is ~2.7h aggregate; no architecture moves that
without a Google-side change. Every speedup below is on top of that floor and
is honestly bounded by it.

## 1. Non-negotiable: the ceilings the design must respect

| Ceiling | Value | Consequence |
|---|---|---|
| Drive writes/sec/account | 3 (sustained) | Copy path ~2.4 writes/file avg → per-account serial floor |
| Cross-user writ right | ~8 users concurrent | Parallelism multiplier, already `user_workers=8` |
| per-user QPS | ~4 req/s avg | Rate limiting is per-account, not global |
| memory | sized via `resources.py` | fixed, bounded workers |

Design rule: **concurrency is across users, not across writes of one user.**
An async rewrite that raises in-file concurrency hits the 429 wall and is
strictly slower. Async here buys *connection reuse and batching*, not
concurrency, for a single account's write path.

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
operability. The claimed 70-80% speed reduction of the original greenfield
pitch is **not achievable** under Google's 3-writes/sec/account ceiling for
Drive writes; V2 is justified by the other four, not by that number.