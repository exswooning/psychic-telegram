# Testing this migration engine

The governing constraint: **a tenant-to-tenant migration engine cannot be
meaningfully tested against production, and a bug is not undoable.** You cannot
un-send 40,000 calendar invitations. You cannot un-mark 200,000 messages unread.
So the strategy is to push as much confidence as possible down into layers that
cost nothing to run, and to make the first live write happen against a tenant
nobody cares about.

Six layers, cheapest first.

| Layer | What it catches | Cost to run | When |
|---|---|---|---|
| 0. Static | Syntax, dead imports, type errors | seconds | every save |
| 1. Unit | Backoff branching, SQL, formatting | ~2 s | every commit |
| 2. Fakes | Idempotency, delta logic, ACL translation, silent-ingestion params | ~5 s | every commit |
| 3. Fault injection | Retry storms, partial failure, quota exhaustion | ~5 s | every commit |
| 4. Sandbox tenants | DWD config, real API semantics, export quirks | ~1 h setup | before first live run |
| 5. Pilot cohort | Real data shapes, user-visible fidelity | ~1 day | before every wave |
| 6. Production waves | — | — | reconcile after each |

---

## Layer 0 — Static

```bash
python -m py_compile *.py tests/*.py tools/*.py
ruff check .
mypy --ignore-missing-imports .
```

---

## Layers 1–3 — The automated suite

```bash
pip install -r requirements-dev.txt
pytest                       # 90 tests, ~5 seconds, no network
pytest --cov=. --cov-report=term-missing
pytest tests/test_drive_engine.py -k delta -v
```

Everything runs offline against `tests/fakes.py`.

### Why fakes and not mocks

A `Mock()` records that `files().create()` was called. It cannot tell you that
your recursive mirror created the same folder twice on resume, or that your
delta pass re-uploaded a terabyte because a `>` should have been a `<`. Those
are the bugs that actually cost money, and they only surface against something
that holds state.

So `tests/fakes.py` contains small working implementations — a dict of files
with parent pointers, a permission store, a message store — presented through
the exact chained-builder surface `googleapiclient` exposes:

```python
drive.files().list(q=...).execute()
calendar.events().import_(calendarId=..., body=...).execute()
```

The engine under test cannot tell the difference, and assertions are made about
resulting **state** ("the target holds exactly 14 files") rather than about call
counts.

### The assertions that matter most

Four tests exist because their failure mode is loud, public, and irreversible:

```python
test_insert_uses_date_header_for_internal_date   # else all mail dated today
test_unread_state_is_preserved_not_invented      # else 200k messages go unread
test_calendar_uses_import_never_insert           # else every attendee re-invited
test_permissions_never_send_notification_email   # else 50k emails to collaborators
```

The last two are also enforced structurally: the fake Calendar raises
`AssertionError` if `events.insert` is ever called, and the fake Gmail raises if
`messages.import` is. A future refactor cannot quietly regress into notifying
people.

Idempotency has its own cluster, because the whole restart-safety design rests
on it:

```python
test_rerun_creates_nothing_new
test_interrupted_run_resumes_from_id_mapping
test_messages_are_never_double_inserted_on_resume
test_delta_comparison_is_not_inverted            # a backwards compare re-copies everything
test_full_rerun_without_delta_flag_skips_everything
```

### Fault injection

Every fake service takes scripted failures:

```python
target.fail_next("files.create", status=403, reason="rateLimitExceeded", times=2)
target.fail_next("files.create", status=403, reason="storageQuotaExceeded", times=99)
```

This is how the 403-branching in `resilience.py` gets exercised against the code
that actually calls it — proving that `rateLimitExceeded` retries and
`storageQuotaExceeded` fails after exactly **one** attempt. Retrying a full Drive
200,000 times is its own outage.

### Writing new tests

Add a case whenever you touch translation logic or an API parameter. The pattern:

```python
def test_my_thing(migrator, auth, db):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("x.pdf", data=b"...")
    src.add_permission(fid, "user", "writer", email="bob@tenanta.com")

    migrator.run()

    tgt = auth.target_drive(TGT_USER)
    assert tgt.by_name("x.pdf")
    assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"
```

Seeding helpers: `add_folder`, `add_binary`, `add_native`, `add_shortcut`,
`add_permission`, `touch` (simulates a user edit, for delta tests),
`add_message`, `add_user_label`, `add_event`.

---

## Layer 4 — Sandbox tenants

**This is the layer people skip, and it is the one that catches the failures
that actually stop a migration.** Fakes cannot reproduce: DWD propagation delays,
`unauthorized_client` from a mistyped scope, the real 10 MB export ceiling,
Drive's behaviour on files with three parents, or a target tenant whose sharing
policy silently rejects every external ACL.

### Setup

Two Workspace trial domains — `sandbox-src.example` and `sandbox-dst.example`.
Trials are free for 14 days and give you real super-admin consoles. Follow
README §1 exactly as you would for production; the point is to rehearse the
setup, not just the transfer.

Then seed the source with data that is deliberately awkward:

- a folder tree at least 15 deep, and one with 1,000 files in a single folder
- a file with characters that break naive path handling: `re/port "final" (v2).pdf`
- a native Doc under 10 MB and one comfortably over it
- a Google Form and a Jamboard (both should be cleanly skipped, not crash)
- a file shared with: an internal user, an external address, a whole domain,
  and "anyone with the link"
- a file owned by someone else but visible to the test user
- a shortcut pointing at a file deeper in the tree
- messages that are read, unread, starred, in Spam, in Trash, with a 30 MB
  attachment, and with a nested label like `Clients/Acme/2024`
- a weekly recurring meeting with one moved instance, plus a meeting with an
  external attendee and a room resource

### Rehearsal sequence

```bash
python main.py preflight                    # must pass before anything else
python main.py discover --include-mail
python main.py scope --status NONE          # walk this with stakeholders
python main.py migrate --dry-run
python main.py migrate
python tools/verify.py --samples 50
```

Then do the thing that matters most: **run it twice.**

```bash
python main.py migrate                      # must be a no-op
python tools/verify.py
```

If the second run creates anything, idempotency is broken and you are not ready
for production. Then kill it mid-run with Ctrl-C and restart, and confirm the
same.

Finally, edit three files in the source and run `python main.py delta --days 1`.
Exactly three files should move.

---

## Layer 5 — Pilot cohort

Three to five volunteers, ideally including one person with a genuinely large
mailbox and one with a Drive full of native Docs. Migrate them into production
target accounts a week before their cutover.

Then **have the humans look**, because reconciliation checks counts and
checksums, not whether a spreadsheet's conditional formatting survived:

- Does the mailbox sort correctly by date? Is unread count roughly right?
- Do threads hold together?
- Open five Docs, three Sheets, two Slides. Formatting intact? Charts?
- Does a shared file still open for the colleague it was shared with?
- Does the recurring meeting show the right exceptions?

Ask them explicitly: *"what's missing?"* The answers will be things on the
`scope --status NONE` list, which is exactly why that list needs signing off
before the wave, not after.

---

## Layer 6 — Production waves

```bash
python main.py migrate --user a@tenantA.com --user b@tenantA.com
python tools/verify.py --user a@tenantA.com --user b@tenantA.com
python main.py report --max-failures 100
```

Wave sizes that work: 5 users → 25 → 100 → the rest. Reconcile between each.

`tools/verify.py` exits non-zero on any failed check, so it can gate a pipeline:

```bash
python tools/verify.py --user "$USER_EMAIL" || { echo "HOLD CUTOVER"; exit 1; }
```

### Why verify.py doesn't trust the audit log

`main.py report` tells you what the engine *believes* happened. `verify.py` asks
the target tenant directly and compares against the source. A bug that wrote
`SUCCESS` rows without writing files would look perfect in `report` and
catastrophic here. Reconciliation must not share code paths with the thing it
reconciles.

It checks: file and folder counts (net of deliberate skips), md5 on a random
sample downloaded from **both** tenants, `modifiedTime` preservation, ACL grants
actually present on the target, message counts against inserted counts, unread
drift on a sample, and any outstanding `FAILED` rows.

---

## What is deliberately not tested automatically

Being honest about this is part of the strategy:

- **Real DWD authorisation.** Requires two live Workspace tenants. Layer 4.
- **Export fidelity.** No assertion can tell you a Sheet's pivot table survived.
  Human review at Layer 5.
- **Throughput and the 750 GB/day cap.** The guard's arithmetic is tested; the
  real quota behaviour is not reproducible offline.
- **Thread-count tuning.** `PER_USER_QPS` needs measuring against real quota.
  Start conservative and watch for `userRateLimitExceeded` in the logs.

---

## Quick reference

```bash
pytest                                   # full suite, offline, ~5s
pytest -k idempot -v                     # the restart-safety cluster
pytest tests/test_gmail_calendar.py -v   # the silent-ingestion guarantees
python main.py migrate --dry-run         # log every intended write, perform none
python tools/verify.py --seed 42         # reproducible reconciliation sample
```
