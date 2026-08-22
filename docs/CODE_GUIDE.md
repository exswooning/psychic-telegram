# Bitport — Code Guide

A Google Workspace tenant-to-tenant migration tool: Drive, Gmail, Calendar,
Contacts, Tasks and Chat, from one Workspace domain to another, using
domain-wide delegation on both sides.

This guide is written for someone who has to change or operate the code. It
favours *why* over *what*, because the what is readable from the source and
the why is where the traps are.

---

## 1. Running it

Three front ends over one engine.

| surface | entry point | for |
|---|---|---|
| CLI | `main.py <command>` | operators, scripting, the real workhorse |
| Web (SPA) | `api_server.py` + `migration-webui/` | SaaS clients and operators |
| Terminal UI | `tui.py` | live watching; reads `migration.db` directly, independent of both web backends |

```bash
# the usual sequence
./.venv/bin/python main.py init-db --auto-map --include-missing
./.venv/bin/python main.py provision-users --tenant target --dry-run
./.venv/bin/python main.py migrate --account-id 7
./.venv/bin/python main.py delta   --account-id 7 --days 2
./.venv/bin/python main.py verify  --account-id 7
```

`--services` defaults to `all`. `--account-id` selects a tenant pair in the
multi-tenant SaaS mode; without it the legacy single-tenant config is used.

---

## 2. Architecture

```
                 ┌───────────────────────────────────────┐
  CLI  ─────────▶│  main.py    orchestration, dispatch    │
  API  ─────────▶│             worker pool, watchdogs     │
                 └──────────────┬────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
   drive_engine           gmail_engine          calendar/contacts/
   (the big one)          chat_engine            tasks engines
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                ▼
              auth.py  ── delegated clients, per tenant
              resilience.py ── retry, rate limits, quota guard
              db.py    ── the ledger (id_mapping, audit_log)
```

**The ledger is the product.** `id_mapping` makes every operation idempotent
and every run resumable; `audit_log` is the record of what was attempted and
why it failed. Most of the hard-won behaviour in this codebase is about
keeping those two honest.

---

## 3. Module reference

### Core engine

| module | purpose |
|---|---|
| `main.py` | Orchestration and CLI. Worker pool, per-user dispatch, memory watchdog, metrics flusher, ledger preflight. |
| `db.py` | The ledger: `id_mapping`, `audit_log`, `identity_map`, `upload_ledger`, `run_metrics`. Schema plus in-process mapping cache. |
| `auth.py` | Domain-wide delegation for both tenants. Hands out per-thread API clients (httplib2 is not thread-safe). |
| `config.py` | Every tunable, the scope lists, and machine-derived defaults via `resources.py`. |
| `resilience.py` | Retry/backoff, `RateLimiter`, `AdaptiveRateLimiter`, and the persisted 750 GB/day upload guard. |
| `resources.py` | Sizes worker pools to the actual host: usable RAM, cgroup limits, swap pressure. CPU deliberately excluded. |
| `metrics.py` | Per-request latency and throughput, recorded where every Google call passes. |

### Per-service engines

| module | notes |
|---|---|
| `drive_engine.py` | The big one. Recursive mirror, three copy strategies with fallback, ACL translation, comments, shortcuts, staging drives. |
| `gmail_engine.py` | `messages.insert`, label mapping, drafts, filters. |
| `calendar_engine.py` | `events.import_`, preserving original ids. |
| `contacts_engine.py` | People API. Strips source-tenant metadata before insert. |
| `tasks_engine.py` | Task lists and tasks. |
| `chat_engine.py` | Spaces, memberships, messages. Import mode. |
| `shared_drives.py` | Shared Drives, which the per-user engines cannot reach. |

### Setup and provisioning

| module | purpose |
|---|---|
| `full_setup.py` | One tenant, one call: project → APIs → service account → key → DWD → verify. |
| `provision_gcp.py` | The Cloud side from nothing: projects, APIs, service accounts, keys, IAM. |
| `dwd_helper.py` | Automates the one DWD step Google gives no API for, via Playwright. |
| `gcloud_browser_auth.py` | Authenticates `gcloud` itself non-interactively as the same admin. |
| `provision.py` | Creates user accounts via the Directory API. Only ever `users().insert()`. |
| `ensure_apis.py` | Are the Cloud APIs this migration calls actually enabled? |
| `verify_scopes.py` | Which DWD scopes are *actually* authorised, one at a time. |
| `scope_guard.py` | Refuses to start a run that will die on a missing scope. |
| `oauth_store.py` | Tenant-wide OAuth credentials, for the admin-consent path. |

### Verification and repair

| module | purpose |
|---|---|
| `verify.py` | Post-migration comparison, source vs target. |
| `ledger_verify.py` | **Does the ledger still describe reality?** Catches target accounts recreated since the ledger was written. |
| `acl_audit.py` | Proves, file by file, that share access survived. |
| `acl_reconcile.py` | Resolves ACL failures the target says are no longer failures. |
| `resolve_failures.py` | Re-attempts `FAILED` rows with more context. |
| `coverage_audit.py` | Which supported data types does this source actually contain? |
| `contract_probe.py` | Checks the assumptions `tests/fakes.py` encodes against the real APIs. |
| `repair_modified_times.py` | One-off repair for data migrated before the modifiedTime fix. |

### Reset and teardown

| module | purpose |
|---|---|
| `reset_drive_ledger.py` | Clears a user's Drive resume state so a re-run actually re-runs. |
| `reset_target.py` | Empties the target of migrated test data for a rehearsal. |
| `undo_migration.py` | Deletes exactly what a migration created, using `id_mapping` as the manifest. |
| `teardown_tenant.py` | The reverse of `full_setup`: delete the project, revoke delegation. |

### Control plane and UI

| module | purpose |
|---|---|
| `api_server.py` | FastAPI + WebSocket control plane. The SPA's backend. |
| `control_plane_db.py` | Ledger access shaped for the control plane. |
| `accounts_auth.py` | SaaS accounts: signup, login, sessions, per-account data dirs. |
| `job_admission.py` | Cross-account admission — `MAX_CONCURRENT_TENANT_JOBS` on a shared box. |
| `user_claims.py` | Who migrates which user, when more than one machine is working. |
| `fleet_agent.py` | Heartbeat from each node. |
| `webui.py` | The older browser front-end (legacy dashboard). |
| `webui_spa.py` | JSON payloads shaped for the React app. |
| `tui.py` | Curses dashboard. Reads `migration.db` directly. |
| `migration-webui/` | React 18 + MUI SPA. |

### Measurement

| module | purpose |
|---|---|
| `inventory.py` | Counts everything per user before a byte moves. |
| `tenant_inventory.py` | Accounts per tenant and data per account. |
| `discovery.py` | Pre-migration discovery scan. |
| `benchmark_run.py` | Wipes, re-migrates, measures and *judges* a Drive run. |
| `ab_transfer.py` | `server_side` vs `download_upload` on the same corpus. |
| `test_report.py` | Runs the suite, parses JUnit, serves it to the UI. |

---

## 4. Data model

```sql
identity_map   source_email → target_email, status, services_done, status_at
id_mapping     (source_user, source_id, type) → target_id
audit_log      every attempt: status, error_message, bytes_moved, timestamp
upload_ledger  bytes sent per target user per UTC day (the 750 GB cap)
run_metrics    process-wide latency/throughput samples, last hour
```

Two distinctions that matter constantly:

- **`id_mapping` is what currently exists. `audit_log` is what was ever
  attempted.** They diverge exactly when the target loses data the ledger
  still claims — which is what `ledger_verify` exists to detect.
- **`identity_map.status` is per user; `services_done` is per service.** A
  phased run that finished Drive once marked everyone DONE, and every later
  phase skipped them all.

---

## 5. Invariants worth not breaking

These are each the residue of a real incident.

**Never average DONE / RUNNING / FAILED / PENDING into one percentage.** They
coexist in every real run and mean different things. Show counts.

**A skip is not a failure.** `SKIPPED_UNEXPORTABLE`, `SKIPPED_EXPORT_TOO_LARGE`,
`SKIPPED_NO_PERMISSION` are decisions. Colouring them red teaches people to
ignore red.

**`log_audit` upserts on `(source_user, item_id, item_type)`.** A later
SUCCESS overwrites an earlier FAILED. Without this the ledger keeps every
stumble and none of the recoveries — one run reported 127,852 failed ACLs on
files whose sharing was completely intact.

**Rate limits are scoped like the quota, not like the code.** A per-user
bucket does nothing about a per-project quota. And a batched call costs *N*
quota units, not one — charging a 20-grant batch as a single token let the
real rate run 20× over the configured ceiling.

**Never let a bare `except Exception` stand in for a value.** Twice in one
day this hid a real defect: a missing `import time` silently downgraded every
worker pool, and an invented function name rendered as a tidy error string
instead of the 500 that got it fixed. Catch what you expect; let bugs surface.

**Sizing constants must derive from their inputs.** `MB_PER_WORKER` was 320,
sized for a 100 MB download chunk that had since been pinned to 8 MB. The
comment explaining the derivation sat directly above the stale number.

**Deleting a Workspace user deletes their Drive and Gmail.** Recoverable for
20 days. Recreating an account with the same address does *not* restore
anything, and leaves the ledger pointing at items that no longer exist.

---

## 6. Operations

```bash
# deploy (rsync + systemd restart + frontend bundle prune)
./sync_vps.sh root@<ip> /root/migration ~/.ssh/<key>

# is the ledger still true?
python main.py --account-id N verify-ledger            # report
python main.py --account-id N verify-ledger --reopen   # forget stale mappings

# what is the box doing
python main.py --account-id N report
python tui.py
```

Services on the VPS: `bitport-api` (8090), `bitport-webui` (8080), fronted by
Caddy. A migration started from the CLI registers itself in `active_jobs`, so
the dashboard and the concurrency cap both see it.

---

## 7. Testing

```bash
./.venv/bin/python -m pytest tests/ -p no:randomly     # ~1,640 tests, ~3 min
cd migration-webui && npx vitest run                   # ~121 tests
```

`tests/fakes.py` is a hand-written fake of the Google APIs. It is load-bearing
and it has been wrong: `corpora="drive"` once ignored the caller's query,
which made a shared-drive walk recurse until it hit the depth limit.
`contract_probe.py` exists to check the fake's assumptions against the real
APIs.

Two habits this codebase learned the hard way:

- **Assert behaviour, not source text.** Tests that did `src.index("...")`
  broke on formatting and passed on real bugs.
- **Check that a guard can fail.** A test written to catch a bug, that passes
  when the bug is reintroduced, is worse than no test.
