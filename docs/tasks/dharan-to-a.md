# dharan.gov.np → a.sarafgloabalexim.com, with dharan untouched

**Withdrawn, 16 Nov 2026.** The operator can no longer read dharan.gov.np. Nothing below was run
against a tenant. The two code fixes stay. The work continues in
[synthetic-10-plan.md](synthetic-10-plan.md).

Operator, 16 Nov 2026: remigrate dharan.gov.np into the brand-new a.sarafgloabalexim.com. dharan is a
real government tenant, never a sandbox; a. is a sandbox. **Nothing on dharan may change.**
(It replaces account 7's target, s., whose subscription lapsed on 16 Nov.)

## What keeps dharan untouched

1. **Google refuses every write.** Bitport asks a government source for 18 scopes, all of them
   read-only (`verify_scopes.grant_scopes`, checked 16 Nov). Readiness checks the write scopes
   are refused before a run (dharan on 24 Oct: 16 of 16 refused).
2. **Bitport's own source writers refuse dharan.** `auth.source_drive(writable=True)` calls
   `domain_guard.require_not_source`, which a government domain can never pass. Server-side
   copy, move, link-flip and exact copies all need write access, so **Drive moves by
   download-and-upload**. Your standing "Drive: server-side copy" rule can't apply here: a
   server-side copy is what left the 12 copies on dharan in October. A new account has no
   `government-exact-copy` flag.
3. **Nothing lands in a dharan mailbox or Drive.** Drive shares are silent, and a share to a
   dharan address with no a. account is skipped (`SKIPPED_UNMAPPED_IDENTITY`). Calendar
   events are imported, which sends no invites. Calendar sharing is silent. A forwarding
   address outside a. is withheld.
4. **Proof.** The Source guard lists every dharan file by id before and after each pass. Google's
   own Drive audit log shows who made any change: Bitport, Google, or a person.

What dharan sees from this run is audit-log entries for our reads, nothing else. Some things are
already on dharan from migration 5 (19 Oct) and are not new:
- our Cloud project and delegation line
- Drive SDK and project creation, which setup switched ON and which are owed back OFF at approval
- 12 copies made in exact-copy mode (9 Forms made by Google, 3 Apps Script), left there at your word

## Gaps to close before the run (code; no tenant needed)

- [x] (16 Nov) **Setup and the scope check could change a source's Admin console on their own.**
  - `full_setup._allow_project_creation` and `_allow_drive_sdk` turned settings on when refused.
  - `scope_guard.ensure` re-granted a source's delegation unattended.

  Fixed: `project_creation.run` refuses to switch a government tenant's setting on, and setup
  names the checkbox instead. `scope_guard._console_login` offers no login for a government
  source, so no re-grant, narrowing or seed key signs in there.
- [x] (16 Nov) **Comment text was copied as is.** A `+name@dharan.gov.np` mention may make Google
  email that person, which would be a new message in a dharan mailbox. Fixed: `migration_comments`
  writes `+name@x` and `@name@x` as plain `name@x`, and the comparison ignores the sign, so older
  copies still match. Still to see live: the pilot's copied mentions email nobody.

## Steps

- [x] 0. (16 Nov) l3novo is back. It was a network outage; the box had been up for 19 days and was
  idle. The run happens here. The old VPS is never used (12 Nov handoff).
- [ ] 1. Check a.: at least 45 licences, storage of at least dharan's Drive, and nothing in it yet.
- [ ] 2. In Bitport, create a new account, "dharan.gov.np → a.sarafgloabalexim.com". It gets its own
  ledger, and account 7's history stays.
- [ ] 3. Source: dharan's read-only key exists only on the old VPS (account 7, `keys/7/source-sa.json`).
  l3novo has no dharan account, so "Use a key…" has nothing to offer. Bring that one file over once,
  through Setup → source → upload key (`/api/v2/setup/credentials`). This makes no change on dharan:
  the delegation follows the key's client ID. Do not make a new key: that would add a project and a
  delegation line on dharan. Setup → Check again must read Ready. If it doesn't, stop: any fix in
  dharan's console is yours to make, by hand.
- [ ] 4. Target: run Setup for a. (project, key and delegation, all on a.). Declare a. a sandbox. The
  capacity check must pass. Identities: 45 users, matched by the part before the @.
- [ ] 5. The owed settings (Drive SDK and project creation, ON on dharan since October) are recorded
  only on the old VPS. Record them again for the new account on l3novo
  (`project_creation.remember`). This writes a file on l3novo and touches nothing on dharan; it
  makes the Final Report list them. You confirm they are still on. The 12 copies become part of
  the Source guard's baseline. Then check readiness.
- [x] 6. (16 Nov) ed3d325 deployed to l3novo while it was idle. The API and web UI restarted, with
  no job running.
- [ ] 7. Source guard → Take baseline.
- [ ] 8. Pilot shiv@, livestock@ and audit@. Pass means:
  - Source guard shows 0 changes
  - Tally reads COMPLETE
  - One-to-one is identical, apart from the known remainder below
- [ ] 9. Run all 45 users in order: Drive, then shared drives, then mail, then calendar, contacts
  and tasks. Chat stays off. Then repair, verify each user, and run the parity gate. Check the
  Source guard after each pass; any change on dharan stops the run.
- [ ] 10. Later, a delta for what dharan's staff changed in the meantime, by the same read-only path.
- [ ] 11. Finish, on your word only:
  - approve the migration
  - Final Report turns Drive SDK and project creation back off (it asks for dharan's password
    once and never stores it)
  - remove our delegation line and dharan-side project yourself: Bitport keeps no dharan login,
    so it reports them instead

## Decisions (operator)

- **Mail.**
  - **A (recommended):** Bitport reads every mailbox read-only. Nothing on dharan; proven on
    migration 5 (145,284 messages). Slower on the box's Wi-Fi, and overrides the standing
    split/DMS rule for this run.
  - **B:** DMS for bulk mail. dharan's admin must authorize Google's migration app, and Google
    emails that admin a request: an app grant plus a new message on dharan.
- **Known remainder from migrations 5 and 7.** Accept it up front:
  - one Slides soft line break lost on the .pptx round trip
  - native files a few bytes off after export
  - 2 Google Vids files (finance@), which have no copy or export route
  - Forms rebuilt from a read: past responses come over in their Sheet, file-upload questions
    don't, and repair relinks Form and Sheet on a.
  - 6 files Google flags as malware, copied with acknowledgement (your call, 19 Oct)
