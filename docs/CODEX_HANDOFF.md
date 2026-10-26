# Handoff: dharan.gov.np → s.sarafgloabalexim.com, and what Bitport still needs

Written 2026-10-25 by Claude for Codex (Codex resets 2026-11-01). Read `CLAUDE.md` first; this file
is the live work list. Tick items here (`[x]` + date + commit) as you finish them.

## Rules that are not negotiable

1. **Nothing on dharan.gov.np is ever changed, modified or deleted.** It is a government
   source: read-only key (all 16 write scopes refused by Google), `domain_guard.is_government`,
   download-and-upload only. Never set it up again, never add a scope, never run setup or a
   "Continue as it.department@dharan.gov.np" button against it. The source guard (Migration
   detail → Source guard) must show nothing attributed to Bitport.
2. **Operate Bitport through its UI** (Playwright against
   https://everything.nishantbohara.com.np/app, or the operator's Mac), and read results from
   the UI. If something you need is not in the UI, build it into the UI first, deploy, then use
   it. Ad-hoc API calls or ledger queries are diagnosis only, never the way work gets done.
3. **Every run teaches Bitport.** Each new run failure or One-to-one finding gets a named kind
   (`known_issues.RUN_KINDS` for run failures, `known_issues.ISSUES` for findings) with its
   cause, its fix, and an automatic handler where one is possible, plus a test that reproduces
   the live case. "Not a known kind yet" must trend to zero.
4. **Tests before commit:** `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider > log; echo rc=$?`
   must print `rc=0` (read the rc, not the dots), plus `npx tsc --noEmit && npm run lint &&
   npx vitest run` in `migration-webui/`. Never edit files while a suite runs.
5. **Deploy only with `./sync_vps.sh root@78.47.176.120 /root/migration ~/.ssh/workspace_migrator_vps`
   when no job runs.** A backend deploy restarts the API and kills a running migration. To
   deploy mid-run: Jobs → Stop (takes effect between items; can take 40 min), wait for the
   process to exit, deploy, then Migration detail → Migrate users → All 45 → Migrate to resume.
   Build the SPA first: `VITE_CP_BASE="" npm run build` (then `grep -c localhost:8090` = 1).
6. Commit and push each piece of work on branch `codex-work-20261023`.

## Where things stand (2026-10-25)

- **Account 7** "dharan.gov.np → s.sarafgloabalexim.com" (login in root-only
  `/etc/bitport/account-dharan-s.env`) is running the full migration (all 45 users, read-only
  source). Both sides read **Ready** on its Setup page. Identity map: 45 users by localpart.
- **mdharan.com (account 5) is gone** for migration purposes: storage limit 0, Gmail /
  Calendar / Tasks switched off. Its Drive still reads, so it is the **test bed** for the
  server-side copy safety test only.
- **info@s.** keeps 426 old sandbox test files (1.2 GB): the operator's choice; the final
  report lists them as extras not from dharan.
- **Source guard:** one difference since the baseline — ward19's own mail (5157 → 5155),
  attributed "not Bitport". ward16 changed its own mail earlier the same way. Both need the
  delta at the end.
- **Run failures** (Migration detail → "What the N failures are") are all named now:
  resource-key 404s (fixed in f85eaba: `resource_keys.py`), disk full + rollback (fixed:
  `housekeeping.py`, `db.write`), a Form image refusal (fixed: rebuild without the image),
  comment ids colliding across files (fixed: keyed by file), malware-flagged (server-side
  only), mail/calendar held for Drive (next run).
- **One-to-one so far:** the main open kind is "Doc/Slide structure differs from the source"
  — Docs and Slides rebuilt by the read-only route. Only a server-side copy fixes it.

- **Box setting (2026-10-25):** `/etc/systemd/system/bitport-api.service.d/speed.conf` sets
  `FIRST_PASS_ACCEPTANCE=false` and `MAIL_WORKERS=8` (8 messages at once per mailbox; was 2 under memory pressure), so runs copy at full speed (the per-user check held each worker
  for hours at ~3 files/min). The One-to-one checks every item after the run. Replace it with a
  per-run tick-box in the Migrate dialog ("Check each user as it finishes", item C) and then
  delete the drop-in.
- **Kill switch built, not deployed:** branch `worktree-agent-aff2009f324507dc9` (5f12f62) --
  after every user, a read-only check of the source; a change attributed to Bitport kills the
  run (children included); the guard's auto-stop escalates to SIGKILL after 30 s. Review, merge,
  full suite (rc=0), deploy at a safe point.

## Work list, in order

### A. Finish the dharan run (each step through the UI)
- [ ] When the run ends, its Repair follow-on runs by itself. Read Migration detail: every
      failure kind's open/fixed counts. Anything "not a known kind yet": name it (rule 3).
- [ ] One-to-one page → run the every-item check for all 45 users (limit 0, with repair).
- [ ] Delta for ward16 and ward19 (their own mail changes); then One-to-one again for them.
- [ ] Final report (Final Report page). Accepted limits to list, never hide: comment authors /
      resolved state / anchors; chat bot messages re-posted with attribution; Form past
      responses and the extra "Form Responses" tab of a relinked Sheet; Classroom group grants;
      genuine other-organisation shared drives; Sites / My Maps / Jamboards (by hand); the 8
      Google-made "Copy of" Forms already on dharan (from an exact-copy run on 10-19);
      info@s.'s 426 test files.

### A2. Scheduled for 2026-10-30 (operator: not before)
- [ ] 2 x "Untitled video" (finance@dharan.gov.np): "no API rebuild for application/vnd…" -- a Google Vids
      file has no copy or export route; find one (server-side copy if the safety test allows it) or keep it
      listed under "Bitport can't move these" with what to do by hand.
- [ ] suchanaadhikari@ and ward1@ failed: "drive: failed -- Full source/destination ID coverage is
      incomplete; findings scheduled for repair". Read their open findings (One-to-one), name each kind,
      fix, re-walk.

### B. Server-side copy safety test (decides the rebuilt Docs/Slides)
Built and deployed (`copy_safety.py`, card on Migration detail, `POST /api/v2/copy-safety/start`).
- [ ] Admin → Accounts → create account 8 "mdharan.com → s.sarafgloabalexim.com" (UI), then
      "Use a key…": source = account 5's *target* key (as source), target = account 7's target key.
- [ ] Run the test from account 8's Migration detail (a few files of each type). Expected from
      the 10-19 audit log: Forms, Sheets linked to a Form and Apps Script make Google drop a
      "Copy of" into the copying user's Drive (UNSAFE); Docs/Sheets/Slides/binaries should be SAFE.
      Known weak spots: it checks every source scope (mdharan's target key may lack some — relax
      to `drive` only if refused), and mdharan's audit log may be unreadable (verdicts UNKNOWN;
      the before/after Drive diff still shows "Copy of" files).
- [ ] Report the per-type verdicts to the operator. **Only the operator** decides whether to
      add `drive` to dharan's delegation for a re-copy window of the SAFE types — that is a
      setting change on dharan and needs their explicit yes. Never do it on your own.

### C. UI gaps found during this run (build, test, deploy at a safe point)
- [ ] Migrate dialog → More options → "Check each user as it finishes" (StartMigration.check_each_user → FIRST_PASS_ACCEPTANCE for that run); then remove the box drop-in above.
- [ ] A superadmin viewing another account sees its running job under "Running now" on Jobs
      (today only in the queue line).
- [ ] A finished migration is listed on Jobs; History rows open their log.
- [ ] Verification page's pair card follows the "Viewing account" switch.
- [ ] A "Scan the source" button (starts `discover`); Identities → "Load identities by matching
      localparts" should start the scan like the other identity build does
      (`api_server._discover_when_mapped`).
- [ ] Per-user source sizes (files, GB, messages) on Users / Migration detail.
- [ ] Migration detail says why a target account was not created (today only the Logs
      transcript has "provision: could not create …").
- [ ] Maintenance → Empty target data: users that simply do not exist on the target yet are
      "nothing to do", not failures, and the job exits 0.

### D. Bitport improvements still open
- [ ] **Form images**: a rebuilt Form drops images the target cannot fetch
      (`native_api._without_images`). Carry them instead: read the image as the source user,
      upload it to the target, give it a URL the Forms API can fetch, use it as `sourceUri`, then
      remove any temporary sharing. Prove it on the sandbox pair before dharan.
- [ ] Malware-flagged files: only a server-side copy carries them (depends on B).
- [ ] Keep `CLAUDE.md` current with every mechanism you add.

## Useful UI scripts on the box (`/root/bitport/scratch/`)
`ui_session.py` (bot login from `/etc/bitport/ui.env`), `ui_as.py <env> <route> <png> [wait]`
(page text), `run_all.py 7 "<reason>" go` (migrate all), `run_users.py 7 "<reason>" <emails…>`,
`jobs_stop.py migrate "<reason>"`, `repair_click.py <acct> "<reason>"`, `o2o_run.py <acct>
"<reason>"`, `ui_empty_target.py`, `ui_reuse.py`, `ui_seed_switch.py`, `ui_group_failures.py 7`,
`ui_failure_open.py 7 "<text>"`, `ui_guard_details.py 7`, `wait_run.py 7`. They are helpers, not
product: anything they do must also be possible by hand in the UI.
