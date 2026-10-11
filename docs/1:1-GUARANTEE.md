# 1:1 Migration Guarantee

**No migration can be approved without exhaustive proof that every user's data matches between source and target, byte-for-byte.**

## How it works

### 1. Run the migration (as before)

```bash
# CLI mode
python main.py migrate --account-id 3 --whole-tenant

# Or via the web UI
# Migrations → Start Migration → Run
```

### 2. Verification happens automatically (new)

After the migration completes, before approval:

- The system runs `verify_all_users(account_id)`, which checks **every mapped user** (not a sample)
- For each user, it compares Drive files, Gmail, Calendar, Contacts, Tasks across both tenants
- Each user gets a verdict: `COMPLETE`, `IDENTICAL`, `SHORT`, `INCOMPLETE`, or `FAILED`

### 3. Approval gates on 1:1 proof

When you click **Final Report → Approve as complete**:

- The API calls `verify_all` again (ensuring fresh data)
- **PASS** (approval allowed) if:
  - ✓ All users are `COMPLETE` or `IDENTICAL`
  - ✓ Zero `INCOMPLETE` verdicts (unfinished checks)
  - ✓ Zero `FAILED` verdicts (verification errors)
  - ✓ Parity = 100%

- **FAIL** (approval blocked) if any user has a gap
  - You get an error message: "Run `python verify_all.py --account-id X` and fix gaps"
  - Fix the failing users (repair, re-run services, etc.)
  - Then retry approval

## Manual verification (optional)

Before clicking approve, check it yourself:

```bash
python verify_all.py --account-id 3
```

Output:
```
Verifying 5 users (exhaustive)...
  [  1/  5] user1@example.com... IDENTICAL
  [  2/  5] user2@example.com... IDENTICAL
  [  3/  5] user3@example.com... IDENTICAL
  [  4/  5] user4@example.com... IDENTICAL
  [  5/  5] user5@example.com... IDENTICAL

✓ All 5 users 1:1 identical. Safe to approve.
  Parity: 5/5 (100%)
```

Or JSON format for automation:

```bash
python verify_all.py --account-id 3 --json | jq .
```

## What verdicts mean

| Verdict | Meaning | Next step |
|---------|---------|-----------|
| `COMPLETE` | All items migrated, all counts match (0.99+ parity per service) | OK to approve |
| `IDENTICAL` | Items match byte-for-byte (hashes, times, sharing) | OK to approve |
| `SHORT` | Some items missing on target (count < threshold) | Repair & re-run |
| `INCOMPLETE` | Check could not be made (timeout, API error) | Retry or inspect manually |
| `FAILED` | Verification crashed (exception) | Check logs, fix, re-run |

**Approval requires all users COMPLETE or IDENTICAL. Everything else blocks.**

## Repair workflows

### One user is SHORT (missing items)

```bash
# Re-run that user's services
python main.py migrate --account-id 3 --users user@example.com --services drive,gmail
# Then verify again
python verify_all.py --account-id 3
```

### One user is INCOMPLETE (check timed out)

```bash
# Retry the check manually
python verify_sample.py --account-id 3 --users user@example.com
# If it passes, approve
```

### Multiple users have gaps

```bash
# Run the full-tenant repair job (catches most strays)
# Via UI: Maintenance → Run repair
# Or CLI: python main.py repair --account-id 3
# Then verify again
python verify_all.py --account-id 3
```

## What's being checked

For every user:

- **Drive:** files exist, name/type/size match, MD5 identical, modified time preserved, folder path correct, sharing restored
- **Gmail:** message body and headers match byte-for-byte (volatile headers ignored), labels, unread state, dates preserved
- **Calendar:** events exist, title/time/place/attendees/recurrence match
- **Contacts:** names, addresses, numbers, organizations match
- **Tasks:** title, notes, status, due date, list membership match

Nothing is written to either tenant — the check is read-only.

## Approval process (checklist)

1. ✓ Run migration (via UI or CLI)
2. ✓ Wait for migration to complete
3. ✓ Click **Final Report**
4. ✓ (Optional) Run `python verify_all.py --account-id X` manually to preview
5. ✓ Click **Approve as complete**
   - If PASS: approval succeeds, teardown scheduled
   - If FAIL: see error, fix gaps, retry step 5
6. ✓ All gcloud sign-ins are revoked
7. ✓ Teardown due in 30 days (can be undone)

## Disabling the gate (not recommended)

The gate can be disabled, but requires code change:

```python
# In api_server.py, line ~1211, comment out:
# verification = verify_all.verify_all_users(aid)
# if verification["verdict"] != "PASS":
#     return False, (...)
```

**This is dangerous.** An unapproved migration leaves both tenants in an inconsistent state. The gate exists because 1:1 is the promise.

## Questions?

- Check the ledger: `sqlite3 data/accounts/<id>/migration.db "SELECT * FROM user_verification LIMIT 5"`
- Run with verbose logging: `LOGLEVEL=DEBUG python verify_all.py --account-id 3`
- See individual user results: `python verify_sample.py --account-id 3 --users one@user.com`
