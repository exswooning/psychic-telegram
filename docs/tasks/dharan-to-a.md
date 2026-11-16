# dharan.gov.np → a.sarafgloabalexim.com, with dharan untouched

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

- [ ] 0. Get the box back online. Tailscale has shown l3novo offline since about 08:30 UTC on 16 Nov.
- [ ] 1. Check a.: at least 45 licences, storage of at least dharan's Drive, and nothing in it yet.
- [ ] 2. In Bitport, create a new account, "dharan.gov.np → a.sarafgloabalexim.com". It gets its own
  ledger, and account 7's history stays.
- [ ] 3. Source: Admin → Accounts → "Use a key…" with dharan's existing read-only key (account 7's).
  This makes no change on dharan. Setup → Check again must read Ready. If it doesn't, stop: any
  fix in dharan's console is yours to make, by hand.
- [ ] 4. Target: run Setup for a. (project, key and delegation, all on a.). Declare a. a sandbox. The
  capacity check must pass. Identities: 45 users, matched by the part before the @.
- [ ] 5. On the box, read which dharan settings are still owed back OFF, whether the 12 copies are
  still there, and readiness.
- [ ] 6. Deploy the gap fixes once the box is idle.
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
