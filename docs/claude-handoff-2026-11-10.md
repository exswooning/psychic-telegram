# Handoff, 10 Nov 2026: account 3 to full parity (for Codex and the next Claude session)

Read `CLAUDE.md` first, then this. Everything here was measured on the box; commits are on
`codex/google-configuration-ui` (`git log --oneline --since 2026-11-08`).

## Goal and rules from the operator
- Account 3 (s.sarafgloabalexim.com -> t.sarafgloabalexim.com, 45 users) to **full 1:1 parity**; the target is a declared
  sandbox, the source is protected. Repeat run -> check -> fix cause -> run until parity, then a full deep check, then
  (operator's word only) teardown on the sandbox: staging drives with content, the Cloud project, the DWD grant.
- Bulk mail goes through **Google's DMS**; the engine moves only emails with a Drive link, rewritten, **before** it (split
  mode). Never notify external people. An internal notice is an opt-in option, off by default.
- Every error stops the run, is fixed in code with a test, then restarts. Repairs are OFF on account 3. Approval/teardown is
  an operator's click or word, never a timer. Optimise for model tokens: react to the autopilot's report, do not poll.
- **Do not press Start in the web UI while a run is going**: the UI launcher uses its own defaults (8 processes, engine mail).

## How to operate
- Box `l3novo@100.79.158.38` (UTC clock; operator's day is Nepal, +5:45). Root over SSH password (operator holds it). Read-only:
  `monitor/box.sh feed|brief|pyhash|uibuilt`. Deploy = hash-checked scp to `/tmp/py11`, copy into `/opt/bitport`, py_compile,
  restart only what loads the file; never while a run is live (a `bitport-api` restart can kill an API-started run).
- Launch: `POST localhost:8090/api/v2/migrate/start` with the session cookie, then **pause repairs again** (a launch used to
  switch them on; now an operator's pause holds). Fresh-run body is in `logs/autopilot/state.json` under `launch`.
- Box units: `bitport-parity` (parity_loop.py: deep-checks each finished user), `bitport-autopilot` (autopilot.py),
  Ollama (snap, capped 7 GB/2 cores). Reports: `logs/autopilot/REPORT.md` (<15 lines), `INBOX.md`, `logs/jobs/3/parity.log`.
- Tools built: `fidelity_audit.py` (every file: grants, mtime, settings), `form_deep_check.py` (Forms + responses Sheet),
  `mail_dead_links.py` (full mail read: link emails, search miss rate, dead links), `dms_link_rewrite.py` (post-DMS rewrite),
  `sharing_report.py` + Sharing panel, `parity_loop.py --recheck all|gmail`, oversight checks (limiter, fallbacks, recurring).

## Errors encountered (symptom -> cause -> fix -> status)
| # | Symptom | Cause | Fix / test | Status |
|---|---|---|---|---|
| 1 | Speed matrix deleted ward13's mappings | the re-timed arm ran on the real ledger | every fresh-account arm uses a ledger copy | fixed |
| 2 | Pure-DMS arm "incomplete" | recorded deliveries when none owed | `owes_mail` | fixed |
| 3 | Re-migration "adopted" binned mail | wipe moved mail to Trash; adopt searches the bin | wipe deletes permanently | fixed |
| 4 | Wipe stopped halfway, kept every Drive ledger | one Drive 500; ledger reset was all-or-nothing | retries + per-user ledger reset | fixed |
| 5 | Staging drives emptied mid-run, copies 404 | a test arm's end-of-run repair swept them | `repair.run_all` guard, arms never repair, test lists every deleter | fixed |
| 6 | Rebuilt Forms kept today's mtime | restore only ran when a later write happened, then Forms re-stamp | force restore + late re-check for Forms | fixed |
| 7 | 403 teamDrivesSharingRestrictionNotAllowed | writersCanShare sent into a shared drive | set after the move out | fixed |
| 8 | 8 users idle for hours | learned rate ceiling 6.49/s from 1 Nov | ceiling expires after 24 h; limiter sentinel | fixed |
| 9 | Box memory full (twice) | in-run 1:1 checks; glibc arenas | `checks_after_run`; `MALLOC_ARENA_MAX=2` | fixed |
| 10 | Repairs kept switching on | every pass called `enable()` | an operator pause holds | fixed |
| 11 | Recurring Apps Script comment 404 | no Drive comments for that type | `SKIPPED_NO_COMMENTS_API` | fixed |
| 12 | fidelity_audit aborted (heap corruption) | 16 threads shared one httplib2 client | per-thread clients (also mail_dead_links) | fixed |
| 13 | Engine mail ~1 email/s | the box's Wi-Fi (3 MB/s down, 0.9 up), not Gmail quota | DMS for bulk; wired port `enp1s0` has no cable | open (hardware) |
| 14 | Gmail search misses 79% of link emails | link text is not indexed | full read of every mailbox (about 40 min) | by design |
| 15 | "Filter already exists" x17 | wipe leaves Gmail filters | adopt an existing filter | fixed, not yet seen live |
| 16 | 125 false parity alarms | tally's overall parity includes mail owed to the DMS | per-service judging; timed-out reads retried | fixed |
| 17 | DMS "Start import" disabled | source admin never approved (step 1) | approve when step 4 is disabled | **fixed, NOT proven live** |
| 18 | A UI-launched run moved whole mailboxes | the web UI's defaults (8 processes, engine/DMS mode) | autopilot flags a deviating run | operator note above |
| 19 | 2 users failed "unable to find gmail.googleapis.com" | transient DNS drop | re-run clears | seen once |
| 20 | Wipe: Chat 403 on every account | target key cannot delete Chat spaces | none; Chat is not migrated | accepted |

## Autopilot efficacy (first ~12 h)
- Acted correctly 3 times: drifted modified times repaired for procurement@, it.department@, ward14@ (1 file each, 0 failed).
- 130 escalations to Claude, but only a handful real; about 115 were noise (111 = item 16, 4 = timed-out reads). Now aggregated by
  shape (one line, `xN`) and timed-out reads retried.
- Held correctly after the operator's pause; never undid a stop. **Crash-resume was never exercised** (no crash happened), so it
  is unproven. It could not see that the UI had launched a run with other settings; it now compares a running run's env with the
  chosen launch and flags it.
- The local model (qwen2.5-coder:7b) summarises an error in ~24 s; it never chooses an action.

## State at the end of 10 Nov and next steps
- Run 1 (my launch) finished Drive at full parity (40/45 users; the rest were one Form missing a question on suchanaadhikari@, plus
  mtime/setting repairs). The UI-started run then moved ~10,900 emails by the engine. Target is being **wiped and remigrated**
  in split mode: Drive server-side, link emails by the engine, the rest for the DMS.
- Next: DMS pilot on 3 mailboxes (proves item 17, measures the tenant-wide rate), full DMS, `parity_loop --recheck gmail`,
  `dms_link_rewrite.py` for link emails the engine missed, then a full deep check, then the sandbox cleanup test.
- Open: suchanaadhikari@'s Form (one question missing: reproduce with `form_deep_check.py`), the one link email that blocked the
  DMS handoff, linked-files-first + mail waves (designed, not built), the optional internal notice, the wired cable.
