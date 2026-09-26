# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Google Workspace tenant-to-tenant migration engine (Drive, Gmail, Calendar,
Chat, Contacts, Tasks) that grew a SaaS control plane on top of it. Three
layers, in the order they were built:

1. **The migration engine** (`main.py`, `*_engine.py`, `resilience.py`,
   `config.py`, `db.py`, `auth.py`) — a CLI tool, documented in full in
   `README.md`. Read that file for auth modes, transfer modes, the scope
   manifest, and the runbook; it is accurate and not repeated here.
2. **`webui.py`** — an embedded single-operator dashboard and job runner
   (port 8080) that predates accounts. Still real and load-bearing: it owns
   the one-job-at-a-time `Job` class, the seeder/reset/wipe launchers, and a
   large `/api/*` surface the SPA calls.
3. **`api_server.py`** (FastAPI, port 8090) + **`migration-webui/`** (React
   SPA at `/app`) — the multi-tenant SaaS layer: accounts, sessions, the
   Setup/Seed Wizards, WebSocket-pushed job state. This is where almost all
   product work happens now.

Both backends serve the same origin in production, split by path (see
`Caddyfile`): `/api/v2/*` and `/ws` → api_server.py (8090), everything else
→ webui.py (8080), which also serves the built SPA under `/app`.

## Commands

**Python** (from repo root, `.venv` already created):
```bash
.venv/bin/python -m pytest tests/ -q          # full suite (200+ files; several minutes)
.venv/bin/python -m pytest tests/test_foo.py -q                    # one file
.venv/bin/python -m pytest tests/test_foo.py::TestClass::test_x -q # one test
.venv/bin/python -m pytest data-generator/ -q  # the seeder's own tests (separate conftest.py)
```
No linter is configured for the Python side.

**Frontend** (from `migration-webui/`):
```bash
npm run dev            # vite, local dev server
npm run build           # tsc --noEmit && vite build -- catches type errors the tests won't
npx vitest run          # full suite
npx vitest run src/pages/Foo.test.tsx   # one file
npm run lint            # eslint, zero-warnings
```
Always run `npx tsc --noEmit` (or the full `build`) after a frontend change,
not just vitest — vitest's transform is more lenient than the real compiler,
and a type error in a test file only surfaces here.

**Building for deploy**: `VITE_CP_BASE="" npm run build` — the empty base is
required, or the bundle bakes in `http://localhost:8090` and every API call
breaks in production. `grep -c localhost:8090 dist/assets/*.js` should show
exactly 1 (an inert placeholder string in Settings.tsx), never more.

**Deploying**: `./sync_vps.sh user@host /remote/dir [keyfile]` — rsyncs the
tree (excluding `.venv`, `keys/`, `migration.db`, other live state), installs
`requirements.txt`/`requirements-control-plane.txt` idempotently, syntax-checks
under the target's Python, warns (but does not block) on a dirty tree or an
in-progress `full_setup.py`/`seed_sandbox.py` run that the restart is about to
kill, then restarts the systemd units. `install.sh` is the from-scratch
installer for a box that has never run this before (creates the venv, the
first superadmin account, systemd units, Caddy config).

## Architecture notes that span multiple files

**Two Python versions matter.** Dev happens on macOS with a newer Python
(3.14 at last check); the VPS runs **3.10**. Newer syntax (e.g. nested
same-quote-type f-strings, a 3.12+ feature) parses locally and fails on
deploy. `sync_vps.sh` syntax-checks under the target's interpreter for
exactly this reason — trust that check over local `python -m py_compile`.

**Per-tenant isolation is by directory, not by column.** A SaaS account's
tenant config lives in `keys/{account_id}/{source,target}-sa.json` and
`data/accounts/{account_id}/migration.db` — not as an `account_id` column on
the engine's own tables. This is why the engine modules
(`drive_engine.py`, `gmail_engine.py`, `db.py`, ...) never needed to change
for the SaaS pivot: each account gets its own complete, isolated copy of the
same single-tenant world. `accounts_auth.py` owns this mapping
(`tenant_configs` table, one row per `(account_id, side)`).

**Domain-wide delegation is granted per service-account client ID, not per
account or per domain.** Copying a key file carries its live delegation with
it. `full_setup.py` orchestrates provisioning a Cloud project + service
account + DWD grant end to end (driving a real browser via Playwright for the
Admin Console steps — see `dwd_helper.py`, `gcloud_browser_auth.py`); DWD
propagation after a grant is accepted can take up to ~15 minutes, and
`verify_scopes.py` is the only way to check it (Google exposes no read API for
a delegation entry — it's confirmed by successfully minting a token per
scope).

**`domain_guard.py` protects every domain a setup wizard has ever configured,
automatically, from the seeder and reset/wipe tooling** — protection starts
the moment a `tenant_configs` row is written, not from an opt-in list. A
domain must be explicitly declared a sandbox (`domain_guard.py --revoke`, or
the `DomainSandboxToggle` UI component) before it can be seeded or reset. This
guards two different operations (seeding writes fabricated data; reset/wipe
deletes real data) and `refuse_reason()` takes an `action` argument so the
message names the right one.

**A subprocess launched by `webui.py` or `api_server.py` does not reliably
survive a restart of its parent's systemd unit**, even with
`KillMode=process` set (which only protects the unit's *own* main PID, not
children) — `full_setup.py`'s runs use `start_new_session=True` and mostly
survive; `webui.py`'s `Job` launcher (seed/reset) does not use it at all and
is fully exposed. `sync_vps.sh` checks for either process before restarting
and warns if one is running — **do not deploy while a real migration, setup,
or seed job is in flight** without heeding that warning.

**The dead man switch (`deadman.py`) is a real, destructive automation**,
armed on the production VPS. `require_checkin` mode means *only* a current
TOTP code from the dedicated check-in account resets its countdown — logins,
deploys, and SSH sessions do not count, by design. `totp.py`'s seeds live at
`/etc/bitport/totp.env` (root-only); once an authenticator is enrolled for
the check-in account, the API refuses to issue another one (see
`_refuse_new_authenticator` in `api_server.py`) — recovery from a lost device
is by root editing that file directly, not through the app.

**A run is watched, judged and reported without anyone asking.**
`run_watch.py` (started by `api_server.py`) *observes* `job_admission` — every
job, whether webui.py or api_server.py launched it, registers there, so nothing
in webui.py had to change. It records start/finish in `run_events`, builds a
report when a `migrate`/`delta`/`seed` job ends (`run_report.py` → JSON + a
human PDF + a Claude PDF under `logs/reports/<account>/`), and opens an
**incident** (deduped by fingerprint over 6 h) for: a crash (a signal death is a
*negative* rc — judge `!= 0`, never `> 0`), a clean exit that failed its
benchmarks, a burst of new failures, or a stall. Each incident has a brief in
`logs/incidents/<id>.md` (also `GET /api/v2/incidents/<id>/brief`, the
"Copy brief" button, and `incidents.py show <id>`), plus an append-only
`logs/incidents/feed.log` — tail it, or run a `Monitor` on it, to be told the
moment one opens.
Benchmarks (`benchmarks.py`) have four outcomes; **a check nobody could make is
`unknown`, and a report with any required `unknown` is `UNVERIFIED`, never
`PASS`.** The ledger alone cannot say the tenants agree, so fidelity
benchmarks read from `run_fidelity`, written only by `main.py tally`
(`tally.py`: counts both tenants, spot-checks a sample of DONE users for
checksums/timestamps/sharing). A tally older than the run it is used for is
ignored. Throughput is items written *during the run*, never the ledger total.

**Handling an incident.** Read the brief; reproduce from its evidence; fix on
a **test sandbox tenant**, never a real one; add a test. **Do not deploy
unattended**: `sync_vps.sh` restarts services and a webui-launched seed or a
migration dies with it. Say what you changed and let the operator pick the
moment (an API-only change needs only `systemctl restart bitport-api`, which
does not touch a webui-launched job). Then `incidents.py resolve <id> -m
"<what fixed it>"`. Notifications are opt-in via `INCIDENT_NTFY_TOPIC`,
`INCIDENT_WEBHOOK_URL`, or `INCIDENT_GITHUB_REPO` + `INCIDENT_GITHUB_TOKEN`
(the last opens an issue a Claude Code routine can be pointed at); none is set
by default and a failed send never fails a run.

**Progress reporting must come from real state, never be simulated.**
`full_setup.py` and the seeder write `{pct, label}` checkpoints a poller
reads; `_job_progress()` in `webui.py` computes a percentage only when there
is a real counter to compute it from (e.g. `[done/total]` in the seeder's own
output) and returns `None` rather than guess. `--create-until-full` mode has
no knowable total by design (it creates accounts until Google's API itself
refuses one) — do not add a fabricated percentage for it, show a count
instead. A `0` percentage is meaningful and distinct from "no data yet";
frontend code must check `typeof pct === 'number'`, not truthiness.

**Migration progress is reported as counts, never averaged into one
percentage.** DONE/RUNNING/FAILED/PENDING states, and "items" vs "users",
are shown as separate figures — folding a partially-failed batch into one
number is the misreading `tui.py`'s own design notes (and the SPA that
followed it) are built to prevent.

**Frontend data comes from two independent sources that don't yet share a
model**: `api/client.ts` (polling, talks to webui.py's `/api/*`) and
`api/controlPlane.ts` (WebSocket + REST, talks to api_server.py's
`/api/v2/*`). A page or hook often has to read both (see
`hooks/useRunningJobs.ts`) and reconcile them itself — there is no unified
job-status endpoint yet.
