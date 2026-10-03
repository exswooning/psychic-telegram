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

**Seed and migrate each have their own key on the source, each delegated exactly
its own scope set** (`separate_credentials.py`). Delegation is one list per
service-account client ID, so one shared key meant the migration's source
credential could write, and "narrow for migrate" was an overwrite the next seed
undid. Now: the account's `source-sa.json` is delegated exactly
`verify_scopes.migrate_source_scopes()` -- what a migration requests, every pass on
and both offered transfer modes (`drive`, for a server-side copy, is its only write
scope; SSO reads SAML profiles with `inboundsso.readonly`, and the app-grant list
behind `admin.directory.user.security` is a report, not a migration step) -- and that is
also what `required_scopes(source)` (the run's gate) and `grant_scopes(source)` (every
grant path) return. `seed-sa.json` (same Cloud project, found by project id, so
accounts sharing a source tenant share it) holds `separate_credentials.seed_scopes()`.
The seeder always uses it (`seed_sandbox._use_own_key`) and on a tenant without one
creates it first (gcloud signed in as the source admin through `gcloud_browser_auth`,
then both entries written by `dwd_helper --no-merge`, then probed until propagated).
A migrate launch narrows the source entry back to exact if a seed write scope ever
mints on it again (`main._gate_on_delegation` -> `narrow_if_wide`). The target is
unchanged: it is written to in every mode.

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
moment one opens. `run_watch.list_runs` reads that same `run_events` table back
out as a full history (paired started/finished by `(job_name, pid)`, newest
first) for the **History** page (`GET /api/v2/history`) — every run of every
kind this account has ever had, not a recent window. `repair` is the one job
kind absent from `run_events`: `_start_repair` runs as a plain background
thread rather than an admitted subprocess, so it never registers with
`job_admission` and keeps its own small `repair_runs` table in the account's
own ledger instead — merged in by the same History endpoint rather than
changing how repair is launched.
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
`split`.** `engine`: this tool moves all mail. A caller that names no mode gets `split` for a whole-tenant run that moves both Drive and mail, `engine` for anything else (a sample, a few users, no Drive or no mail; `api_server._default_mail_mode`). `dms`:
Google's Data Migration Service does, and the run migrates everything else.
`split`: the engine inserts only mail that carries a Drive link and rewrites it;
the rest is written `SKIPPED_NO_DRIVE_LINK` (`config.DEFERRED_TO_DMS`) and moved by
the DMS **after** the run — before, and the DMS moves link mail unrewritten and the
engine then adopts that copy. **Ordered passes are the rule for every mode, not only split**: `_mail_plan` orders any run that is rewriting links and has Drive plus mail or calendar (a calendar description carries Drive links too); with rewriting off ordering would only cost the interleaving. Two invariants make split safe, both decided in
`api_server._mail_plan`: the run is `--ordered` (every target account the run needs
created first -- `main._ensure_run_accounts` -- then Drive for *every* user, then mail,
then calendar, contacts, tasks and chat; `main.ORDERED_PASSES` — a link names whoever owned the file, and an interleaved run reads
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

**Repair also rides along on its own, on any real (non-dry) run** — `then` on
`_run_admitted`/`_start_admitted` is a list of follow-on kinds now, not one, run in
order (`_wait_then_release` iterates it; one failing does not block the next): a
whole-tenant split run gets `["repair", "dms"]`, everything else that is not a dry
run gets `["repair"]`. `repair.run_all`'s own docstring already promised this
("Called automatically at the end of a migration") and the manual endpoint's
refusal already claimed it too ("repair runs automatically when it finishes") —
neither was actually wired up before `api_server._start_repair`/`_follow_on`. The
manual `/api/v2/repair/{id}` now calls the same function (so the two can never
drift) and keeps its own guard (refuses while a migration is running); the
automatic path has none of its own, because `_follow_on` only ever runs the
instant after this account's job_admission slot has been released.

**An ordered run also repairs itself between the Drive pass and whatever comes
next** (`main._repair_between_passes`, called from the pass loop when a pass just
run was `drive` and another pass follows) — the gap `_auto_repair`/the API's
automatic follow-on cannot close, because both only run once, after the WHOLE
run exits. Without this, a file that permanently failed to copy left its Drive
links pointed at the source tenant forever once mail started rewriting them
(`link_rewrite.py` leaves an id it cannot map exactly as it found it — safe,
never corrupted, but wrong once the source is later decommissioned). Deliberately
narrower than `repair.run_all`: only `repair.retry_drive_stragglers` (a plain
Drive re-run for the affected users — `drive_engine`'s own `get_target_id` check
means a fresh walk skips everything already mapped and retries only what is
still missing, no bespoke per-item re-fetch needed, unlike Gmail/Calendar's
stranded items) and the ACL quota reconcile-then-reapply cycle, because neither
writes `identity_map.status` — safe to call mid-run only because this always runs
at a pass boundary, after `run_batch`'s own worker pool has fully drained. The
false-done/stale-user/stale-grantee families in `run_all` are end-of-run
housekeeping about a run's FINAL state and stay there. `retry_drive_stragglers`
is also wired into `run_all` itself now, as a new `drive_stragglers` family —
previously a generic copy failure (a stray 500, a 404) matched nothing in
`RETRYABLE_FAMILIES` and stayed permanently failed even after the automatic
end-of-run repair.

**The rate limiter now learns from the first quota rejection, not only the
last** (`resilience.retry_on_google_error`'s new `on_quota_rejection` hook, wired
to `drive_engine._retry`'s `project.penalise`): a single call's own retry ladder
used to run up to ~4 minutes deaf to its own rejections, so a burst of concurrent
grants each ground through their own ladder in ignorance of the others before the
`AdaptiveRateLimiter` ever heard about it — live, 425 ACL grants permanently
failed during one such burst while the limiter's own "no pushback" reading (still
mid-ladder, nothing had reached exhaustion) read as nothing being wrong. The hook
fires on every attempt Google rejects for pacing, immediately, so concurrent
siblings under the same project limiter see the reduced rate within seconds.

**The rate limiter's ceiling is discovered once and then permanent, not a number it
keeps probing past.** The constructor's `ceiling` (`DRIVE_PROJECT_QPS_CEILING`, 1200)
is a guess made before anything about a project's real limit is known, deliberately set
high so it is never the binding constraint — confirmed live, a run can sit flat against
it for hours, zero rejections, using a fraction of even that. `AdaptiveRateLimiter.
penalise()` now tightens `self.ceiling` itself, permanently, to 95% of the rate that just
broke, the first time Google actually says no — not only the pre-existing `_ceiling_hint`
(same 95% margin), which only ever softened the climb *back up* to a rejection point and
forgot itself after two clean probes, letting the rate walk straight past the real limit
toward the old guessed one again. Ratchets down only: a second, lower rejection tightens
it further; nothing ever loosens it back up, because a clean stretch is not evidence the
true limit rose. One structural consequence: once the tightened ceiling converges with the
hint's own cap, `acquire()`'s own guard (`rate < ceiling`) stops calling into the
hint-forgetting logic at all, so `_ceiling_hint` can end up permanently set rather than
returning to `None` — harmless (nothing downstream reads it as though a fresh probe were
still due), but worth knowing if `stats()["ceilingHint"]` on the Metrics page never clears
after a real rejection.

**A real crossing is one event, not one per concurrent worker that happened to be mid-flight
when it landed.** Measured live: 11 different workers each called `penalise()` within 1.3s of
each other, all blaming the SAME ceiling crossing — each one multiplying an already-just-cut
rate by 0.7 again, so the multiplicative decrease meant for ONE correction compounded eleven
times (`0.7**11 ≈ 2%`; 278 calls/s to 5.0), and the climb back afterward starts from a rate far
lower than the one real correction ever justified. `penalise()`'s new `debounce_window`
(default 2.0s) coalesces this: `_rejections` still counts every call Google actually refused
(honest reporting of how often it happened), but the rate/ceiling change applies only to the
first call inside the window — everything closer than that to the last real change is the
same crossing arriving late, not a fresh one.

**A discovered ceiling survives the process that found it.** Every one of the fixes above
still starts a *fresh* process back at the guessed 1,200 — confirmed live, restarting the
same migration five times in one day made every single one re-crash into roughly the same
~78–280/s range from scratch, each rediscovery costing real failed items on the way down.
`drive_engine._project_limiter(qps, tenant, db=...)` now reads the account's own ledger
(`MigrationDB.load_rate_ceiling`) for a previously-proven ceiling on this exact tenant side
and starts there instead — `min()` against the configured safety guess, never *above* it, so
a stale or bogus learned value can't start a run more aggressively than the deliberately
conservative default would. Every real `penalise()` backoff writes the new ceiling back
(`save_rate_ceiling`, one row per `tenant` in `rate_limiter_ceiling`, newest wins) so the
*next* run on this tenant starts warm. Both directions are advisory only — a read or write
that fails logs a warning and falls back to the old guess-and-discover behavior; it must
never be able to stop a migration starting or running.

**The production default for how Drive content moves is `server_side`, not `download_upload`**
— set as `TRANSFER_MODE=server_side` in `systemd/bitport-api.service`, not in `config.py`
itself (whose own fallback stays `download_upload`, for a bare/local run with no unit).
`download_upload` streams every file's bytes through the box running `api_server.py`;
confirmed live, that host's own CPU/network was the actual bottleneck, not Google's quota
— the rate limiter sat at its own configured ceiling (1,200 calls/sec, near-zero pushback)
while only ~34.5 calls/sec were ever achieved. `server_side` asks Drive to copy the file
itself (`files.copy`), so no bytes cross this host at all — but it needs a per-user staging
shared drive on the target (`drive_engine._ensure_staging_drive`) that grants the
**source**-domain user organizer access to it, which depends on the target tenant's own
external-sharing settings actually allowing that cross-domain grant. `StartMigration.
transfer_mode` (`Literal["download_upload", "server_side"]`, `link_flip` deliberately never
offered here — deprecated, benchmark-only, briefly makes the source file public) overrides
this per launch, passed as an env var to just that subprocess; the Start Migration dialog's
"How does Drive content move?" section defaults to leaving it unset (this server's own
config) rather than assuming every target tenant's sharing settings allow the staging-drive
grant.

**A share with a colleague who has no target account yet is owed, not lost**
(`config.OWED_GRANT`). A run for a few users shares their files with colleagues not
migrated yet. The engine asks the target directory once per colleague
(`drive_engine._target_account_exists`); a 404 records the grant as
`OWED_GRANTEE_NO_ACCOUNT` at once, never through the retry ladder. Drive's own "no
Google account" 400 gets two tries (`resilience.NO_ACCOUNT_RETRY_BUDGET`), then is
owed too. `repair.reapply_owed_grants` (start of `run_all`, so every run's automatic
repair) grants each owed share whose colleague now has an account, through the
engine's `_sync_acls(only=...)`. The status does not start with `SKIPPED`, so the
one-to-one check counts it missing until then; `GET /api/v2/owed-grants` feeds the
header's notifications. An outsider with no Google account stays
`SKIPPED_GRANTEE_NOT_ON_GOOGLE`.

**The source is indexed when a pair is ready** (`api_server._start_discovery`, a `discover` job: `main.py discover --include-mail`, the ETA baseline): after `link-domains`, and after an identity-map build ends cleanly (`_discover_when_mapped`) -- but only when the account's ledger maps THIS pair's users. A ledger outlives the pair it was built for, so a scan over whatever is mapped could read the previous pair's tenant; it says why it did not start instead.

**`reset_target` also trashes Google's own welcome mail** (`from:mail-noreply@google.com`, `trash_google_welcome_mail`): the seeder's reset only trashes `@seed.test` mail, the source mailboxes hold the same two welcome messages and a migration copies them, so each reset-and-rerun cycle used to stack another pair on the target. The listing excludes the trash, so its count is also the check.

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

**Every user is also tallied -- after the run, not as they finish** (default): a run
launched from the API ends with a `tally` follow-on (`api_server._start_tally_after_repair`)
that waits for the repair follow-on, then runs `tally.py` for every user as one `user-tally`
job; after a split run the DMS job tallies instead, once Google's import has finished
(`dms_migrate.py --until-done`). Inside the run it re-listed both tenants for each user
after every pass, on the one core the copy needed. `TALLY_ON_COMPLETE=true` brings back the
per-user hook (`tally.tally_user_and_save`, the same `main.VERIFY` queue). Either way: an *exhaustive*
count of every service on both tenants, not a sample — `tally.count_side` + `aggregate`
scoped to one pair, reusing exactly what `main.py tally` uses for the whole tenant, but
**never** writing `run_fidelity` (that stays the whole-tenant number the report's fidelity
section reads; a per-user pass overwriting it with one user's counts would corrupt it for
everyone else). Results are one row per user (`user_tally`, one row combining every
service — no per-service split, unlike `user_verification`), served by `GET /api/v2/tally`
and shown on its own **Tally** page, which can also run it again (`POST /api/v2/tally/run`,
a job named `user-tally` — deliberately not `tally`, so it never shares a slot or a log
file with the whole-tenant job behind the reports panel's "Run tally"). Verdicts: `COMPLETE`
(every service at or above the same parity bar `benchmarks.py`'s own `count_parity` check
uses, 0.999), `SHORT` (a service came up short), `UNKNOWN` (a tally ran but nothing could be
counted), `NOT_TALLIED` (nobody has, yet — never a blank). `reset_drive_ledger` clears a
user's whole tally row on any of its services being reset (there is no per-service slice
to preserve, unlike `user_verification`).

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
mark, so a ledger from before it reads as finished, as it always did. **Rebuilding a
native Doc/Sheet/Slide by uploading exported bytes under the native mimeType (the
download/upload path's only way to do it) makes Drive ignore the requested
modifiedTime in the create call itself** — measured directly, unlike a plain upload
or a bare create, which both honour it. `_finish_item`'s restore only runs when
something else writes to the file afterward, so an unshared, uncommented native file
had nothing to ever trigger a correction; `_sync_native` now widens its create
`fields` to read back what Drive actually kept (free — already paying for the round
trip) and forces the restore via `force_mtime_restore` when it disagrees.
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

**Worker count is re-probed once per pass, not just once per process.**
`Settings.user_workers` used to be decided exactly once, at `Settings()`
construction (`resources.recommend()` against RAM measured at that instant),
and frozen for the rest of the process — including every later pass of an
`--ordered` run (drive, then mail, then the rest), each a separate
`run_batch()` call. A pass that started under memory pressure stayed stuck
small even after a later pass had room to grow into, and a mail/calendar
pass paid whatever pool size drive's heavier `download_upload` budget had
needed. `run_batch()` now re-probes via `config._auto("user_workers", ...)`
at the top of every call, unless the operator pinned it (`USER_WORKERS` env,
or `--workers`, which `main()` mirrors into that same env var so both paths
share one override signal). This does **not** resize a pool already
dispatching: `ThreadPoolExecutor` only spawns threads at `submit()` time,
and every pair for a pass is submitted in one batch in `run_batch()` —
mutating a running pool's `_max_workers` would be a no-op with nothing left
to submit. Re-probing between passes was the one place a fresh pool is
already built, so it was real, free adaptivity that nothing was claiming.
Mid-pass concurrency changes (a single drive pass against 300 users runs for
days) would need the dispatch loop restructured to a resizable gate instead
of submit-all, and that has not been done — scoped out as bigger surgery
than this pass-boundary fix, not as an oversight.

**The seeder's own Drive write pacing got the same fix the migration side
already had — proactive and cross-run, not just reactive.** `resources.
DRIVE_WRITES_PER_SEC` (a real measurement, but corrected by hand three
times already — see `SEED_LEAF_SECONDS`'s own comment, 1.18s → 5.4s →
15.0s round trips) sized how many leaf threads to run, and that was *all*
the pacing that existed: `retry_on_google_error` only reacted to a 429
after Google had already sent one, with no `AdaptiveRateLimiter` and no
cross-run memory the way `drive_engine._project_limiter` has. `seed_
sandbox._seed_drive_limiter` is that same fix, ported: one process-global
`AdaptiveRateLimiter` (Drive's write ceiling is per ACCOUNT — the tenant
being seeded — not per user, so every seeded user's leaf threads share the
one real budget regardless of how the seeder divides its own worker pool),
seeded from a previous run's discovery in the account's own ledger
(`rate_limiter_ceiling`, `tenant="seed"` — the same table and methods
migration's ceiling uses, a different row), `min()`'d against `SEED_
DRIVE_CEILING` the same way migration's never starts hotter than its
configured guard. `_retry_factory(settings, limiter=...)` is the one
place this is wired in — passed only at Drive call sites (`trim_filler`,
the storage top-up, the corpus builder, the corpus reset); Gmail/Calendar/
Chat/Contacts/Tasks call `_retry_factory(settings)` exactly as before, no
behaviour change, because none of them has a documented ceiling finding
behind it. `drive_write_qps`'s own migration-side counterpart (`config.py`,
a **fixed**, non-adaptive 3.0/sec `RateLimiter` for the per-account write
bucket, distinct from `_project_limiter`'s per-project one) is the same
shape on the migration side, and was checked against a live `server_side`
run and left alone deliberately: 97,340 `files.copy` calls, zero target-side
rejections, and per-user request rate around a fifth of the 3/sec it gates.
Nothing is near it, so an adaptive version would discover a ceiling nobody
is touching. Revisit only if per-user rate ever climbs toward 3/sec.

**What a live `server_side` run showed, and what changed because of it.**
Three things were measured against the running production migration
(`db.latest_metrics` on the account's own ledger, `ps -o nlwp`, `free -h`)
rather than reasoned about:
- **`metrics.py` reported "workers" as every thread name it had ever seen**
  (a `set` that only grew), not live concurrency: 304 against 195 real
  threads on the same process, because `drive_engine._open_file_pool`
  builds a fresh `ThreadPoolExecutor` per user and each pool's threads get
  new names. Now `threading.active_count()` at snapshot time.
- **`db._mapping_cache` held every user's whole `id_mapping` for the life of
  the process** — 834,937 rows across 235 users on that ledger, plausibly
  250–400 MB resident. Now a bounded LRU (`MAPPING_CACHE_USER_CAP`, 100
  users, `OrderedDict` touched on preload/hit/write). Per-user eviction on
  completion would be *wrong* (an ordered run's mail pass reads other
  users' Drive mappings hours later); a cap is *safe* only because
  `get_target_id` already falls through to SQL for an uncached user, so
  eviction costs a query, never a false "not migrated". The hit path now
  decides membership under `_cache_lock` — an unlocked check followed by an
  eviction was a `KeyError` in the first version (see
  `test_readers_racing_eviction_never_raise_or_answer_wrong`).
- **`resources.MIGRATE_FILE_SECONDS` was never a measurement** (1.33s, backed
  out algebraically from the old frozen `drive_file_workers=4`). Re-derived
  from per-label p50s weighted by calls-per-`files.copy`: 2.47s, so
  `migrate_file_workers()` now sizes 7 threads per user, not 4. The staging
  move (`files.update ... removeParents=staging`) had no label of its own and
  was proxied by `drive.files.update.mtime`'s p50; it is now labelled
  `drive.files.move`, so the next run can replace the proxy with a real
  number. The generic `drive` label is excluded — it mixes once-per-user
  staging setup and once-per-folder traversal with per-file work.
`HARD_CAP` (48) was checked and left alone: this box had ~1.6 GB available,
~40 workers at `server_side`'s 40 MB each — RAM binds below the cap already.

**`data-generator/conftest.py`'s `settings` must track `tests/conftest.py`'s.**
It had fallen behind by two lines (`drive_write_qps = 10_000`,
`mtime_settle_sec = 0`), so its one real-`DriveMigrator` test paced faked
writes at 3/sec and then sat in a real 240s `_verify_modified_times` sleep —
minutes at ~0% CPU that read as a hang. `pytest -o faulthandler_timeout=45`
is what found it; reach for that before guessing at a "stuck" test.

**A pass skips a user only on ledger evidence, never on an assumption**
(`main._services_already_done`). A DONE user with an empty `services_done`
used to be assumed to have run whatever was asked -- with a warning nobody
read -- and on account 3 that left 22 users with Drive migrated and zero rows
of mail, calendar, contacts, tasks or chat, indistinguishable on the Tally
page from users merely waiting on the DMS. Evidence is `SERVICE_EVIDENCE`
(SUCCESS rows of each service's item types), shared with `backfill-services`.
Shared drives now run inside every whole-tenant run (after Drive, before
mail) and repair re-creates a FAILED, unmapped one.

**The DMS runs unattended end to end** when `/etc/bitport/dwd.env` (root, 600) holds
both admins' console logins (`DWD_PASSWORD_TARGET`, `DWD_EMAIL_SOURCE`/`DWD_PASSWORD_SOURCE`):
the job signs in as the target admin (Xvfb `:99`), requests the connection, approves it
as the source admin (`dms_migrate.approve_as_source`: the "Request for authorization"
mail read over the source Gmail grant, the link LABELLED as the request -- every link is
a c.gle redirect -- an account chooser, then the consents page), presses Start import,
reads status every 15 min (`--until-done`) and tallies when Google reports it finished.
A DMS job survives a webui restart; `_EXT_SCRIPTS` lists it so the Jobs page can stop it.
Before building anything browser-driven here, look for the one-off script that already
did it (`dms_*.py`, `*_probe.py`) -- the approval was rebuilt once from guesses.

**Full scope, always** (`fidelity.py`): every optional pass -- external-owned shares,
secondary calendars, groups, Gmail settings, calendar ACLs, rooms, comments, chat,
contacts, tasks, SSO profiles -- defaults ON in config.py. A run mints ONE token for every
scope and one ungranted scope fails every call, so: the launch probes each pass's extra
scopes and writes it on or off (`plan`, named in the launch detail); the run's own gate
(`main._gate_on_delegation`) tries scope_guard's unattended re-grant first and otherwise
switches off only the ungranted passes (`drop_ungranted`, exported to child processes via
`SCOPE_DROPPED`, which `_enable_selected_services` honours); and every other process's
`AuthManager` does the same check once before its first credential, in its own settings
only (never os.environ in the shared API process). There is no switch in the UI to
migrate less. Groups (before Drive) and rooms (before Calendar) are created
by the run itself (`main._before_passes`); calendar subscriptions are re-followed after
the last pass. `verify_scopes.every_toggle_scopes` must list every flag, or no wizard
grant ever includes its scope.

**One-to-one details now carried**: Drive `createdTime`, star, folder colour, custom
properties, download ban, "writers can share" (in the create/move call, with a retry
without them if Drive refuses -- `with_carried_fallback`) and a lock (applied last);
calendar guest permissions, `source`, typed out-of-office/focus/working-location events,
calendar-list colour/name/reminders, a new Meet link for a future meeting on its
organizer's copy; exact Gmail threads (`threadId`, one conversation at a time); Chat
`createTime` in import mode (falls back to "now" if refused), threads, reactions, DMs and
group chats, and a shared space migrated once (`db.claim`, the `tenant_claims` table);
contact photos. The per-user tally compares every mapped Drive item and reads `DIFFERS`
when counts agree but items do not.

**A pass can be split across OS processes** (`MIGRATE_PROCESSES`, `StartMigration.
processes`, default 1 until measured): `main._run_pass_in_processes` runs `main.py
run-shard` children, each a disjoint slice of the users (`MIGRATE_SHARD=k/n`), each
sized to 1/n of the RAM and of the learned project rate (`PROCESS_SHARE`). The parent
keeps the admission slot, the PASS markers, the memory watchdog and Stop (forwarded as
SIGINT); a child whose parent dies kills itself.
