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

**Deploying**: `./sync_vps.sh user@host /remote/dir [keyfile]` — first dry-runs
by *content* to see what would change on the box. **If nothing that runs changed
(the frontend, tests, docs) it restarts nothing and touches no job** — a colour
change must never cost a twelve-hour seed. Any other file restarts the units and
the output names the files that forced it; `FORCE_RESTART=1` restarts regardless.
It then rsyncs the tree (excluding `.venv`, `keys/`, `migration.db`, `logs/`,
`node_modules/`, other live state), installs
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

**Stop is cooperative, and only looks between items.** SIGINT sets a flag the
Drive walk reads between files, so one file with a hundred slow grants keeps a
"stopped" run alive for an hour. The Jobs page's second press on the same run
sends SIGKILL (`POST /api/v2/jobs/{pid}/stop` with `force`, audited as
`job.force-stop`, refused for any pid `webui._external_processes()` does not
list). Work already in the ledger survives; a file mid-copy does not.

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

**Who moves the mail is a per-run choice (`mail_mode`), and the dialog defaults to
`split`.** `engine`: this tool moves all mail (the API's own default). `dms`:
Google's Data Migration Service does, and the run migrates everything else.
`split`: the engine inserts only mail that carries a Drive link and rewrites it;
the rest is written `SKIPPED_NO_DRIVE_LINK` (`config.DEFERRED_TO_DMS`) and moved by
the DMS **after** the run — before, and the DMS moves link mail unrewritten and the
engine then adopts that copy. Two invariants make split safe, both decided in
`api_server._mail_plan`: the run is `--ordered` (Drive for *every* user, then mail,
then the rest — a link names whoever owned the file, and an interleaved run reads
mail before other users' Drive has migrated, leaving those links on the source
tenant forever), and rewriting is forced on. **Deferred mail is owed, not
declined**: `tally`, the report and the migrations page count it apart from
`SKIPPED%` decisions, or a split run that never reached the DMS would score mail
parity 100%. Status is per user, not per service, so an ordered run prints `PASS
i/n pid=…` markers and the detail page says which pass the "users done" counts
belong to. **The DMS starts on its own** (`StartMigration.dms_after`, default on):
after a *clean whole-tenant split run* it is launched as a job named `dms` by
`api_server._follow_on` (the follow-on rides `_start_admitted`'s waiter, and the
queue payload, so a job that waited for a slot still does it), beside a `dms` run
straight away. It only asks the source admin to approve a connection and waits
(`dms_migrate.py --apply --watch`); nothing moves until they do. It does **not**
start over a failed, running or blocked user — their link mail would cross the DMS
unrewritten — and says why as a `dms_not_started` incident. Never for a sample, a
dry run, or a chosen few users. Its identities come from the account's own ledger
(`_export_identities_csv`), not the repo-root `identities.csv` the seeder leaves.

**Every user is verified as they finish** (`verify_sample.verify_user`, started from
the end of `main.migrate_user`, gated by `VERIFY_ON_COMPLETE`, default on): a
bounded, evenly spaced sample (`VERIFY_SAMPLE_PER_SERVICE`, 25) of each service that
pass finished, compared against both tenants on two daemon threads of their own
(`main.VERIFY`), so a check never holds a migration worker and can never fail one.
The run drains the queue before it leaves its registration, so the job reads as
running while it checks. Results are one row per (user, service) in the account's
ledger (`user_verification`), served by `GET /api/v2/one-to-one` and shown on the
**One-to-one** page, which can also run it again (`POST .../run`, a job named
`verify`). Rules that must hold: a user nobody checked is `NOT_VERIFIED`, never a
blank; a check that could not be made is `INCOMPLETE`, never a pass; a sample says
how much of the user it covered; strays are only judged against the *whole* ledger,
so sampling never turns the rest of a migration into strays. `verify_sample.py`'s
CLI exits 0 whenever the check ran — a non-zero exit reads as a crash to
`run_watch`. A sample migration (`SAMPLE_LIMIT`) keeps its own `--verify-after`
report under `logs/quick/`. `reset_drive_ledger` clears a service's verification
with the items it describes.

**What happens to an item after it lands is one step, `drive_engine._finish_item`**:
sharing, comments, then the modifiedTime those writes moved. Each stands alone (an
exception used to be logged at DEBUG by `_sync_with_fallback` and dropped, so the
time was never restored). **A comment written to a Doc or Sheet moves its
modifiedTime ~3 minutes later, to the comment's own write time** (measured on a
scratch tenant; grants, a bare restore and a bare create never do), overwriting any
restore made in between — so `_verify_modified_times` checks only files that had
comments, and only after `MTIME_SETTLE_SEC` (240) since the last one, then puts back
what moved. The ledger calls an item done the
moment it lands — before its sharing runs — so an item is marked `acl_pass` PENDING
first and cleared when the sharing has run; a resume finishes what is still pending
and does not re-attempt grants already decided. Only an interrupted item keeps the
mark, so a ledger from before it reads as finished, as it always did.
`gmail_engine._ascii_headers` sends a draft's non-ASCII headers as encoded words:
`drafts.create` reads raw 8-bit header bytes as Latin-1, one more layer of mojibake
per copy.

**Only Google is implemented** (`providers.py`). OneDrive for Business and Zoho
WorkDrive are scaffolds that refuse by name, and `tests/test_providers_scaffold.py`
fails if one is marked implemented while its operations are still stubs. No engine
imports it yet; its endpoints have never been run against a real tenant.

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
