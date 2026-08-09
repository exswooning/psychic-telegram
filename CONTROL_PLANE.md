# Migration Command Center

Operator control plane for the tenant-to-tenant migration. Additive: it runs
beside the existing `webui.py` rather than replacing it, so a node can execute
migrations with the control plane switched off, stopped, or crashed.

---

## 1. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Browser — React 18 + TS + MUI            (migration-webui/)          │
│                                                                      │
│  FleetDashboard ── JobController ── ForensicModal ── EmergencyBrake  │
│         └──────────── ReasonCodeDialog (every write) ───────────┘    │
└───────────┬────────────────────────────────────┬─────────────────────┘
            │ WS /ws  (server → client push)     │ REST /api/v2/* (commands)
            │ sub-second, no client polling      │ each write: RBAC + Reason
            ▼                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│ api_server.py — FastAPI + uvicorn, 127.0.0.1:8090                    │
│                                                                      │
│  ┌────────────┐  ┌──────────────────┐  ┌─────────────────────────┐   │
│  │  WS Hub    │  │ _gated()         │  │ read models             │   │
│  │  fan-out   │  │ RBAC → log       │  │ user_progress()         │   │
│  │            │  │ intent → run     │  │ forensic_detail()       │   │
│  │            │  │ → patch outcome  │  │ fleet(), failures()     │   │
│  └─────▲──────┘  └────────┬─────────┘  └───────────┬─────────────┘   │
│        │                  │                        │                 │
│  ┌─────┴──────┐           │ run_in_executor        │ run_in_executor │
│  │ _tailer()  │           │ (never blocks loop)    │                 │
│  │ 1 reader,  │           │                        │                 │
│  │ N clients  │           │                        │                 │
│  └─────▲──────┘           │                        │                 │
└────────┼──────────────────┼────────────────────────┼─────────────────┘
         │ read-only WAL    │ subprocess.Popen       │ read-only WAL
         │                  │ (detached)             │
         ▼                  ▼                        ▼
┌─────────────────┐  ┌──────────────────┐  ┌────────────────────────┐
│  migration.db   │  │ main.py migrate  │  │ webui.py :8080         │
│  WAL mode       │◄─┤ drive_engine.py  │  │ (stdlib, untouched,    │
│  ┌───────────┐  │  │ …                │  │  still serves the      │
│  │ engine    │  │  │ hours-long I/O   │  │  existing pages)       │
│  │  tables   │◄─┼──┤ OWN PROCESS      │  └────────────────────────┘
│  ├───────────┤  │  └──────────────────┘
│  │ control-  │  │
│  │  plane    │◄─┤ (only api_server writes these)
│  │  tables   │  │
│  └───────────┘  │
└─────────────────┘
         ▲
         │ POST /api/v2/fleet/heartbeat  (push, every 30s)
┌────────┴─────────┐
│ fleet_agent.py   │  one per node, stdlib only
└──────────────────┘
```

### Why this cannot block the migration

The spec's real question. Four properties, each load-bearing:

| # | Property | Why |
|---|---|---|
| 1 | **Engines are subprocesses, never coroutines** | `Popen(["python","main.py",…])`. No engine code runs in the event loop, so a 4-hour copy cannot stall an HTTP request — or vice versa. |
| 2 | **Every read is a read-only WAL handle** | SQLite WAL gives readers lock-free concurrency with one writer. A read/write handle from the dashboard would eventually block the engine's writer mid-copy. Enforced in `control_plane_db.ro()` and pinned by a test that asserts `CREATE TABLE` raises `readonly`. |
| 3 | **One tailer, N clients** | A single background task reads the ledger and broadcasts diffs. 50 open browsers = 1 DB read/tick, not 50. |
| 4 | **Blocking calls go to a threadpool** | Every SQLite read and every `Popen` goes through `run_in_executor`, so a slow disk delays one request, not the loop. |

**On "NO polling":** the engines emit no events, so *something* must read the
ledger. The honest design is that the **server** tails once per second and
**pushes**; clients never poll. Claiming zero polling anywhere would be false.

### Deviations from the spec, and why

- **MUI, not shadcn/ui.** The SPA is entirely MUI on a Material-3 token system
  (`theme/index.ts` ← `DESIGN.md`). shadcn needs Tailwind + Radix — a second
  component library and a second design language in one bundle, for
  DataTable/Dialog/Toast primitives already in use. Cost real, benefit ~zero.
- **FastAPI accepted but quarantined.** `webui.py` promises stdlib-only ("no
  pip install on a migration host at 2am"). Overridden *only* for this
  process, because hand-rolling WebSockets on `http.server` is not
  maintainable. Deps live in `requirements-control-plane.txt`, so the
  migration path keeps its guarantee.
- **Fleet features are built for a fleet of one.** The schema and
  registration path are real; the dashboard is not load-bearing until a
  second node exists.

---

## 2. Safety model

Every write goes through one function, `_gated()`:

```
RBAC check ──► REFUSED?  ──► log the refusal, 403
     │                        (who *tried* matters)
     ▼
log intent (operator_actions_log, outcome=PENDING)   ← BEFORE the action
     ▼
run off-loop
     ▼
patch outcome (OK | FAILED) + broadcast ACTION_COMPLETE
```

Intent is logged **before** execution deliberately: an action that crashes the
process still leaves a row saying who tried what and why. A log that only
records successes is a scoreboard, not an audit trail.

Three independent enforcement points for the Reason Code, so no single
mistake removes it:

1. **Type layer** — `reason` lives on the `WriteAction` base model. A new
   write endpoint cannot be declared without inheriting it. Missing → `422`
   before any handler logic runs.
2. **DB layer** — `begin_action()` raises on a blank/whitespace reason, so a
   script bypassing FastAPI still cannot write unattributed history.
3. **UI layer** — `ReasonCodeDialog` is the single confirmation component; a
   new dangerous button cannot ship without it.

Destructive actions add a **typed confirmation** (`REVERT`) on top. An OK
button gets clicked by reflex; a word has to be read and typed.

**RBAC is anti-accident, not anti-attacker.** The real access control is the
SSH tunnel — you cannot reach :8090 without a key to the box. This layer stops
a viewer fat-fingering a tenant wipe. Unlisted operators default to `viewer`
(fail closed).

---

## 3. WebSocket protocol

Envelope, every frame:

```jsonc
{ "type": "<EVENT>", "ts": "2026-08-09T12:00:00Z", "data": { } }
```

| Event | When | `data` |
|---|---|---|
| `SNAPSHOT` | on connect | `{ users[], nodes[], publicShares }` — full state, so a client joining mid-run is immediately correct instead of blank |
| `JOB_PROGRESS` | tailer sees a change | same shape as `SNAPSHOT`; **only sent on diff**, so an idle run does not push an identical frame forever |
| `NODE_HEARTBEAT` | node posts | the heartbeat body (`node_id`, cpu/ram/disk, `active_job`, `job_pid`, `code_commit`) |
| `CRITICAL_ALERT` | public shares increase | `{ kind:"PUBLIC_SHARE_DETECTED", count, sample[], message }` |
| `ACTION_COMPLETE` | any write finishes | `{ actionId, action, outcome, actor, reason, detail }` |
| `TAILER_ERROR` | tailer raised | `{ error }` — surfaced rather than swallowed, so a silently dead feed is visible |

Client → server: only `"ping"` keepalive. The server is push-only.

Reconnect is exponential backoff capped at 30s, then re-`SNAPSHOT`. The
control plane shares a host with the migration, so restarts during a long run
are normal, and several dashboards in a tight reconnect loop would be a
self-inflicted load spike on a busy box.

---

## 4. Schema (`migrations/001_control_plane.sql`)

Purely additive — every statement is `CREATE … IF NOT EXISTS` on tables no
engine reads. Applied for the first time against a database with a live
migration in flight. A test asserts the file contains no `DROP`, `DELETE FROM`
or `ALTER TABLE` against engine tables.

| Table | Purpose |
|---|---|
| `operator_actions_log` | who / what / why / outcome. `reason` is `NOT NULL` with no default. |
| `fleet_nodes` | one row per node, last-write-wins. Liveness is **derived on read** from `last_seen` — a crashed node cannot mark itself down, which is the whole failure mode. |
| `public_share_watch` | open public grants. Row written when observed, cleared when revoked, so "how many public files right now" is an indexed count, not a live crawl of both tenants. |

---

## 5. Partial failure

The state the operator actually faces — on this tenant pair a batch is
routinely **7 DONE / 1 RUNNING / 2 FAILED at once**. Two design consequences:

- **No single batch percentage.** Averaging 9 users hides which need
  attention, and two permanently-dead accounts would make the batch look
  broken forever. Counts render as separate chips; progress stays per-row.
- **Percent is of *attempted*, not discovered.** A discovery figure can be
  stale or missing, and a percentage against a denominator we cannot defend
  is worse than one we can.
- **`supersededBySuccess`.** A `FAILED` audit row whose item has an
  `id_mapping` row was already fixed by a later pass. `ForensicModal`
  withholds Retry there and says why, instead of inviting an operator to redo
  finished work.

---

## 6. Running it

```bash
# once, on the control-plane host only
pip install -r requirements-control-plane.txt

# who may write. unlisted = viewer.
export CP_OPERATORS="aryan:admin,teammate:viewer"
python3 api_server.py --port 8090          # 127.0.0.1 only

# on every migration node
python3 fleet_agent.py --api http://localhost:8090 --interval 30

# reach it the same way as webui.py
ssh -L 8090:localhost:8090 root@<vps>
```

Frontend: `VITE_CP_BASE` (default `http://localhost:8090`), route `/command`.

---

## 7. Implementation plan

Written to be safe against a live benchmark. **Steps 1–4 were executed with
B4 Trial A running**; nothing in them touches a running job.

| # | Step | Risk | Status |
|---|---|---|---|
| 1 | New files only (`api_server.py`, `control_plane_db.py`, `fleet_agent.py`, `migrations/`, 6 UI files) | none — nothing imports them yet | **done** |
| 2 | Apply additive migrations | none — `IF NOT EXISTS`, engine tables untouched | **done** |
| 3 | Wire `/command` route + one nav item | none — existing routes unchanged | **done** |
| 4 | Tests (17 control-plane, pinning the safety properties) | none | **done** |
| 5 | Install deps + start `api_server.py` on the node | low — separate port, separate process | pending |
| 6 | Start `fleet_agent.py` | low — push-only, fails quiet if API is down | pending |
| 7 | Populate `public_share_watch` from `acl_audit.py` output | low — read path exists, writer not yet wired | **pending, gap** |
| 8 | Multi-node config push | deferred — one node today | not started |

### Known gaps

- **`public_share_watch` has no writer yet.** `EmergencyBrake` reads it and
  the kill switch works, but nothing populates the table — `acl_audit.py`
  needs to write its `extra_grants` findings there. Until then the panel
  reads green because the table is empty, *not* because the tenant is
  verified clean. That distinction matters and should be closed before the
  panel is trusted.
- **Config push (spec item 5) is not built.** Multi-VPS `env.sh` distribution
  is deferred; `deploy_remote.py` already covers single-node deploys.
- **`unpublish_target.py` exists only on the VPS**, not in git — the kill
  switch reports failure loudly if the script is absent rather than claiming
  a success it did not achieve.
